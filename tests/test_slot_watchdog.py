"""tools/slot_watchdog.py: quando reiniciar o llama-server (llama.cpp #28280)."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

TOOLS_DIR = Path(__file__).resolve().parent.parent / "tools"
if str(TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(TOOLS_DIR))

import slot_watchdog  # noqa: E402


def _slot(sid: int, *, busy: bool, task: int = 7, processed: int = 100, decoded: int = 0) -> dict:
    return {"id": sid, "is_processing": busy, "id_task": task,
            "n_prompt_tokens_processed": processed,
            "next_token": [{"n_decoded": decoded}]}


@pytest.fixture()
def watchdog(tmp_path, monkeypatch):
    restarts: list[list[str]] = []
    replies: list = []
    monkeypatch.setattr(slot_watchdog, "fetch_slots", lambda *a, **k: replies.pop(0))
    monkeypatch.setattr(slot_watchdog.subprocess, "run",
                        lambda cmd, check=False: restarts.append(cmd))

    def run(reply, now: float, *extra: str) -> dict:
        replies.append(reply)
        args = slot_watchdog.build_parser().parse_args(["--state-dir", str(tmp_path), *extra])
        slot_watchdog.check(args, now)
        return json.loads((tmp_path / "status.json").read_text())

    run.restarts = restarts
    return run


def test_stuck_slot_restarts_after_the_threshold(watchdog):
    stuck = [_slot(0, busy=False), _slot(1, busy=True)]
    watchdog(stuck, 1000.0)
    watchdog(stuck, 1030.0)
    assert watchdog.restarts == []
    status = watchdog(stuck, 1050.0)
    assert watchdog.restarts == [["systemctl", "restart", "llama-server"]]
    assert status["restarts"] == 1
    assert "slot 1" in status["last_restart"]["reason"]


def test_slot_that_progresses_is_left_alone(watchdog):
    for step in range(10):
        status = watchdog([_slot(0, busy=True, processed=100 + step * 512)], 1000.0 + step * 20)
    assert watchdog.restarts == []
    assert status["slots"][0]["stalled_s"] == 0.0


def test_task_bouncing_between_two_positions_still_counts_as_stuck(watchdog):
    for step, processed in enumerate((37013, 2798, 37013, 2798, 37013)):
        watchdog([_slot(0, busy=True, processed=processed)], 1000.0 + step * 12)
    assert watchdog.restarts == [["systemctl", "restart", "llama-server"]]


def test_new_task_resets_the_clock(watchdog):
    watchdog([_slot(0, busy=True, task=1)], 1000.0)
    watchdog([_slot(0, busy=True, task=2)], 1040.0)
    watchdog([_slot(0, busy=True, task=2)], 1080.0)
    assert watchdog.restarts == []


def test_cooldown_blocks_a_restart_loop(watchdog):
    stuck = [_slot(0, busy=True)]
    watchdog(stuck, 1000.0)
    watchdog(stuck, 1050.0)
    assert len(watchdog.restarts) == 1
    watchdog(stuck, 1060.0)
    watchdog(stuck, 1120.0)
    assert len(watchdog.restarts) == 1
    watchdog(stuck, 1300.0)
    assert len(watchdog.restarts) == 2


def test_slots_endpoint_timing_out_counts_as_stuck(watchdog):
    watchdog(None, 1000.0)
    status = watchdog(None, 1050.0)
    assert len(watchdog.restarts) == 1
    assert "/slots" in status["last_restart"]["reason"]


def test_refused_connection_is_not_a_stall(watchdog):
    watchdog([], 1000.0)
    status = watchdog([], 1100.0)
    assert watchdog.restarts == []
    assert status["slots"] == []


def test_dry_run_records_without_restarting(watchdog):
    stuck = [_slot(0, busy=True)]
    watchdog(stuck, 1000.0, "--dry-run")
    status = watchdog(stuck, 1050.0, "--dry-run")
    assert watchdog.restarts == []
    assert status["last_restart"]["dry_run"] is True
