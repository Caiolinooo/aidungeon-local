"""Testes v0.3.0 — pipeline assíncrono de assets: jobs, retratos de NPC e
campos de assets nos story cards."""

from __future__ import annotations

import base64
import json
from pathlib import Path

import httpx

from app import db, image_client, jobs
from conftest import make_adventure

PNG_B64 = ("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8"
           "z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==")
PNG_BYTES = base64.b64decode(PNG_B64)


def make_assets_transport(chat_content="silver-haired sky captain, bust portrait",
                          capture: list | None = None) -> httpx.MockTransport:
    """LLM (/models + /chat/completions) + sdwebui (/sdapi/v1/txt2img)."""
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/models"):
            return httpx.Response(200, json={
                "object": "list",
                "data": [{"id": "mock-model", "object": "model"}],
            })
        if path.endswith("/chat/completions"):
            if capture is not None:
                capture.append(json.loads(request.content.decode("utf-8")))
            return httpx.Response(200, json={
                "choices": [{"message": {"content": chat_content}}],
            })
        if path.endswith("/sdapi/v1/txt2img"):
            return httpx.Response(200, json={"images": [PNG_B64], "parameters": {}})
        return httpx.Response(404, json={"error": f"no mock route: {path}"})

    return httpx.MockTransport(handler)


def make_npc(adventure_id: str, **fields) -> dict:
    with db.session() as conn:
        return db.insert_card(conn, adventure_id, type="character", **fields)


def use_tmp_storage(tmp_path) -> None:
    with db.session() as conn:
        db.set_setting(conn, "image.storage_dir", json.dumps(str(tmp_path / "imgs")))


# --------------------------------------------------------------- card fields
def test_card_asset_fields_roundtrip(client):
    adv = make_adventure()
    resp = client.post(f"/api/adventures/{adv['id']}/cards", json={
        "title": "Ada Vane", "type": "character", "entry": "Sky captain.",
        "portrait_prompt": "silver-haired captain, bust portrait",
        "voice": "F2", "vrm_id": "ada-v1",
    })
    assert resp.status_code == 201, resp.text
    card = resp.json()
    assert card["portrait_prompt"].startswith("silver-haired")
    assert card["voice"] == "F2"
    assert card["vrm_id"] == "ada-v1"
    assert card["portrait"] is None

    resp = client.patch(f"/api/adventures/{adv['id']}/cards/{card['id']}",
                        json={"voice": "M1"})
    assert resp.json()["voice"] == "M1"
    assert resp.json()["portrait_prompt"] == card["portrait_prompt"]  # preserva

    listed = client.get(f"/api/adventures/{adv['id']}/cards").json()
    assert listed[0]["voice"] == "M1"


# ----------------------------------------------------------------- enqueue
def test_enqueue_portrait_dedupes_active_job(client):
    adv = make_adventure()
    npc = make_npc(adv["id"], title="Ada Vane", entry="Sky captain.")
    first = client.post(f"/api/adventures/{adv['id']}/cards/{npc['id']}/portrait")
    second = client.post(f"/api/adventures/{adv['id']}/cards/{npc['id']}/portrait")
    assert first.status_code == 202 and second.status_code == 202
    assert first.json()["id"] == second.json()["id"]
    assert first.json()["status"] == "pending"

    queued = client.get(f"/api/adventures/{adv['id']}/jobs").json()
    assert len(queued) == 1
    assert queued[0]["type"] == "character_portrait"
    assert queued[0]["ref_id"] == npc["id"]


def test_enqueue_portrait_unknown_card_404(client):
    adv = make_adventure()
    resp = client.post(f"/api/adventures/{adv['id']}/cards/nope/portrait")
    assert resp.status_code == 404


def test_bulk_portraits_only_character_cards(client):
    adv = make_adventure()
    make_npc(adv["id"], title="Ada", entry="Captain.")
    make_npc(adv["id"], title="Rex", entry="Engineer.")
    with db.session() as conn:
        db.insert_card(conn, adv["id"], title="Kestrel", type="location")
    resp = client.post(f"/api/adventures/{adv['id']}/portraits")
    assert resp.status_code == 202, resp.text
    assert len(resp.json()["jobs"]) == 2


def test_adventure_create_auto_enqueues_character_portraits(client):
    scenario = client.post("/api/scenarios", json={
        "title": "Draft", "opening_text": "You wake aboard the Kestrel.",
        "story_cards": [
            {"title": "Ada Vane", "type": "character", "entry": "Sky captain."},
            {"title": "Kestrel", "type": "location", "entry": "Old freighter."},
        ],
    }).json()
    adv = client.post("/api/adventures",
                      json={"scenario_id": scenario["id"]}).json()
    queued = client.get(f"/api/adventures/{adv['id']}/jobs").json()
    portraits = [j for j in queued if j["type"] == "character_portrait"]
    assert len(portraits) == 1
    assert portraits[0]["status"] == "pending"
    # o NPC também entra na fila do preenchimento da ficha viva (card_assets)
    assert any(j["type"] == "card_assets" for j in queued)


