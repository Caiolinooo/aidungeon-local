"""G3: scheduler das chamadas ao llama-server e fallback da narrativa parada.

- prioridade por role, uma auxiliar por slot, teto global (menor com
  narrativa em curso), cancelamento e teto de espera na fila;
- circuit breaker (abre com 3 timeouts, meio-aberto depois do prazo,
  listeners `llm_degraded`/`llm_recovered`);
- preempção do slot pelo fallback da narrativa e repetição da auxiliar;
- narrativa sem token nem `prompt_progress` repetida no slot do image_prompt,
  com servidor de mentira num socket real; servidores com menos de 4 slots.
"""

from __future__ import annotations

import json
import socket
import threading
import time
from pathlib import Path

import httpx
import pytest

from app import db, llm_client, llm_scheduler, settings_store
from app.llm_scheduler import Limits, Scheduler

MESSAGES = [{"role": "user", "content": "hi"}]


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _wait_for(predicate, timeout: float = 3.0) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate():
            return True
        time.sleep(0.01)
    return False


def _start(sched: Scheduler, role: str, slot: int | None, **kwargs):
    """acquire numa thread; `box` recebe o ticket ou o erro."""
    box: dict = {}

    def run() -> None:
        try:
            box["ticket"] = sched.acquire(role, slot, **kwargs)
        except llm_scheduler.SchedulerError as exc:
            box["error"] = exc

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return thread, box


def _waiting(sched: Scheduler) -> int:
    return len(sched.snapshot()["waiting"])


# ------------------------------------------------------------ fila e slots
def test_waiting_calls_are_admitted_by_priority_then_arrival():
    sched = Scheduler()
    sched.configure(Limits(aux_cap=1))
    current = sched.acquire("assistant", None)
    pending: dict[str, dict] = {}
    for role in ("summary", "chronicle", "choices", "image_prompt", "meta"):
        _, pending[role] = _start(sched, role, None)
        assert _wait_for(lambda n=len(pending): _waiting(sched) == n)

    admitted: list[str] = []
    for _ in range(len(pending)):
        sched.release(current, "ok")
        assert _wait_for(lambda: any("ticket" in box and role not in admitted
                                     for role, box in pending.items()))
        role = next(r for r, box in pending.items() if "ticket" in box and r not in admitted)
        admitted.append(role)
        current = pending[role]["ticket"]
    sched.release(current, "ok")

    assert admitted == ["choices", "image_prompt", "meta", "chronicle", "summary"]
    assert sched.snapshot()["aux_in_flight"] == 0


def test_one_aux_per_slot_and_a_free_slot_is_not_held_back():
    sched = Scheduler()
    held = sched.acquire("image_prompt", 3)
    waiter, box = _start(sched, "meta", 3)
    assert _wait_for(lambda: _waiting(sched) == 1)

    # summary tem prioridade menor que meta, mas o slot dela está livre.
    summary = sched.acquire("summary", 2, max_wait=1.0)
    assert summary.admitted
    assert "ticket" not in box
    assert sched.snapshot()["busy_slots"] == [2, 3]

    sched.release(held, "ok")
    waiter.join(2.0)
    assert box["ticket"].slot == 3
    sched.release(box["ticket"], "ok")
    sched.release(summary, "ok")
    assert sched.snapshot()["busy_slots"] == []


def test_narrative_never_waits_and_lowers_the_aux_cap():
    sched = Scheduler()
    first = sched.acquire("choices", 1)
    second = sched.acquire("chronicle", 2)
    narrative = sched.acquire("narrative", 0)
    assert narrative.admitted and narrative.waited_s == 0.0
    assert sched.snapshot()["aux_cap"] == 2

    waiter, box = _start(sched, "image_prompt", 3)
    assert _wait_for(lambda: _waiting(sched) == 1)
    time.sleep(0.15)
    assert "ticket" not in box

    sched.release(narrative, "ok")
    waiter.join(2.0)
    assert "ticket" in box
    assert sched.snapshot()["aux_cap"] == 3
    for ticket in (first, second, box["ticket"]):
        sched.release(ticket, "ok")


