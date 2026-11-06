"""Undo cancels media work tied to the removed canon."""

from __future__ import annotations

import pytest

from app import db, game_engine, jobs
from conftest import add_action, make_adventure


@pytest.mark.parametrize("type_", ["see_image", "scene_backdrop", "scene_clip"])
def test_undo_cancels_pending_media_jobs(tmp_db, type_):
    adventure = make_adventure(title="Revision guard")
    add_action(adventure["id"], 0, "start", "A scene begins.")
    add_action(adventure["id"], 1, "do", "The player changes the scene.")
    with db.session() as conn:
        job = jobs.enqueue(conn, type_, adventure["id"], ref_id="source",
                           payload={"prompt": "scene"})

    game_engine.handle_undo(adventure["id"])

    with db.session() as conn:
        canceled = db.get_job(conn, job["id"])
        assert canceled["status"] == "canceled"
        events = [row["type"] for row in conn.execute(
            "SELECT type FROM world_events WHERE adventure_id=?",
            (adventure["id"],),
        )]
    assert "job_progress" in events


def test_stale_see_job_cannot_append_action(tmp_db):
    adventure = make_adventure(title="Stale See")
    add_action(adventure["id"], 0, "start", "A scene begins.")
    with db.session() as conn:
        job = jobs.enqueue(conn, "see_image", adventure["id"], status="pending")
        conn.execute("UPDATE jobs SET source_revision=-1 WHERE id=?", (job["id"],))
    with pytest.raises(db.JobCanceled):
        jobs.store_see(adventure["id"], "Old image", {
            "file_path": "unused.png", "provider": "test", "width": 1, "height": 1,
        }, image_id="old-see-image", prompt="old", negative_prompt="",
           job_id=job["id"])
    with db.session() as conn:
        assert not any(a["type"] == "see" for a in db.list_actions(conn, adventure["id"]))
