"""Turnaround 360° local: extração, publicação atômica e referências multiview."""

from __future__ import annotations

import io
import json
import shutil
from pathlib import Path

import cv2
import numpy as np
from PIL import Image

from app import db, events, image_client, jobs, likeness, scene
from app import turnaround as turnaround_module
from conftest import add_action, make_adventure
from test_scene import _face, _reference_image_nodes, pipeline_transport
from test_v3 import PNG_BYTES, use_tmp_storage


def _write_video(path: Path, *, frames: int = 8, size: tuple[int, int] = (12, 16)) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    writer = cv2.VideoWriter(
        str(path), cv2.VideoWriter_fourcc(*"mp4v"), 8, size)
    assert writer.isOpened(), "OpenCV precisa do codec mp4v para este teste"
    colors = ((255, 0, 0), (0, 255, 0), (0, 0, 255), (255, 255, 255))
    for index in range(frames):
        color = colors[min(3, index * 4 // frames)]
        frame = np.full((size[1], size[0], 3), color, dtype=np.uint8)
        writer.write(frame)
    writer.release()


def _png_bytes(color: tuple[int, int, int]) -> bytes:
    output = io.BytesIO()
    Image.new("RGB", (8, 8), color).save(output, format="PNG")
    return output.getvalue()


def _character(adventure_id: str, tmp_path: Path, title: str = "NPC") -> tuple[dict, dict]:
    card_path = tmp_path / f"{title}-anchor.png"
    card_path.write_bytes(PNG_BYTES)
    with db.session() as conn:
        card = db.insert_card(
            conn, adventure_id, title=title, type="character", entry="A traveller")
        anchor = db.insert_image(
            conn, adventure_id=adventure_id, action_seq=-1,
            file_path=str(card_path), prompt="anchor", kind="portrait", ref_id=card["id"],
            width=8, height=8,
        )
        db.set_card_portrait(conn, card["id"], anchor["id"], anchor_id=anchor["id"])
        return db.get_card(conn, adventure_id, card["id"]), anchor


def _mock_generation(monkeypatch, tmp_path: Path, source_video: Path,
                     capture: dict | None = None, after_render=None) -> None:
    image_calls: list[dict] = []
    payloads = [
        _png_bytes((255, 0, 0)),
        _png_bytes((0, 255, 0)),
        _png_bytes((0, 0, 255)),
    ]

    def generate_image(cfg, prompt, *, adventure_id, image_id, **kwargs):
        index = len(image_calls)
        content = payloads[index % len(payloads)]
        image_calls.append({"prompt": prompt, "content": content, **kwargs})
        target = tmp_path / "imgs" / adventure_id / f"{image_id}.png"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
        return {
            "file_path": str(target), "provider": "comfyui",
            "width": kwargs.get("width") or 8, "height": kwargs.get("height") or 8,
        }

    def generate_clip(cfg, prompt, *, adventure_id, image_id, source_png, transport):
        if capture is not None:
            capture["prompt"] = prompt
            capture["source_png"] = source_png
            capture["image_calls"] = image_calls
        target = tmp_path / "imgs" / adventure_id / f"{image_id}.mp4"
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source_video, target)
        if after_render is not None:
            after_render()
        return {
            "file_path": str(target), "provider": "comfyui",
            "width": 12, "height": 16, "media_type": "video/mp4",
            "duration_ms": 1000,
        }

    monkeypatch.setattr(image_client, "generate_image", generate_image)
    monkeypatch.setattr(image_client, "generate_clip", generate_clip)


def test_extracts_quarter_frames_and_builds_2x2_sheet(tmp_path):
    video = tmp_path / "turn.mp4"
    _write_video(video)
    views = [tmp_path / f"view-{index}.png" for index in range(4)]
    sheet = tmp_path / "sheet.png"

    size = turnaround_module.extract_turnaround(video, views, sheet)

    assert size == (12, 16)
    assert all(Image.open(path).size == (12, 16) for path in views)
    with Image.open(sheet) as image:
        assert image.size == (24, 32)
        assert image.getpixel((0, 0))[2] > 180       # frame 0: azul em RGB
        assert image.getpixel((12, 0))[1] > 180      # frame 2: verde
        assert image.getpixel((0, 16))[0] > 180      # frame 4: vermelho
        assert image.getpixel((12, 16))[0] > 180     # frame 6: branco
        assert image.getpixel((12, 16))[1] > 180


def test_builds_split_from_canonical_face_and_master_body(tmp_path):
    anchor = _png_bytes((255, 0, 0))
    master = _png_bytes((0, 0, 255))
    output = tmp_path / "split.png"

    assert turnaround_module.build_split_frame(anchor, master, output) == (1344, 768)

    with Image.open(output) as image:
        assert image.size == (1344, 768)
        assert image.getpixel((224, 384)) == (0, 0, 255)
        assert image.getpixel((896, 384)) == (255, 0, 0)
        assert image.getpixel((448, 384)) == (224, 224, 224)


def test_route_executor_result_event_and_card_reference(client, tmp_path, monkeypatch):
    use_tmp_storage(tmp_path)
    adv = make_adventure()
    card, anchor = _character(adv["id"], tmp_path)
    video = tmp_path / "source.mp4"
    _write_video(video)
    capture: dict = {}
    _mock_generation(monkeypatch, tmp_path, video, capture)

    response = client.post(f"/api/adventures/{adv['id']}/cards/{card['id']}/turnaround")
    assert response.status_code == 202, response.text
    queued = response.json()
    assert queued["type"] == "character_turnaround"
    assert queued["ref_id"] == card["id"]
    assert queued["payload"]["source_image_id"] == anchor["id"]

    with events.subscribe(adv["id"]) as subscription:
        assert jobs.process_pending() == 1
        terminal = []
        while (event := subscription.get(0.05)) is not None:
            if event.type == "turnaround_ready":
                terminal.append(event.data)

    with db.session() as conn:
        done = db.get_job(conn, queued["id"])
        updated = db.get_card(conn, adv["id"], card["id"])
        result = done["result"]
        assert done["status"] == "done"
        assert result["card_id"] == card["id"]
        assert result["image"] == result["sheet"]
        assert len(result["views"]) == 4
        reference = json.loads(updated["sheet"])["reference"]["turnaround"]
        assert reference == {
            "source_image_id": anchor["id"],
            "master_image_id": result["master"]["id"],
            "design_sheet_image_id": result["design_sheet"]["id"],
            "split_image_id": result["split"]["id"],
            "sheet_image_id": result["sheet"]["id"],
            "clip_image_id": result["clip"]["id"],
            "view_image_ids": [view["id"] for view in result["views"]],
        }
        master = db.get_image(conn, result["master"]["id"])
        design_sheet = db.get_image(conn, result["design_sheet"]["id"])
        split = db.get_image(conn, result["split"]["id"])
        clip = db.get_image(conn, result["clip"]["id"])
        sheet = db.get_image(conn, result["sheet"]["id"])
        views = [db.get_image(conn, view["id"]) for view in result["views"]]
        assert master["kind"] == "character_master"
        assert master["source_image_id"] == anchor["id"]
        assert design_sheet["kind"] == "character_sheet"
        assert design_sheet["source_image_id"] == master["id"]
        assert split["kind"] == "turnaround_start"
        assert split["source_image_id"] == master["id"]
        assert clip["kind"] == "turnaround_clip"
        assert clip["media_type"] == "video/mp4"
        assert clip["source_image_id"] == split["id"]
        assert sheet["kind"] == "reference_sheet"
        assert (sheet["width"], sheet["height"]) == (24, 32)
        assert [view["kind"] for view in views] == ["reference_view"] * 4
        assert all(view["ref_id"] == card["id"] for view in views)
        assert len(terminal) == 1
        assert terminal[0]["card_id"] == card["id"]
        assert terminal[0]["sheet"] == result["sheet"]
        assert terminal[0]["clip"] == result["clip"]
        assert terminal[0]["views"] == result["views"]

    image_calls = capture["image_calls"]
    assert len(image_calls) == 2
    assert image_calls[0]["reference_images"] == [PNG_BYTES]
    assert image_calls[1]["reference_images"] == [image_calls[0]["content"]]
    with Image.open(io.BytesIO(capture["source_png"])) as split:
        assert split.size == (1344, 768)
        assert split.getpixel((224, 384)) == (255, 0, 0)
        with Image.open(io.BytesIO(PNG_BYTES)) as anchor_image:
            expected_face = anchor_image.convert("RGB").getpixel((0, 0))
        assert split.getpixel((896, 384)) == expected_face

    repeated = client.post(
        f"/api/adventures/{adv['id']}/cards/{card['id']}/turnaround")
    assert repeated.status_code == 202
    assert repeated.json()["id"] == queued["id"]
    assert repeated.json()["status"] == "done"
    forced = client.post(
        f"/api/adventures/{adv['id']}/cards/{card['id']}/turnaround",
        json={"force": True},
    )
    assert forced.status_code == 202
    assert forced.json()["id"] != queued["id"]
    assert forced.json()["status"] == "pending"


def test_cancel_cleans_every_unpublished_file_and_interrupts_comfy(
        tmp_db, tmp_path, monkeypatch):
    use_tmp_storage(tmp_path)
    adv = make_adventure()
    card, anchor = _character(adv["id"], tmp_path)
    video = tmp_path / "source.mp4"
    _write_video(video)
    with db.session() as conn:
        queued = jobs.enqueue_turnaround(conn, adv["id"], card["id"], anchor["id"])

    def cancel_after_render():
        with db.session() as conn:
            jobs.cancel(conn, queued["id"])

    _mock_generation(monkeypatch, tmp_path, video, after_render=cancel_after_render)
    assert jobs.process_pending() == 1

    with db.session() as conn:
        assert db.get_job(conn, queued["id"])["status"] == "canceled"
        assert conn.execute(
            "SELECT COUNT(*) FROM images WHERE kind IN "
            "('character_master','character_sheet','turnaround_start',"
            "'turnaround_clip','reference_view','reference_sheet')"
        ).fetchone()[0] == 0
        updated = db.get_card(conn, adv["id"], card["id"])
        assert "turnaround" not in json.loads(updated["sheet"] or "{}").get("reference", {})
    assert list((tmp_path / "imgs").rglob("*.mp4")) == []
    assert list((tmp_path / "imgs").rglob("*.png")) == []

    interrupted = []
    monkeypatch.setattr(
        image_client, "interrupt_comfy",
        lambda cfg, transport: interrupted.append((cfg, transport)))
    marker = object()
    assert jobs.interrupt_image({**queued, "status": "canceled"}, marker)
    assert interrupted and interrupted[0][1] is marker


def test_comfy_multiview_budget_and_sdwebui_single_anchor(tmp_db, tmp_path):
    adv = make_adventure()
    first, first_anchor = _character(adv["id"], tmp_path, "First")
    second, second_anchor = _character(adv["id"], tmp_path, "Second")
    with db.session() as conn:
        for card in (first, second):
            view_ids = []
            for index in range(4):
                path = tmp_path / f"{card['title']}-view-{index}.png"
                path.write_bytes(PNG_BYTES)
                view = db.insert_image(
                    conn, adventure_id=adv["id"], action_seq=-1,
                    file_path=str(path), kind="reference_view", ref_id=card["id"])
                view_ids.append(view["id"])
            sheet = json.loads(card["sheet"] or "{}")
            sheet["reference"] = {"turnaround": {
                "sheet_image_id": "sheet", "clip_image_id": "clip",
                "view_image_ids": view_ids,
            }}
            db.update_card(conn, adv["id"], card["id"], {"sheet": json.dumps(sheet)})
        first = db.get_card(conn, adv["id"], first["id"])
        second = db.get_card(conn, adv["id"], second["id"])
        cast = [first, second]
        first_views = json.loads(first["sheet"])["reference"]["turnaround"]["view_image_ids"]
        second_views = json.loads(second["sheet"])["reference"]["turnaround"]["view_image_ids"]
        card_ids, comfy_ids = likeness.cast_reference_ids_for_provider(cast, "comfyui")
        assert card_ids == [first["id"], second["id"]]
        assert comfy_ids == [
            first_anchor["id"], second_anchor["id"], first_views[1], second_views[1],
        ]
        assert likeness.cast_reference_ids_for_provider(cast, "sdwebui")[1] == [
            first_anchor["id"], second_anchor["id"]]
        plan = scene.cast_reference_plan(conn, adv["id"], cast, provider="comfyui")
        assert plan["reference_image_ids"] == comfy_ids
        assert len(plan["blobs"]) == len(plan["reference_hashes"]) == 4
        sd_plan = scene.cast_reference_plan(conn, adv["id"], cast, provider="sdwebui")
        assert sd_plan["reference_image_ids"] == [first_anchor["id"], second_anchor["id"]]

        one_card_ids, one_refs = likeness.cast_reference_ids_for_provider([first], "comfyui")
        assert one_card_ids == [first["id"]]
        assert one_refs == [first_anchor["id"], *first_views[1:4]]

    four = [{
        "id": f"card-{index}", "type": "character",
        "portrait_anchor_id": f"anchor-{index}",
        "sheet": json.dumps({"reference": {"turnaround": {
            "view_image_ids": [f"view-{index}-0", f"view-{index}-1"],
        }}}),
    } for index in range(4)]
    assert likeness.cast_reference_ids_for_provider(four, "comfyui")[1] == [
        f"anchor-{index}" for index in range(4)]


def test_complete_references_prioritize_master_sheet_and_distinct_angles(tmp_db, tmp_path):
    adv = make_adventure()
    first, first_anchor = _character(adv["id"], tmp_path, "First")
    second, second_anchor = _character(adv["id"], tmp_path, "Second")
    with db.session() as conn:
        for card in (first, second):
            rows = {}
            for key, kind in (("master", "character_master"),
                              ("design", "character_sheet"), ("back", "reference_view")):
                path = tmp_path / f"{card['title']}-{key}.png"
                path.write_bytes(PNG_BYTES)
                rows[key] = db.insert_image(
                    conn, adventure_id=adv["id"], action_seq=-1,
                    file_path=str(path), kind=kind, ref_id=card["id"])
            sheet = json.loads(card["sheet"] or "{}")
            sheet["reference"] = {"turnaround": {
                "master_image_id": rows["master"]["id"],
                "design_sheet_image_id": rows["design"]["id"],
                "view_image_ids": ["front", rows["back"]["id"]],
            }}
            db.update_card(conn, adv["id"], card["id"], {"sheet": json.dumps(sheet)})
        first = db.get_card(conn, adv["id"], first["id"])
        second = db.get_card(conn, adv["id"], second["id"])
        first_ref = json.loads(first["sheet"])["reference"]["turnaround"]
        second_ref = json.loads(second["sheet"])["reference"]["turnaround"]
        assert likeness.cast_reference_ids_for_provider([first], "comfyui")[1] == [
            first_ref["master_image_id"], first_ref["design_sheet_image_id"],
        ]
        assert likeness.cast_reference_ids_for_provider([first, second], "comfyui")[1] == [
            first_ref["master_image_id"], second_ref["master_image_id"],
            first_ref["design_sheet_image_id"], second_ref["design_sheet_image_id"],
        ]
        assert likeness.cast_reference_ids_for_provider([first, second], "sdwebui")[1] == [
            first_anchor["id"], second_anchor["id"],
        ]
        prompt, include_people = scene._prompt(
            "Rainy alley", "wet stone and neon signs", "photorealistic", "rain",
            [{**first, "portrait_prompt": "short red hair, green eyes, leather coat"}],
            "First waits beside the exit.",
        )
        assert include_people
        assert "Do not copy the reference composition" in prompt
        assert "First waits beside the exit" in prompt
        explicit_prompt, _ = scene._prompt(
            "Rainy alley", "wet stone and neon signs", "photorealistic", "rain",
            [{**first, "portrait_prompt": "short red hair, green eyes, leather coat"}],
            "First is naked beside the exit.",
        )
        assert "Ignore outfits" in explicit_prompt
        assert "body, outfit" not in explicit_prompt


def test_worker_replans_reference_ids_when_provider_changes(tmp_db, tmp_path):
    adv = make_adventure()
    card, anchor = _character(adv["id"], tmp_path, "First")
    with db.session() as conn:
        rows = {}
        for key, kind in (("master", "character_master"), ("design", "character_sheet")):
            path = tmp_path / f"First-{key}.png"
            path.write_bytes(PNG_BYTES)
            rows[key] = db.insert_image(
                conn, adventure_id=adv["id"], action_seq=-1,
                file_path=str(path), kind=kind, ref_id=card["id"])
        sheet = json.loads(card["sheet"] or "{}")
        sheet["reference"] = {"turnaround": {
            "master_image_id": rows["master"]["id"],
            "design_sheet_image_id": rows["design"]["id"],
        }}
        card = db.update_card(conn, adv["id"], card["id"], {"sheet": json.dumps(sheet)})
        plan = scene.cast_reference_plan(conn, adv["id"], [card], provider="comfyui")

    payload = {
        "cast_card_ids": [card["id"]],
        "reference_image_ids": plan["reference_image_ids"],
        "reference_hashes": plan["reference_hashes"],
        "reference_provider": "comfyui",
        "explicit": False,
    }
    job = {"adventure_id": adv["id"], "payload": payload}

    sd_blobs = jobs._reference_blobs(job, people=False, provider="sdwebui")
    assert sd_blobs == [PNG_BYTES]
    assert payload["reference_provider"] == "sdwebui"
    assert payload["reference_image_ids"] == [anchor["id"]]
    assert payload["reference_hashes"] == likeness.reference_hashes(sd_blobs)

    comfy_blobs = jobs._reference_blobs(job, people=False, provider="comfyui")
    assert comfy_blobs == [PNG_BYTES, PNG_BYTES]
    assert payload["reference_provider"] == "comfyui"
    assert payload["reference_image_ids"] == [
        rows["master"]["id"], rows["design"]["id"],
    ]
    assert payload["reference_hashes"] == likeness.reference_hashes(comfy_blobs)

    payload.update({
        "reference_provider": "openai_compatible",
        "reference_image_ids": [], "reference_hashes": [],
    })
    reopened = jobs._reference_blobs(job, people=True, provider="comfyui")
    assert reopened == [PNG_BYTES, PNG_BYTES]
    assert payload["reference_image_ids"] == [
        rows["master"]["id"], rows["design"]["id"],
    ]


def test_backdrop_worker_reuses_image_under_recomputed_ref(tmp_db, tmp_path):
    use_tmp_storage(tmp_path)
    with db.session() as conn:
        db.set_setting(conn, "image.provider", json.dumps("comfyui"))
    adv = make_adventure()
    payload = {
        "prompt": "An empty room", "include_people": False, "explicit": False,
        "cache_key": "stale", "cast_card_ids": [],
        "reference_image_ids": [], "reference_hashes": [],
        "reference_provider": "comfyui",
    }
    with db.session() as conn:
        queued = db.insert_job(
            conn, adventure_id=adv["id"], type="scene_backdrop", ref_id="old-ref",
            status="pending", payload=payload, priority=jobs.PRIORITY_BACKDROP,
        )
        running = db.claim_next_job(conn)
    capture: dict = {}
    transport = pipeline_transport(capture=capture)
    result = jobs._execute_backdrop(running, transport)
    with db.session() as conn:
        image = db.get_image(conn, result["image"]["id"])
        expected_ref = f"{adv['id']}:{image['cache_key'][:40]}"
        assert image["ref_id"] == expected_ref
        assert result["ref_id"] == expected_ref
        db.finish_job(conn, queued["id"], "done", result)
        second = db.insert_job(
            conn, adventure_id=adv["id"], type="scene_backdrop", ref_id="old-ref",
            status="pending", payload=payload, priority=jobs.PRIORITY_BACKDROP,
        )
        running_second = db.claim_next_job(conn)
    reused = jobs._execute_backdrop(running_second, transport)
    assert reused["reused"] is True
    assert reused["image"]["id"] == image["id"]
    assert reused["ref_id"] == expected_ref
    assert len(capture["graphs"]) == 1
    with db.session() as conn:
        db.finish_job(conn, second["id"], "done", reused)


def test_openai_backdrop_replans_cast_when_comfy_executes(client, tmp_path):
    use_tmp_storage(tmp_path)
    adv = make_adventure()
    with db.session() as conn:
        db.set_setting(conn, "image.provider", json.dumps("openai_compatible"))
        card = db.insert_card(
            conn, adv["id"], type="character", title="Ada", triggers=["Ada"],
            entry="Barkeep.", portrait_prompt="silver hair",
        )
    _face(adv["id"], card["id"], PNG_BYTES)
    add_action(adv["id"], 0, "continue", "Ada waits outside.")
    response = client.post(f"/api/adventures/{adv['id']}/scene", json={})
    assert response.status_code == 200
    with db.session() as conn:
        job = next(item for item in db.list_jobs(conn, adv["id"])
                   if item["type"] == "scene_backdrop")
        assert job["payload"]["reference_provider"] == "openai_compatible"
        assert job["payload"]["cast_card_ids"] == [card["id"]]
        assert job["payload"]["reference_image_ids"] == []
        db.set_setting(conn, "image.provider", json.dumps("comfyui"))

    capture: dict = {}
    assert jobs.process_pending(pipeline_transport(capture=capture)) == 1
    assert _reference_image_nodes(capture["graphs"][0])
    with db.session() as conn:
        assert db.get_job(conn, job["id"])["status"] == "done"


def test_portrait_schedules_complete_reference_only_once(tmp_db, tmp_path, monkeypatch):
    use_tmp_storage(tmp_path)
    adv = make_adventure()
    card, _anchor = _character(adv["id"], tmp_path)
    video = tmp_path / "source.mp4"
    _write_video(video)
    _mock_generation(monkeypatch, tmp_path, video)
    real_video_settings = image_client.video_settings
    monkeypatch.setattr(
        image_client, "video_settings",
        lambda cfg: {**real_video_settings(cfg), "auto_turnaround": True},
    )
    with db.session() as conn:
        db.update_card(conn, adv["id"], card["id"], {"portrait_prompt": "brown-haired traveller"})
        portrait = jobs.enqueue_portrait(conn, adv["id"], card["id"], replace=True)

    assert jobs.process_pending() == 2
    with db.session() as conn:
        turnarounds = [job for job in db.list_jobs(conn, adv["id"])
                       if job["type"] == "character_turnaround"]
        assert len(turnarounds) == 1
        assert turnarounds[0]["status"] == "done"
        jobs._schedule_turnaround_followup(portrait)
        assert len(db.list_jobs(conn, adv["id"])) == 2


def test_upload_preserves_and_clear_removes_turnaround(tmp_db, tmp_path, monkeypatch):
    monkeypatch.setattr(likeness, "references_dir", lambda: tmp_path / "references")
    adv = make_adventure()
    card, _ = _character(adv["id"], tmp_path)
    turnaround = {
        "sheet_image_id": "sheet", "clip_image_id": "clip",
        "view_image_ids": ["0", "90", "180", "270"],
    }
    with db.session() as conn:
        sheet = {"reference": {"turnaround": turnaround}}
        card = db.update_card(conn, adv["id"], card["id"], {"sheet": json.dumps(sheet)})
        uploaded = likeness.save_upload(conn, card, PNG_BYTES, filename="anchor.png")
        assert json.loads(uploaded["sheet"])["reference"]["turnaround"] == turnaround
        cleared = likeness.clear_reference(conn, uploaded)
        assert json.loads(cleared["sheet"])["reference"] == {}
