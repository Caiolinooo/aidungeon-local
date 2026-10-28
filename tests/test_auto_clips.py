"""Clipe automático depois do PNG: flags, fonte, prompt, dedupe e falha isolada."""

from __future__ import annotations

import json
from pathlib import Path

from app import db, game_engine, image_client, jobs, likeness
from conftest import make_adventure, make_llm_transport
from test_scene_clip import _cfg, _png
from test_v3 import use_tmp_storage

SCENE_PROMPT = "torchlit hall, closed oak door"


def _patch_flags(monkeypatch, *, scene: bool, portraits: bool) -> None:
    real = image_client.video_settings

    def wrapped(cfg):
        merged = real(cfg)
        merged["auto_scene"] = scene
        merged["auto_portraits"] = portraits
        return merged

    monkeypatch.setattr(image_client, "video_settings", wrapped)


def _fake_generate(calls: list[str]):
    def generate(cfg, prompt, *, adventure_id, image_id, transport=None, **kwargs):
        calls.append(str(prompt or ""))
        raw = _png(4, 4)
        directory = Path(cfg["storage_dir"])
        if not directory.is_absolute():
            directory = db.REPO_ROOT / directory
        directory = directory / adventure_id
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{image_id}.png"
        path.write_bytes(raw)
        try:
            file_path = path.relative_to(db.REPO_ROOT).as_posix()
        except ValueError:
            file_path = str(path)
        return {
            "file_path": file_path,
            "width": 4,
            "height": 4,
            "provider": cfg.get("provider") or "sdwebui",
        }

    return generate


def _drain_origins() -> list[str]:
    ran: list[str] = []
    while True:
        with db.session() as conn:
            row = conn.execute(
                "SELECT id, type FROM jobs WHERE status='pending'"
                " AND type<>'scene_clip'"
                " ORDER BY priority ASC, created_at ASC, rowid ASC LIMIT 1"
            ).fetchone()
        if row is None:
            break
        with db.session() as conn:
            claimed = conn.execute(
                "UPDATE jobs SET status='running', started_at=? WHERE id=?"
                " AND status='pending'", (db.utcnow(), row["id"]))
            assert claimed.rowcount == 1
            job = db.get_job(conn, row["id"])
        jobs._run_job(job, None)
        ran.append(job["type"])
    return ran


def _jobs_of(type_: str) -> list[dict]:
    with db.session() as conn:
        ids = [row["id"] for row in conn.execute(
            "SELECT id FROM jobs WHERE type=? ORDER BY priority ASC, created_at ASC, rowid ASC",
            (type_,),
        )]
        return [db.get_job(conn, job_id) for job_id in ids]


def _card(adventure_id: str, title: str, *, prompt: str, sheet: dict,
          player_username: str = "") -> dict:
    with db.session() as conn:
        return db.insert_card(
            conn, adventure_id, title=title, type="character",
            portrait_prompt=prompt, entry=title,
            player_username=player_username,
            sheet=json.dumps(sheet, ensure_ascii=False),
        )


def _enqueue_scene(adventure_id: str, prompt: str = SCENE_PROMPT) -> dict:
    with db.session() as conn:
        return jobs.enqueue(
            conn, "scene_backdrop", adventure_id, "room",
            {"prompt": prompt}, priority=jobs.PRIORITY_BACKDROP,
        )


def test_video_flags_default_false(tmp_db):
    assert image_client._VIDEO_DEFAULTS["auto_scene"] is False
    assert image_client._VIDEO_DEFAULTS["auto_portraits"] is False
    blank = image_client.video_settings({"comfyui_video": {}})
    assert blank["auto_scene"] is False
    assert blank["auto_portraits"] is False


def test_clip_cache_ignores_scheduling_flags(tmp_path):
    raw = _png(4, 4)
    cfg = _cfg(tmp_path)
    base = image_client.clip_request_key(cfg, "walks home", raw)
    flagged = _cfg(tmp_path)
    flagged["comfyui_video"] = {
        **flagged["comfyui_video"],
        "auto_scene": True,
        "auto_portraits": False,
        "note": "fora da chave",
    }
    assert image_client.clip_request_key(flagged, "walks home", raw) == base
    refined = _cfg(tmp_path)
    refined["comfyui_video"] = {**refined["comfyui_video"], "steps": 21}
    assert image_client.clip_request_key(refined, "walks home", raw) != base


