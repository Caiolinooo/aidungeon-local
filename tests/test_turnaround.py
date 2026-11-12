"""Turnaround 360° local: extração, publicação atômica e referências multiview."""

from __future__ import annotations

import io
import json
import shutil
from datetime import datetime, timezone
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


def _fake_view_cycle(view_pngs, output_path, **_kwargs):
    Path(output_path).parent.mkdir(parents=True, exist_ok=True)
    Path(output_path).write_bytes(b"\x00\x00\x00\x18ftypisom-cycle")
    return 4000


# Mestre, frente, perfil, costas e close-up, nessa ordem, cada um de uma cor.
PAINT_COLORS = [(255, 0, 0), (0, 255, 0), (0, 0, 255), (255, 255, 0), (0, 255, 255)]


def _mock_generation(monkeypatch, tmp_path: Path, source_video: Path,
                     capture: dict | None = None, after_render=None,
                     after_sheet=None, checks=None, clip_report=None) -> None:
    """Pinturas e clipe falsos. `checks(kind, call_index)` simula o conferidor
    das vistas (padrão: tudo aceito); `clip_report` o portão do Wan (padrão:
    recusado, para o giro das vistas ficar)."""
    image_calls: list[dict] = []
    clip_calls: list[dict] = []
    payloads = [_png_bytes(color) for color in PAINT_COLORS]

    def generate_image(cfg, prompt, *, adventure_id, image_id, **kwargs):
        index = len(image_calls)
        content = payloads[index % len(payloads)]
        image_calls.append({"prompt": prompt, "content": content, "seed": cfg.get("seed"),
                            **kwargs})
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

    def check_view(content, kind):
        return list(checks(kind, len(image_calls) - 1)) if checks else []

    def analyze_clip(path):
        if clip_report is not None:
            return dict(clip_report)
        return {"clean": False, "problems": ["half_turn"]}

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
    monkeypatch.setattr(turnaround_module, "check_view", check_view)
    monkeypatch.setattr(turnaround_module, "analyze_clip", analyze_clip)


