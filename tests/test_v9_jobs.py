"""v0.9 — a fila de jobs cede enquanto alguma geração segura o lock."""

from __future__ import annotations

from app import db, game_engine, jobs
from conftest import make_adventure
from test_v3 import make_assets_transport, make_npc, use_tmp_storage


def test_process_pending_pauses_while_generation_lock_held(tmp_db, tmp_path):
    adv = make_adventure(title="Lock Test")
    npc = make_npc(
        adv["id"], title="Ada Vane", entry="Sky captain.",
        portrait_prompt="scarred veteran captain, bust portrait",
    )
    use_tmp_storage(tmp_path)
    with db.session() as conn:
        job = jobs.enqueue_portrait(conn, adv["id"], npc["id"])
    assert job is not None
    assert job["status"] == "pending"
    assert job["type"] == "character_portrait"

    assert game_engine.acquire_generation_lock(adv["id"]) is True
    try:
        assert game_engine.is_any_generation_active() is True
        assert jobs.process_pending(
            make_assets_transport(),
            should_pause=game_engine.is_any_generation_active,
        ) == 0
        with db.session() as conn:
            assert db.get_job(conn, job["id"])["status"] == "pending"
    finally:
        game_engine.release_generation_lock(adv["id"])

    assert game_engine.is_any_generation_active() is False
    assert jobs.process_pending(make_assets_transport()) == 1
    with db.session() as conn:
        assert db.get_job(conn, job["id"])["status"] == "done"