def test_manual_clip_uses_scene_priority_and_scene_precedes_portrait(tmp_db):
    adv = make_adventure()
    with db.session() as conn:
        manual = jobs.enqueue_scene_clip(conn, adv["id"], "src-manual", "nods")
        scene = jobs.enqueue_scene_clip(
            conn, adv["id"], "src-scene", "door opens", kind="scene")
        portrait = jobs.enqueue_scene_clip(
            conn, adv["id"], "src-portrait", "blinks", kind="portrait")
        explicit = jobs.enqueue_scene_clip(
            conn, adv["id"], "src-explicit", "waves",
            priority=jobs.PRIORITY_SCENE_CLIP, kind="portrait")
    assert manual["priority"] == jobs.PRIORITY_SCENE_CLIP
    assert "kind" not in (manual.get("payload") or {})
    assert set((manual.get("payload") or {})) == {"prompt", "source_image_id", "cache_key"}
    assert scene["priority"] == jobs.PRIORITY_SCENE_CLIP
    assert scene["payload"]["kind"] == "scene"
    assert portrait["priority"] == jobs.PRIORITY_PORTRAIT_CLIP
    assert portrait["priority"] > scene["priority"]
    assert explicit["priority"] == jobs.PRIORITY_SCENE_CLIP
    with db.session() as conn:
        order = [row["id"] for row in conn.execute(
            "SELECT id FROM jobs WHERE type='scene_clip' AND status='pending' "
            "ORDER BY priority ASC, created_at ASC, rowid ASC"
        )]
    assert order.index(portrait["id"]) > order.index(scene["id"])
    assert order.index(portrait["id"]) > order.index(manual["id"])


def test_flags_off_do_not_schedule_clips(tmp_db, tmp_path, monkeypatch):
    use_tmp_storage(tmp_path)
    monkeypatch.setattr(image_client, "generate_image", _fake_generate([]))
    adv = make_adventure()
    _card(adv["id"], "Mira", prompt="scarred jaw, bust portrait", sheet={
        "current_outfit": "blue cloak",
        "visual_state": {"action": "raises the lantern", "emotion": "wary",
                         "pose": "half-turned", "seq": 42},
        "alignment": "desconfiado",
    })
    with db.session() as conn:
        card = db.list_cards(conn, adv["id"])[0]
        jobs.enqueue_portrait(conn, adv["id"], card["id"])
    _enqueue_scene(adv["id"])
    ran = _drain_origins()
    assert ran == ["character_portrait", "scene_backdrop"]
    assert _jobs_of("scene_clip") == []
    assert _jobs_of("character_portrait")[0]["status"] == "done"
    assert _jobs_of("scene_backdrop")[0]["status"] == "done"


