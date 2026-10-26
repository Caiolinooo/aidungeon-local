"""Foto enviada trava o rosto: denoise baixo no retrato e ControlNet na cena."""

from __future__ import annotations

import base64
import json

import httpx

from app import db, image_client, jobs, likeness
from conftest import add_action, make_adventure
from test_v2 import PNG_BYTES, override_transport
from test_v3 import use_tmp_storage


def _sd_handler(seen: list):
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/models"):
            return httpx.Response(200, json={
                "object": "list",
                "data": [{"id": "mock-model", "object": "model"}],
            })
        if path.endswith("/chat/completions"):
            return httpx.Response(200, json={
                "choices": [{"message": {"content": "Clara steps into the rain."}}],
            })
        if path.endswith("/sdapi/v1/txt2img") or path.endswith("/sdapi/v1/img2img"):
            seen.append(json.loads(request.content.decode("utf-8")))
            return httpx.Response(200, json={
                "images": [base64.b64encode(PNG_BYTES).decode("ascii")],
                "parameters": {},
            })
        return httpx.Response(404, json={"error": f"no mock route: {path}"})

    return httpx.MockTransport(handler)


def _upload(client, adv_id: str, card_id: str) -> None:
    resp = client.put(
        f"/api/adventures/{adv_id}/cards/{card_id}/reference",
        files={"file": ("face.png", PNG_BYTES, "image/png")},
        data={"mode": "reference"},
    )
    assert resp.status_code == 200, resp.text


def _unit(payload: dict) -> dict:
    args = payload["alwayson_scripts"]["controlnet"]["args"]
    assert len(args) == 1
    return args[0]


def test_first_name_puts_the_reference_card_in_the_scene():
    from app import scene
    card = {"id": "c1", "type": "character", "title": "Thalita Massoto", "triggers": []}
    other = {"id": "c2", "type": "character", "title": "Maria Fernanda", "triggers": []}
    text = "Thalita soltou o lápis e olhou para Caio."
    assert scene._cast([card, other], text) == [card]

    # Nome composto não pode virar substring de outra palavra.
    assert scene._cast([card], "O talitalmo brilhou.") == []


def test_reference_images_skip_a_minor(tmp_path, monkeypatch):
    monkeypatch.setenv("AIDUNGEON_REFERENCES_DIR", str(tmp_path))
    photo = tmp_path / "face.png"
    photo.write_bytes(PNG_BYTES)
    adult = {"title": "Ada", "sheet": json.dumps({
        "reference": {"image_path": str(photo)},
    })}
    child = {"title": "Kid", "sheet": json.dumps({
        "minor": True,
        "reference": {"image_path": str(photo)},
    })}
    assert likeness.reference_images([adult]) == [PNG_BYTES]
    assert likeness.reference_images([child]) == []
    assert likeness.reference_images([adult, adult]) == [PNG_BYTES]


def test_uploaded_photo_uses_the_rigid_denoise(tmp_path, monkeypatch):
    monkeypatch.setenv("AIDUNGEON_REFERENCES_DIR", str(tmp_path / "refs"))
    card = {"title": "Clara", "type": "character", "sheet": json.dumps({
        "real_person": True,
        "reference": {"image_path": str(tmp_path / "refs" / "face.png"), "source": "upload"},
    })}
    (tmp_path / "refs").mkdir()
    (tmp_path / "refs" / "face.png").write_bytes(PNG_BYTES)
    data, denoise = likeness.portrait_init(card, b"anchor", anchor_denoise=0.35)
    assert data == PNG_BYTES
    assert denoise == likeness.VISUAL_REFERENCE_DENOISE
    assert denoise <= 0.15
    assert denoise < likeness.REFERENCE_DENOISE

