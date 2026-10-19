"""v0.9 — pós-turno serializado por aventura; PATCH não regrava colunas omitidas."""

from __future__ import annotations

import sqlite3
import threading

from app import db, game_engine
from conftest import make_adventure


class _TrackingLock:
    """Lock de teste: conta quem entrou em acquire antes de bloquear."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._mu = threading.Lock()
        self.attempts = 0
        self.both_attempted = threading.Event()

    def acquire(self, blocking: bool = True, timeout: float = -1):
        with self._mu:
            self.attempts += 1
            if self.attempts >= 2:
                self.both_attempted.set()
        if timeout is None or timeout < 0:
            return self._lock.acquire(blocking)
        return self._lock.acquire(blocking, timeout)

    def release(self) -> None:
        self._lock.release()

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, *exc) -> None:
        self.release()


def test_overlapping_post_turn_passes_serialize(monkeypatch):
    adventure_id = "race-post-turn"
    tracker = _TrackingLock()
    game_engine._post_turn_locks[adventure_id] = tracker
    monkeypatch.setenv("AIDUNGEON_CHRONICLE", "1")
    monkeypatch.setattr(
        game_engine.npc_chronicle, "update_from_turn", lambda *args, **kwargs: None,
    )

    state = {"inside": 0, "max": 0, "done": 0}
    mu = threading.Lock()
    finished = threading.Event()

    def memory(_adventure_id: str, _cancel=None) -> None:
        with mu:
            state["inside"] += 1
            state["max"] = max(state["max"], state["inside"])
            ticket = state["inside"]
        if ticket == 1:
            assert tracker.both_attempted.wait(timeout=2)
            with mu:
                assert state["inside"] == 1
                assert state["max"] == 1
        with mu:
            state["inside"] -= 1
            state["done"] += 1
            if state["done"] == 2:
                finished.set()

    monkeypatch.setattr(game_engine, "_maybe_update_memory", memory)
    try:
        game_engine._spawn_post_turn(adventure_id)
        game_engine._spawn_post_turn(adventure_id)
        assert finished.wait(timeout=2)
        assert tracker.attempts >= 2
        assert state["max"] == 1
        assert state["done"] == 2
        assert state["inside"] == 0
    finally:
        game_engine._post_turn_locks.pop(adventure_id, None)


class _RecordingConn:
    def __init__(self, inner, statements: list[str]) -> None:
        self._inner = inner
        self._statements = statements

    def execute(self, sql, parameters=()):
        self._statements.append(" ".join(str(sql).split()))
        return self._inner.execute(sql, parameters)

    def __getattr__(self, name):
        return getattr(self._inner, name)


def test_update_adventure_title_keeps_concurrent_story_summary(tmp_db, monkeypatch):
    adv = make_adventure(title="Old", description="Keep me", story_summary="original")
    aid = adv["id"]
    real_get = db.get_adventure
    calls = {"n": 0}
    statements: list[str] = []

    def stale_get(conn, adventure_id):
        calls["n"] += 1
        if calls["n"] == 1:
            side = sqlite3.connect(db.db_path())
            try:
                side.execute(
                    "UPDATE adventures SET story_summary=? WHERE id=?",
                    ("from-summarizer", adventure_id),
                )
                side.commit()
            finally:
                side.close()
            snap = dict(real_get(conn, adventure_id))
            snap["story_summary"] = "original"
            return snap
        return real_get(conn, adventure_id)

    monkeypatch.setattr(db, "get_adventure", stale_get)
    conn = db.connect()
    try:
        updated = db.update_adventure(_RecordingConn(conn, statements), aid, title="New")
        conn.commit()
    finally:
        conn.close()

    assert updated["title"] == "New"
    assert updated["story_summary"] == "from-summarizer"
    assert updated["description"] == "Keep me"
    updates = [sql for sql in statements if sql.upper().startswith("UPDATE ADVENTURES")]
    assert any("title=?" in sql and "story_summary" not in sql for sql in updates)
    assert not any("title=?" in sql and "story_summary" in sql for sql in updates)


def test_update_adventure_explicit_fields_still_write(tmp_db):
    adv = make_adventure(title="Old", description="Keep me", story_summary="original")
    with db.session() as conn:
        updated = db.update_adventure(
            conn, adv["id"],
            title="New", description="Changed", story_summary="Replaced",
            third_person=True,
        )
    assert updated["title"] == "New"
    assert updated["description"] == "Changed"
    assert updated["story_summary"] == "Replaced"
    assert updated["third_person"] is True
    assert updated["authors_note"] == ""

    with db.session() as conn:
        again = db.update_adventure(conn, adv["id"], title="Newer", story_summary=None)
    assert again["title"] == "Newer"
    assert again["story_summary"] == "Replaced"
