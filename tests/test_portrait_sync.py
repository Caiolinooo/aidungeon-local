"""Variantes de retrato: cache-hit troca na hora, miss enfileira img2img."""

from __future__ import annotations

import json

from app import db, npc_assets, portrait_sync, settings_store

_CFG = {"provider": "comfyui", "comfyui_unet": "fineview-a.safetensors"}


def _card(conn, adv, *, sheet=None, prompt="bust portrait of Elara"):
    return db.insert_card(
        conn, adv["id"], title="Elara", type="character", entry="A guard.",
        portrait_prompt=prompt,
        sheet=json.dumps(sheet or {"appearance": "dark hair", "characteristics": "x"}),
    )


def _key(conn, adv, card, *, reference_bytes=b""):
    return npc_assets.portrait_state_key(
        card,
        settings_store.effective_image_settings(conn),
        image_style=str(adv.get("image_style") or ""),
        reference_bytes=reference_bytes,
    )


def _bare(**extra):
    card = {
        "portrait_prompt": "bust portrait of Elara",
        "sheet": json.dumps({"appearance": "dark hair", "current_outfit": "red cloak"}),
        "portrait_anchor_id": "face-a",
    }
    card.update(extra)
    return card


def test_sync_swaps_a_cached_variant_without_enqueuing(tmp_db):
    with db.session() as conn:
        adv = db.insert_adventure(conn, title="Portraits")
        card = _card(conn, adv)
        sheet = npc_assets.parse_sheet(card["sheet"])
        sheet["current_outfit"] = "red cloak"
        sheet["expression"] = "angry"
        db.update_card(conn, adv["id"], card["id"], {
            "sheet": json.dumps(sheet, ensure_ascii=False),
        })
        card = db.get_card(conn, adv["id"], card["id"])
        key = _key(conn, adv, card)
        cached = db.insert_image(
            conn, adventure_id=adv["id"], action_seq=0, file_path="outputs/elara.png",
            kind="portrait", ref_id=card["id"], cache_key=key,
        )
        updated = portrait_sync.sync_card(conn, adv["id"], card["id"])
        assert updated["portrait_image_id"] == cached["id"]
        assert npc_assets.parse_sheet(updated["sheet"])["portrait_state_key"] == key
        assert db.list_jobs(conn, adv["id"]) == []


def test_sync_enqueues_a_variant_with_the_state_key_on_a_miss(tmp_db):
    with db.session() as conn:
        adv = db.insert_adventure(conn, title="Portraits")
        card = _card(conn, adv)
        sheet = npc_assets.parse_sheet(card["sheet"])
        sheet["current_outfit"] = "red cloak"
        db.update_card(conn, adv["id"], card["id"], {
            "sheet": json.dumps(sheet, ensure_ascii=False),
        })
        card = db.get_card(conn, adv["id"], card["id"])
        key = _key(conn, adv, card)
        updated = portrait_sync.sync_card(conn, adv["id"], card["id"])
        jobs = db.list_jobs(conn, adv["id"])
        saved = npc_assets.parse_sheet(updated["sheet"])
        variant = npc_assets.portrait_variant_prompt(
            db.get_card(conn, adv["id"], card["id"]))
    assert len(jobs) == 1
    assert jobs[0]["type"] == "character_portrait"
    assert jobs[0]["payload"]["state_key"] == key
    assert "red cloak" in variant
    assert saved["portrait_state_key"] == key
    assert saved["portrait_job_id"] == jobs[0]["id"]


def test_sync_does_not_enqueue_again_for_the_same_pending_state(tmp_db):
    with db.session() as conn:
        adv = db.insert_adventure(conn, title="Portraits")
        card = _card(conn, adv)
        portrait_sync.sync_card(conn, adv["id"], card["id"])
        portrait_sync.sync_card(conn, adv["id"], card["id"])
        jobs = db.list_jobs(conn, adv["id"])
    assert len(jobs) == 1


def test_sync_does_not_duplicate_the_same_appearance_while_running(tmp_db):
    with db.session() as conn:
        adv = db.insert_adventure(conn, title="Portraits")
        card = _card(conn, adv)
        portrait_sync.sync_card(conn, adv["id"], card["id"])
        job = db.list_jobs(conn, adv["id"])[0]
        conn.execute("UPDATE jobs SET status='running' WHERE id=?", (job["id"],))
        portrait_sync.sync_card(conn, adv["id"], card["id"])
        jobs = db.list_jobs(conn, adv["id"])
    assert len(jobs) == 1
    assert jobs[0]["id"] == job["id"]
    assert jobs[0]["status"] == "running"