def test_cancel_drops_a_queued_call():
    sched = Scheduler()
    held = sched.acquire("choices", 1)
    cancel = threading.Event()
    waiter, box = _start(sched, "choices", 1, cancel=cancel)
    assert _wait_for(lambda: _waiting(sched) == 1)
    cancel.set()
    waiter.join(2.0)
    assert isinstance(box["error"], llm_scheduler.Cancelled)
    assert sched.snapshot()["waiting"] == []
    sched.release(held, "ok")


def test_queue_timeout_does_not_count_for_the_breaker():
    sched = Scheduler()
    sched.configure(Limits(breaker_threshold=1))
    held = sched.acquire("choices", 1)
    started = time.monotonic()
    with pytest.raises(llm_scheduler.QueueTimeout):
        sched.acquire("choices", 1, max_wait=0.2)
    assert 0.15 <= time.monotonic() - started < 1.0
    assert sched.snapshot()["breaker"] == {"state": "closed", "failures": 0, "until": None}
    sched.release(held, "ok")


def test_release_is_idempotent():
    sched = Scheduler()
    ticket = sched.acquire("choices", 1)
    sched.release(ticket, "ok")
    sched.release(ticket, "ok")
    assert sched.snapshot()["aux_in_flight"] == 0


# ----------------------------------------------------------------- breaker
def test_breaker_opens_after_three_timeouts_and_recovers():
    clock = FakeClock()
    sched = Scheduler(clock=clock)
    events: list[tuple[str, dict]] = []
    sched.add_listener(lambda type_, data: events.append((type_, data)))

    for _ in range(2):
        sched.release(sched.acquire("chronicle", 2), "timeout")
    assert sched.snapshot()["breaker"]["state"] == "closed"
    sched.release(sched.acquire("chronicle", 2), "timeout")

    breaker = sched.snapshot()["breaker"]
    assert breaker["state"] == "open" and breaker["failures"] == 3
    assert events[-1][0] == "llm_degraded"
    assert events[-1][1]["reason"] == "3 consecutive llm timeouts"
    assert events[-1][1]["until"].endswith("Z")
    with pytest.raises(llm_scheduler.Degraded):
        sched.acquire("choices", 1)
    narrative = sched.acquire("narrative", 0)
    assert narrative.admitted

    clock.advance(61)
    assert sched.snapshot()["breaker"]["state"] == "half_open"
    sched.release(sched.acquire("chronicle", 2), "timeout")
    assert sched.snapshot()["breaker"]["state"] == "open"
    assert [type_ for type_, _ in events].count("llm_degraded") == 2

    clock.advance(61)
    sched.release(sched.acquire("choices", 1), "ok")
    assert sched.snapshot()["breaker"] == {"state": "closed", "failures": 0, "until": None}
    assert events[-1] == ("llm_recovered", {})
    sched.release(narrative, "ok")


def test_an_ok_in_between_resets_the_count():
    sched = Scheduler(clock=FakeClock())
    for outcome in ("timeout", "timeout", "ok", "timeout", "timeout"):
        sched.note_outcome(outcome)
    assert sched.snapshot()["breaker"] == {"state": "closed", "failures": 2, "until": None}


def test_cancel_and_errors_do_not_count():
    sched = Scheduler(clock=FakeClock())
    for outcome in ("cancelled", "error", "preempted", "aborted", "queue_timeout"):
        sched.note_outcome(outcome)
    assert sched.snapshot()["breaker"]["failures"] == 0


def test_a_failing_listener_does_not_break_the_others():
    sched = Scheduler(clock=FakeClock())
    sched.configure(Limits(breaker_threshold=1))
    seen: list[str] = []

    def boom(type_: str, data: dict) -> None:
        raise RuntimeError("listener quebrado")

    sched.add_listener(boom)
    sched.add_listener(lambda type_, data: seen.append(type_))
    sched.note_outcome("stalled")
    assert seen == ["llm_degraded"]