def test_visual_upload_updates_card_fields_without_erasing_personality(tmp_db, tmp_path, monkeypatch):
    monkeypatch.setenv("AIDUNGEON_REFERENCES_DIR", str(tmp_path / "refs"))
    adv = make_adventure()
    with db.session() as conn:
        card = db.insert_card(
            conn, adv["id"], type="character", title="Clara",
            entry="Uma ranger da floresta.",
            sheet=json.dumps({
                "personality": " cautelosa e leal ",
                "background": "Cresceu entre os pinheiros.",
                "appearance": "old description",
            }),
        )
    analysis = {
        "appearance": "long blonde hair, green eyes, light skin",
        "body": "athletic build, upright posture",
        "clothing": "green cloak, leather boots",
        "portrait_prompt": "blonde woman in a green cloak, forest portrait",
        "entry_visual": "a blonde woman in a green cloak",
    }
    monkeypatch.setattr(likeness, "analyze_visual_card", lambda *args, **kwargs: analysis)
    with db.session() as conn:
        updated = likeness.apply_visual_card(conn, card, PNG_BYTES, llm_cfg=None, transport=None)
    sheet = json.loads(updated["sheet"])
    assert sheet["appearance"] == analysis["appearance"]
    assert sheet["body"] == analysis["body"]
    assert sheet["clothing"] == analysis["clothing"]
    assert sheet["personality"] == " cautelosa e leal "
    assert sheet["background"] == "Cresceu entre os pinheiros."
    assert updated["portrait_prompt"] == analysis["portrait_prompt"]
    assert "Visual de referência: a blonde woman in a green cloak" in updated["entry"]
    assert "Uma ranger da floresta." in updated["entry"]

def test_new_image_from_photo_uses_identity_not_img2img(client, tmp_path, monkeypatch):
    monkeypatch.setenv("AIDUNGEON_REFERENCES_DIR", str(tmp_path / "refs"))
    use_tmp_storage(tmp_path)
    adv = make_adventure()
    with db.session() as conn:
        card = db.insert_card(
            conn, adv["id"], type="character", title="Clara",
            entry="A ranger.", portrait_prompt="long brown hair, green eyes",
        )
    _upload(client, adv["id"], card["id"])
    with db.session() as conn:
        queued = jobs.enqueue_portrait(conn, adv["id"], card["id"], replace=True, force_reference=True)
    assert queued is not None
    seen: list[dict] = []
    assert jobs.process_pending(_sd_handler(seen)) == 1
    assert len(seen) == 1
    payload = seen[0]
    assert payload["init_images"]
    assert payload["denoising_strength"] == 0.12
    assert base64.b64decode(_unit(payload)["image"]) == PNG_BYTES

def test_multiple_reference_photos_are_kept_and_sent(client, tmp_path, monkeypatch):
    monkeypatch.setenv("AIDUNGEON_REFERENCES_DIR", str(tmp_path / "refs"))
    use_tmp_storage(tmp_path)
    adv = make_adventure()
    with db.session() as conn:
        card = db.insert_card(conn, adv["id"], type="character", title="Clara", entry="A ranger.")
    _upload(client, adv["id"], card["id"])
    other = b"\x89PNG\r\n\x1a\nsecond-photo"
    resp = client.put(
        f"/api/adventures/{adv['id']}/cards/{card['id']}/reference",
        files={"file": ("second.png", other, "image/png")},
        data={"mode": "reference"},
    )
    assert resp.status_code == 200, resp.text
    sheet = json.loads(resp.json()["sheet"])
    assert len(sheet["reference"]["images"]) == 2
    with db.session() as conn:
        saved = db.get_card(conn, adv["id"], card["id"])
    refs = likeness.reference_images([saved])
    assert refs == [PNG_BYTES]
    seen: list[dict] = []
    with db.session() as conn:
        queued = jobs.enqueue_portrait(conn, adv["id"], card["id"], replace=True, force_reference=True)
    assert queued is not None
    assert jobs.process_pending(_sd_handler(seen)) == 1
    args = seen[0]["alwayson_scripts"]["controlnet"]["args"]
    assert len(args) == 1
    assert base64.b64decode(args[0]["image"]) == PNG_BYTES
def test_multiple_npcs_use_regional_reference_masks():
    payload = image_client._attach_references(
        {"prompt": "two women"},
        [PNG_BYTES, b"second-face"],
        width=1344, height=768,
    )
    args = payload["alwayson_scripts"]["controlnet"]["args"]
    assert len(args) == 2
    assert args[0]["mask_image"] != args[1]["mask_image"]
    assert args[0]["weight"] == 0.55
    assert args[1]["weight"] == 0.55
    assert base64.b64decode(args[0]["mask_image"]) != base64.b64decode(args[1]["mask_image"])
    # Uma referência sozinha não precisa de máscara regional.
    single = image_client._attach_references(
        {"prompt": "one woman"}, [PNG_BYTES], width=1344, height=768)
    assert "mask_image" not in single["alwayson_scripts"]["controlnet"]["args"][0]


