"""GET /api/health/llm com scheduler, slots, watchdog, jobs, memória e
narrativas recentes (G10).

- breaker aberto vira status "degraded" na leitura seguinte;
- status.json do watchdog: slot parado e /slots mudo degradam, arquivo velho
  só marca `stale`;
- servidor fora do ar mantém o status da sonda e não sonda /props.
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from app import db, deps, llm_client, llm_scheduler, settings_store
from app.routes import health

MEMINFO = """MemTotal:       51963392 kB
MemFree:         3006464 kB
MemAvailable:   12210176 kB
SwapTotal:      20969468 kB
SwapFree:       16666624 kB
"""


def _transport(*, total_slots: int = 5, fail: str | None = None,
               props_calls: list | None = None) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        if fail == "connect":
            raise httpx.ConnectError("mock: connection refused")
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": [{"id": "qwen", "object": "model"}]})
        if request.url.path.endswith("/props"):
            if props_calls is not None:
                props_calls.append(request.url.path)
            return httpx.Response(200, json={"total_slots": total_slots})
        return httpx.Response(404)

    return httpx.MockTransport(handler)


@pytest.fixture()
def probe(client, tmp_path, monkeypatch):
    meminfo = tmp_path / "meminfo"
    meminfo.write_text(MEMINFO, encoding="utf-8")
    monkeypatch.setattr(health, "MEMINFO_PATH", meminfo)
    monkeypatch.setattr(health, "_query_gpu",
                        lambda: {"vram_used_mb": 15908, "vram_total_mb": 23034})
    monkeypatch.setattr(health, "_gpu_cache", (0.0, None))
    status = tmp_path / "status.json"
    settings_store.load_file_settings()
    monkeypatch.setitem(settings_store._file_cache["scheduler"], "watchdog_status_path",
                        str(status))
    with llm_client._recent_lock:
        llm_client._recent_calls.clear()

    def get(transport: httpx.MockTransport | None = None) -> dict:
        client.app.dependency_overrides[deps.get_http_transport] = \
            lambda: transport or _transport()
        resp = client.get("/api/health/llm")
        assert resp.status_code == 200, resp.text
        return resp.json()

    get.status_path = status
    yield get
    client.app.dependency_overrides.clear()
    with llm_client._recent_lock:
        llm_client._recent_calls.clear()


def _watchdog_status(path, *, age_s: float = 2.0, slots=None, unreachable_s: float = 0.0):
    checked = datetime.now(timezone.utc) - timedelta(seconds=age_s)
    path.write_text(json.dumps({
        "checked_at": checked.isoformat(timespec="seconds"),
        "slots": slots if slots is not None else [{"id": 0, "busy": False, "stalled_s": 0.0}],
        "unreachable_s": unreachable_s,
        "restarts": 1,
        "last_restart": {"at": "2026-10-18T19:00:00+00:00", "reason": "slot 1 sem progresso há 95s",
                         "dry_run": False},
        "hint": None,
    }), encoding="utf-8")


def _narrative(ttft_s: float, tok_s: float, *, outcome: str = "ok",
               prompt_n: int = 40, cache_n: int = 9000) -> None:
    state = llm_client.StreamState()
    state.ttft_s = ttft_s
    state.timings = {"prompt_n": prompt_n, "cache_n": cache_n, "predicted_n": 280,
                     "predicted_per_second": tok_s}
    llm_client._record_call("narrative", 0, state, time.monotonic() - 20, outcome)


def test_health_shows_scheduler_slots_jobs_memory_and_watchdog(probe):
    _watchdog_status(probe.status_path)
    with db.session() as conn:
        adv = db.insert_adventure(conn, title="Costazul")
        db.insert_job(conn, adventure_id=adv["id"], type="scene_backdrop")
        db.insert_job(conn, adventure_id=adv["id"], type="character_portrait", priority=3)
        running = db.insert_job(conn, adventure_id=adv["id"], type="character_portrait",
                                priority=1)
        conn.execute("UPDATE jobs SET status='running' WHERE id=?", (running["id"],))

    data = probe()

    assert data["status"] == "ok"
    assert data["model_discovered"] == "qwen"
    assert data["scheduler"]["breaker"] == {"state": "closed", "failures": 0, "until": None}
    assert data["slots"]["total"] == 5
    assert data["slots"]["roles"] == {"narrative": 0, "choices": 1, "chronicle": 2,
                                      "summary": 2, "image_prompt": 3, "assistant": 3,
                                      "meta": 3}
    assert data["slots"]["watchdog"]["restarts"] == 1
    assert data["slots"]["watchdog"]["stale"] is False
    assert data["jobs"]["pending"] == {"scene_backdrop": 1, "character_portrait": 1}
    assert data["jobs"]["running"] == {"character_portrait": 1}
    assert data["jobs"]["preparing"] == {}
    assert data["jobs"]["oldest_pending_s"] is not None
    assert data["memory"] == {"ram_total_mb": 50745, "ram_available_mb": 11924,
                              "swap_total_mb": 20477, "swap_used_mb": 4201,
                              "vram_used_mb": 15908, "vram_total_mb": 23034}


def test_open_breaker_is_degraded_on_the_next_read(probe):
    assert probe()["status"] == "ok"
    for _ in range(3):
        llm_scheduler.note_outcome("timeout")

    started = time.monotonic()
    data = probe()

    assert time.monotonic() - started < 5.0
    assert data["status"] == "degraded"
    assert data["scheduler"]["breaker"]["state"] == "open"
    assert "circuit breaker aberto" in data["details"]
    llm_scheduler.note_outcome("ok")
    assert probe()["status"] == "ok"


@pytest.mark.parametrize(("slots", "unreachable_s", "details"), [
    ([{"id": 0, "busy": True, "stalled_s": 12.0}, {"id": 2, "busy": True, "stalled_s": 45.0}],
     0.0, "slot 2 sem progresso há 45s"),
    ([], 20.0, "/slots sem resposta há 20s"),
])
def test_watchdog_seeing_a_stuck_server_degrades(probe, slots, unreachable_s, details):
    _watchdog_status(probe.status_path, slots=slots, unreachable_s=unreachable_s)
    data = probe()
    assert data["status"] == "degraded"
    assert data["details"] == details


def test_stale_watchdog_status_is_flagged_but_not_degraded(probe):
    _watchdog_status(probe.status_path, age_s=300,
                     slots=[{"id": 0, "busy": True, "stalled_s": 120.0}])
    data = probe()
    assert data["status"] == "ok"
    assert data["slots"]["watchdog"]["stale"] is True
    assert data["slots"]["watchdog"]["age_s"] >= 300


def test_missing_watchdog_status_is_null(probe):
    assert probe()["slots"]["watchdog"] is None


def test_garbage_in_watchdog_status_or_job_dates_never_breaks_health(probe):
    probe.status_path.write_text(json.dumps({
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "slots": [{"id": 0, "stalled_s": "muito"}, "x"], "unreachable_s": "abc",
    }), encoding="utf-8")
    with db.session() as conn:
        adv = db.insert_adventure(conn, title="Costazul")
        job = db.insert_job(conn, adventure_id=adv["id"], type="scene_backdrop")
        conn.execute("UPDATE jobs SET created_at='ontem' WHERE id=?", (job["id"],))

    data = probe()

    assert data["status"] == "ok"
    assert data["jobs"]["oldest_pending_s"] is None
    assert data["jobs"]["pending"] == {"scene_backdrop": 1}


def test_unreachable_server_keeps_status_and_skips_props(probe):
    props_calls: list = []
    for _ in range(3):
        llm_scheduler.note_outcome("timeout")
    data = probe(_transport(fail="connect", props_calls=props_calls))
    assert data["status"] == "unreachable"
    assert data["slots"]["total"] is None
    assert props_calls == []
    assert data["scheduler"]["breaker"]["state"] == "open"
    assert data["memory"]["ram_available_mb"] == 11924


def test_narrative_ttft_and_speed_of_recent_turns(probe):
    for ttft in (1.0, 2.0, 3.0, 4.0, 9.0):
        _narrative(ttft, tok_s=17.5 + ttft)
    _narrative(30.0, tok_s=0.0, outcome="timeout", prompt_n=None, cache_n=None)

    stats = probe()["narrative"]

    assert stats["samples"] == 5
    assert stats["ttft_p50_s"] == 3.0
    assert stats["ttft_p95_s"] == 9.0
    assert stats["tok_s_p50"] == 20.5
    assert stats["last"]["outcome"] == "timeout"
    assert stats["last"]["at"].endswith("Z")


def test_narrative_stats_empty():
    with llm_client._recent_lock:
        llm_client._recent_calls.clear()
    assert llm_client.narrative_stats() == {"samples": 0, "ttft_p50_s": None,
                                            "ttft_p95_s": None, "tok_s_p50": None,
                                            "last": None}
