"""Turnaround 360° local: extração, publicação atômica e referências multiview."""

from __future__ import annotations

import io
import json
import shutil
from pathlib import Path

import cv2
import numpy as np
import pytest
from PIL import Image

from app import db, events, image_client, jobs, likeness, scene
from app import turnaround as turnaround_module
from conftest import add_action, make_adventure
from test_scene import _face, _reference_image_nodes, pipeline_transport
from test_v3 import PNG_BYTES, use_tmp_storage


def _write_video(path: Path, *, frames: int = 8, size: tuple[int, int] = (12, 16),
                 clean: bool = False) -> None:
    """Quatro cores chapadas (fundo que muda: o portão recusa) ou, com `clean`,
    fundo cinza fixo e só um bloco central girando de cor (o portão aceita)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    writer = cv2.VideoWriter(
        str(path), cv2.VideoWriter_fourcc(*"mp4v"), 8, size)
    assert writer.isOpened(), "OpenCV precisa do codec mp4v para este teste"
    colors = ((255, 0, 0), (0, 255, 0), (0, 0, 255), (255, 255, 255))
    for index in range(frames):
        color = colors[min(3, index * 4 // frames)]
        if clean:
            frame = np.full((size[1], size[0], 3), 200, dtype=np.uint8)
            x0, x1 = size[0] * 3 // 8, size[0] * 5 // 8
            y0, y1 = size[1] // 4, size[1] * 3 // 4
            frame[y0:y1, x0:x1] = color
        else:
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


def _fake_view_cycle(sheet_png, output_path, **_kwargs):
    Path(output_path).parent.mkdir(parents=True, exist_ok=True)
    Path(output_path).write_bytes(b"\x00\x00\x00\x18ftypisom-cycle")
    return 4000


def _mock_generation(monkeypatch, tmp_path: Path, source_video: Path,
                     capture: dict | None = None, after_render=None,
                     after_sheet=None) -> None:
    image_calls: list[dict] = []
    clip_calls: list[dict] = []
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
        if after_sheet is not None and index == 1:
            after_sheet()
        if capture is not None:
            capture["image_calls"] = image_calls
        return {
            "file_path": str(target), "provider": "comfyui",
            "width": kwargs.get("width") or 8, "height": kwargs.get("height") or 8,
        }

    def generate_clip(cfg, prompt, *, adventure_id, image_id, source_png, transport,
                      **kwargs):
        clip_calls.append({"prompt": prompt, "source_png": source_png, **kwargs})
        if capture is not None:
            capture["prompt"] = prompt
            capture["source_png"] = source_png
            capture["clip_calls"] = clip_calls
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
    monkeypatch.setattr(turnaround_module, "build_view_cycle", _fake_view_cycle)


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
        # turnaround + giro do Wan; o vídeo do mock troca o fundo e é recusado
        assert jobs.process_pending(pipeline_transport()) == 2
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
        assert clip["provider"] == "pillow"           # giro da ficha, sem modelo
        assert clip["source_image_id"] == design_sheet["id"]
        assert clip["duration_ms"] == 4000
        clip_job = [job for job in db.list_jobs(conn, adv["id"])
                    if job["type"] == "turnaround_clip"]
        assert len(clip_job) == 1 and clip_job[0]["status"] == "done"
        assert clip_job[0]["result"]["rejected"] is True
        assert clip_job[0]["payload"]["source_image_id"] == split["id"]
        assert sheet["kind"] == "reference_sheet"
        # folha 2×2 com células de 1/3 × 1/2 da ficha (8×8 no mock)
        assert (sheet["width"], sheet["height"]) == (4, 8)
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
    # recusado pelo portão: uma segunda semente, nunca mais que isso
    clip_calls = capture["clip_calls"]
    assert len(clip_calls) == 2
    assert all(isinstance(call["seed"], int) for call in clip_calls)
    assert all(call["video_overrides"]["engine"] == "wan" for call in clip_calls)
    assert clip_calls[0]["video_overrides"]["steps"] == 20
    assert clip_calls[0]["video_overrides"]["frames"] == 121
    assert "360 degree" in capture["prompt"]
    assert not list((tmp_path / "imgs").rglob("*.mp4")) or all(
        path.read_bytes().endswith(b"cycle") for path in (tmp_path / "imgs").rglob("*.mp4"))
    with Image.open(io.BytesIO(capture["source_png"])) as frame:
        # o vídeo parte só do mestre (vermelho no mock), num quadro vertical
        assert frame.size == (448, 768)
        assert frame.getpixel((224, 384)) == (255, 0, 0)
        assert frame.getpixel((2, 2)) == (224, 224, 224)

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


def test_uploaded_photo_leads_the_master_and_the_sheet(client, tmp_path, monkeypatch):
    """Com foto enviada no card, ela é a primeira referência das duas etapas;
    a âncora (e depois o mestre) vem em seguida. Miniatura sobe para 512 px."""
    use_tmp_storage(tmp_path)
    refs = tmp_path / "refs"
    refs.mkdir()
    monkeypatch.setenv("AIDUNGEON_REFERENCES_DIR", str(refs))
    photo = refs / "npc-photo.jpg"
    Image.new("RGB", (150, 150), (200, 120, 80)).save(photo, format="JPEG")
    adv = make_adventure()
    card, _anchor = _character(adv["id"], tmp_path)
    with db.session() as conn:
        conn.execute(
            "UPDATE story_cards SET sheet=? WHERE id=?",
            (json.dumps({"reference": {"image_path": str(photo), "source": "upload"}}),
             card["id"]))
    video = tmp_path / "source.mp4"
    _write_video(video)
    capture: dict = {}
    _mock_generation(monkeypatch, tmp_path, video, capture)

    response = client.post(f"/api/adventures/{adv['id']}/cards/{card['id']}/turnaround")
    assert response.status_code == 202, response.text
    assert jobs.process_pending(pipeline_transport()) == 2   # + giro do Wan

    master_call, sheet_call = capture["image_calls"][:2]
    assert len(master_call["reference_images"]) == 2
    assert len(sheet_call["reference_images"]) == 2
    with Image.open(io.BytesIO(master_call["reference_images"][0])) as lead:
        assert lead.format == "PNG"
        assert lead.size == (512, 512)
    assert master_call["reference_images"][0] == sheet_call["reference_images"][0]
    assert master_call["reference_images"][1] == PNG_BYTES            # âncora
    assert sheet_call["reference_images"][1] == master_call["content"]  # mestre


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

    _mock_generation(monkeypatch, tmp_path, video, after_sheet=cancel_after_render)
    assert jobs.process_pending(pipeline_transport()) == 1

    with db.session() as conn:
        assert db.get_job(conn, queued["id"])["status"] == "canceled"
        assert conn.execute(
            "SELECT COUNT(*) FROM images WHERE kind IN "
            "('character_master','character_sheet','turnaround_start',"
            "'turnaround_clip','reference_view','reference_sheet')"
        ).fetchone()[0] == 0
        updated = db.get_card(conn, adv["id"], card["id"])
        assert "turnaround" not in json.loads(updated["sheet"] or "{}").get("reference", {})
        assert not [job for job in db.list_jobs(conn, adv["id"])
                    if job["type"] == "turnaround_clip"]
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
        # Cena explícita ignora mestre/ficha (folhas de roupa) e usa só a âncora.
        assert likeness.cast_reference_ids_for_provider(
            [first, second], "comfyui", explicit=True)[1] == [
            first_anchor["id"], second_anchor["id"],
        ]
        prompt, include_people = scene._prompt(
            "Rainy alley", "wet stone and neon signs", "photorealistic", "rain",
            [{**first, "portrait_prompt": "short red hair, green eyes, leather coat"}],
            "First waits beside the exit.",
        )
        assert include_people
        assert "one single continuous photograph of that place" in prompt
        assert "First waits beside the exit" in prompt
        explicit_prompt, _ = scene._prompt(
            "Rainy alley", "wet stone and neon signs", "photorealistic", "rain",
            [{**first, "portrait_prompt": "short red hair, green eyes, leather coat"}],
            "First is naked beside the exit.",
        )
        assert "the clothing state in this prompt is authoritative" in explicit_prompt.lower()
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

    # retrato, turnaround e o giro do Wan
    assert jobs.process_pending(pipeline_transport()) == 3
    with db.session() as conn:
        turnarounds = [job for job in db.list_jobs(conn, adv["id"])
                       if job["type"] == "character_turnaround"]
        assert len(turnarounds) == 1
        assert turnarounds[0]["status"] == "done"
        jobs._schedule_turnaround_followup(portrait)
        assert len(db.list_jobs(conn, adv["id"])) == 3


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


def test_sheet_views_come_from_the_design_sheet_cells(tmp_path):
    """Frente, perfil e costas da linha de cima e o close-up de frente."""
    colors = [(255, 0, 0), (0, 255, 0), (0, 0, 255), (255, 255, 0), (0, 255, 255), (255, 0, 255)]
    sheet = Image.new("RGB", (30, 20))
    for index, color in enumerate(colors):
        column, row = index % 3, index // 3
        sheet.paste(Image.new("RGB", (10, 10), color), (column * 10, row * 10))
    raw = io.BytesIO(); sheet.save(raw, format="PNG")
    views = [tmp_path / f"v{i}.png" for i in range(4)]
    out = tmp_path / "grid.png"
    assert turnaround_module.extract_sheet_views(raw.getvalue(), views, out) == (10, 10)
    got = []
    for path in views:
        with Image.open(path) as view:
            got.append(view.convert("RGB").getpixel((5, 5)))
    assert got == [colors[0], colors[1], colors[2], colors[3]]
    with Image.open(out) as grid:
        assert grid.size == (20, 20)


def _published_turnaround(adv_id: str, card_id: str) -> dict:
    with db.session() as conn:
        card = db.get_card(conn, adv_id, card_id)
        return json.loads(card["sheet"])["reference"]["turnaround"]


def test_clean_wan_clip_replaces_the_sheet_cycle(tmp_db, tmp_path, monkeypatch):
    """O turnaround publica na hora com o giro da ficha; o Wan chega depois e
    troca só `clip_image_id`, com evento próprio, quando passa no portão."""
    use_tmp_storage(tmp_path)
    adv = make_adventure()
    card, anchor = _character(adv["id"], tmp_path)
    video = tmp_path / "clean.mp4"
    _write_video(video, frames=16, size=(48, 80), clean=True)
    capture: dict = {}
    _mock_generation(monkeypatch, tmp_path, video, capture)
    with db.session() as conn:
        queued = jobs.enqueue_turnaround(conn, adv["id"], card["id"], anchor["id"])

    with events.subscribe(adv["id"]) as subscription:
        assert jobs.process_pending(pipeline_transport()) == 2
        ready = []
        while (event := subscription.get(0.05)) is not None:
            if event.type == "turnaround_ready":
                ready.append(event.data)

    with db.session() as conn:
        result = db.get_job(conn, queued["id"])["result"]
        saved = _published_turnaround(adv["id"], card["id"])
        clip = db.get_image(conn, saved["clip_image_id"])
        assert saved["clip_image_id"] != result["clip"]["id"]
        assert saved["view_image_ids"] == [view["id"] for view in result["views"]]
        assert clip["kind"] == "turnaround_clip"
        assert clip["provider"] == "comfyui"
        assert clip["source_image_id"] == result["split"]["id"]
    assert len(capture["clip_calls"]) == 1
    assert [bool(item.get("views")) for item in ready] == [True, False]
    assert ready[1]["clip"]["id"] == saved["clip_image_id"]


def test_wan_clip_for_an_older_turnaround_is_discarded(tmp_db, tmp_path, monkeypatch):
    use_tmp_storage(tmp_path)
    adv = make_adventure()
    card, anchor = _character(adv["id"], tmp_path)
    video = tmp_path / "clean.mp4"
    _write_video(video, frames=16, size=(48, 80), clean=True)
    capture: dict = {}
    _mock_generation(monkeypatch, tmp_path, video, capture)
    with db.session() as conn:
        jobs.enqueue_turnaround(conn, adv["id"], card["id"], anchor["id"])
    with db.session() as conn:
        first = db.claim_next_job(conn)
    jobs._run_job(first, pipeline_transport())
    before = _published_turnaround(adv["id"], card["id"])
    with db.session() as conn:
        stale = db.claim_next_job(conn)
        assert stale["type"] == "turnaround_clip"
        sheet = json.loads(db.get_card(conn, adv["id"], card["id"])["sheet"])
        sheet["reference"]["turnaround"]["split_image_id"] = "newer"
        db.update_card(conn, adv["id"], card["id"], {"sheet": json.dumps(sheet)})
    jobs._run_job(stale, pipeline_transport())
    with db.session() as conn:
        assert db.get_job(conn, stale["id"])["result"]["stale"] is True
    assert _published_turnaround(adv["id"], card["id"])["clip_image_id"] == before["clip_image_id"]
    assert "clip_calls" not in capture


def test_new_image_preempts_the_wan_clip_and_requeues_it(tmp_db, tmp_path):
    adv = make_adventure()
    payload = {"card_id": "c1", "source_image_id": "s1", "prompt": "turn", "automatic": True}
    with db.session() as conn:
        clip = jobs.enqueue(conn, "turnaround_clip", adv["id"], "c1", payload,
                            priority=jobs.PRIORITY_PORTRAIT_CLIP,
                            supersede_key=f"turnaround_clip:{adv['id']}:c1")
        assert db.claim_next_job(conn)["id"] == clip["id"]
    for round_ in range(1, jobs._TURNAROUND_CLIP_MAX_REQUEUES + 2):
        with db.session() as conn:
            jobs.enqueue(conn, "location_image", adv["id"], f"loc{round_}", {"prompt": "x"})
            assert db.get_job(conn, clip["id"])["status"] == "canceled"
            again = [job for job in db.list_jobs(conn, adv["id"])
                     if job["type"] == "turnaround_clip" and job["status"] == "pending"]
            if round_ > jobs._TURNAROUND_CLIP_MAX_REQUEUES:
                assert again == []
                break
            assert len(again) == 1
            assert again[0]["payload"]["requeues"] == round_
            conn.execute("UPDATE jobs SET status='canceled' WHERE type='location_image'")
            conn.execute("UPDATE jobs SET status='running' WHERE id=?", (again[0]["id"],))
            clip = again[0]


def test_quality_gate_rejects_a_changing_background(tmp_path):
    clean, dirty = tmp_path / "clean.mp4", tmp_path / "dirty.mp4"
    _write_video(clean, frames=16, size=(48, 80), clean=True)
    _write_video(dirty, frames=16, size=(48, 80))
    assert turnaround_module.clip_is_clean(clean) is True
    assert turnaround_module.clip_is_clean(dirty) is False
    assert turnaround_module.clip_is_clean(tmp_path / "missing.mp4") is False


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg não instalado")
def test_view_cycle_is_a_browser_mp4_through_four_views(tmp_path):
    colors = [(255, 0, 0), (0, 255, 0), (0, 0, 255)]
    sheet = Image.new("RGB", (90, 80), (224, 224, 224))
    for column, color in enumerate(colors):
        sheet.paste(Image.new("RGB", (10, 30), color), (column * 30 + 10, 5))
    raw = io.BytesIO(); sheet.save(raw, format="PNG")
    out = tmp_path / "cycle.mp4"
    duration = turnaround_module.build_view_cycle(raw.getvalue(), out, size=(48, 80), fps=10)
    assert duration == 4000                       # 4 vistas × (6 + 4 frames) a 10 fps
    capture = cv2.VideoCapture(str(out))
    try:
        assert int(capture.get(cv2.CAP_PROP_FRAME_COUNT)) == 40
        firsts = []
        for index in (0, 10, 20, 30):
            capture.set(cv2.CAP_PROP_POS_FRAMES, index)
            ok, frame = capture.read()
            assert ok
            b, g, r = frame[40, 24]
            firsts.append(max(range(3), key=lambda i: (r, g, b)[i]))
    finally:
        capture.release()
    assert firsts == [0, 1, 2, 1]                 # frente, perfil, costas, perfil espelhado
    assert out.read_bytes()[4:8] == b"ftyp"
