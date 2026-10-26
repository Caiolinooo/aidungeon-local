"""Cena do chat: lugar, clima, elenco e fundo enfileirado."""

from __future__ import annotations

import json

from app import db, jobs
from conftest import add_action, make_adventure
from test_v2 import make_v2_transport, override_transport
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
        assert image["width"] == 1344
        assert image["height"] == 768
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


def test_explicit_scene_does_not_restore_the_outfit(client, tmp_path):
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
    add_action(adv["id"], 0, "continue", "Princess Alusair is naked on the bed.")
    resp = client.post(f"/api/adventures/{adv['id']}/scene", json={})
    assert resp.status_code == 200, resp.text
    with db.session() as conn:
        job = next(item for item in db.list_jobs(conn, adv["id"])
                   if item["type"] == "scene_backdrop")
    prompt = job["payload"]["prompt"].lower()
    assert job["payload"]["explicit"] is True
    assert "naked" in prompt
    assert "plate armor" not in prompt
    assert "long red hair" in prompt
    assert jobs.process_pending(make_assets_transport()) >= 1
    with db.session() as conn:
        image = db.latest_image(conn, adv["id"], "backdrop")
    assert "costume change" not in image["negative_prompt"].lower()
    assert "nude" not in image["negative_prompt"].lower()


def test_explicit_backdrop_locks_the_vampire_face(client, tmp_path):
    use_tmp_storage(tmp_path)
    adv = make_adventure()
    with db.session() as conn:
        db.insert_card(
            conn, adv["id"], type="character", title="Selene",
            triggers=["Selene", "vampira"], entry="A vampira.",
            portrait_prompt=(
                "Selene is a pale gothic vampiress with long black hair and dark eyes, "
                "wearing a black velvet dress, captured in a bust portrait against a neutral background."
            ),
            sheet=json.dumps({
                "appearance": (
                    "Pele pálida como mármore, lábios vermelhos vibrantes, "
                    "cabelos pretos longos e lisos, olhos escuros profundos"
                ),
                "body": "Estrutura fina mas atlética, postura elegante e fluida",
                "clothing": "Vestido de veludo preto",
            }),
        )
    add_action(adv["id"], 0, "continue", "Selene is naked on the sofa, rain on the window.")
    resp = client.post(f"/api/adventures/{adv['id']}/scene", json={})
    assert resp.status_code == 200, resp.text
    with db.session() as conn:
        job = next(item for item in db.list_jobs(conn, adv["id"])
                   if item["type"] == "scene_backdrop")
    prompt = job["payload"]["prompt"]
    head = prompt.lower()
    assert job["payload"]["explicit"] is True
    assert head.index("black hair") < head.index("naked")
    assert head.index("pale") < head.index("naked")
    assert "straight" in head
    assert "velvet" not in head
    assert "bust portrait" not in head
    assert "pele pálida" not in head
    assert jobs.process_pending(make_assets_transport()) >= 1
    with db.session() as conn:
        image = db.latest_image(conn, adv["id"], "backdrop")
    negative = image["negative_prompt"].lower()
    assert "wrong skin tone" in negative
    assert "different person" in negative
    assert "nude" not in negative


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


# ------------------------------------------------------- See e fundo: a mesma cena
_FAIRY_PROMPT = (
    "Elara is portrayed as a slender, agile fairy queen with a graceful posture, "
    "wearing translucent silk and crystal garments that shimmer against a neutral "
    "background. Her hair floats around her head like stars, and her amber eyes "
    "gaze forward with an intense, captivating expression in a bust portrait."
)


def _fairy(adventure_id: str) -> None:
    with db.session() as conn:
        db.insert_card(
            conn, adventure_id, type="character", title="Elara",
            triggers=["Elara", "rainha"], entry="Rainha de cristal.",
            portrait_prompt=_FAIRY_PROMPT,
        )


def _fairy_protest(adventure_id: str) -> None:
    """A fada vestida e o jogador protestando contra uma rainha nua."""
    _fairy(adventure_id)
    add_action(adventure_id, 0, "start", "Elara espera na floresta de cristal.")
    add_action(adventure_id, 1, "say",
               "Elara, tenha decência! Crie uma roupa de cristal, não posso me "
               "vingar lutando com uma rainha nua!")
    add_action(adventure_id, 2, "continue",
               "Elara recua um passo, ainda vestida de seda e cristal, e ergue as mãos.")


def _backdrop_job(adventure_id: str) -> dict:
    with db.session() as conn:
        return next(item for item in db.list_jobs(conn, adventure_id)
                    if item["type"] == "scene_backdrop")


def _see(client, adventure_id: str, action_seq: int) -> str:
    override_transport(client, make_v2_transport(
        chat_content=("Elara stands in the crystal forest, wearing translucent silk "
                      "and crystal garments, hair floating like stars.")))
    try:
        resp = client.post(f"/api/adventures/{adventure_id}/see",
                           json={"action_seq": action_seq})
    finally:
        client.app.dependency_overrides.clear()
    assert resp.status_code == 200, resp.text
    return resp.json()["prompt"]


