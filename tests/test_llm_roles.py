"""Contrato C1 (roles, slots, prazos, cancelamento) e o hotfix G1.

- slot e cache_prompt por role com 1, 2, 3 e 4 slots (`GET /props`);
- prazo e cancelamento derrubam o socket de verdade (servidor local que
  nunca responde), sem esperar o timeout de leitura;
- choices, See e ui-profile não usam o lock de geração;
- See e narrador gravando juntos não colidem no seq;
- ganchos C3 (meta_commands, output_sanitizer).
"""

from __future__ import annotations

import json
import socket
import threading
import time

import httpx
import pytest

from app import (
    db,
    deps,
    game_engine,
    llm_client,
    meta_commands,
    output_sanitizer,
    settings_store,
)
from conftest import add_action, make_adventure, sse_body

PNG_B64 = ("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8"
           "z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==")

MESSAGES = [{"role": "user", "content": "hi"}]


def _cfg(base_url: str = "http://fake:1234/v1", **extra) -> dict:
    cfg = {"base_url": base_url, "model_alias": "auto", "disable_thinking": True,
           "timeout_seconds": 30}
    cfg.update(extra)
    return cfg


def _transport(*, total_slots: int | None, capture: list | None = None,
               aux_reply: str = "ok", props_status: int = 200,
               chat_error: Exception | None = None) -> httpx.MockTransport:
    """llama-server fake: /props, /models, /chat/completions (SSE ou JSON)."""
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/props"):
            if total_slots is None:
                return httpx.Response(props_status, json={})
            return httpx.Response(props_status, json={"total_slots": total_slots})
        if path.endswith("/models"):
            return httpx.Response(200, json={"data": [{"id": "mock-model"}]})
        if path.endswith("/chat/completions"):
            if chat_error is not None:
                raise chat_error
            payload = json.loads(request.content.decode("utf-8"))
            if capture is not None:
                capture.append(payload)
            if payload.get("stream"):
                return httpx.Response(200, headers={"content-type": "text/event-stream"},
                                      text=sse_body(("You", " wait.")))
            return httpx.Response(200, json={
                "choices": [{"message": {"content": aux_reply}}]})
        if path.endswith("/sdapi/v1/txt2img") or path.endswith("/sdapi/v1/img2img"):
            return httpx.Response(200, json={"images": [PNG_B64], "parameters": {}})
        return httpx.Response(404, json={"error": f"no mock route: {path}"})

    return httpx.MockTransport(handler)


def _aux_payload(cfg: dict, transport, role: str, capture: list) -> dict:
    llm_client.chat_completion(cfg, MESSAGES, role=role, transport=transport)
    return capture[-1]


def _narrative_payload(cfg: dict, transport, capture: list) -> dict:
    list(llm_client.stream_chat_completion(cfg, MESSAGES, sampler={}, stop=[],
                                           transport=transport))
    return capture[-1]


# ------------------------------------------------------------ slots por role
def test_four_slots_follow_the_c1_map():
    capture: list = []
    transport = _transport(total_slots=4, capture=capture)
    cfg = _cfg()
    expected = {"choices": 1, "chronicle": 2, "summary": 2,
                "image_prompt": 3, "assistant": 3, "meta": 3}
    for role, slot in expected.items():
        payload = _aux_payload(cfg, transport, role, capture)
        assert payload["id_slot"] == slot, role
        assert payload["cache_prompt"] is False, role
        assert payload["stream"] is False, role
    narrative = _narrative_payload(cfg, transport, capture)
    assert narrative["id_slot"] == 0
    assert narrative["cache_prompt"] is True
    assert narrative["stream"] is True


def test_two_slots_pin_every_aux_to_slot_one():
    capture: list = []
    transport = _transport(total_slots=2, capture=capture)
    cfg = _cfg()
    for role in ("choices", "chronicle", "summary", "image_prompt", "assistant", "meta"):
        assert _aux_payload(cfg, transport, role, capture)["id_slot"] == 1, role
    assert _narrative_payload(cfg, transport, capture)["id_slot"] == 0


def test_three_slots_keep_choices_apart():
    capture: list = []
    transport = _transport(total_slots=3, capture=capture)
    cfg = _cfg()
    assert _aux_payload(cfg, transport, "choices", capture)["id_slot"] == 1
    for role in ("chronicle", "summary", "image_prompt", "assistant", "meta"):
        assert _aux_payload(cfg, transport, role, capture)["id_slot"] == 2, role


def test_single_slot_server_gets_no_id_slot():
    capture: list = []
    transport = _transport(total_slots=1, capture=capture)
    cfg = _cfg()
    assert "id_slot" not in _aux_payload(cfg, transport, "choices", capture)
    assert "id_slot" not in _narrative_payload(cfg, transport, capture)


