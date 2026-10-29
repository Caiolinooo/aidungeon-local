"""Reinício e falha de job deixam um evento terminal durável."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from app import db, image_client, jobs, llm_client
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


def _stamp(moment: datetime) -> str:
    return moment.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _running(adventure_id: str, type_: str, ref_id: str, started: datetime,
             payload: dict | None = None) -> dict:
    with db.session() as conn:
        job = db.insert_job(
            conn, adventure_id=adventure_id, type=type_, ref_id=ref_id,
            payload=payload or {})
        conn.execute(
            "UPDATE jobs SET status='running', started_at=? WHERE id=?",
            (_stamp(started), job["id"]),
        )
        return db.get_job(conn, job["id"])


def test_reaper_times_out_from_started_at_without_draining_the_queue(
        tmp_db, monkeypatch):
    """process_pending não colhe. Heartbeat fresco não estende o prazo.
    Publicação tardia não cobre o erro."""
    assert jobs._REAPER_INTERVAL_S == 5.0
    now = datetime(2026, 10, 29, tzinfo=timezone.utc)
    interrupted = []
    monkeypatch.setattr(
        jobs, "interrupt_image",
        lambda job, transport=None: interrupted.append((job["id"], transport)) or True)
    adventure = make_adventure()
    image = _running(
        adventure["id"], "see_image", "", now - timedelta(seconds=360))
    tick = _running(
        adventure["id"], "npc_day_tick", adventure["id"], now - timedelta(seconds=900))
    assets = _running(
        adventure["id"], "card_assets", "card", now - timedelta(seconds=180))
    clip = _running(
        adventure["id"], "scene_clip", "src", now - timedelta(seconds=1860),
        {"automatic": True, "prompt": "rain"})
    young = _running(
        adventure["id"], "scene_backdrop", "room", now - timedelta(seconds=359),
        {"prompt": "hall"})
    with db.session() as conn:
        assert db.touch_job(conn, image["id"], _stamp(now))
    assert jobs.process_pending(None) == 0
    with db.session() as conn:
        for job in (image, tick, assets, clip, young):
            assert db.get_job(conn, job["id"])["status"] == "running"
    assert jobs.reap_stalled_jobs("wire", now=now) == 4
    assert {item[0] for item in interrupted} == {
        image["id"], tick["id"], assets["id"], clip["id"]}
    assert {item[1] for item in interrupted} == {"wire"}
    with db.session() as conn:
        for job_id in (image["id"], tick["id"], assets["id"], clip["id"]):
            stored = db.get_job(conn, job_id)
            assert stored["status"] == "error"
            assert stored["result"] == {"code": "job_timeout"}
            assert stored["error"] == "job exceeded its deadline"
            assert "heartbeat_at" not in stored
            assert "heartbeat_at" not in (stored["payload"] or {})
            assert db.finish_job(
                conn, job_id, "done", result={"image": {"id": "late"}}) is False
            again = db.get_job(conn, job_id)
            assert again["status"] == "error"
            assert again["result"] == {"code": "job_timeout"}
        assert db.get_job(conn, young["id"])["status"] == "running"
        row = conn.execute(
            "SELECT payload_json FROM world_events WHERE adventure_id=? AND type='see_failed'",
            (adventure["id"],),
        ).fetchone()
    assert row is not None and "job_timeout" in row["payload_json"]

    ran = []

    def late(job, transport):
        ran.append(job["id"])
        return {"image": {"id": "late"}}

    monkeypatch.setitem(jobs._EXECUTORS, "see_image", late)
    with db.session() as conn:
        current = db.get_job(conn, image["id"])
    jobs._run_job(current, None)
    assert ran == [image["id"]]
    with db.session() as conn:
        stored = db.get_job(conn, image["id"])
    assert stored["status"] == "error"
    assert stored["result"] == {"code": "job_timeout"}
    assert stored["error"] == "job exceeded its deadline"


def test_deadlines_follow_image_video_and_assets_timeouts(tmp_db, monkeypatch):
    now = datetime(2026, 10, 29, tzinfo=timezone.utc)
    monkeypatch.setattr(
        image_client, "video_settings", lambda cfg: {"timeout_seconds": 2000})
    monkeypatch.setattr(llm_client, "role_timeout", lambda cfg, role: 500.0)
    adventure = make_adventure()
    with db.session() as conn:
        db.set_setting(conn, "image.timeout_seconds", json.dumps(400))
    image = _running(
        adventure["id"], "location_image", "loc", now - timedelta(seconds=459),
        {"prompt": "dock"})
    clip = _running(
        adventure["id"], "scene_clip", "src", now - timedelta(seconds=2059),
        {"prompt": "waves"})
    assets = _running(
        adventure["id"], "card_assets", "card", now - timedelta(seconds=559))
    assert jobs.reap_stalled_jobs(None, now=now) == 0
    with db.session() as conn:
        conn.execute(
            "UPDATE jobs SET started_at=? WHERE id=?",
            (_stamp(now - timedelta(seconds=460)), image["id"]))
        conn.execute(
            "UPDATE jobs SET started_at=? WHERE id=?",
            (_stamp(now - timedelta(seconds=2060)), clip["id"]))
        conn.execute(
            "UPDATE jobs SET started_at=? WHERE id=?",
            (_stamp(now - timedelta(seconds=560)), assets["id"]))
    assert jobs.reap_stalled_jobs(None, now=now) == 3


def test_reaper_loop_is_independent_of_the_queue_worker(tmp_db, monkeypatch):
    calls = {"reap": 0, "pending": 0}
    monkeypatch.setattr(jobs, "worker_enabled", lambda: True)
    monkeypatch.setattr(jobs, "recover_interrupted", lambda: (0, 0))

    def fake_reap(transport=None, now=None):
        calls["reap"] += 1
        return 0

    def fake_pending(*args, **kwargs):
        calls["pending"] += 1
        return 0

    monkeypatch.setattr(jobs, "reap_stalled_jobs", fake_reap)
    monkeypatch.setattr(jobs, "process_pending", fake_pending)
    app = SimpleNamespace(state=SimpleNamespace())

    async def scenario():
        jobs.start_worker(app)
        try:
            deadline = asyncio.get_running_loop().time() + 2
            while asyncio.get_running_loop().time() < deadline:
                if calls["reap"] and calls["pending"]:
                    break
                await asyncio.sleep(0.01)
            assert calls["reap"] >= 1
            assert calls["pending"] >= 1
            assert app.state.job_reaper is not app.state.job_worker
        finally:
            jobs.stop_worker(app)
            await asyncio.wait_for(
                asyncio.gather(
                    app.state.job_worker, app.state.job_reaper,
                    return_exceptions=True),
                timeout=3,
            )

    asyncio.run(scenario())