def test_flags_on_schedule_scene_then_portrait_from_the_png(tmp_db, tmp_path, monkeypatch):
    use_tmp_storage(tmp_path)
    _patch_flags(monkeypatch, scene=True, portraits=True)
    monkeypatch.setattr(image_client, "generate_image", _fake_generate([]))
    adv = make_adventure()
    bram = _card(adv["id"], "Bram", prompt="broad shoulders, bust portrait", sheet={})
    mira = _card(adv["id"], "Mira", prompt="scarred jaw, dark hair, bust portrait", sheet={
        "current_outfit": "blue cloak",
        "visual_state": {
            "action": "raises the lantern",
            "emotion": "wary",
            "pose": "half-turned",
            "seq": 42,
        },
        "alignment": "desconfiado",
        "bonds": {
            "trust": 12, "distrust": 80, "relationship": 10,
            "fear": 70, "desire": 3, "boldness": 90,
        },
        "relations": {
            bram["id"]: {
                "trust": 11,
                "fear": 88,
                "affinity": 5,
                "history": ['seq 7 | {"trust": -4} | insult'],
            },
        },
    })
    with db.session() as conn:
        jobs.enqueue_portrait(conn, adv["id"], mira["id"])
    _enqueue_scene(adv["id"])
    ran = _drain_origins()
    assert ran == ["character_portrait", "scene_backdrop"]
    backdrop = _jobs_of("scene_backdrop")[0]
    portrait = _jobs_of("character_portrait")[0]
    assert backdrop["status"] == "done"
    assert portrait["status"] == "done"
    clips = _jobs_of("scene_clip")
    assert [clip["payload"]["kind"] for clip in clips] == ["scene", "portrait"]
    assert [clip["priority"] for clip in clips] == [
        jobs.PRIORITY_SCENE_CLIP, jobs.PRIORITY_PORTRAIT_CLIP,
    ]
    scene_clip, portrait_clip = clips
    assert scene_clip["status"] == "pending"
    assert portrait_clip["status"] == "pending"
    assert scene_clip["payload"]["source_image_id"] == backdrop["result"]["image"]["id"]
    assert portrait_clip["payload"]["source_image_id"] == portrait["result"]["image"]["id"]
    assert scene_clip["ref_id"] == backdrop["result"]["image"]["id"]
    scene_prompt = scene_clip["payload"]["prompt"]
    assert SCENE_PROMPT in scene_prompt
    assert "stable camera" in scene_prompt
    assert "identity" in scene_prompt
    assert "Coherent continuous motion" in scene_prompt
    portrait_prompt = portrait_clip["payload"]["prompt"]
    assert "Portrait of Mira." in portrait_prompt
    assert "blue cloak" in portrait_prompt
    assert "scarred jaw, dark hair, bust portrait" in portrait_prompt
    assert "action: raises the lantern" in portrait_prompt
    assert "emotion: wary" in portrait_prompt
    assert "pose: half-turned" in portrait_prompt
    assert "alignment stage: desconfiado" in portrait_prompt
    assert "toward Bram:" in portrait_prompt
    assert "Preserve the same face, clothes and framing" in portrait_prompt
    assert "seq" not in portrait_prompt.lower()
    for raw_number in ("42", "88", "90", "12", "80"):
        assert raw_number not in portrait_prompt
    with db.session() as conn:
        source = db.get_image(conn, portrait["result"]["image"]["id"])
        raw = likeness.read_stored_image_bytes(source)
        expected = image_client.clip_request_key(
            image_client.effective_settings(conn), portrait_prompt, raw)
    assert portrait_clip["payload"]["cache_key"] == expected


def test_initial_portrait_without_turn_stays_still(tmp_db, tmp_path, monkeypatch):
    use_tmp_storage(tmp_path)
    _patch_flags(monkeypatch, scene=False, portraits=True)
    monkeypatch.setattr(image_client, "generate_image", _fake_generate([]))
    adv = make_adventure()
    card = _card(adv["id"], "Nia", prompt="plain face, bust portrait", sheet={
        "current_outfit": "grey coat",
    })
    with db.session() as conn:
        jobs.enqueue_portrait(conn, adv["id"], card["id"])
    _enqueue_scene(adv["id"])
    _drain_origins()
    assert _jobs_of("scene_clip") == []
    assert _jobs_of("character_portrait")[0]["status"] == "done"
    assert _jobs_of("scene_backdrop")[0]["status"] == "done"


def test_follow_up_failure_leaves_the_origin_done(tmp_db, tmp_path, monkeypatch):
    use_tmp_storage(tmp_path)
    _patch_flags(monkeypatch, scene=True, portraits=False)
    monkeypatch.setattr(image_client, "generate_image", _fake_generate([]))

    def explode(*args, **kwargs):
        raise RuntimeError("follow-up exploded")

    monkeypatch.setattr(jobs, "enqueue_scene_clip", explode)
    adv = make_adventure()
    origin = _enqueue_scene(adv["id"])
    ran = _drain_origins()
    assert ran == ["scene_backdrop"]
    done = _jobs_of("scene_backdrop")[0]
    assert done["id"] == origin["id"]
    assert done["status"] == "done"
    assert not done.get("error")
    assert done["result"]["image"]["id"]
    assert _jobs_of("scene_clip") == []


