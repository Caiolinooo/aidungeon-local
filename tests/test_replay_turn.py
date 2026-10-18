"""tools/replay_turn.py: o soak no staging cede a vez ao jogador do app principal."""

from __future__ import annotations

import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

TOOLS_DIR = Path(__file__).resolve().parent.parent / "tools"
if str(TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(TOOLS_DIR))

import replay_turn  # noqa: E402


class FakeMain:
    def __init__(self, replies: list[float | None]) -> None:
        self.replies = replies
        self.calls = 0

    def call(self, method: str, path: str, body: dict | None = None) -> dict:
        self.calls += 1
        ago = self.replies.pop(0)
        if ago is None:
            return {"status": None, "body": None, "error": "URLError", "elapsed_s": 0.0}
        stamp = datetime.fromtimestamp(time.time() - ago, timezone.utc)
        updated = stamp.isoformat(timespec="milliseconds").replace("+00:00", "Z")
        return {"status": 200, "body": [{"updated_at": None}, {"updated_at": updated}],
                "error": None, "elapsed_s": 0.0}


def test_soak_waits_for_the_player_to_go_quiet(monkeypatch):
    sleeps: list[float] = []
    monkeypatch.setattr(replay_turn.time, "sleep", sleeps.append)
    args = replay_turn.build_parser().parse_args(
        ["--adventure", "x", "--turn", "do:olho em volta", "--yield-to", "http://main", "--idle-s", "300"])
    main = FakeMain([None, 30.0, 120.0, 400.0])
    replay = replay_turn.Replay(None, args, main)
    replay.yield_to_player()
    replay.pool.shutdown()
    assert main.calls == 4
    assert len(sleeps) == 3


def _ago(seconds: float) -> str:
    stamp = datetime.fromtimestamp(time.time() - seconds, timezone.utc)
    return stamp.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _main_db(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE adventures(id TEXT, updated_at TEXT);
        CREATE TABLE actions(id INTEGER, created_at TEXT, edited_at TEXT);
        CREATE TABLE story_cards(id TEXT, created_at TEXT, updated_at TEXT);
        CREATE TABLE memories(id INTEGER, created_at TEXT);
        CREATE TABLE images(id TEXT, created_at TEXT);
        CREATE TABLE jobs(id TEXT, status TEXT, created_at TEXT, started_at TEXT,
                          finished_at TEXT);
    """)
    conn.execute("INSERT INTO adventures VALUES ('a', ?)", (_ago(3600),))
    conn.execute("INSERT INTO actions VALUES (1, ?, NULL)", (_ago(3600),))
    conn.commit()
    return conn


def test_db_guard_sees_background_writes_and_live_jobs(tmp_path):
    path = tmp_path / "main.db"
    conn = _main_db(path)
    assert 3590 < replay_turn.db_quiet_s(str(path)) < 3610

    conn.execute("INSERT INTO story_cards VALUES ('c', ?, ?)", (_ago(900), _ago(30)))
    conn.commit()
    assert 25 < replay_turn.db_quiet_s(str(path)) < 40

    conn.execute("INSERT INTO jobs VALUES ('stale', 'pending', ?, NULL, NULL)", (_ago(7200),))
    conn.commit()
    assert replay_turn.db_quiet_s(str(path)) > 25

    conn.execute("INSERT INTO jobs VALUES ('j', 'running', ?, ?, NULL)", (_ago(600), _ago(590)))
    conn.commit()
    assert replay_turn.db_quiet_s(str(path)) == 0.0
    conn.close()

    assert replay_turn.db_quiet_s(str(tmp_path / "missing.db")) is None


def test_soak_yields_on_the_db_guard_alone(monkeypatch, tmp_path):
    path = tmp_path / "main.db"
    _main_db(path).close()
    readings = iter([0.0, 120.0, 400.0])
    monkeypatch.setattr(replay_turn, "db_quiet_s", lambda _path: next(readings))
    sleeps: list[float] = []
    monkeypatch.setattr(replay_turn.time, "sleep", sleeps.append)
    args = replay_turn.build_parser().parse_args(
        ["--adventure", "x", "--turn", "do:olho", "--yield-db", str(path), "--idle-s", "300"])
    replay = replay_turn.Replay(None, args, None)
    replay.yield_to_player()
    replay.pool.shutdown()
    assert len(sleeps) == 2
