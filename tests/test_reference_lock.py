"""Foto enviada trava o rosto: denoise baixo no retrato e ControlNet na cena."""

from __future__ import annotations

import base64
import json

import httpx
import pytest

from app import db, image_client, jobs, likeness
from conftest import add_action, image_english_echo, make_adventure
from test_v2 import PNG_BYTES, override_transport
from test_scene import _face, pipeline_transport
from test_v3 import use_tmp_storage


def _use_comfy() -> None:
    with db.session() as conn:
        db.set_setting(conn, "image.provider", json.dumps("comfyui"))


def _capture_generate(monkeypatch, tmp_path) -> list[dict]:
    """Isola o executor. O teste olha kwargs do Qwen e o PNG gravado."""
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
    return seen


def _unused_handler(request: httpx.Request) -> httpx.Response:
    if request.url.path.endswith("/models"):
        return httpx.Response(200, json={
            "object": "list",
            "data": [{"id": "mock-model", "object": "model"}],
        })
    if request.url.path.endswith("/chat/completions"):
        payload = json.loads(request.content.decode("utf-8"))
        if not payload.get("stream"):
            echo = image_english_echo(payload)
            if echo is not None:
                return httpx.Response(200, json={
                    "choices": [{"message": {"content": echo}}]})
    return httpx.Response(404, json={"error": "unused"})


def _unused_transport() -> httpx.MockTransport:
    return httpx.MockTransport(_unused_handler)


def _sd_handler(seen: list) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/models"):
            return httpx.Response(200, json={
                "object": "list",
                "data": [{"id": "mock-model", "object": "model"}],
            })
        if path.endswith("/chat/completions"):
            payload = json.loads(request.content.decode("utf-8"))
            if not payload.get("stream"):
                echo = image_english_echo(payload)
                if echo is not None:
                    return httpx.Response(200, json={
                        "choices": [{"message": {"content": echo}}]})
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


def _unit(payload: dict) -> dict:
    args = payload["alwayson_scripts"]["controlnet"]["args"]
    assert len(args) == 1
    return args[0]


def _upload(client, adv_id: str, card_id: str) -> None:
    resp = client.put(
        f"/api/adventures/{adv_id}/cards/{card_id}/reference",
        files={"file": ("face.png", PNG_BYTES, "image/png")},
        data={"mode": "reference"},
    )
    assert resp.status_code == 200, resp.text


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
    _use_comfy()
    seen = _capture_generate(monkeypatch, tmp_path)
    assert jobs.process_pending(_unused_transport()) == 1
    assert len(seen) == 1
    assert seen[0]["init_image"] is None
    assert seen[0]["reference_images"] == [PNG_BYTES]

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
    with db.session() as conn:
        queued = jobs.enqueue_portrait(conn, adv["id"], card["id"], replace=True, force_reference=True)
    assert queued is not None
    _use_comfy()
    seen = _capture_generate(monkeypatch, tmp_path)
    assert jobs.process_pending(_unused_transport()) == 1
    assert seen[0]["init_image"] is None
    assert seen[0]["reference_images"] == [PNG_BYTES, other]


def test_sdwebui_multiple_reference_photos_use_primary_controlnet(
        client, tmp_path, monkeypatch):
    monkeypatch.setenv("AIDUNGEON_REFERENCES_DIR", str(tmp_path / "refs"))
    use_tmp_storage(tmp_path)
    adv = make_adventure()
    with db.session() as conn:
        card = db.insert_card(
            conn, adv["id"], type="character", title="Clara", entry="A ranger.")
    _upload(client, adv["id"], card["id"])
    other = b"\x89PNG\r\n\x1a\nsecond-photo"
    resp = client.put(
        f"/api/adventures/{adv['id']}/cards/{card['id']}/reference",
        files={"file": ("second.png", other, "image/png")},
        data={"mode": "reference"},
    )
    assert resp.status_code == 200, resp.text
    with db.session() as conn:
        queued = jobs.enqueue_portrait(
            conn, adv["id"], card["id"], replace=True, force_reference=True)
    assert queued is not None
    seen: list[dict] = []
    assert jobs.process_pending(_sd_handler(seen)) == 1
    args = seen[0]["alwayson_scripts"]["controlnet"]["args"]
    assert len(args) == 1
    assert base64.b64decode(args[0]["image"]) == PNG_BYTES


