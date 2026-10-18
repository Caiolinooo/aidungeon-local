"""Eventos por aventura (G4, contrato C2): pub/sub em memória e rota SSE.

A rota SSE roda num uvicorn de verdade numa thread: o TestClient do Starlette
espera o corpo inteiro e nunca termina num stream sem fim.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time

import httpx
import pytest
import uvicorn

from app import auth as auth_core
from app import events, llm_scheduler
from app.routes import events as events_routes
from conftest import TEST_AUTH_SECRET, make_adventure


# ------------------------------------------------------------------ bus
def test_subscriber_gets_events_in_publish_order():
    with events.subscribe("adv") as sub:
        ids = [events.publish("adv", "turn_done", {"seq": i}) for i in range(5)]
        got = [sub.get(1.0) for _ in range(5)]
    assert ids == sorted(ids) and len(set(ids)) == 5
    assert [e.id for e in got] == ids
    assert [e.data["seq"] for e in got] == list(range(5))
    assert {e.type for e in got} == {"turn_done"}


def test_new_subscriber_starts_after_the_last_event():
    old = events.publish("adv", "turn_done", {"seq": 1})
    with events.subscribe("adv") as sub:
        assert sub.since == old
        assert sub.get(0.05) is None
        new = events.publish("adv", "turn_done", {"seq": 2})
        assert sub.get(1.0).id == new


def test_last_event_id_replays_the_buffer():
    first = events.publish("adv", "turn_done", {"seq": 1})
    second = events.publish("adv", "choices_ready", {"for_seq": 1, "choices": []})
    third = events.publish("adv", "turn_done", {"seq": 2})
    with events.subscribe("adv", last_event_id=first) as sub:
        assert [sub.get(1.0).id for _ in range(2)] == [second, third]
        assert sub.get(0.05) is None


def test_buffer_keeps_only_the_latest():
    for i in range(events.BUFFER_SIZE + 10):
        events.publish("adv", "job_progress", {"n": i})
    with events.subscribe("adv", last_event_id=0) as sub:
        got = [sub.get(1.0) for _ in range(events.BUFFER_SIZE)]
        assert sub.get(0.05) is None
    assert got[0].data["n"] == 10
    assert got[-1].data["n"] == events.BUFFER_SIZE + 9


def test_channels_are_per_adventure():
    with events.subscribe("a") as sub_a, events.subscribe("b") as sub_b:
        events.publish("a", "turn_done", {"seq": 1})
        assert sub_a.get(1.0).adventure_id == "a"
        assert sub_b.get(0.05) is None


def test_broadcast_reaches_every_open_channel():
    with events.subscribe("a") as sub_a, events.subscribe("b") as sub_b:
        ids = events.broadcast("llm_recovered")
        assert len(ids) == 2
        assert sub_a.get(1.0).type == "llm_recovered"
        assert sub_b.get(1.0).type == "llm_recovered"


def test_slow_subscriber_drops_the_oldest(monkeypatch):
    monkeypatch.setattr(events, "SUBSCRIPTION_MAX", 3)
    with events.subscribe("adv") as sub:
        for i in range(5):
            events.publish("adv", "job_progress", {"n": i})
        assert sub.dropped == 2
        assert [sub.get(1.0).data["n"] for _ in range(3)] == [2, 3, 4]


def test_concurrent_publishers_deliver_in_id_order():
    with events.subscribe("adv") as sub:
        def burst(tag: int) -> None:
            for i in range(20):
                events.publish("adv", "job_progress", {"tag": tag, "n": i})

        threads = [threading.Thread(target=burst, args=(t,)) for t in range(5)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        got = [sub.get(1.0) for _ in range(100)]
    ids = [e.id for e in got]
    assert ids == sorted(ids) and len(set(ids)) == 100
    for tag in range(5):
        assert [e.data["n"] for e in got if e.data["tag"] == tag] == list(range(20))


def test_aget_wakes_up_on_publish_from_another_thread():
    async def scenario() -> events.Event | None:
        with events.subscribe("adv") as sub:
            first = await sub.aget(0.05)
            assert first is None
            timer = threading.Timer(0.1, events.publish, args=("adv", "turn_done", {"seq": 3}))
            timer.start()
            try:
                return await sub.aget(2.0)
            finally:
                timer.cancel()

    started = time.monotonic()
    event = asyncio.run(scenario())
    assert event is not None and event.data == {"seq": 3}
    assert time.monotonic() - started < 1.5


def test_forget_closes_subscribers():
    sub = events.subscribe("adv")
    events.publish("adv", "turn_done", {"seq": 1})
    events.forget("adv")
    assert sub.closed
    with events.subscribe("adv", last_event_id=0) as fresh:
        assert fresh.get(0.05) is None


def _open_breaker() -> None:
    for _ in range(llm_scheduler.Limits().breaker_threshold):
        llm_scheduler.note_outcome("timeout")


def test_scheduler_breaker_is_broadcast():
    with events.subscribe("adv") as sub:
        _open_breaker()
        degraded = sub.get(1.0)
        llm_scheduler.note_outcome("ok")
        recovered = sub.get(1.0)
    assert degraded.type == "llm_degraded"
    assert degraded.data["reason"] and degraded.data["until"]
    assert recovered.type == "llm_recovered"


def test_sse_format():
    event = events.Event(42, "turn_done", {"action_id": 7, "seq": 3, "texto": "ção"}, "adv")
    assert event.sse() == (
        'id: 42\nevent: turn_done\ndata: {"action_id":7,"seq":3,"texto":"ção"}\n\n')


# ------------------------------------------------------------ rota (sem stream)
def _url(adventure_id: str) -> str:
    return f"/api/adventures/{adventure_id}/events"


def test_events_require_a_token(client):
    adv = make_adventure()
    resp = client.get(_url(adv["id"]), headers={"Authorization": ""})
    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "unauthorized"
    resp = client.get(_url(adv["id"]) + "?token=forjado.123.abc",
                      headers={"Authorization": ""})
    assert resp.status_code == 401


def test_events_unknown_adventure_404(client, auth_token):
    resp = client.get(_url("f" * 32) + f"?token={auth_token}",
                      headers={"Authorization": ""})
    assert resp.status_code == 404


def test_events_hide_someone_elses_story(client):
    adv = make_adventure(owner="alice")
    bob = auth_core.make_token("bob", TEST_AUTH_SECRET, ttl_h=1)
    resp = client.get(_url(adv["id"]) + f"?token={bob}", headers={"Authorization": ""})
    assert resp.status_code == 404
    resp = client.get(_url(adv["id"]), headers={"Authorization": f"Bearer {bob}"})
    assert resp.status_code == 404


def test_access_log_redacts_the_token():
    record = logging.LogRecord(
        "uvicorn.access", logging.INFO, __file__, 1, '%s - "%s %s HTTP/%s" %d',
        ("127.0.0.1:5000", "GET",
         "/api/adventures/abc/events?token=dXNlcg.MTIz.deadbeef&last_event_id=5",
         "1.1", 200), None)
    for log_filter in logging.getLogger("uvicorn.access").filters:
        log_filter.filter(record)
    message = record.getMessage()
    assert "deadbeef" not in message
    assert "token=<redacted>&last_event_id=5" in message


# ------------------------------------------------------- rota (stream real)
@pytest.fixture()
def live_server(tmp_db):
    from app.main import app

    config = uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning",
                            lifespan="on", timeout_graceful_shutdown=2)
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, name="uvicorn-test", daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started:
        if not thread.is_alive() or time.monotonic() > deadline:
            raise RuntimeError("uvicorn não subiu")
        time.sleep(0.02)
    port = server.servers[0].sockets[0].getsockname()[1]
    yield f"http://127.0.0.1:{port}"
    events.reset()
    server.should_exit = True
    thread.join(10)


class _SseReader:
    """Lê eventos SSE de uma resposta httpx em stream."""

    def __init__(self, resp: httpx.Response) -> None:
        self._lines = resp.iter_lines()

    def next(self) -> dict:
        event: dict = {}
        for line in self._lines:
            if line == "":
                if event:
                    return event
                continue
            if line.startswith(":"):
                return {"comment": line[1:].strip()}
            field, _, value = line.partition(":")
            event[field] = value[1:] if value.startswith(" ") else value
        raise AssertionError(f"stream acabou no meio de {event}")

    def remaining(self) -> list[str]:
        return list(self._lines)


def _open(client: httpx.Client, base: str, adventure_id: str, token: str,
          headers: dict | None = None, query: str = ""):
    return client.stream("GET", f"{base}{_url(adventure_id)}?token={token}{query}",
                         headers=headers or {})


def test_stream_starts_with_ready_and_delivers_in_order(live_server, auth_token):
    adv = make_adventure()
    with httpx.Client(timeout=5.0) as http, \
            _open(http, live_server, adv["id"], auth_token) as resp:
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/event-stream")
        assert resp.headers["cache-control"] == "no-cache"
        reader = _SseReader(resp)
        ready = reader.next()
        assert ready["event"] == "ready" and ready["retry"] == "3000"
        ids = [events.publish(adv["id"], "turn_done", {"action_id": 10 + i, "seq": i})
               for i in range(3)]
        got = [reader.next() for _ in range(3)]
    assert int(ready["id"]) < ids[0]
    assert [int(e["id"]) for e in got] == ids
    assert [e["event"] for e in got] == ["turn_done"] * 3
    assert [e["data"] for e in got] == [
        f'{{"action_id":{10 + i},"seq":{i}}}' for i in range(3)]


def test_reconnect_with_last_event_id_loses_nothing(live_server, auth_token):
    adv = make_adventure()
    with httpx.Client(timeout=5.0) as http:
        with _open(http, live_server, adv["id"], auth_token) as resp:
            reader = _SseReader(resp)
            ready = reader.next()
            first = events.publish(adv["id"], "turn_done", {"seq": 1})
            assert int(reader.next()["id"]) == first
        missed = [events.publish(adv["id"], "choices_ready",
                                 {"for_seq": 1, "choices": [{"type": "do", "text": "x"}]}),
                  events.publish(adv["id"], "turn_done", {"seq": 3})]
        with _open(http, live_server, adv["id"], auth_token,
                   headers={"Last-Event-ID": str(first)}) as resp:
            reader = _SseReader(resp)
            again = reader.next()
            replayed = [reader.next() for _ in range(2)]
            live = events.publish(adv["id"], "turn_done", {"seq": 5})
            after = reader.next()
    assert int(ready["id"]) < first
    assert again["event"] == "ready" and int(again["id"]) == first
    assert [int(e["id"]) for e in replayed] == missed
    assert [e["event"] for e in replayed] == ["choices_ready", "turn_done"]
    assert int(after["id"]) == live


def test_ready_id_covers_a_drop_before_the_first_event(live_server, auth_token):
    adv = make_adventure()
    with httpx.Client(timeout=5.0) as http:
        with _open(http, live_server, adv["id"], auth_token) as resp:
            ready = _SseReader(resp).next()
        missed = events.publish(adv["id"], "turn_done", {"seq": 1})
        with _open(http, live_server, adv["id"], auth_token,
                   query=f"&last_event_id={ready['id']}") as resp:
            reader = _SseReader(resp)
            reader.next()
            assert int(reader.next()["id"]) == missed


def test_stream_pings_and_announces_open_breaker(live_server, auth_token, monkeypatch):
    monkeypatch.setattr(events_routes, "PING_S", 0.2)
    _open_breaker()
    adv = make_adventure()
    with httpx.Client(timeout=5.0) as http, \
            _open(http, live_server, adv["id"], auth_token) as resp:
        reader = _SseReader(resp)
        assert reader.next()["event"] == "ready"
        degraded = reader.next()
        ping = reader.next()
    assert degraded["event"] == "llm_degraded" and "id" not in degraded
    assert ping == {"comment": "ping"}


def test_forgotten_channel_ends_the_stream(live_server, auth_token):
    adv = make_adventure()
    with httpx.Client(timeout=5.0) as http, \
            _open(http, live_server, adv["id"], auth_token) as resp:
        reader = _SseReader(resp)
        reader.next()
        events.forget(adv["id"])
        assert reader.remaining() == []