def test_props_failure_is_retried_after_a_short_ttl(monkeypatch):
    capture: list = []
    cfg = _cfg()
    broken = _transport(total_slots=None, props_status=500, capture=capture)
    healthy = _transport(total_slots=4, capture=capture)
    assert "id_slot" not in _aux_payload(cfg, broken, "choices", capture)
    assert "id_slot" not in _aux_payload(cfg, healthy, "choices", capture)
    llm_client.invalidate_slot_cache()
    monkeypatch.setattr(llm_client, "_SLOT_FAILURE_TTL_S", 0.0)
    assert "id_slot" not in _aux_payload(cfg, broken, "choices", capture)
    assert _aux_payload(cfg, healthy, "choices", capture)["id_slot"] == 1


def test_unreachable_server_drops_the_slot_count():
    capture: list = []
    cfg = _cfg()
    _aux_payload(cfg, _transport(total_slots=4, capture=capture), "summary", capture)
    down = _transport(total_slots=4, chat_error=httpx.ConnectError("refused"))
    with pytest.raises(llm_client.LlmUnreachable):
        llm_client.chat_completion(cfg, MESSAGES, role="summary", transport=down)
    two = _transport(total_slots=2, capture=capture)
    assert _aux_payload(cfg, two, "summary", capture)["id_slot"] == 1


def test_timeout_drops_the_slot_count():
    capture: list = []
    cfg = _cfg()
    _aux_payload(cfg, _transport(total_slots=4, capture=capture), "summary", capture)
    stuck = _transport(total_slots=4, chat_error=httpx.ReadTimeout("slot 2 is gone"))
    with pytest.raises(llm_client.LlmTimeout):
        llm_client.chat_completion(cfg, MESSAGES, role="summary", transport=stuck)
    two = _transport(total_slots=2, capture=capture)
    assert _aux_payload(cfg, two, "summary", capture)["id_slot"] == 1


def test_aux_cache_prompt_can_be_turned_back_on():
    capture: list = []
    transport = _transport(total_slots=4, capture=capture)
    payload = _aux_payload(_cfg(aux_cache_prompt=True), transport, "choices", capture)
    assert payload["cache_prompt"] is True


def test_unknown_role_is_rejected():
    with pytest.raises(ValueError):
        llm_client.chat_completion(_cfg(), MESSAGES, role="narrator",
                                   transport=_transport(total_slots=4))


def test_role_timeouts_come_from_settings(tmp_db):
    with db.session() as conn:
        cfg = settings_store.effective_llm_settings(conn)
    assert llm_client.role_timeout(cfg, "choices") == 15.0
    assert llm_client.role_timeout(cfg, "chronicle") == 45.0
    assert llm_client.role_timeout(cfg, "summary") == 60.0
    assert llm_client.role_timeout(cfg, "image_prompt") == 20.0
    assert cfg["aux_cache_prompt"] is False
    assert llm_client.role_timeout({"role_timeouts": {"choices": 7}}, "choices") == 7.0


def test_long_assistant_output_gets_a_longer_deadline():
    cfg = _cfg()
    assert llm_client.scaled_timeout(cfg, "assistant", 100) == 90.0
    assert llm_client.scaled_timeout(cfg, "assistant", 2500) > 180.0


def test_timeout_error_maps_to_llm_timeout():
    transport = _transport(total_slots=4, chat_error=httpx.ReadTimeout("slow"))
    with pytest.raises(llm_client.LlmTimeout) as exc:
        llm_client.chat_completion(_cfg(), MESSAGES, role="choices", transport=transport)
    assert exc.value.code == "llm_timeout"


def test_calls_are_recorded_for_health():
    transport = _transport(total_slots=4)
    llm_client.chat_completion(_cfg(), MESSAGES, role="chronicle", transport=transport)
    last = llm_client.recent_calls(1)[0]
    assert last["role"] == "chronicle"
    assert last["slot"] == 2
    assert last["outcome"] == "ok"


# ------------------------------------------------- socket real: prazo/cancel
class _SilentServer:
    """llama-server que aceita o POST e nunca responde (ou manda um token e
    para). Registra quando o cliente fecha a conexão."""

    def __init__(self, mode: str = "hang") -> None:
        self.mode = mode
        self.disconnected = threading.Event()
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind(("127.0.0.1", 0))
        self._sock.listen(8)
        self.port = self._sock.getsockname()[1]
        self._stop = threading.Event()
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
    def _read_request(conn: socket.socket) -> str:
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
        return head.split(b"\r\n")[0].decode()

    def _handle(self, conn: socket.socket) -> None:
        with conn:
            request_line = self._read_request(conn)
            if "/props" in request_line:
                body = json.dumps({"total_slots": 4}).encode()
                conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
                             + f"Content-Length: {len(body)}\r\n".encode()
                             + b"Connection: close\r\n\r\n" + body)
                return
            if self.mode == "partial_sse":
                token = json.dumps({"choices": [{"delta": {"content": "You"}}]})
                conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\n"
                             b"Connection: close\r\n\r\n"
                             + f"data: {token}\n\n".encode())
            conn.settimeout(0.05)
            while not self._stop.is_set():
                try:
                    if conn.recv(1) == b"":
                        break
                except socket.timeout:
                    continue
                except OSError:
                    break
            self.disconnected.set()