def test_follow_up_skips_when_finish_returns_false(tmp_db, tmp_path, monkeypatch):
    use_tmp_storage(tmp_path)
    _patch_flags(monkeypatch, scene=True, portraits=True)
    called: list[str] = []
    real = jobs._schedule_clip_followup

    def spy(job, result):
        called.append(job["id"])
        return real(job, result)

    monkeypatch.setattr(jobs, "_schedule_clip_followup", spy)

    def cancel_then_return(job, transport):
        with db.session() as conn:
            db.cancel_job(conn, job["id"])
        return {"image": {"id": "never-stored", "url": "/api/images/never-stored"}}

    monkeypatch.setitem(jobs._EXECUTORS, "scene_backdrop", cancel_then_return)
    adv = make_adventure()
    origin = _enqueue_scene(adv["id"])
    with db.session() as conn:
        job = db.claim_next_job(conn)
    jobs._run_job(job, None)
    assert called == []
    done = _jobs_of("scene_backdrop")[0]
    assert done["id"] == origin["id"]
    assert done["status"] == "canceled"
    assert done["status"] != "error"
    assert _jobs_of("scene_clip") == []


def test_bulk_portrait_stays_still_and_scene_still_moves(tmp_db, tmp_path, monkeypatch):
    use_tmp_storage(tmp_path)
    _patch_flags(monkeypatch, scene=True, portraits=True)
    monkeypatch.setattr(image_client, "generate_image", _fake_generate([]))
    adv = make_adventure()
    npc = _card(adv["id"], "Ivo", prompt="lined face, bust portrait", sheet={
        "current_outfit": "brown coat",
        "visual_state": {"action": "stacks crates", "emotion": "tired",
                         "pose": "bent over"},
    })
    player = _card(adv["id"], "Ada", prompt="short hair, bust portrait", sheet={
        "current_outfit": "red scarf",
    }, player_username="ada")
    with db.session() as conn:
        jobs.enqueue_portrait(
            conn, adv["id"], npc["id"], priority=jobs.PRIORITY_BULK_PORTRAIT)
        jobs.enqueue_portrait(
            conn, adv["id"], player["id"], priority=jobs.PRIORITY_BULK_PORTRAIT)
    _enqueue_scene(adv["id"])
    ran = _drain_origins()
    assert ran == ["scene_backdrop", "character_portrait", "character_portrait"]
    portraits = {job["ref_id"]: job for job in _jobs_of("character_portrait")}
    assert portraits[npc["id"]]["status"] == "done"
    assert portraits[npc["id"]]["priority"] == jobs.PRIORITY_BULK_PORTRAIT
    assert portraits[player["id"]]["status"] == "done"
    assert portraits[player["id"]]["priority"] == jobs.PRIORITY_BULK_PORTRAIT
    clips = _jobs_of("scene_clip")
    assert [clip["payload"]["kind"] for clip in clips] == ["scene"]
    assert clips[0]["payload"]["source_image_id"] == (
        _jobs_of("scene_backdrop")[0]["result"]["image"]["id"])
    assert portraits[npc["id"]]["result"]["image"]["id"] not in {
        clip["payload"]["source_image_id"] for clip in clips
    }
    assert portraits[player["id"]]["result"]["image"]["id"] not in {
        clip["payload"]["source_image_id"] for clip in clips
    }


def test_auto_does_not_supersede_an_active_manual_clip(tmp_db):
    adv = make_adventure()
    with db.session() as conn:
        manual = jobs.enqueue_scene_clip(
            conn, adv["id"], "src-user", "user asked for rain", cache_key="user-key")
        kept = jobs.enqueue_scene_clip(
            conn, adv["id"], "src-user", "auto sways the curtains",
            cache_key="auto-key", kind="scene", supersede=False)
        assert kept["id"] == manual["id"]
        stored = db.get_job(conn, manual["id"])
        assert stored["status"] == "pending"
        assert stored["payload"]["prompt"] == "user asked for rain"
        assert stored["payload"]["cache_key"] == "user-key"
        assert "kind" not in stored["payload"]
        assert conn.execute(
            "SELECT COUNT(*) FROM jobs WHERE type='scene_clip' AND ref_id=?",
            ("src-user",),
        ).fetchone()[0] == 1
        replacement = jobs.enqueue_scene_clip(
            conn, adv["id"], "src-user", "user changed their mind",
            cache_key="user-key-2")
        assert replacement["id"] != manual["id"]
        assert replacement["status"] == "pending"
        assert replacement["payload"]["prompt"] == "user changed their mind"
        canceled = db.get_job(conn, manual["id"])
    assert canceled["status"] == "canceled"
    assert canceled["error"] == "superseded"