# ---------------------------------------------------------------- lifecycle
def test_portrait_job_lifecycle_llm_prompt(client, tmp_path):
    adv = make_adventure(image_style="ink sketch")
    npc = make_npc(adv["id"], title="Ada Vane", entry="Sky captain of the Kestrel.")
    use_tmp_storage(tmp_path)

    job = client.post(
        f"/api/adventures/{adv['id']}/cards/{npc['id']}/portrait").json()
    capture: list = []
    processed = jobs.process_pending(make_assets_transport(capture=capture))
    assert processed == 1

    done = client.get(f"/api/adventures/{adv['id']}/jobs/{job['id']}").json()
    assert done["status"] == "done", done
    assert done["result"]["card_id"] == npc["id"]
    image = done["result"]["image"]
    assert image["url"] == f"/api/images/{image['id']}"

    # card ganhou portrait embutido + prompt do LLM persistido
    card = client.get(f"/api/adventures/{adv['id']}/cards/{npc['id']}").json()
    assert card["portrait"]["id"] == image["id"]
    assert "silver-haired sky captain" in card["portrait_prompt"]

    # imagem gravada como retrato (fora do feed de ações) e servida
    with db.session() as conn:
        stored = db.get_image(conn, image["id"])
        assert stored["kind"] == "portrait"
        assert stored["ref_id"] == npc["id"]
        assert stored["action_seq"] == -1
    resp = client.get(image["url"])
    assert resp.status_code == 200 and resp.content == PNG_BYTES

    # o LLM recebeu a descrição do NPC + contexto da aventura
    assert capture and "Ada Vane" in json.dumps(capture[0]["messages"])
    assert "Sky captain of the Kestrel" in json.dumps(capture[0]["messages"])


def test_portrait_job_uses_card_prompt_without_llm(client, tmp_path):
    adv = make_adventure(image_style="ink sketch")
    npc = make_npc(adv["id"], title="Ada Vane", entry="Sky captain.",
                   portrait_prompt="scarred veteran captain, oil painting")
    use_tmp_storage(tmp_path)

    capture: list = []
    assert jobs.process_pending(make_assets_transport(capture=capture)) == 0
    job = client.post(
        f"/api/adventures/{adv['id']}/cards/{npc['id']}/portrait").json()
    assert jobs.process_pending(make_assets_transport(capture=capture)) == 1

    done = client.get(f"/api/adventures/{adv['id']}/jobs/{job['id']}").json()
    assert done["status"] == "done"
    # prompt final = image_style + portrait_prompt do card + style_suffix
    with db.session() as conn:
        stored = db.get_image(conn, done["result"]["image"]["id"])
        assert stored["prompt"].startswith("ink sketch")
        assert "scarred veteran captain, oil painting" in stored["prompt"]
    assert capture == []  # nenhuma chamada ao LLM


def test_portrait_job_card_deleted_errors(client, tmp_path):
    adv = make_adventure()
    npc = make_npc(adv["id"], title="Ada Vane", entry="Sky captain.")
    use_tmp_storage(tmp_path)
    job = client.post(
        f"/api/adventures/{adv['id']}/cards/{npc['id']}/portrait").json()
    assert client.delete(f"/api/adventures/{adv['id']}/cards/{npc['id']}"
                         ).status_code == 204

    assert jobs.process_pending(make_assets_transport()) == 1
    done = client.get(f"/api/adventures/{adv['id']}/jobs/{job['id']}").json()
    assert done["status"] == "error"
    assert "not found" in done["error"]


def test_portrait_job_images_disabled_errors(client, tmp_path, admin_token):
    adv = make_adventure()
    npc = make_npc(adv["id"], title="Ada Vane", entry="Sky captain.")
    use_tmp_storage(tmp_path)
    resp = client.put("/api/settings/image", json={"enabled": False},
                      headers={"Authorization": f"Bearer {admin_token}"})
    assert resp.status_code == 200
    job = client.post(
        f"/api/adventures/{adv['id']}/cards/{npc['id']}/portrait").json()

    assert jobs.process_pending(make_assets_transport()) == 1
    done = client.get(f"/api/adventures/{adv['id']}/jobs/{job['id']}").json()
    assert done["status"] == "error"
    assert "disabled" in done["error"]