def test_sync_enqueues_a_substitute_while_the_portrait_is_running(tmp_db):
    with db.session() as conn:
        adv = db.insert_adventure(conn, title="Portraits")
        card = _card(conn, adv)
        portrait_sync.sync_card(conn, adv["id"], card["id"])
        running = db.list_jobs(conn, adv["id"])[0]
        conn.execute("UPDATE jobs SET status='running' WHERE id=?", (running["id"],))
        running_key = running["payload"]["state_key"]
        stored = db.get_card(conn, adv["id"], card["id"])
        sheet = npc_assets.parse_sheet(stored["sheet"])
        sheet["current_outfit"] = "blue cloak"
        db.update_card(conn, adv["id"], card["id"], {
            "sheet": json.dumps(sheet, ensure_ascii=False),
        })
        updated = portrait_sync.sync_card(conn, adv["id"], card["id"])
        jobs = db.list_jobs(conn, adv["id"])
        saved = npc_assets.parse_sheet(updated["sheet"])
    still_running = next(job for job in jobs if job["id"] == running["id"])
    pending = [job for job in jobs if job["status"] == "pending"]
    assert still_running["status"] == "running"
    assert still_running["payload"]["state_key"] == running_key
    assert len(pending) == 1
    assert pending[0]["payload"]["state_key"] != running_key
    assert saved["portrait_state_key"] == pending[0]["payload"]["state_key"]
    assert saved["portrait_job_id"] == pending[0]["id"]
    assert "blue cloak" in npc_assets.portrait_variant_prompt(updated)


def test_sync_replaces_a_pending_job_when_the_style_changes(tmp_db):
    with db.session() as conn:
        adv = db.insert_adventure(conn, title="Portraits", image_style="ink sketch")
        card = _card(conn, adv)
        first = portrait_sync.sync_card(conn, adv["id"], card["id"])
        first_key = npc_assets.parse_sheet(first["sheet"])["portrait_state_key"]
        db.update_adventure(conn, adv["id"], image_style="oil paint")
        second = portrait_sync.sync_card(conn, adv["id"], card["id"])
        second_key = npc_assets.parse_sheet(second["sheet"])["portrait_state_key"]
        live = [job for job in db.list_jobs(conn, adv["id"]) if job["status"] != "canceled"]
    assert first_key != second_key
    assert len(live) == 1
    assert live[0]["status"] == "pending"
    assert live[0]["payload"]["state_key"] == second_key
    assert npc_assets.parse_sheet(second["sheet"])["portrait_job_id"] == live[0]["id"]


def test_sync_state_key_changes_with_the_anchor_file(tmp_db, tmp_path):
    face = tmp_path / "face.png"
    face.write_bytes(b"anchor-a")
    with db.session() as conn:
        adv = db.insert_adventure(conn, title="Portraits")
        card = _card(conn, adv)
        image = db.insert_image(
            conn, adventure_id=adv["id"], action_seq=-1, file_path=str(face),
            kind="portrait", ref_id=card["id"],
        )
        db.set_card_portrait(conn, card["id"], image["id"], anchor_id=image["id"])
        first = portrait_sync.sync_card(conn, adv["id"], card["id"])
        key_a = npc_assets.parse_sheet(first["sheet"])["portrait_state_key"]
        face.write_bytes(b"anchor-b")
        second = portrait_sync.sync_card(conn, adv["id"], card["id"])
        key_b = npc_assets.parse_sheet(second["sheet"])["portrait_state_key"]
        live = [job for job in db.list_jobs(conn, adv["id"]) if job["status"] != "canceled"]
    assert key_a != key_b
    assert len(live) == 1
    assert live[0]["payload"]["state_key"] == key_b


def test_sync_returns_a_real_minor_without_a_job(tmp_db):
    with db.session() as conn:
        adv = db.insert_adventure(conn, title="Portraits")
        card = _card(conn, adv, sheet={
            "appearance": "dark hair", "real_person": True, "minor": True,
        })
        updated = portrait_sync.sync_card(conn, adv["id"], card["id"])
        saved = npc_assets.parse_sheet(updated["sheet"])
        assert updated["id"] == card["id"]
        assert db.list_jobs(conn, adv["id"]) == []
        assert "portrait_state_key" not in saved
        assert "portrait_job_id" not in saved


def test_state_key_separates_anchors_models_references_and_style():
    card = _bare()
    base = npc_assets.portrait_state_key(
        card, _CFG, image_style="ink", reference_bytes=b"photo-a")
    other_anchor = npc_assets.portrait_state_key(
        {**card, "portrait_anchor_id": "face-b"}, _CFG,
        image_style="ink", reference_bytes=b"photo-a")
    other_model = npc_assets.portrait_state_key(
        card, {**_CFG, "comfyui_unet": "fineview-b.safetensors"},
        image_style="ink", reference_bytes=b"photo-a")
    other_clip = npc_assets.portrait_state_key(
        card, {**_CFG, "comfyui_clip": "qwen-other.safetensors"},
        image_style="ink", reference_bytes=b"photo-a")
    other_ref = npc_assets.portrait_state_key(
        card, _CFG, image_style="ink", reference_bytes=b"photo-b")
    other_style = npc_assets.portrait_state_key(
        card, _CFG, image_style="oil paint", reference_bytes=b"photo-a")
    same_image_id = npc_assets.portrait_state_key(
        {**card, "portrait_image_id": "not-the-anchor"}, _CFG,
        image_style="ink", reference_bytes=b"photo-a")
    assert len({base, other_anchor, other_model, other_clip, other_ref, other_style}) == 6
    assert same_image_id == base


def test_state_key_changes_when_sheet_appearance_changes():
    card = _bare()
    first = npc_assets.portrait_state_key(card, _CFG)
    sheet = json.loads(card["sheet"])
    sheet["appearance"] = "white hair"
    changed = npc_assets.portrait_state_key(
        {**card, "sheet": json.dumps(sheet)}, _CFG)
    assert changed != first