def test_limits_come_from_the_settings_block():
    limits = Limits.from_settings({"aux_cap": 4, "aux_cap_during_narrative": "1",
                                   "breaker": {"threshold": 5, "open_s": 30}})
    assert limits == Limits(aux_cap=4, aux_cap_during_narrative=1,
                            breaker_threshold=5, breaker_open_s=30.0)
    assert Limits.from_settings({"aux_cap": "x", "breaker": None}) == Limits()
    assert Limits.from_settings(None) == Limits()


def test_scheduler_settings_reach_the_llm_client(tmp_db):
    with db.session() as conn:
        cfg = settings_store.effective_llm_settings(conn)
    sched = cfg["scheduler"]
    assert sched["aux_cap"] == 3
    assert sched["aux_cap_during_narrative"] == 2
    assert sched["breaker"] == {"threshold": 3, "open_s": 60}
    assert Path(sched["stall_hint_path"]).is_absolute()
    assert "timeouts" not in sched and "note" not in sched
    assert llm_client._stall_limits(cfg) == (30.0, 15.0)
    assert llm_client._stall_limits({"scheduler": {"stall": {"enabled": False}}}) is None


# --------------------------------------------------------------- preempção
def test_preempt_flags_the_victim_and_holds_the_slot():
    sched = Scheduler()
    victim = sched.acquire("image_prompt", 3)
    with sched.preempt(3):
        assert victim.preempt.is_set()
        sched.release(victim, "preempted")
        waiter, box = _start(sched, "meta", 3)
        assert _wait_for(lambda: _waiting(sched) == 1)
        time.sleep(0.15)
        assert "ticket" not in box
        assert sched.snapshot()["reserved_slots"] == [3]
    waiter.join(2.0)
    assert "ticket" in box
    assert sched.snapshot()["reserved_slots"] == []
    sched.release(box["ticket"], "ok")


# ------------------------------------------------- llm_client (mock httpx)
def _cfg(base_url: str = "http://fake:1234/v1", **extra) -> dict:
    cfg = {"base_url": base_url, "model_alias": "mock-model", "disable_thinking": True,
           "timeout_seconds": 30}
    cfg.update(extra)
    return cfg


def _transport(total_slots: int = 4, capture: list | None = None) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/props"):
            return httpx.Response(200, json={"total_slots": total_slots})
        payload = json.loads(request.content.decode("utf-8"))
        if capture is not None:
            capture.append(payload)
        if payload.get("stream"):
            body = (f"data: {json.dumps({'choices': [{'delta': {'content': 'Ok.'}}]})}\n\n"
                    "data: [DONE]\n\n")
            return httpx.Response(200, headers={"content-type": "text/event-stream"}, text=body)
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    return httpx.MockTransport(handler)


def test_open_breaker_fails_aux_calls_fast_but_not_the_narrative():
    for _ in range(3):
        llm_scheduler.note_outcome("timeout")
    capture: list = []
    transport = _transport(capture=capture)
    with pytest.raises(llm_client.LlmDegraded) as exc:
        llm_client.chat_completion(_cfg(), MESSAGES, role="choices", transport=transport)
    assert exc.value.code == "llm_degraded"
    assert capture == []
    assert llm_client.recent_calls(1)[0]["outcome"] == "degraded"

    tokens = list(llm_client.stream_chat_completion(_cfg(), MESSAGES, sampler={}, stop=[],
                                                    transport=transport))
    assert tokens == ["Ok."]
    assert llm_scheduler.snapshot()["breaker"]["state"] == "closed"


