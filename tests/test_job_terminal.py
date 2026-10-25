"""Reinício e falha de job deixam um evento terminal durável."""

from __future__ import annotations

from app import db, jobs
from conftest import make_adventure


def _types(adventure_id: str, event_type: str) -> int:
    with db.session() as conn:
        return conn.execute(
            "SELECT COUNT(*) FROM world_events WHERE adventure_id=? AND type=?",
            (adventure_id, event_type),
        ).fetchone()[0]


def test_restart_records_one_terminal_event_for_an_interrupted_see(tmp_db):
    adventure = make_adventure()
    with db.session() as conn:
        portrait = jobs.enqueue(conn, "character_portrait", adventure["id"], "c1")
        assert db.claim_next_job(conn)["id"] == portrait["id"]
        see = db.insert_job(
            conn, adventure_id=adventure["id"], type="see_image",
            priority=jobs.PRIORITY_SEE, status="preparing",
        )
    assert jobs.recover_interrupted() == (1, 1)
    assert _types(adventure["id"], "see_failed") == 1
    jobs.recover_interrupted()
    assert _types(adventure["id"], "see_failed") == 1
    with db.session() as conn:
        row = conn.execute(
            "SELECT payload_json FROM world_events WHERE adventure_id=? AND type='see_failed'",
            (adventure["id"],),
        ).fetchone()
    assert "interrupted" in row["payload_json"]
    assert see["id"]
