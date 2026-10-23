"""Variantes de retrato: cache-hit troca na hora, miss enfileira img2img."""

from __future__ import annotations

import json

from app import db, npc_assets, portrait_sync


def _card(conn, adv, *, sheet=None, prompt="bust portrait of Elara"):
    return db.insert_card(
        conn, adv["id"], title="Elara", type="character", entry="A guard.",
        portrait_prompt=prompt,
        sheet=json.dumps(sheet or {"appearance": "dark hair", "characteristics": "x"}),
    )


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
        key = npc_assets.portrait_state_key(card)
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
        key = npc_assets.portrait_state_key(card)
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


def test_state_key_separates_anchors_for_the_same_outfit():
    card = {"portrait_prompt": "bust portrait of Elara",
            "sheet": json.dumps({"appearance": "dark hair"}),
            "portrait_anchor_id": "face-a"}
    first = npc_assets.portrait_state_key(card)
    card["portrait_anchor_id"] = "face-b"
    assert npc_assets.portrait_state_key(card) != first
