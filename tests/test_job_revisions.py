"""Media jobs: undo cancels them, a finished render publishes, no requeue storms."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app import db, events, game_engine, jobs, npc_assets, scene
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


def _see_meta() -> dict:
    return {"file_path": "unused.png", "provider": "test", "width": 1, "height": 1}


def test_canceled_see_job_cannot_append_action(tmp_db):
    adventure = make_adventure(title="Stale See")
    add_action(adventure["id"], 0, "start", "A scene begins.")
    with db.session() as conn:
        job = jobs.enqueue(conn, "see_image", adventure["id"], status="pending")
        assert db.claim_next_job(conn)["id"] == job["id"]
        jobs.cancel_adventure_jobs(conn, adventure["id"], "superseded by undo")
    with pytest.raises(db.JobCanceled):
        jobs.store_see(adventure["id"], "Old image", _see_meta(),
                       image_id="old-see-image", prompt="old", negative_prompt="",
                       job_id=job["id"])
    with db.session() as conn:
        assert not any(a["type"] == "see" for a in db.list_actions(conn, adventure["id"]))


def test_running_see_job_publishes_after_later_events(tmp_db):
    """`state_revision` sobe a cada evento, inclusive o job_progress do próprio
    See. Comparar com o snapshot do enqueue cancelava todo See já renderizado."""
    adventure = make_adventure(title="Live See")
    add_action(adventure["id"], 0, "start", "A scene begins.")
    with db.session() as conn:
        job = jobs.enqueue(conn, "see_image", adventure["id"], status="pending")
        assert db.claim_next_job(conn)["id"] == job["id"]
        for step in range(3):
            events.record(conn, adventure["id"], "job_progress",
                          {"job_id": job["id"], "type": "see_image", "status": "running",
                           "step": step}, entity_id=job["id"])
    result = jobs.store_see(adventure["id"], "A fresh image", _see_meta(),
                            image_id="new-see-image", prompt="new", negative_prompt="",
                            job_id=job["id"])
    assert result["id"] == "new-see-image"
    with db.session() as conn:
        seen = [a for a in db.list_actions(conn, adventure["id"]) if a["type"] == "see"]
        assert len(seen) == 1
        assert db.get_job(conn, job["id"])["status"] == "running"


def test_finished_backdrop_is_found_under_the_translated_ref(tmp_db):
    """O executor grava o fundo sob a chave do prompt traduzido. A cena pede
    pelo ref do texto de origem e tem de achar o job concluído, não enfileirar
    outro a cada leitura."""
    adventure = make_adventure(title="Backdrop loop")
    add_action(adventure["id"], 0, "start", "A scene begins.")
    source_ref = f"{adventure['id']}:{'a' * 40}"
    stored_ref = f"{adventure['id']}:{'b' * 40}"
    with db.session() as conn:
        job = jobs.enqueue(conn, "scene_backdrop", adventure["id"], source_ref,
                           {"prompt": "cena", "cache_key": "a" * 64})
        assert db.claim_next_job(conn)["id"] == job["id"]
        image = db.insert_image(
            conn, adventure_id=adventure["id"], action_seq=-1, file_path="unused.png",
            prompt="scene", provider="test", width=1, height=1, image_id="backdrop-1",
            kind="backdrop", ref_id=stored_ref, cache_key="b" * 64, job_id=job["id"])
        assert db.finish_job(conn, job["id"], "done",
                             result={"image": db.image_ref(image), "ref_id": stored_ref})
    for _ in range(3):
        with db.session() as conn:
            state = scene._backdrop_state(
                conn, adventure["id"], source_ref, enabled=True, retry=False,
                prompt="cena", cache_key="a" * 64)
        assert state["status"] == "ready"
        assert state["image"]["id"] == "backdrop-1"
    with db.session() as conn:
        count = conn.execute(
            "SELECT COUNT(*) FROM jobs WHERE type='scene_backdrop'").fetchone()[0]
    assert count == 1


def test_automatic_portrait_retry_backs_off_after_repeated_errors(tmp_db):
    adventure = make_adventure(title="Portrait storm")
    with db.session() as conn:
        for index in range(3):
            job = jobs.enqueue(conn, "character_portrait", adventure["id"], "card-1",
                               {"n": index}, status="pending")
            assert db.claim_next_job(conn)["id"] == job["id"]
            assert db.finish_job(conn, job["id"], "error",
                                 error="image prompt could not be translated to English")
            hold = jobs.auto_retry_hold(conn, "character_portrait", "card-1")
            # o primeiro erro tenta de novo na hora; do segundo em diante espera
            assert (hold is None) == (index == 0)
        later = datetime.now(timezone.utc) + timedelta(seconds=jobs.AUTO_RETRY_MAX_S + 1)
        assert jobs.auto_retry_hold(conn, "character_portrait", "card-1", now=later) is None


def test_portrait_framing_does_not_leak_into_the_scene_look():
    caio = ("A slender pirate with dark brown hair and tanned skin, "
            "framed in a bust portrait against a neutral background.")
    thief = ("A stout pirate with a large belly, messy gray hair, and a scruffy beard, "
             "upper body shot, neutral background")
    assert npc_assets.without_framing(caio) == (
        "A slender pirate with dark brown hair and tanned skin")
    assert npc_assets.without_framing(thief) == (
        "A stout pirate with a large belly, messy gray hair, and a scruffy beard")