@pytest.fixture()
def silent_server():
    servers: list[_SilentServer] = []

    def make(mode: str = "hang") -> _SilentServer:
        server = _SilentServer(mode)
        servers.append(server)
        return server

    yield make
    for server in servers:
        server.close()


def test_deadline_closes_the_socket_before_any_response(silent_server):
    server = silent_server()
    cfg = _cfg(server.base_url, model_alias="mock-model")
    started = time.monotonic()
    with pytest.raises(llm_client.LlmTimeout):
        llm_client.chat_completion(cfg, MESSAGES, role="choices", timeout_s=0.5)
    elapsed = time.monotonic() - started
    # O timeout de leitura de reserva só dispararia em 1,5 s.
    assert elapsed < 1.3
    assert server.disconnected.wait(2.0)


def test_cancel_closes_the_socket_right_away(silent_server):
    server = silent_server()
    cfg = _cfg(server.base_url, model_alias="mock-model")
    cancel = threading.Event()
    threading.Timer(0.3, cancel.set).start()
    started = time.monotonic()
    with pytest.raises(llm_client.LlmCancelled):
        llm_client.chat_completion(cfg, MESSAGES, role="summary", cancel=cancel)
    assert time.monotonic() - started < 1.5
    assert server.disconnected.wait(2.0)


def test_cancelled_stream_is_not_taken_as_complete(silent_server):
    server = silent_server("partial_sse")
    cfg = _cfg(server.base_url, model_alias="mock-model")
    cancel = threading.Event()
    tokens: list[str] = []
    with pytest.raises(llm_client.LlmCancelled):
        for token in llm_client.stream_chat_completion(cfg, MESSAGES, sampler={}, stop=[],
                                                       cancel=cancel):
            tokens.append(token)
            cancel.set()
    assert tokens == ["You"]
    assert server.disconnected.wait(2.0)


# ----------------------------------------------- rotas sem lock de geração
def _override(client, transport) -> None:
    client.app.dependency_overrides[deps.get_http_transport] = lambda: transport


def test_choices_run_while_the_narrator_holds_the_lock(client):
    adv = make_adventure()
    add_action(adv["id"], 0, "start", "The gate is shut.")
    reply = json.dumps([{"type": "do", "text": "knock"}, {"type": "say", "text": "hello"},
                        {"type": "story", "text": "rain"}])
    capture: list = []
    _override(client, _transport(total_slots=4, capture=capture, aux_reply=reply))
    assert game_engine.acquire_generation_lock(adv["id"])
    try:
        resp = client.post(f"/api/adventures/{adv['id']}/choices")
    finally:
        game_engine.release_generation_lock(adv["id"])
    assert resp.status_code == 200, resp.text
    assert len(resp.json()["choices"]) == 3
    assert capture[-1]["id_slot"] == 1
    assert capture[-1]["cache_prompt"] is False


def test_choices_timeout_fails_clean(client):
    adv = make_adventure()
    add_action(adv["id"], 0, "start", "The gate is shut.")
    _override(client, _transport(total_slots=4, chat_error=httpx.ReadTimeout("slow")))
    resp = client.post(f"/api/adventures/{adv['id']}/choices")
    assert resp.status_code == 503
    assert resp.json()["error"]["code"] == "llm_timeout"
    events = list(game_engine.handle_continue(
        adv["id"], transport=_transport(total_slots=4)))
    assert events[-1]["event"] == "done"


def test_see_runs_while_the_narrator_holds_the_lock(client):
    adv = make_adventure()
    add_action(adv["id"], 0, "start", "A dark tower rises over the bay.")
    capture: list = []
    _override(client, _transport(total_slots=4, capture=capture,
                                 aux_reply="a dark tower over the bay at dusk"))
    assert game_engine.acquire_generation_lock(adv["id"])
    try:
        resp = client.post(f"/api/adventures/{adv['id']}/see", json={"action_seq": 0})
    finally:
        game_engine.release_generation_lock(adv["id"])
    assert resp.status_code == 200, resp.text
    assert resp.json()["action"]["type"] == "see"
    assert capture[-1]["id_slot"] == 3


