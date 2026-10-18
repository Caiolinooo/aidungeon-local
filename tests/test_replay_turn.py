"""tools/replay_turn.py: o soak no staging cede a vez ao jogador do app principal."""

from __future__ import annotations

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