def test_busy_slot_becomes_a_queue_timeout_that_does_not_trip_the_breaker():
    transport = _transport()
    held = llm_scheduler.acquire("choices", 1)
    try:
        for _ in range(4):
            with pytest.raises(llm_client.LlmQueueTimeout) as exc:
                llm_client.chat_completion(_cfg(), MESSAGES, role="choices", timeout_s=0.1,
                                           transport=transport)
            assert exc.value.code == "llm_timeout"
    finally:
        llm_scheduler.release(held, "ok")
    assert llm_scheduler.snapshot()["breaker"]["state"] == "closed"
    assert llm_client.recent_calls(1)[0]["outcome"] == "queue_timeout"
    assert llm_client.chat_completion(_cfg(), MESSAGES, role="choices",
                                      transport=transport) == "ok"


def test_stall_settings_add_return_progress_only_to_the_narrative():
    capture: list = []
    transport = _transport(capture=capture)
    list(llm_client.stream_chat_completion(_cfg(), MESSAGES, sampler={}, stop=[],
                                           transport=transport))
    llm_client.chat_completion(_cfg(), MESSAGES, role="chronicle", transport=transport)
    off = _cfg(scheduler={"stall": {"enabled": False}})
    list(llm_client.stream_chat_completion(off, MESSAGES, sampler={}, stop=[],
                                           transport=transport))
    narrative, chronicle, narrative_off = capture
    assert narrative["return_progress"] is True
    assert "return_progress" not in chronicle
    assert "return_progress" not in narrative_off


# ------------------------------------------- llama-server de mentira (socket)
class _LlamaServer:
    """Cada POST /chat/completions consome o próximo passo de `script`:
    ("json", texto), ("hang",), ("sse_silent",) ou ("sse", itens), com itens
    str (token), ("progress", n) ou ("sleep", s)."""

    def __init__(self, script: list[tuple], total_slots: int = 4) -> None:
        self.script = list(script)
        self.total_slots = total_slots
        self.requests: list[dict] = []
        self.disconnects = 0
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind(("127.0.0.1", 0))
        self._sock.listen(16)
        self.port = self._sock.getsockname()[1]
        threading.Thread(target=self._serve, daemon=True).start()

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}/v1"

    def close(self) -> None:
        self._stop.set()
        self._sock.close()

    def _serve(self) -> None:
        while not self._stop.is_set():
            try:
                conn, _ = self._sock.accept()
            except OSError:
                return
            threading.Thread(target=self._handle, args=(conn,), daemon=True).start()

    @staticmethod
    def _read_request(conn: socket.socket) -> tuple[str, bytes]:
        data = b""
        while b"\r\n\r\n" not in data:
            chunk = conn.recv(65536)
            if not chunk:
                break
            data += chunk
        head, _, body = data.partition(b"\r\n\r\n")
        length = 0
        for line in head.split(b"\r\n")[1:]:
            name, _, value = line.partition(b":")
            if name.strip().lower() == b"content-length":
                length = int(value.strip())
        while len(body) < length:
            chunk = conn.recv(65536)
            if not chunk:
                break
            body += chunk
        return head.split(b"\r\n")[0].decode(), body

    @staticmethod
    def _send_json(conn: socket.socket, data: dict) -> None:
        body = json.dumps(data).encode()
        conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
                     + f"Content-Length: {len(body)}\r\n".encode()
                     + b"Connection: close\r\n\r\n" + body)

    def _wait_disconnect(self, conn: socket.socket) -> None:
        conn.settimeout(0.05)
        while not self._stop.is_set():
            try:
                if conn.recv(1) == b"":
                    break
            except socket.timeout:
                continue
            except OSError:
                break
        with self._lock:
            self.disconnects += 1

    def _handle(self, conn: socket.socket) -> None:
        with conn:
            request_line, body = self._read_request(conn)
            if "/props" in request_line:
                self._send_json(conn, {"total_slots": self.total_slots})
                return
            with self._lock:
                self.requests.append(json.loads(body or b"{}"))
                step = self.script.pop(0) if self.script else ("json", "ok")
            kind = step[0]
            if kind == "json":
                self._send_json(conn, {"choices": [{"message": {"content": step[1]},
                                                    "finish_reason": "stop"}]})
                return
            if kind in ("sse", "sse_silent"):
                conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\n"
                             b"Connection: close\r\n\r\n")
            if kind != "sse":
                self._wait_disconnect(conn)
                return
            try:
                for item in step[1]:
                    if isinstance(item, tuple) and item[0] == "sleep":
                        time.sleep(item[1])
                        continue
                    if isinstance(item, tuple) and item[0] == "progress":
                        chunk = {"choices": [{"delta": {"role": "assistant", "content": None}}],
                                 "prompt_progress": {"total": 100, "cache": 0,
                                                     "processed": item[1], "time_ms": 1}}
                    else:
                        chunk = {"choices": [{"delta": {"content": item}}]}
                    conn.sendall(f"data: {json.dumps(chunk)}\n\n".encode())
                done = {"choices": [{"delta": {}, "finish_reason": "stop"}]}
                conn.sendall(f"data: {json.dumps(done)}\n\ndata: [DONE]\n\n".encode())
            except OSError:
                with self._lock:
                    self.disconnects += 1