def test_multiple_npcs_use_independent_regional_face_identities(
        client, tmp_path, monkeypatch):
    monkeypatch.setenv("AIDUNGEON_REFERENCES_DIR", str(tmp_path / "refs"))
    use_tmp_storage(tmp_path)
    adv = make_adventure(plot_essentials="[Name: Caio]")
    second_photo = b"\x89PNG\r\n\x1a\nsecond-face"
    with db.session() as conn:
        clara = db.insert_card(
            conn, adv["id"], type="character", title="Clara",
            triggers=["Clara"], entry="A ranger.",
        )
        sofia = db.insert_card(
            conn, adv["id"], type="character", title="Sofia",
            triggers=["Sofia"], entry="A mage.",
        )
    _upload(client, adv["id"], clara["id"])
    _face(adv["id"], clara["id"], PNG_BYTES)
    resp = client.put(
        f"/api/adventures/{adv['id']}/cards/{sofia['id']}/reference",
        files={"file": ("sofia.png", second_photo, "image/png")},
        data={"mode": "reference"},
    )
    assert resp.status_code == 200, resp.text
    _face(adv["id"], sofia["id"], second_photo)
    add_action(adv["id"], 0, "start", "The road is empty.")
    add_action(adv["id"], 1, "continue", "Clara and Sofia step into the rain.")
    seen: list[dict] = []
    expected_counts = []
    def fake_match(content, *, width, height, count, expected_people):
        expected_counts.append(expected_people)
        return [(100, 100, 120, 140), (600, 100, 120, 140)][:count]

    monkeypatch.setattr(image_client.face_regions, "match_faces", fake_match)
    override_transport(client, _sd_handler(seen))
    try:
        resp = client.post(f"/api/adventures/{adv['id']}/see", json={"action_seq": 1})
    finally:
        client.app.dependency_overrides.clear()
    assert resp.status_code == 200, resp.text
    # Uma composição seguida de um passe mascarado por NPC: nunca dois
    # embeddings InstantID competindo no mesmo condicionamento.
    assert len(seen) == 3
    assert "alwayson_scripts" not in seen[0]
    left, right = seen[1:]
    assert base64.b64decode(right["init_images"][0]) == PNG_BYTES
    for payload, photo in ((left, PNG_BYTES), (right, second_photo)):
        assert payload["denoising_strength"] == 0.45
        assert payload["inpainting_fill"] == 1
        assert payload["inpaint_full_res"] is True
        assert payload["inpaint_full_res_padding"] == 32
        assert payload["mask"]
        unit = _unit(payload)
        assert unit["module"] == "InsightFace (InstantID)"
        assert unit["model"] == "ip-adapter-instantid"
        assert unit["weight"] == 1.0
        assert "mask_image" not in unit
        assert base64.b64decode(unit["image"]) == photo
    assert "Exactly 2 people, no more, no fewer" in seen[0]["prompt"]
    assert expected_counts == [2]
    import io
    from PIL import Image, ImageChops
    left_mask = Image.open(io.BytesIO(base64.b64decode(left["mask"])))
    right_mask = Image.open(io.BytesIO(base64.b64decode(right["mask"])))
    assert ImageChops.multiply(left_mask, right_mask).getbbox() is None


def test_multi_reference_does_not_destroy_scene_when_face_detection_fails(
        monkeypatch):
    def fail_detection(content, *, width, height, count, expected_people):
        raise image_client.face_regions.FaceDetectionError("nenhum rosto")

    monkeypatch.setattr(image_client.face_regions, "match_faces", fail_detection)
    cfg = {
        "sdwebui_base_url": "http://sd",
        "txt2img_path": "/sdapi/v1/txt2img",
        "sdwebui_width": 800,
        "sdwebui_height": 720,
        "sdwebui_steps": 20,
        "negative_prompt": "",
    }
    seen: list[dict] = []
    with httpx.Client(transport=_sd_handler(seen)) as client:
        with pytest.raises(image_client.ImageError, match="face_detection"):
            image_client._generate_sdwebui(
                cfg, client, "two distinct people",
                references=[PNG_BYTES, b"second-face"],
            )
    assert len(seen) == 1
    assert "alwayson_scripts" not in seen[0]


def test_face_mask_does_not_cover_the_scene():
    import io
    from PIL import Image

    mask = Image.open(io.BytesIO(
        image_client._face_mask(800, 720, (100, 100, 80, 100))))
    assert mask.getbbox()
    assert mask.getpixel((140, 150)) == 255
    assert mask.getpixel((700, 600)) == 0
    assert mask.getpixel((400, 360)) == 0