def _figure_png(size=(672, 1152), *, box=None, color=(20, 20, 20),
                background=(214, 214, 214), extra_box=None) -> bytes:
    """Estúdio cinza com um retângulo escuro no lugar da pessoa."""
    width, height = size
    image = Image.new("RGB", size, background)
    x0, y0, x1, y1 = box or (width * 3 // 8, height // 12, width * 5 // 8, height * 11 // 12)
    image.paste(Image.new("RGB", (x1 - x0, y1 - y0), color), (x0, y0))
    if extra_box:
        ex0, ey0, ex1, ey1 = extra_box
        image.paste(Image.new("RGB", (ex1 - ex0, ey1 - ey0), color), (ex0, ey0))
    output = io.BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


def test_view_check_flags_cut_feet_empty_and_extra_people(monkeypatch):
    monkeypatch.setattr(turnaround_module, "face_info", lambda rgb, **kw: ("", 0.0, 0.0))
    assert turnaround_module.check_view(_figure_png(), "back") == []
    assert turnaround_module.check_view(_figure_png(), "profile") == []
    cut = _figure_png(box=(252, 96, 420, 1152))
    assert "feet_cut" in turnaround_module.check_view(cut, "back")
    head = _figure_png(box=(252, 0, 420, 1000))
    assert "head_cut" in turnaround_module.check_view(head, "back")
    assert turnaround_module.check_view(_figure_png(box=(300, 500, 310, 510)), "back") == ["empty"]
    twins = _figure_png(box=(60, 96, 230, 1050), extra_box=(440, 96, 610, 1050))
    assert "extra_figure" in turnaround_module.check_view(twins, "back")
    small = _figure_png(box=(300, 600, 372, 1000))
    assert "too_small" in turnaround_module.check_view(small, "back")
    # o rosto é do YuNet: de frente na frente e no close-up, nenhum nas costas
    assert "no_face" in turnaround_module.check_view(_figure_png(), "front")
    monkeypatch.setattr(turnaround_module, "face_info", lambda rgb, **kw: ("frontal", 0.1, 4.5))
    assert turnaround_module.check_view(_figure_png(), "front") == []
    assert "face_visible" in turnaround_module.check_view(_figure_png(), "back")
    assert "not_profile" in turnaround_module.check_view(_figure_png(), "profile")
    closeup = _figure_png((768, 1024), box=(160, 120, 610, 1024))
    # rosto alto com muito corpo embaixo (meio corpo) não é close-up
    assert "not_closeup" in turnaround_module.check_view(closeup, "face")
    assert "not_closeup" in turnaround_module.check_view(_figure_png((768, 1024)), "face")
    monkeypatch.setattr(turnaround_module, "face_info", lambda rgb, **kw: ("frontal", 0.4, 0.7))
    assert turnaround_module.check_view(closeup, "face") == []
    # rosto grande demais numa vista de corpo inteiro: veio um retrato
    assert "not_full_body" in turnaround_module.check_view(_figure_png(), "front")


def test_view_check_reads_a_gradient_floor_as_background(monkeypatch):
    """Chão de estúdio mais escuro embaixo não é figura nem corta os pés."""
    monkeypatch.setattr(turnaround_module, "face_info", lambda rgb, **kw: ("", 0.0, 0.0))
    width, height = 672, 1152
    pixels = np.zeros((height, width, 3), np.uint8)
    for y in range(height):
        pixels[y, :] = 225 - int(70 * y / height)       # 225 no alto, 155 embaixo
    pixels[96:1050, 252:420] = 20
    output = io.BytesIO()
    Image.fromarray(pixels).save(output, format="PNG")
    assert turnaround_module.check_view(output.getvalue(), "back") == []


def test_sheets_have_a_fixed_layout_from_the_painted_views(tmp_path):
    """Folha 2×2 e ficha 1×4 em grade fixa: nenhuma célula vazia ou trocada."""
    views = [_png_bytes(color) for color in PAINT_COLORS[1:]]
    master = _png_bytes(PAINT_COLORS[0])
    sheet = tmp_path / "sheet.png"
    design = tmp_path / "design.png"
    assert turnaround_module.compose_reference_sheet(views, sheet) == (448, 768)
    assert turnaround_module.compose_design_sheet(master, views, design) == (1792, 768)
    with Image.open(sheet) as image:
        assert image.size == (896, 1536)
        got = [image.getpixel((224 + 448 * (i % 2), 384 + 768 * (i // 2))) for i in range(4)]
    assert got == PAINT_COLORS[1:]
    with Image.open(design) as image:
        got = [image.getpixel((224 + 448 * i, 384)) for i in range(4)]
    # mestre, perfil, costas, close-up
    assert got == [PAINT_COLORS[0], PAINT_COLORS[2], PAINT_COLORS[3], PAINT_COLORS[4]]


def test_master_frame_has_no_inner_border(tmp_path):
    """O mestre vira o quadro do Wan por redução, sem moldura cinza: o
    retângulo do fundo antigo aparecia nos primeiros frames do vídeo."""
    output = tmp_path / "frame.png"
    assert turnaround_module.build_master_frame(
        _png_bytes((230, 230, 232)), output) == (448, 768)
    with Image.open(output) as image:
        assert image.size == (448, 768)
        assert {image.getpixel((1, 1)), image.getpixel((446, 766))} == {(230, 230, 232)}


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
        # turnaround + giro do Wan; o portão do mock recusa o giro
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
        assert clip["provider"] == "pillow"           # giro das vistas, sem modelo
        assert clip["source_image_id"] == master["id"]
        assert clip["duration_ms"] == 4000
        assert design_sheet["provider"] == "pillow"   # montada, não pintada
        assert (design_sheet["width"], design_sheet["height"]) == (1792, 768)
        clip_job = [job for job in db.list_jobs(conn, adv["id"])
                    if job["type"] == "turnaround_clip"]
        done_clip = [job for job in clip_job if job["status"] == "done"]
        assert len(done_clip) == 1
        assert done_clip[0]["result"]["rejected"] is True
        assert done_clip[0]["payload"]["source_image_id"] == split["id"]
        # três sementes recusadas: nova rodada adiada, ainda não pegável
        retry = [job for job in clip_job if job["status"] == "pending"]
        assert len(retry) == 1 and retry[0]["payload"]["round"] == 2
        assert retry[0]["payload"]["not_before"] > db.utcnow()
        assert done_clip[0]["result"]["retry_job_id"] == retry[0]["id"]
        assert sheet["kind"] == "reference_sheet"
        # folha 2×2 montada das quatro vistas pintadas, célula 448×768
        assert (sheet["width"], sheet["height"]) == (896, 1536)
        assert [view["kind"] for view in views] == ["reference_view"] * 4
        assert all(view["ref_id"] == card["id"] for view in views)
        assert all(view["source_image_id"] == master["id"] for view in views)
        assert all(view["provider"] == "comfyui" for view in views)
        assert [(view["width"], view["height"]) for view in views] == [
            (672, 1152), (672, 1152), (672, 1152), (768, 1024)]
        with Image.open(likeness.stored_image_path(sheet["file_path"])) as grid:
            cells = [grid.getpixel((224 + 448 * (i % 2), 384 + 768 * (i // 2)))
                     for i in range(4)]
        assert cells == PAINT_COLORS[1:]               # frente, perfil, costas, rosto
        assert "view_problems" not in result or result["view_problems"] == {}
        assert len(terminal) == 1
        assert terminal[0]["card_id"] == card["id"]
        assert terminal[0]["sheet"] == result["sheet"]
        assert terminal[0]["clip"] == result["clip"]
        assert terminal[0]["views"] == result["views"]

    image_calls = capture["image_calls"]
    assert len(image_calls) == 5                      # mestre + 4 vistas, uma a uma
    master_png = image_calls[0]["content"]
    assert image_calls[0]["reference_images"] == [PNG_BYTES]
    for call in image_calls[1:4]:
        assert call["reference_images"] == [master_png]
        assert (call["width"], call["height"]) == (672, 1152)
    assert image_calls[4]["reference_images"] == [master_png, PNG_BYTES]
    assert (image_calls[4]["width"], image_calls[4]["height"]) == (768, 1024)
    assert "seen from directly behind" in image_calls[3]["prompt"]
    assert "Close-up headshot" in image_calls[4]["prompt"]
    # o close-up leva só as peças visíveis da trava: calça e sapato puxavam o
    # quadro para meio corpo
    assert "footwear" not in image_calls[4]["prompt"]
    assert "soles of the shoes" in image_calls[1]["prompt"]
    # recusado pelo portão: até três sementes, nunca mais que isso
    clip_calls = capture["clip_calls"]
    assert len(clip_calls) == 3
    assert clip_calls[0]["video_overrides"]["loop"] is True
    assert all(isinstance(call["seed"], int) for call in clip_calls)
    assert all(call["video_overrides"]["engine"] == "wan" for call in clip_calls)
    assert clip_calls[0]["video_overrides"]["steps"] == 20
    assert clip_calls[0]["video_overrides"]["frames"] == 121
    assert "360 degree" in capture["prompt"]
    assert not list((tmp_path / "imgs").rglob("*.mp4")) or all(
        path.read_bytes().endswith(b"cycle") for path in (tmp_path / "imgs").rglob("*.mp4"))
    with Image.open(io.BytesIO(capture["source_png"])) as frame:
        # o vídeo parte só do mestre (vermelho no mock), sem moldura cinza
        assert frame.size == (448, 768)
        assert frame.getpixel((224, 384)) == (255, 0, 0)
        assert frame.getpixel((2, 2)) == (255, 0, 0)

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


def test_uploaded_photo_leads_the_master_and_every_view(client, tmp_path, monkeypatch):
    """Com foto enviada no card, ela é a primeira referência de toda pintura;
    depois vem o mestre (vistas e close-up). Miniatura sobe para 512 px."""
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

    master_call, *view_calls = capture["image_calls"]
    assert len(view_calls) == 4
    with Image.open(io.BytesIO(master_call["reference_images"][0])) as lead:
        assert lead.format == "PNG"
        assert lead.size == (512, 512)
    photo_png = master_call["reference_images"][0]
    # Com foto, a âncora gerada sai: ela fazia o cabelo do mestre mudar entre
    # staging e produção. Foto sem rosto detectável entra inteira.
    assert master_call["reference_images"] == [photo_png]
    for call in view_calls[:3]:
        assert call["reference_images"] == [photo_png, master_call["content"]]
    assert view_calls[3]["reference_images"] == [photo_png, master_call["content"]]


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
        # retrato, turnaround, giro recusado e a rodada adiada do giro
        assert len(db.list_jobs(conn, adv["id"])) == 4


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
    _mock_generation(monkeypatch, tmp_path, video, capture,
                     clip_report={"clean": True, "loop_end": 12, "problems": []})
    loops = []

    def finalize_loop(source, output, end):
        loops.append(end)
        Path(output).write_bytes(Path(source).read_bytes() + b"looped")
        return 500

    monkeypatch.setattr(turnaround_module, "finalize_loop", finalize_loop)
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
        assert clip["duration_ms"] == 500                 # cortado no ponto de loop
        assert likeness.stored_image_path(clip["file_path"]).read_bytes().endswith(b"looped")
    assert loops == [12]
    assert not list((tmp_path / "imgs").rglob("*.loop.mp4"))
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


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg não instalado")
def test_view_cycle_is_a_browser_mp4_through_four_views(tmp_path):
    colors = [(255, 0, 0), (0, 255, 0), (0, 0, 255)]
    views = [_figure_png((48, 80), box=(16, 10, 32, 70), color=color) for color in colors]
    out = tmp_path / "cycle.mp4"
    duration = turnaround_module.build_view_cycle(views, out, size=(48, 80), fps=10)
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


def test_broken_view_cycle_never_fails_the_turnaround(tmp_db, tmp_path, monkeypatch):
    """Regressão do staging: o ffmpeg levantou ValueError no Python 3.12 e o
    turnaround inteiro virou erro. Sem giro, as vistas publicam mesmo assim."""
    use_tmp_storage(tmp_path)
    adv = make_adventure()
    card, anchor = _character(adv["id"], tmp_path)
    video = tmp_path / "source.mp4"
    _write_video(video)
    _mock_generation(monkeypatch, tmp_path, video)

    def broken(*_args, **_kwargs):
        raise ValueError("flush of closed file")

    monkeypatch.setattr(turnaround_module, "build_view_cycle", broken)
    with db.session() as conn:
        queued = jobs.enqueue_turnaround(conn, adv["id"], card["id"], anchor["id"])
    with db.session() as conn:
        first = db.claim_next_job(conn)
    jobs._run_job(first, pipeline_transport())
    with db.session() as conn:
        done = db.get_job(conn, queued["id"])
        assert done["status"] == "done"
        assert done["result"]["clip"] is None
        assert len(done["result"]["views"]) == 4
    saved = _published_turnaround(adv["id"], card["id"])
    assert saved["clip_image_id"] == ""
    assert saved["design_sheet_image_id"] == done["result"]["design_sheet"]["id"]
    assert list((tmp_path / "imgs").rglob("*.mp4")) == []




def test_a_view_that_fails_the_check_is_painted_again(tmp_db, tmp_path, monkeypatch):
    """Costas com pés cortados: mais uma semente; fica a boa e a ruim some.
    Close-up que falha duas vezes fica a de menos problemas, registrada."""
    use_tmp_storage(tmp_path)
    adv = make_adventure()
    card, anchor = _character(adv["id"], tmp_path)
    video = tmp_path / "source.mp4"
    _write_video(video)
    capture: dict = {}
    seen = []

    def checks(kind, index):
        seen.append((kind, index))
        if kind == "back" and len([k for k, _ in seen if k == "back"]) == 1:
            return ["feet_cut"]
        if kind == "face":
            first = len([k for k, _ in seen if k == "face"]) == 1
            return ["no_face", "too_small"] if first else ["no_face"]
        return []

    _mock_generation(monkeypatch, tmp_path, video, capture, checks=checks)
    with db.session() as conn:
        queued = jobs.enqueue_turnaround(conn, adv["id"], card["id"], anchor["id"])
    with db.session() as conn:
        job = db.claim_next_job(conn)
    jobs._run_job(job, pipeline_transport())
    calls = capture["image_calls"]
    assert len(calls) == 7                         # mestre, frente, perfil, 2 costas, 2 rostos
    with db.session() as conn:
        result = db.get_job(conn, queued["id"])["result"]
        views = [db.get_image(conn, view["id"]) for view in result["views"]]
    back_png = likeness.stored_image_path(views[2]["file_path"]).read_bytes()
    assert back_png == calls[4]["content"]         # a segunda costas
    assert result["view_problems"] == {"face": ["no_face"]}
    # 5 pinturas aceitas + quadro do Wan + ficha + folha; as recusadas sumiram
    assert len(list((tmp_path / "imgs" / adv["id"]).glob("*.png"))) == 8
    # a segunda tentativa troca a semente e repete as referências
    assert isinstance(calls[4]["seed"], int) and calls[4]["seed"] != calls[3]["seed"]
    assert calls[4]["reference_images"] == calls[3]["reference_images"]


def test_face_timeline_accepts_one_full_turn_and_cuts_a_second_spin():
    """F de frente, < > rosto virado, . sem rosto. Ida e volta por lados
    opostos = volta completa; o loop fecha no primeiro retorno."""
    loop = turnaround_module._loop_from_faces
    def run(text):
        timeline = list(text)
        distances = [0.0 if mark == "F" else 0.1 for mark in timeline]
        return loop(timeline, distances, len(timeline))
    full = "FFFFFF>>>>>>" + "." * 20 + "<<<<<<" + "FFFFFF" + ">>>" + "." * 10 + "<<FFFF"
    end, problems = run(full)
    assert problems == [] and full[end] == "F" and end < full.index("FFFFFF", 12) + 6
    assert run("FFFF>>>>" + "." * 20 + ">>>>FFFFFF") == (None, ["half_turn"])
    assert run("FFFF<<<<<<<<FFFF<<<<<FFFF") == (None, ["no_back_view"])
    wobble = "FFFF>>>>" + "." * 12 + "<<<<<<<<<<" + "." * 12 + "<<<FFFFF"
    assert run(wobble) == (None, ["wobble"])
    # caveira do Caio: o rosto some por 8 quadros já no perfil, antes das
    # costas de verdade (54 quadros). Isso não é vai-e-volta.
    mask = ("FFF..FFF>FFF>>>>>FFFF" + "." * 8 + ">>>" + "." * 5 + ">" * 10 + "...>"
            + "." * 54 + "<FFFFFFFFF")
    end, problems = run(mask)
    assert problems == [] and mask[end] == "F"
    # rosto de lado por 5 quadros no meio das costas (staging): ainda uma volta
    blip = "F" * 14 + ">" * 10 + "." * 20 + "<<<<<" + "." * 18 + "<" * 12 + "FFFFFF"
    end, problems = run(blip)
    assert problems == [] and blip[end] == "F"
    end, problems = run("FFFF>>>>" + "." * 20 + "<<<<<<")      # nunca volta à frente
    assert end is None and problems == []
    # produção: de costas em 65% do clipe e de volta à frente num estalo,
    # sem mostrar o outro perfil. Não é giro uniforme.
    snap = "F" * 17 + ">" * 8 + "." * 73 + ">......>>>...." + "F" * 9
    assert run(snap) == (None, ["no_return_side"])
    stalled = "F" * 10 + ">" * 6 + "." * 80 + "<" * 8 + "F" * 10
    assert run(stalled) == (None, ["uneven_turn"])
    even = "F" * 14 + ">" * 13 + "." * 50 + "<" * 24 + "F" * 15
    end, problems = run(even)
    assert problems == [] and even[end] == "F"


def _synthetic_clip(path: Path, *, stray: bool = False, panel: bool = False,
                    smear: bool = False, frames: int = 40) -> None:
    """Figura que afina (de perfil) e volta; opcional mancha solta ou painel."""
    size = (448, 768)
    writer = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"mp4v"), 24, size)
    assert writer.isOpened()
    for index in range(frames):
        frame = np.full((size[1], size[0], 3), 214, np.uint8)
        phase = abs((index % 32) - 16) / 16          # 1 → 0 → 1
        half = int(30 + 50 * phase)
        frame[64:704, 224 - half:224 + half] = (40, 40, 40)
        frame[64:150, 200:248] = (60, 90, 160)       # cabeça
        if stray and 20 <= index < 30:
            frame[300:420, 20:90] = (200, 120, 60)
        if panel and index >= 12:
            frame[:, 400:] = 250
        if smear and index >= 10:
            frame = cv2.GaussianBlur(frame, (41, 41), 0)
        else:
            # textura fina na roupa: o detalhe que o fantasma apaga
            frame[160:700:6, 224 - half + 4:224 + half - 4] = (90, 90, 90)
        writer.write(frame)
    writer.release()


def test_clip_analysis_catches_stray_shapes_and_background_panels(tmp_path):
    good, stray, panel = tmp_path / "good.mp4", tmp_path / "stray.mp4", tmp_path / "panel.mp4"
    smear = tmp_path / "smear.mp4"
    _synthetic_clip(good)
    _synthetic_clip(stray, stray=True)
    _synthetic_clip(panel, panel=True)
    _synthetic_clip(smear, smear=True)
    report = turnaround_module.analyze_clip(good)
    assert report["clean"] is True, report
    assert 24 <= report["loop_end"] <= 36            # volta ao quadro 0 no fim do ciclo
    assert "stray_shapes" in turnaround_module.analyze_clip(stray)["problems"]
    assert "background_changes" in turnaround_module.analyze_clip(panel)["problems"]
    assert "smeared" in turnaround_module.analyze_clip(smear)["problems"]
    assert turnaround_module.analyze_clip(tmp_path / "missing.mp4")["clean"] is False


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg não instalado")
def test_finalize_loop_keeps_frames_up_to_the_loop_point(tmp_path):
    source, output = tmp_path / "clip.mp4", tmp_path / "loop.mp4"
    _synthetic_clip(source, frames=40)
    assert turnaround_module.finalize_loop(source, output, 30) == 1250
    capture = cv2.VideoCapture(str(output))
    try:
        assert int(capture.get(cv2.CAP_PROP_FRAME_COUNT)) == 30
    finally:
        capture.release()
    assert output.read_bytes()[4:8] == b"ftyp"


def test_rejected_turns_retry_later_with_new_seeds_up_to_three_rounds(
        tmp_db, tmp_path, monkeypatch):
    """Rodada 1 recusada → rodada 2 depois de 10 min → rodada 3 depois de 30
    min → fim. Cada rodada usa sementes novas e fixas; a adiada não é pega
    antes da hora e não segura outros jobs."""
    use_tmp_storage(tmp_path)
    adv = make_adventure()
    card, anchor = _character(adv["id"], tmp_path)
    video = tmp_path / "source.mp4"
    _write_video(video)
    capture: dict = {}
    _mock_generation(monkeypatch, tmp_path, video, capture)
    with db.session() as conn:
        jobs.enqueue_turnaround(conn, adv["id"], card["id"], anchor["id"])
    assert jobs.process_pending(pipeline_transport()) == 2
    seeds = [call["seed"] for call in capture["clip_calls"]]
    assert len(set(seeds)) == 3
    for expected_round, delay in ((2, 600), (3, 1800)):
        with db.session() as conn:
            pending = [job for job in db.list_jobs(conn, adv["id"])
                       if job["type"] == "turnaround_clip" and job["status"] == "pending"]
            assert len(pending) == 1 and pending[0]["payload"]["round"] == expected_round
            wait = (datetime.fromisoformat(pending[0]["payload"]["not_before"].replace("Z", "+00:00"))
                    - datetime.now(timezone.utc)).total_seconds()
            assert delay - 30 < wait <= delay
            # outro job passa na frente; o adiado não é pego antes da hora
            jobs.enqueue(conn, "location_image", adv["id"], "loc", {"prompt": "x"})
            claimed = db.claim_next_job(conn)
            assert claimed["type"] == "location_image"
            db.finish_job(conn, claimed["id"], "done")
            assert db.claim_next_job(conn) is None
            conn.execute("UPDATE jobs SET payload=json_set(payload, '$.not_before', ?)"
                         " WHERE id=?", ("2000-01-01T00:00:00.000Z", pending[0]["id"]))
        assert jobs.process_pending(pipeline_transport()) == 1
    new_seeds = [call["seed"] for call in capture["clip_calls"]][3:]
    assert len(new_seeds) == 6 and not set(new_seeds) & set(seeds)
    with db.session() as conn:
        last = [job for job in db.list_jobs(conn, adv["id"]) if job["type"] == "turnaround_clip"]
        assert all(job["status"] == "done" for job in last)          # sem rodada 4
        assert max(job["payload"].get("round", 1) for job in last) == 3


def test_turnaround_paints_use_fixed_seeds_per_card_and_view(tmp_db, tmp_path, monkeypatch):
    """Refazer o turnaround repete a semente de cada vista: mesmo cabelo e
    mesmo tom de jeans de uma rodada para outra."""
    use_tmp_storage(tmp_path)
    adv = make_adventure()
    card, anchor = _character(adv["id"], tmp_path)
    video = tmp_path / "source.mp4"
    _write_video(video)
    capture: dict = {}
    _mock_generation(monkeypatch, tmp_path, video, capture)
    for _ in range(2):
        with db.session() as conn:
            jobs.enqueue_turnaround(conn, adv["id"], card["id"], anchor["id"], force=True)
        with db.session() as conn:
            job = db.claim_next_job(conn)
        jobs._run_job(job, pipeline_transport())
        with db.session() as conn:
            conn.execute("UPDATE jobs SET status='canceled' WHERE type='turnaround_clip'")
    seeds = [call["seed"] for call in capture["image_calls"]]
    assert len(seeds) == 10 and seeds[:5] == seeds[5:]
    assert len(set(seeds[:5])) == 5
    assert seeds[0] == jobs.stable_seed(card["id"], "master", 0)


def test_masked_face_starting_turned_still_gets_the_turn_check(tmp_path, monkeypatch):
    """A caveira do Caio aparece como rosto virado (>) já no quadro 0. Antes
    isso caía no caminho sem rosto, que aceitou um giro que volta de estalo
    pelo mesmo lado; agora a linha do tempo do rosto vale e pega a meia volta."""
    clip = tmp_path / "caio.mp4"
    _synthetic_clip(clip, frames=40)
    snap = list(">>>FFFF<<<<<" + "." * 16 + "><<<" + "F" * 8)
    monkeypatch.setattr(turnaround_module, "_faces_timeline", lambda frames: snap[:len(frames)])
    report = turnaround_module.analyze_clip(clip)
    assert "half_turn" in report["problems"]