@pytest.fixture()
def llama():
    servers: list[_LlamaServer] = []

    def make(script: list[tuple], total_slots: int = 4) -> _LlamaServer:
        server = _LlamaServer(script, total_slots)
        servers.append(server)
        return server

    yield make
    for server in servers:
        server.close()


FAST_STALL = {"stall": {"first_token_s": 1.0, "progress_s": 0.8}}


def _narrate(cfg: dict, state: llm_client.StreamState | None = None) -> str:
    return "".join(llm_client.stream_chat_completion(cfg, MESSAGES, sampler={}, stop=[],
                                                     state=state))


def test_role_deadline_starts_at_admission(llama):
    server = llama([("hang",)])
    cfg = _cfg(server.base_url)
    held = llm_scheduler.acquire("choices", 1)

    def release_after_queue() -> None:
        assert _wait_for(lambda: bool(llm_scheduler.snapshot()["waiting"]))
        time.sleep(0.3)
        llm_scheduler.release(held, "ok")

    threading.Thread(target=release_after_queue, daemon=True).start()
    started = time.monotonic()
    with pytest.raises(llm_client.LlmTimeout) as exc:
        llm_client.chat_completion(cfg, MESSAGES, role="choices", timeout_s=0.6)
    elapsed = time.monotonic() - started
    assert not isinstance(exc.value, llm_client.LlmQueueTimeout)
    assert 0.9 <= elapsed < 2.0
    last = llm_client.recent_calls(1)[0]
    assert last["outcome"] == "timeout"
    assert 0.2 <= last["waited_s"] < 0.8


def test_preempted_aux_is_retried_once(llama):
    server = llama([("hang",), ("json", "second try")])
    cfg = _cfg(server.base_url)
    box: dict = {}

    def run() -> None:
        box["text"] = llm_client.chat_completion(cfg, MESSAGES, role="image_prompt",
                                                 timeout_s=5)

    worker = threading.Thread(target=run, daemon=True)
    worker.start()
    assert _wait_for(lambda: len(server.requests) == 1)
    with llm_scheduler.preempt(3):
        assert _wait_for(lambda: server.disconnects == 1)
        time.sleep(0.2)
        assert len(server.requests) == 1
    worker.join(3.0)
    assert box["text"] == "second try"
    assert [c["outcome"] for c in llm_client.recent_calls(2)] == ["preempted", "ok"]


def test_stalled_narrative_is_retried_on_the_image_prompt_slot(llama, tmp_path):
    hint = tmp_path / "hint.json"
    server = llama([("sse_silent",), ("sse", ["Lá", " fora."])])
    cfg = _cfg(server.base_url, scheduler={**FAST_STALL, "stall_hint_path": str(hint)})
    state = llm_client.StreamState()

    assert _narrate(cfg, state) == "Lá fora."

    first, retry = server.requests
    assert first["id_slot"] == 0 and first["cache_prompt"] is True
    assert first["return_progress"] is True
    assert retry["id_slot"] == 3 and retry["cache_prompt"] is False
    assert state.fallback is True and state.slot == 3
    data = json.loads(hint.read_text())
    assert data["slot"] == 0 and data["reason"] == "narrative_stalled"
    calls = [(c["outcome"], c["slot"], c["fallback"]) for c in llm_client.recent_calls(2)]
    assert calls == [("stalled", 0, False), ("ok", 3, True)]
    assert server.disconnects == 1