def test_auto_followup_keeps_the_manual_clip_on_the_same_png(tmp_db, tmp_path, monkeypatch):
    use_tmp_storage(tmp_path)
    flags = {"scene": False}
    real = image_client.video_settings

    def wrapped(cfg):
        merged = real(cfg)
        merged["auto_scene"] = flags["scene"]
        merged["auto_portraits"] = False
        return merged

    monkeypatch.setattr(image_client, "video_settings", wrapped)
    monkeypatch.setattr(image_client, "generate_image", _fake_generate([]))
    adv = make_adventure()
    _enqueue_scene(adv["id"])
    assert _drain_origins() == ["scene_backdrop"]
    assert _jobs_of("scene_clip") == []
    source_id = _jobs_of("scene_backdrop")[0]["result"]["image"]["id"]
    with db.session() as conn:
        manual = jobs.enqueue_scene_clip(
            conn, adv["id"], source_id, "user asked for rain", cache_key="user-rain")
    flags["scene"] = True
    _enqueue_scene(adv["id"])
    assert _drain_origins() == ["scene_backdrop"]
    clips = _jobs_of("scene_clip")
    assert len(clips) == 1
    assert clips[0]["id"] == manual["id"]
    assert clips[0]["status"] == "pending"
    assert clips[0]["payload"]["prompt"] == "user asked for rain"
    assert clips[0]["payload"]["cache_key"] == "user-rain"
    assert not clips[0].get("error")
    reused = _jobs_of("scene_backdrop")[1]
    assert reused["status"] == "done"
    assert reused["result"].get("reused") is True


def test_second_backdrop_reuses_the_active_clip(tmp_db, tmp_path, monkeypatch):
    use_tmp_storage(tmp_path)
    _patch_flags(monkeypatch, scene=True, portraits=False)
    calls: list[str] = []
    monkeypatch.setattr(image_client, "generate_image", _fake_generate(calls))
    adv = make_adventure()
    _enqueue_scene(adv["id"])
    assert _drain_origins() == ["scene_backdrop"]
    first = _jobs_of("scene_clip")
    assert len(first) == 1
    assert len(calls) == 1
    _enqueue_scene(adv["id"])
    assert _drain_origins() == ["scene_backdrop"]
    again = _jobs_of("scene_clip")
    assert len(again) == 1
    assert again[0]["id"] == first[0]["id"]
    assert again[0]["status"] == "pending"
    assert again[0]["payload"]["cache_key"] == first[0]["payload"]["cache_key"]
    assert len(calls) == 1
    assert _jobs_of("scene_backdrop")[1]["result"].get("reused") is True


def test_new_action_preempts_only_automatic_clips(tmp_db, monkeypatch):
    adv = make_adventure()
    with db.session() as conn:
        running = jobs.enqueue_scene_clip(
            conn, adv["id"], "auto-running", "rain moves", automatic=True)
        pending = jobs.enqueue_scene_clip(
            conn, adv["id"], "auto-pending", "portrait breathes", automatic=True,
            kind="portrait")
        manual = jobs.enqueue_scene_clip(
            conn, adv["id"], "manual", "keep my requested clip")
        claimed = db.claim_next_job(conn)
        assert claimed["id"] == running["id"]
    interrupted = []
    monkeypatch.setattr(jobs, "interrupt_image", lambda job, transport=None: (
        interrupted.append(job["id"]) or True))

    output = list(game_engine.handle_action(
        adv["id"], "do", "look around",
        transport=make_llm_transport(tokens=("The room answers.",))))

    assert output[-1]["event"] == "done"
    assert interrupted == [running["id"]]
    with db.session() as conn:
        for job in (running, pending):
            stored = db.get_job(conn, job["id"])
            assert stored["status"] == "canceled"
            assert stored["error"] == "preempted for a new action"
        assert db.get_job(conn, manual["id"])["status"] == "pending"