def test_see_locks_the_uploaded_photo_without_img2img(client, tmp_path, monkeypatch):
    monkeypatch.setenv("AIDUNGEON_REFERENCES_DIR", str(tmp_path / "refs"))
    use_tmp_storage(tmp_path)
    _use_comfy()
    adv = make_adventure(plot_essentials="[Name: Caio]")
    with db.session() as conn:
        card = db.insert_card(
            conn, adv["id"], type="character", title="Clara",
            triggers=["Clara"], entry="A ranger.",
            portrait_prompt="long brown hair, green eyes",
        )
    _upload(client, adv["id"], card["id"])
    portrait = _face(adv["id"], card["id"], PNG_BYTES)
    add_action(adv["id"], 0, "start", "The road is empty.")
    add_action(adv["id"], 1, "continue", "Clara steps into the rain.")
    seen: list[dict] = []

    def fake_generate(cfg, prompt, **kwargs):
        seen.append(kwargs)
        path = tmp_path / f"{kwargs['image_id']}.png"
        path.write_bytes(PNG_BYTES)
        return {"file_path": str(path), "width": 1024, "height": 1024, "provider": "comfyui"}

    monkeypatch.setattr(image_client, "generate_image", fake_generate)
    override_transport(client, pipeline_transport("Clara steps into the rain."))
    try:
        resp = client.post(f"/api/adventures/{adv['id']}/see", json={"action_seq": 1})
    finally:
        client.app.dependency_overrides.clear()
    assert resp.status_code == 200, resp.text
    assert len(seen) == 1
    assert seen[0].get("init_image") is None
    assert seen[0]["reference_images"] == [PNG_BYTES]
    with db.session() as conn:
        stored = db.get_card(conn, adv["id"], card["id"])
    assert stored["portrait_image_id"] == portrait["id"]


def test_backdrop_and_portrait_use_the_same_uploaded_photo(client, tmp_path, monkeypatch):
    monkeypatch.setenv("AIDUNGEON_REFERENCES_DIR", str(tmp_path / "refs"))
    use_tmp_storage(tmp_path)
    _use_comfy()
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
    portrait = _face(adv["id"], card["id"], PNG_BYTES)
    add_action(adv["id"], 0, "continue", "Clara waits by the tavern door.")
    resp = client.post(f"/api/adventures/{adv['id']}/scene", json={})
    assert resp.status_code == 200, resp.text
    with db.session() as conn:
        backdrop = next(item for item in db.list_jobs(conn, adv["id"])
                        if item["type"] == "scene_backdrop")
    assert backdrop["payload"]["cast_card_ids"] == [card["id"]]
    assert backdrop["payload"]["reference_image_ids"] == [portrait["id"]]
    seen: list[dict] = []

    def fake_generate(cfg, prompt, **kwargs):
        seen.append(kwargs)
        path = tmp_path / f"{kwargs['image_id']}.png"
        path.write_bytes(PNG_BYTES)
        return {
            "file_path": str(path),
            "width": kwargs.get("width") or 1024,
            "height": kwargs.get("height") or 1024,
            "provider": "comfyui",
        }

    monkeypatch.setattr(image_client, "generate_image", fake_generate)
    assert jobs.process_pending(pipeline_transport()) >= 1
    scenes = [item for item in seen if item.get("width") == 1344]
    assert scenes and scenes[0].get("init_image") is None
    assert scenes[0]["reference_images"] == [PNG_BYTES]


def test_sdxl_variant_uses_img2img_and_scene_uses_reference(
        client, tmp_path, monkeypatch):
    use_tmp_storage(tmp_path)
    adv = make_adventure()
    with db.session() as conn:
        card = db.insert_card(
            conn, adv["id"], type="character", title="Clara",
            triggers=["Clara"], entry="A ranger.",
            portrait_prompt="long brown hair, green eyes",
            sheet=json.dumps({"visual_state": {"action": "running", "emotion": "afraid"}}),
        )
    _face(adv["id"], card["id"], PNG_BYTES)
    add_action(adv["id"], 0, "continue", "Clara runs through the rain.")
    seen: list[dict] = []

    def fake_generate(cfg, prompt, **kwargs):
        seen.append(kwargs)
        path = tmp_path / f"{kwargs['image_id']}.png"
        path.write_bytes(PNG_BYTES)
        return {
            "file_path": str(path), "provider": "sdwebui",
            "width": kwargs.get("width") or 1024,
            "height": kwargs.get("height") or 1024,
        }

    monkeypatch.setattr(image_client, "generate_image", fake_generate)
    assert client.post(
        f"/api/adventures/{adv['id']}/cards/{card['id']}/portrait",
        json={},
    ).status_code == 202
    assert client.post(f"/api/adventures/{adv['id']}/scene", json={}).status_code == 200
    assert jobs.process_pending(pipeline_transport()) == 2

    variant = next(item for item in seen if item.get("init_image") is not None)
    assert variant["init_image"] == PNG_BYTES
    assert variant["reference_images"] == [PNG_BYTES]
    assert 0 < variant["denoising_strength"] < 1
    backdrop = next(item for item in seen if item.get("width") == 1344)
    assert backdrop.get("init_image") is None
    assert backdrop["reference_images"] == [PNG_BYTES]
    with db.session() as conn:
        job = next(item for item in db.list_jobs(conn, adv["id"])
                   if item["type"] == "scene_backdrop")
    assert job["payload"]["reference_image_ids"]


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
