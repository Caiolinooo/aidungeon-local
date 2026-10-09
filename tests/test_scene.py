"""Cena do chat: lugar, clima, elenco e fundo enfileirado."""

from __future__ import annotations

import json

from app import db, jobs
from conftest import add_action, make_adventure
from test_v3 import make_assets_transport, use_tmp_storage


def _seed(text="You enter the tavern at night. Ada smiles, happy to see you."):
    adv = make_adventure()
    with db.session() as conn:
        db.insert_card(conn, adv["id"], type="location", title="The Tavern",
                       triggers=["tavern"], entry="A smoky common room with a hearth.")
        db.insert_card(conn, adv["id"], type="character", title="Ada",
                       entry="The barkeep.")
    add_action(adv["id"], 0, "start", text)
    return adv


def test_scene_paints_backdrop_and_portrait(client, tmp_path):
    use_tmp_storage(tmp_path)
    adv = _seed()
    resp = client.post(f"/api/adventures/{adv['id']}/scene", json={})
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["location"] == "The Tavern"
    assert data["atmosphere"] == "night"
    assert data["backdrop"]["status"] == "pending"
    assert data["cast"][0]["title"] == "Ada"
    assert data["cast"][0]["mood"] == "happy"
    assert data["cast"][0]["portrait"] is None
    with db.session() as conn:
        backdrop_job = next(item for item in db.list_jobs(conn, adv["id"])
                            if item["type"] == "scene_backdrop")
    assert "ada" not in backdrop_job["payload"]["prompt"].lower()
    assert "no people" in backdrop_job["payload"]["prompt"].lower()

    assert jobs.process_pending(make_assets_transport()) == 2

    # O retrato acabou de gravar o visual. A sala vazia não serve mais:
    # a próxima consulta pede a mesma Ada, com essa cara.
    resp = client.post(f"/api/adventures/{adv['id']}/scene", json={})
    data = resp.json()
    assert data["cast"][0]["portrait"]["id"]
    assert data["backdrop"]["status"] == "pending"
    with db.session() as conn:
        repaint = next(item for item in reversed(db.list_jobs(conn, adv["id"]))
                       if item["type"] == "scene_backdrop" and item["status"] == "pending")
    assert repaint["payload"]["include_people"] is True
    assert "silver-haired" in repaint["payload"]["prompt"].lower()

    assert jobs.process_pending(make_assets_transport()) == 1
    resp = client.post(f"/api/adventures/{adv['id']}/scene", json={})
    data = resp.json()
    assert data["backdrop"]["status"] == "ready"
    assert data["backdrop"]["image"]["url"].startswith("/api/images/")
    with db.session() as conn:
        image = db.get_image(conn, data["backdrop"]["image"]["id"])
        assert image["kind"] == "backdrop"
        assert image["width"] == 896
        assert image["height"] == 512
        assert "wrong hair" in image["negative_prompt"]
        assert "silver-haired" in image["prompt"].lower()
        # mesma cara: não enfileira de novo
        assert len(db.list_jobs(conn, adv["id"])) == 3


def test_scene_paints_the_card_face_not_the_prose(client, tmp_path):
    use_tmp_storage(tmp_path)
    adv = make_adventure()
    with db.session() as conn:
        db.insert_card(
            conn, adv["id"], type="character", title="Princess Alusair",
            triggers=["Alusair"], entry="Steel Regent of Cormyr.",
            sheet=json.dumps({
                "appearance": "long red hair, green eyes",
                "clothing": "plate armor",
            }),
        )
    add_action(adv["id"], 0, "continue",
               "Princess Alusair reclines in a white gown, dark hair loose.")
    resp = client.post(f"/api/adventures/{adv['id']}/scene", json={})
    assert resp.status_code == 200, resp.text
    with db.session() as conn:
        job = next(item for item in db.list_jobs(conn, adv["id"])
                   if item["type"] == "scene_backdrop")
    prompt = job["payload"]["prompt"].lower()
    assert job["payload"]["include_people"] is True
    assert "long red hair" in prompt
    assert "plate armor" in prompt
    assert "white gown" not in prompt
    assert "dark hair" not in prompt
    assert "no people" not in prompt
    assert jobs.process_pending(make_assets_transport()) >= 1
    with db.session() as conn:
        image = db.latest_image(conn, adv["id"], "backdrop")
    assert "wrong hair" in image["negative_prompt"]
    assert "person, people" not in image["negative_prompt"]


def test_scene_new_atmosphere_paints_again(client, tmp_path):
    use_tmp_storage(tmp_path)
    adv = _seed()
    client.post(f"/api/adventures/{adv['id']}/scene", json={})
    jobs.process_pending(make_assets_transport())

    add_action(adv["id"], 1, "continue", "Fire engulfs the tavern. Ada is afraid.")
    resp = client.post(f"/api/adventures/{adv['id']}/scene", json={})
    assert resp.status_code == 200, resp.text
    assert "fire" in resp.json()["atmosphere"]
    assert resp.json()["backdrop"]["status"] == "pending"
    assert resp.json()["cast"][0]["mood"] == "fear"


def test_scene_error_does_not_requeue_until_retry(client, tmp_path):
    use_tmp_storage(tmp_path)
    adv = _seed("The tavern is quiet.")
    first = client.post(f"/api/adventures/{adv['id']}/scene", json={}).json()
    assert first["backdrop"]["status"] == "pending"
    with db.session() as conn:
        job = next(item for item in db.list_jobs(conn, adv["id"])
                   if item["type"] == "scene_backdrop")
        db.finish_job(conn, job["id"], "error", error="boom")

    again = client.post(f"/api/adventures/{adv['id']}/scene", json={}).json()
    assert again["backdrop"]["status"] == "error"
    with db.session() as conn:
        assert len(db.list_jobs(conn, adv["id"])) == 1

    retried = client.post(f"/api/adventures/{adv['id']}/scene", json={"retry": True}).json()
    assert retried["backdrop"]["status"] == "pending"
    with db.session() as conn:
        assert sum(1 for j in db.list_jobs(conn, adv["id"]) if j["type"] == "scene_backdrop") == 2


def test_scene_disabled_does_not_enqueue(client):
    adv = _seed()
    with db.session() as conn:
        db.set_setting(conn, "image.enabled", json.dumps(False))
    resp = client.post(f"/api/adventures/{adv['id']}/scene", json={})
    assert resp.status_code == 200, resp.text
    assert resp.json()["backdrop"]["status"] == "disabled"
    with db.session() as conn:
        assert db.list_jobs(conn, adv["id"]) == []


def test_scene_unknown_adventure_404(client):
    resp = client.post("/api/adventures/does-not-exist/scene", json={})
    assert resp.status_code == 404
