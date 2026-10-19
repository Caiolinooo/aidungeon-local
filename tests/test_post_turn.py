"""Pós-turno da G3: `turn_done`, choices pré-geradas (C7) e chronicle → summary.

Os testes do pipeline ligam `AIDUNGEON_CHRONICLE=1` (o modo de fundo de
verdade) e trocam chronicle, summary e vetor das memórias por gravadores.
"""

from __future__ import annotations

import json
import threading
import time

import httpx
import pytest

from app import choices_service, db, deps, events, game_engine, llm_client
from app import npc_chronicle, settings_store
from conftest import add_action, make_adventure, sse_body
from memory import summarizer

CHOICES = [{"type": "do", "text": "knock"}, {"type": "say", "text": "hello"},
           {"type": "story", "text": "rain"}]


class _Llama:
    """llama-server fake: narrativa em SSE, auxiliares em JSON. `gate` segura
    as auxiliares até ser setado."""

    def __init__(self) -> None:
        self.aux_calls: list[dict] = []
        self.gate = threading.Event()
        self.gate.set()
        self.entered = threading.Event()

    def transport(self) -> httpx.MockTransport:
        def handler(request: httpx.Request) -> httpx.Response:
            path = request.url.path
            if path.endswith("/props"):
                return httpx.Response(200, json={"total_slots": 5})
            if path.endswith("/models"):
                return httpx.Response(200, json={"data": [{"id": "mock-model"}]})
            if path.endswith("/chat/completions"):
                payload = json.loads(request.content.decode("utf-8"))
                if payload.get("stream"):
                    return httpx.Response(200, headers={"content-type": "text/event-stream"},
                                          text=sse_body(("The gate", " opens.")))
                self.aux_calls.append(payload)
                self.entered.set()
                self.gate.wait(5)
                return httpx.Response(200, json={
                    "choices": [{"message": {"content": json.dumps(CHOICES)}}]})
            return httpx.Response(404, json={"error": f"no mock route: {path}"})

        return httpx.MockTransport(handler)


class _Pipeline:
    """Gravador do pós-turno de fundo. `chronicle_gate` segura o chronicle."""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.cancels: list[threading.Event | None] = []
        self.chronicle_gate = threading.Event()
        self.chronicle_gate.set()
        self.chronicle_entered = threading.Event()
        self.choices_started: list[str] = []

    def chronicle(self, adventure_id: str, transport=None) -> None:
        self.calls.append("chronicle")
        self.chronicle_entered.set()
        self.chronicle_gate.wait(5)

    def summary(self, adventure_id: str, cancel: threading.Event | None = None) -> None:
        self.calls.append("summary")
        self.cancels.append(cancel)

    def wait_idle(self, adventure_id: str, calls: int) -> None:
        _wait(lambda: len(self.calls) >= calls
              and adventure_id not in game_engine._post_turn_cancels)


def _wait(predicate, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError("condição não chegou a tempo")
        time.sleep(0.01)


@pytest.fixture()
def pipeline(monkeypatch, tmp_db):
    monkeypatch.setenv("AIDUNGEON_CHRONICLE", "1")
    rec = _Pipeline()
    monkeypatch.setattr(npc_chronicle, "update_from_turn", rec.chronicle)
    monkeypatch.setattr(game_engine, "_maybe_update_memory", rec.summary)
    monkeypatch.setattr(game_engine, "_prime_memory_query", lambda adventure_id: None)
    yield rec
    rec.chronicle_gate.set()
    with game_engine._post_turn_guard:
        game_engine._post_turn_cancels.clear()
        game_engine._summary_deferrals.clear()


@pytest.fixture()
def no_choices(monkeypatch, pipeline):
    monkeypatch.setattr(choices_service, "start",
                        lambda adventure_id, transport=None: pipeline.choices_started.append(
                            adventure_id))
    return pipeline


def _adventure() -> dict:
    adv = make_adventure()
    add_action(adv["id"], 0, "start", "The gate is shut.")
    return adv


def _play(adventure_id: str, transport, text: str = "knock") -> dict:
    out = list(game_engine.handle_action(adventure_id, "do", text, transport=transport))
    assert out[-1]["event"] == "done", out[-1]
    return out[-1]["data"]


def _next_of(sub: events.Subscription, type_: str) -> events.Event:
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        event = sub.get(0.2)
        if event is not None and event.type == type_:
            return event
    raise AssertionError(f"evento {type_} não chegou")


def _drain(sub: events.Subscription) -> list[str]:
    types = []
    while (event := sub.get(0.05)) is not None:
        types.append(event.type)
    return types


def _override(client, transport) -> None:
    client.app.dependency_overrides[deps.get_http_transport] = lambda: transport


# --------------------------------------------------------------- turn_done
def test_turn_done_comes_before_done(tmp_db):
    adv = _adventure()
    transport = _Llama().transport()
    with events.subscribe(adv["id"]) as sub:
        for event in game_engine.handle_action(adv["id"], "do", "knock", transport=transport):
            if event["event"] == "done":
                turn = sub.get(0)
                break
    action = event["data"]["action"]
    assert turn is not None and turn.type == "turn_done"
    assert turn.data == {"action_id": action["id"], "seq": action["seq"]}


# ----------------------------------------------------------------- choices
def test_choices_are_pregenerated_and_served_from_cache(client, pipeline):
    adv = _adventure()
    llama = _Llama()
    transport = llama.transport()
    _override(client, transport)
    with events.subscribe(adv["id"]) as sub:
        done = _play(adv["id"], transport)
        ready = _next_of(sub, "choices_ready")
    assert ready.data == {"for_seq": done["action"]["seq"], "choices": CHOICES}
    resp = client.post(f"/api/adventures/{adv['id']}/choices")
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"choices": CHOICES}
    assert len(llama.aux_calls) == 1
    assert llama.aux_calls[0]["id_slot"] == 1
    assert llama.aux_calls[0]["cache_prompt"] is False
    pipeline.wait_idle(adv["id"], calls=2)