def test_the_players_protest_does_not_paint_the_fairy_nude(client, tmp_path):
    use_tmp_storage(tmp_path)
    adv = make_adventure()
    _fairy_protest(adv["id"])
    resp = client.post(f"/api/adventures/{adv['id']}/scene", json={})
    assert resp.status_code == 200, resp.text
    job = _backdrop_job(adv["id"])
    prompt = job["payload"]["prompt"].lower()
    assert job["payload"]["explicit"] is False
    assert "wearing translucent silk and crystal garments" in prompt
    assert "hair floats around her head like stars" in prompt
    assert "nude" not in prompt
    assert "naked" not in prompt
    assert "clothes off" not in prompt
    assert jobs.process_pending(make_assets_transport()) >= 1
    with db.session() as conn:
        image = db.latest_image(conn, adv["id"], "backdrop")
    assert "nude" in image["negative_prompt"].lower()


def test_a_hypothetical_nudity_does_not_paint_the_fairy_nude(client, tmp_path):
    """A fada fala em condicional ("se estiver nua") e continua vestida."""
    use_tmp_storage(tmp_path)
    adv = make_adventure()
    _fairy(adv["id"])
    add_action(adv["id"], 0, "continue",
               "O vestido surge das profundezas da floresta: camadas fluidas que "
               "brilham sob o frio cortante, bordados em fios finos feitos com "
               "cristais. A decência é apenas uma ilusão — sussurra Elara enquanto "
               "ajusta a nova camada de seda translúcida, os ombros agora "
               "protegidos por um colar. E não posso me vingar com elegância se "
               "estiver nua!")
    resp = client.post(f"/api/adventures/{adv['id']}/scene", json={})
    assert resp.status_code == 200, resp.text
    job = _backdrop_job(adv["id"])
    prompt = job["payload"]["prompt"].lower()
    assert job["payload"]["explicit"] is False
    assert "wearing translucent silk and crystal garments" in prompt
    assert "nude" not in prompt
    assert "clothes off" not in prompt


def test_the_see_and_the_backdrop_paint_the_same_scene(client, tmp_path):
    use_tmp_storage(tmp_path)
    adv = make_adventure()
    _fairy_protest(adv["id"])
    client.post(f"/api/adventures/{adv['id']}/scene", json={})
    backdrop = _backdrop_job(adv["id"])["payload"]["prompt"]
    seen = _see(client, adv["id"], 2)

    for prompt in (backdrop, seen):
        folded = prompt.lower()
        assert "wearing translucent silk and crystal garments" in folded
        assert "hair floats around her head like stars" in folded
        assert "nude" not in folded
        assert "naked" not in folded
        assert "clothes off" not in folded


def test_a_nude_narration_paints_the_see_and_the_backdrop_nude(client, tmp_path):
    use_tmp_storage(tmp_path)
    adv = make_adventure()
    _fairy(adv["id"])
    add_action(adv["id"], 0, "continue",
               "Elara desliza o vestido de seda e fica nua, a pele na luz da lua.")
    client.post(f"/api/adventures/{adv['id']}/scene", json={})
    job = _backdrop_job(adv["id"])
    backdrop = job["payload"]["prompt"]
    seen = _see(client, adv["id"], 0)

    assert job["payload"]["explicit"] is True
    for prompt in (backdrop, seen):
        folded = prompt.lower()
        assert "clothes off" in folded
        assert "nude" in folded
        assert "translucent silk and crystal garments" not in folded


def test_a_salamander_is_not_painted_as_a_naked_woman(client, tmp_path):
    """O nome curto do plot não vira uma segunda pessoa humana."""
    use_tmp_storage(tmp_path)
    adv = make_adventure(
        plot_essentials="[Name: Sandra; Role: Salamandra; Appearance: escamas âmbar]",
        image_style="dark fantasy, amphibian characters",
    )
    with db.session() as conn:
        db.insert_card(
            conn, adv["id"], type="character",
            title="Sandra, A Salamandra",
            entry="Realmente uma salamandra que se chama sandra.",
            use_for_character_creation=True,
            portrait_prompt="Sandra the Salamander, an ancient amphibian, amber scales, long tail",
            sheet=json.dumps({"class": "salamandra", "clothing": ""}),
        )
    add_action(adv["id"], 0, "start", "Sandra está no escritório pantanoso.")
    add_action(adv["id"], 1, "do", "> You pular até o teto com os cristais")
    add_action(adv["id"], 2, "continue",
               "Sandra salta e a cauda bate na mesa. Ela segura os cristais.")
    override_transport(client, make_v2_transport(
        "Sandra leaps to the ceiling clutching the crystals."))
    try:
        resp = client.post(f"/api/adventures/{adv['id']}/see", json={"action_seq": 2})
    finally:
        client.app.dependency_overrides.clear()
    assert resp.status_code == 200, resp.text
    prompt = resp.json()["prompt"].lower()
    assert "adult woman" not in prompt
    assert "salamander" in prompt
    assert "not a human" in prompt
    assert "natural hide" in prompt
    assert "nude" not in prompt
    assert "naked" not in prompt
    assert "clothes off" not in prompt
    assert "natural skin texture" not in prompt
    with db.session() as conn:
        negative = db.get_image(conn, resp.json()["id"])["negative_prompt"].lower()
    assert "nude" in negative
    assert "naked" in negative
    assert "nipples" in negative