# ------------------------------------------------------------------ cancel
def test_cancel_pending_job(client):
    adv = make_adventure()
    npc = make_npc(adv["id"], title="Ada Vane", entry="Sky captain.")
    job = client.post(
        f"/api/adventures/{adv['id']}/cards/{npc['id']}/portrait").json()

    resp = client.delete(f"/api/adventures/{adv['id']}/jobs/{job['id']}")
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "canceled"

    # cancelado não é executado nem re-enfileirado pelo claim
    assert jobs.process_pending(make_assets_transport()) == 0
    assert client.get(f"/api/adventures/{adv['id']}/jobs/{job['id']}"
                      ).json()["status"] == "canceled"


def test_cancel_finished_job_409(client, tmp_path):
    adv = make_adventure()
    npc = make_npc(adv["id"], title="Ada Vane", entry="Sky captain.",
                   portrait_prompt="captain portrait")
    use_tmp_storage(tmp_path)
    job = client.post(
        f"/api/adventures/{adv['id']}/cards/{npc['id']}/portrait").json()
    assert jobs.process_pending(make_assets_transport()) == 1

    resp = client.delete(f"/api/adventures/{adv['id']}/jobs/{job['id']}")
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "job_not_cancelable"


def test_bulk_does_not_replace_an_existing_portrait(client, tmp_path):
    adv = make_adventure()
    keep = make_npc(adv["id"], title="Ada", entry="Captain.",
                    portrait_prompt="silver hair, bust portrait")
    make_npc(adv["id"], title="Rex", entry="Engineer.")
    use_tmp_storage(tmp_path)
    client.post(f"/api/adventures/{adv['id']}/cards/{keep['id']}/portrait")
    assert jobs.process_pending(make_assets_transport()) == 1
    saved = client.get(f"/api/adventures/{adv['id']}/cards/{keep['id']}").json()
    assert saved["portrait_anchor_id"] == saved["portrait"]["id"]

    resp = client.post(f"/api/adventures/{adv['id']}/portraits")
    assert resp.status_code == 202, resp.text
    assert len(resp.json()["jobs"]) == 1
    assert resp.json()["jobs"][0]["ref_id"] != keep["id"]
    again = client.get(f"/api/adventures/{adv['id']}/cards/{keep['id']}").json()
    assert again["portrait"]["id"] == saved["portrait"]["id"]
    assert again["portrait_anchor_id"] == saved["portrait_anchor_id"]


def test_regenerate_portrait_img2img_from_anchor(client, tmp_path, monkeypatch):
    adv = make_adventure(image_style="ink sketch")
    npc = make_npc(adv["id"], title="Ada Vane", entry="Sky captain.",
                   portrait_prompt="scarred veteran captain, oil painting")
    use_tmp_storage(tmp_path)
    first = client.post(f"/api/adventures/{adv['id']}/cards/{npc['id']}/portrait").json()
    assert jobs.process_pending(make_assets_transport()) == 1
    card = client.get(f"/api/adventures/{adv['id']}/cards/{npc['id']}").json()
    anchor = card["portrait_anchor_id"]
    assert anchor == card["portrait"]["id"]
    assert first["id"]

    with db.session() as conn:
        db.set_setting(conn, "image.provider", json.dumps("comfyui"))
        anchor_bytes = Path(db.get_image(conn, anchor)["file_path"]).read_bytes()
    seen: list[dict] = []

    def fake_generate(cfg, prompt, *, adventure_id, image_id, **kwargs):
        seen.append({"prompt": prompt, **kwargs})
        directory = tmp_path / "imgs" / adventure_id
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{image_id}.png"
        path.write_bytes(PNG_BYTES)
        return {
            "file_path": str(path),
            "width": int(kwargs.get("width") or 1024),
            "height": int(kwargs.get("height") or 1024),
            "provider": "comfyui",
        }

    monkeypatch.setattr(image_client, "generate_image", fake_generate)
    second = client.post(f"/api/adventures/{adv['id']}/cards/{npc['id']}/portrait").json()
    assert second["payload"]["replace"] is True
    assert second["payload"]["init_image_id"] == anchor
    assert jobs.process_pending(httpx.MockTransport(
        lambda request: httpx.Response(404, json={"error": "unused"}))) == 1
    assert seen and seen[0]["init_image"] is None
    assert seen[0]["reference_images"] == [anchor_bytes]
    assert "same face" in seen[0]["prompt"]

    updated = client.get(f"/api/adventures/{adv['id']}/cards/{npc['id']}").json()
    assert updated["portrait"]["id"] != anchor
    assert updated["portrait_anchor_id"] == anchor