def test_ui_profile_runs_while_the_narrator_holds_the_lock(client):
    adv = make_adventure()
    add_action(adv["id"], 0, "start", "You wake up in a cell.")
    profile = json.dumps({"tabs": ["do", "say", "story"], "widgets": []})
    _override(client, _transport(total_slots=4, aux_reply=profile))
    assert game_engine.acquire_generation_lock(adv["id"])
    try:
        resp = client.post(f"/api/adventures/{adv['id']}/ui-profile")
    finally:
        game_engine.release_generation_lock(adv["id"])
    assert resp.status_code != 409, resp.text


def test_see_landing_mid_turn_does_not_collide(client):
    adv = make_adventure()
    add_action(adv["id"], 0, "start", "A dark tower rises over the bay.")
    transport = _transport(total_slots=4, aux_reply="a dark tower over the bay")
    _override(client, transport)
    turn = game_engine.handle_action(adv["id"], "do", "look up", transport=transport)
    see = client.post(f"/api/adventures/{adv['id']}/see", json={"action_seq": 0})
    assert see.status_code == 200, see.text
    events = list(turn)
    assert events[-1]["event"] == "done", events
    with db.session() as conn:
        actions = db.list_actions(conn, adv["id"])
    assert [(a["seq"], a["type"]) for a in actions] == [
        (0, "start"), (1, "do"), (2, "see"), (3, "continue")]


def test_concurrent_appends_get_distinct_seqs(tmp_db):
    adv = make_adventure()
    add_action(adv["id"], 0, "start", "Opening.")
    errors: list[Exception] = []

    def worker(i: int) -> None:
        try:
            with game_engine.appending(adv["id"]) as conn:
                game_engine.append_action(conn, adv["id"], "see", f"see {i}")
        except Exception as exc:  # pragma: no cover - só aparece se colidir
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    with db.session() as conn:
        seqs = [a["seq"] for a in db.list_actions(conn, adv["id"])]
    assert seqs == list(range(9))


# ------------------------------------------------------------- ganchos C3
def test_meta_command_replaces_the_narrator(tmp_db, monkeypatch):
    adv = make_adventure()
    add_action(adv["id"], 0, "start", "Opening.")
    seen: list = []
    monkeypatch.setattr(meta_commands, "match",
                        lambda text, type_: meta_commands.MetaCommand("card", "Clara", text))

    def execute(adventure_id, cmd, transport=None):
        seen.append((adventure_id, cmd.kind, cmd.name))
        yield {"event": "done", "data": {"meta": cmd.kind}}

    monkeypatch.setattr(meta_commands, "execute", execute)
    capture: list = []
    events = list(game_engine.handle_action(
        adv["id"], "do", "/card Clara", transport=_transport(total_slots=4, capture=capture)))
    assert events == [{"event": "done", "data": {"meta": "card"}}]
    assert seen == [(adv["id"], "card", "Clara")]
    assert capture == []
    with db.session() as conn:
        assert [a["type"] for a in db.list_actions(conn, adv["id"])] == ["start"]
    assert game_engine.acquire_generation_lock(adv["id"])
    game_engine.release_generation_lock(adv["id"])


def test_meta_command_failure_becomes_an_error_event(tmp_db, monkeypatch):
    adv = make_adventure()
    add_action(adv["id"], 0, "start", "Opening.")
    monkeypatch.setattr(meta_commands, "match",
                        lambda text, type_: meta_commands.MetaCommand("portrait", "Clara", text))

    def execute(adventure_id, cmd, transport=None):
        raise llm_client.LlmTimeout("llm call timed out")
        yield  # pragma: no cover

    monkeypatch.setattr(meta_commands, "execute", execute)
    events = list(game_engine.handle_action(adv["id"], "do", "/foto Clara",
                                            transport=_transport(total_slots=4)))
    assert events == [{"event": "error", "data": {"message": "llm call timed out"}}]


def test_stub_meta_commands_never_match():
    assert meta_commands.match("/card Clara", "do") is None


def test_sanitizer_output_is_what_gets_saved(tmp_db, monkeypatch):
    adv = make_adventure()
    add_action(adv["id"], 0, "start", "Opening.")
    calls: list = []

    def clean(text, truncated):
        calls.append((text, truncated))
        return "Clean text."

    monkeypatch.setattr(output_sanitizer, "clean_narration", clean)
    events = list(game_engine.handle_continue(adv["id"], transport=_transport(total_slots=4)))
    done = events[-1]
    assert done["event"] == "done"
    assert done["data"]["action"]["text"] == "Clean text."
    assert calls == [("You wait.", False)]


def test_sanitizer_cuts_the_unfinished_sentence():
    assert output_sanitizer.clean_narration("She ran. And then", True) == "She ran."
    assert output_sanitizer.clean_narration("She ran. And then", False) == "She ran. And then"
    assert output_sanitizer.cut_incomplete_sentence("apesar") == "apesar"