def test_choices_wait_is_bounded_and_the_result_still_arrives(client, monkeypatch):
    settings_store.load_file_settings()
    monkeypatch.setitem(settings_store._file_cache, "choices", {"enabled": True, "wait_s": 0.2})
    adv = _adventure()
    llama = _Llama()
    llama.gate.clear()
    _override(client, llama.transport())
    with events.subscribe(adv["id"]) as sub:
        started = time.monotonic()
        resp = client.post(f"/api/adventures/{adv['id']}/choices")
        assert time.monotonic() - started < 2.0
        assert resp.status_code == 503
        assert resp.json()["error"]["code"] == "llm_timeout"
        llama.gate.set()
        ready = _next_of(sub, "choices_ready")
    assert ready.data["choices"] == CHOICES
    resp = client.post(f"/api/adventures/{adv['id']}/choices")
    assert resp.json() == {"choices": CHOICES}
    assert len(llama.aux_calls) == 1


def test_failed_generation_is_published_and_retried_on_demand(client):
    adv = _adventure()
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/props"):
            return httpx.Response(200, json={"total_slots": 5})
        if path.endswith("/models"):
            return httpx.Response(200, json={"data": [{"id": "mock-model"}]})
        calls.append(path)
        content = "not json" if len(calls) == 1 else json.dumps(CHOICES)
        return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})

    _override(client, httpx.MockTransport(handler))
    with events.subscribe(adv["id"]) as sub:
        resp = client.post(f"/api/adventures/{adv['id']}/choices")
        failed = _next_of(sub, "choices_failed")
    assert resp.status_code == 503
    assert resp.json()["error"]["code"] == "generation_failed"
    assert failed.data["code"] == "generation_failed" and failed.data["for_seq"] == 0
    resp = client.post(f"/api/adventures/{adv['id']}/choices")
    assert resp.json() == {"choices": CHOICES}
    assert len(calls) == 2


def test_new_action_cancels_pending_choices(tmp_db):
    adv = _adventure()
    llama = _Llama()
    llama.gate.clear()
    transport = llama.transport()
    with events.subscribe(adv["id"]) as sub:
        entry = choices_service.start(adv["id"], transport=transport)
        assert llama.entered.wait(5)
        _play(adv["id"], transport)
        assert entry.cancel.is_set()
        llama.gate.set()
        assert entry.done.wait(5)
        types = _drain(sub)
    assert entry.error[0] == "llm_cancelled"
    assert "choices_ready" not in types and "choices_failed" not in types


def test_retry_undo_and_edit_never_reuse_old_choices(client):
    adv = _adventure()
    llama = _Llama()
    transport = llama.transport()
    _override(client, transport)
    url = f"/api/adventures/{adv['id']}/choices"
    _play(adv["id"], transport)
    assert client.post(url).status_code == 200
    assert client.post(url).status_code == 200
    assert len(llama.aux_calls) == 1
    out = list(game_engine.handle_retry(adv["id"], transport=transport))
    assert out[-1]["event"] == "done"
    assert client.post(url).status_code == 200
    assert len(llama.aux_calls) == 2
    edited = out[-1]["data"]["action"]
    assert client.patch(f"/api/actions/{edited['id']}", json={"text": "The gate creaks."}) \
        .status_code == 200
    assert client.post(url).status_code == 200
    assert len(llama.aux_calls) == 3
    assert client.post(f"/api/adventures/{adv['id']}/undo").status_code == 200
    assert client.post(url).status_code == 200
    assert len(llama.aux_calls) == 4


def test_choices_disabled_skip_pregeneration(pipeline, monkeypatch):
    settings_store.load_file_settings()
    monkeypatch.setitem(settings_store._file_cache, "choices", {"enabled": False})
    adv = _adventure()
    llama = _Llama()
    _play(adv["id"], llama.transport())
    pipeline.wait_idle(adv["id"], calls=2)
    assert llama.aux_calls == []
    assert choices_service.start(adv["id"]) is None