def test_prompt_progress_keeps_a_slow_narrative_alive(llama):
    script = [("sse", [("progress", 0), ("sleep", 0.25), ("progress", 40), ("sleep", 0.25),
                       ("progress", 80), ("sleep", 0.25), "Ok."])]
    server = llama(script)
    cfg = _cfg(server.base_url, scheduler={"stall": {"first_token_s": 0.6, "progress_s": 0.5}})
    state = llm_client.StreamState()
    assert _narrate(cfg, state) == "Ok."
    assert len(server.requests) == 1
    assert state.fallback is False
    assert state.progress["processed"] == 80


def test_fallback_takes_the_slot_from_a_running_aux(llama):
    server = llama([("hang",), ("sse_silent",), ("sse", ["Ok."]), ("json", "after")])
    cfg = _cfg(server.base_url, scheduler=FAST_STALL)
    box: dict = {}

    def run() -> None:
        box["text"] = llm_client.chat_completion(cfg, MESSAGES, role="image_prompt",
                                                 timeout_s=10)

    worker = threading.Thread(target=run, daemon=True)
    worker.start()
    assert _wait_for(lambda: len(server.requests) == 1)

    assert _narrate(cfg) == "Ok."
    worker.join(3.0)

    assert box["text"] == "after"
    slots = [request.get("id_slot") for request in server.requests]
    assert slots == [3, 0, 3, 3]
    assert server.disconnects == 2


def test_two_slot_server_falls_back_to_slot_one(llama):
    server = llama([("sse_silent",), ("sse", ["Ok."])], total_slots=2)
    cfg = _cfg(server.base_url, scheduler=FAST_STALL)
    assert _narrate(cfg) == "Ok."
    assert [request["id_slot"] for request in server.requests] == [0, 1]


def test_single_slot_server_has_no_fallback(llama, tmp_path):
    hint = tmp_path / "hint.json"
    server = llama([("sse_silent",)], total_slots=1)
    cfg = _cfg(server.base_url, scheduler={**FAST_STALL, "stall_hint_path": str(hint)})
    with pytest.raises(llm_client.LlmStalled):
        _narrate(cfg)
    assert len(server.requests) == 1
    assert "id_slot" not in server.requests[0]
    assert not hint.exists()
    assert llm_scheduler.snapshot()["breaker"]["failures"] == 1


def test_without_slots_only_the_global_cap_applies(llama):
    server = llama([("hang",)] * 3, total_slots=1)
    cfg = _cfg(server.base_url, scheduler={"aux_cap": 2})
    cancel = threading.Event()
    errors: list[Exception] = []

    def run(role: str) -> None:
        try:
            llm_client.chat_completion(cfg, MESSAGES, role=role, cancel=cancel, timeout_s=10)
        except llm_client.LlmError as exc:
            errors.append(exc)

    workers = [threading.Thread(target=run, args=(role,), daemon=True)
               for role in ("chronicle", "summary", "assistant")]
    for worker in workers:
        worker.start()
    assert _wait_for(lambda: len(server.requests) == 2)
    time.sleep(0.2)
    assert len(server.requests) == 2
    assert len(llm_scheduler.snapshot()["waiting"]) == 1

    cancel.set()
    for worker in workers:
        worker.join(3.0)
    assert len(errors) == 3
    assert all(isinstance(exc, llm_client.LlmCancelled) for exc in errors)
    assert llm_scheduler.snapshot()["aux_in_flight"] == 0