def test_see_locks_the_uploaded_photo_without_img2img(client, tmp_path, monkeypatch):
    monkeypatch.setenv("AIDUNGEON_REFERENCES_DIR", str(tmp_path / "refs"))
    use_tmp_storage(tmp_path)
    adv = make_adventure(plot_essentials="[Name: Caio]")
    with db.session() as conn:
        card = db.insert_card(
            conn, adv["id"], type="character", title="Clara",
            triggers=["Clara"], entry="A ranger.",
            portrait_prompt="long brown hair, green eyes",
        )
    _upload(client, adv["id"], card["id"])
    add_action(adv["id"], 0, "start", "The road is empty.")
    add_action(adv["id"], 1, "continue", "Clara steps into the rain.")
    seen: list[dict] = []
    override_transport(client, _sd_handler(seen))
    try:
        resp = client.post(f"/api/adventures/{adv['id']}/see", json={"action_seq": 1})
    finally:
        client.app.dependency_overrides.clear()
    assert resp.status_code == 200, resp.text
    assert len(seen) == 1
    payload = seen[0]
    assert "init_images" not in payload
    unit = _unit(payload)
    assert unit["enabled"] is True
    assert unit["module"] == "InsightFace (InstantID)"
    assert unit["model"] == "ip-adapter-instantid"
    assert unit["weight"] == 1.0
    assert unit["threshold_a"] == -1
    assert unit["control_mode"] == "ControlNet is more important"
    assert base64.b64decode(unit["image"]) == PNG_BYTES


def test_backdrop_and_portrait_use_the_same_uploaded_photo(client, tmp_path, monkeypatch):
    monkeypatch.setenv("AIDUNGEON_REFERENCES_DIR", str(tmp_path / "refs"))
    use_tmp_storage(tmp_path)
    adv = make_adventure()
    with db.session() as conn:
        card = db.insert_card(
            conn, adv["id"], type="character", title="Clara",
            triggers=["Clara"], entry="A ranger.",
            portrait_prompt="long brown hair, green eyes",
            sheet=json.dumps({
                "appearance": "long brown hair, green eyes",
                "clothing": "a green cloak",
            }),
        )
    _upload(client, adv["id"], card["id"])
    add_action(adv["id"], 0, "continue", "Clara waits by the tavern door.")
    resp = client.post(f"/api/adventures/{adv['id']}/scene", json={})
    assert resp.status_code == 200, resp.text
    with db.session() as conn:
        backdrop = next(item for item in db.list_jobs(conn, adv["id"])
                        if item["type"] == "scene_backdrop")
    assert card["id"] in backdrop["payload"]["reference_card_ids"]
    seen: list[dict] = []
    assert jobs.process_pending(_sd_handler(seen)) >= 1
    img2img = [item for item in seen if item.get("init_images")]
    scenes = [item for item in seen if item.get("width") == 1344]
    assert img2img and img2img[0]["denoising_strength"] == likeness.VISUAL_REFERENCE_DENOISE
    assert base64.b64decode(_unit(img2img[0])["image"]) == PNG_BYTES
    assert scenes and "init_images" not in scenes[0]
    assert base64.b64decode(_unit(scenes[0])["image"]) == PNG_BYTES


def test_cache_key_changes_when_the_reference_changes():
    cfg = {
        "provider": "sdwebui",
        "negative_prompt": "",
        "sd_model_checkpoint": "model",
        "sdwebui_width": 1024,
        "sdwebui_height": 1024,
        "sdwebui_steps": 20,
        "sampler_name": "DPM++ 2M",
        "scheduler": "Karras",
        "cfg_scale": 5,
    }
    plain = image_client.request_key(cfg, "a ranger")
    locked = image_client.request_key(cfg, "a ranger", reference_images=[PNG_BYTES])
    other = image_client.request_key(cfg, "a ranger", reference_images=[b"other-face"])
    assert plain != locked
    assert locked != other