def test_deleting_the_adventure_forgets_choices_and_events(client):
    adv = _adventure()
    _override(client, _Llama().transport())
    assert client.post(f"/api/adventures/{adv['id']}/choices").status_code == 200
    sub = events.subscribe(adv["id"])
    assert client.delete(f"/api/adventures/{adv['id']}").status_code == 200
    assert sub.closed
    assert adv["id"] not in choices_service._entries


# ------------------------------------------------------- chronicle → summary
def test_post_turn_runs_chronicle_then_summary(no_choices):
    adv = _adventure()
    _play(adv["id"], _Llama().transport())
    no_choices.wait_idle(adv["id"], calls=2)
    assert no_choices.calls == ["chronicle", "summary"]
    assert no_choices.choices_started == [adv["id"]]
    cancel = no_choices.cancels[0]
    assert isinstance(cancel, threading.Event) and not cancel.is_set()


def test_new_action_skips_the_summary_but_not_the_chronicle(no_choices):
    adv = _adventure()
    transport = _Llama().transport()
    no_choices.chronicle_gate.clear()
    _play(adv["id"], transport)
    assert no_choices.chronicle_entered.wait(5)
    _play(adv["id"], transport, text="push the gate")
    no_choices.chronicle_gate.set()
    no_choices.wait_idle(adv["id"], calls=3)
    assert no_choices.calls == ["chronicle", "chronicle", "summary"]
    assert adv["id"] not in game_engine._summary_deferrals


def test_fast_player_does_not_starve_the_summary(no_choices):
    adv = _adventure()
    transport = _Llama().transport()
    expected: list[str] = []
    for summary_runs in (False, False, True):
        no_choices.chronicle_gate.clear()
        no_choices.chronicle_entered.clear()
        game_engine._spawn_post_turn(adv["id"], transport)
        assert no_choices.chronicle_entered.wait(5)
        game_engine.cancel_post_turn(adv["id"])
        no_choices.chronicle_gate.set()
        expected += ["chronicle", "summary"] if summary_runs else ["chronicle"]
        no_choices.wait_idle(adv["id"], calls=len(expected))
    assert no_choices.calls == expected
    assert adv["id"] not in game_engine._summary_deferrals


# --------------------------------------------------------------- summarizer
class _DeferringLLM:
    def __init__(self, *, error: Exception | None = None,
                 cancel_on_call: threading.Event | None = None) -> None:
        self.error = error
        self.cancel_on_call = cancel_on_call
        self.calls: list[dict] = []

    def chat_completion(self, cfg, messages, *, role, temperature, max_tokens,
                        transport=None, cancel=None):
        self.calls.append({"role": role, "cancel": cancel})
        if self.error is not None:
            raise self.error
        if self.cancel_on_call is not None:
            self.cancel_on_call.set()
        return "You did things."


def _long_story(adventure_id: str) -> None:
    for seq in range(4):
        add_action(adventure_id, seq, "continue", f"Action {seq}. " + "x" * 580)


def _memory_cfg(conn) -> dict:
    cfg = settings_store.runtime_config(conn)
    cfg["memory_bank"].update({"trigger_tokens": 100})
    cfg["context_size"] = 3800 + 512 + 100
    return cfg


@pytest.mark.parametrize("error", [
    llm_client.LlmCancelled("llm call cancelled"),
    llm_client.LlmDegraded("llm circuit breaker open"),
    llm_client.LlmQueueTimeout("llm queue wait timed out"),
])
def test_deferred_summary_writes_nothing(tmp_db, error):
    adv = make_adventure()
    _long_story(adv["id"])
    llm = _DeferringLLM(error=error)
    cancel = threading.Event()
    with db.session() as conn:
        summarizer.maybe_update(adv["id"], conn=conn, llm_client=llm,
                                settings=_memory_cfg(conn), cancel=cancel)
        memories = db.list_memories(conn, adv["id"])
        covered = db.get_setting(conn, f"memory.summary_covered_seq.{adv['id']}")
        story = db.get_adventure(conn, adv["id"])["story_summary"]
    assert memories == []
    assert covered is None
    assert story == ""
    assert {call["role"] for call in llm.calls} == {"summary"}
    assert all(call["cancel"] is cancel for call in llm.calls)


def test_cancel_during_the_memory_bank_skips_the_story_summary(tmp_db):
    adv = make_adventure()
    _long_story(adv["id"])
    cancel = threading.Event()
    llm = _DeferringLLM(cancel_on_call=cancel)
    with db.session() as conn:
        summarizer.maybe_update(adv["id"], conn=conn, llm_client=llm,
                                settings=_memory_cfg(conn), cancel=cancel)
        memories = db.list_memories(conn, adv["id"])
        story = db.get_adventure(conn, adv["id"])["story_summary"]
    assert len(llm.calls) == 1
    assert [m["text"] for m in memories] == ["You did things."]
    assert story == ""
