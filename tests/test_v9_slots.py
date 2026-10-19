"""Modo Turbo: id_slot só quando GET /props reporta total_slots >= 2."""

from __future__ import annotations

import json
import time

import httpx
import pytest

from app import llm_client
from conftest import sse_body

SAMPLER = {"temperature": 0.5, "max_tokens": 16, "id_slot": 99}


def _cfg() -> dict:
    return {
        "base_url": "http://llm.test/v1/",
        "model_alias": "mock-model",
        "timeout_seconds": 30,
        "api_key": "test-key",
        "disable_thinking": True,
    }


def make_v2_transport(*, props_status: int = 200, props_body: dict | None = None,
                      props_exc: Exception | None = None, props_raw: bytes | None = None,
                      props_hits: list | None = None, capture: list | None = None,
                      chat_content: str = "ok") -> httpx.MockTransport:
    """Mesmo formato do mock em test_v2 (models + chat stream/não-stream),
    mais GET /props para a sonda de slots."""
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/props":
            if props_hits is not None:
                props_hits.append(request)
            if props_exc is not None:
                raise props_exc
            if props_raw is not None:
                return httpx.Response(props_status, content=props_raw)
            if props_status != 200:
                return httpx.Response(props_status, json={"error": "nope"})
            body = {"total_slots": 2} if props_body is None else props_body
            return httpx.Response(200, json=body)
        if path.endswith("/models"):
            return httpx.Response(200, json={
                "object": "list",
                "data": [{"id": "mock-model", "object": "model"}],
            })
        if path.endswith("/chat/completions"):
            payload = json.loads(request.content.decode("utf-8"))
            if capture is not None:
                capture.append(payload)
            if payload.get("stream"):
                return httpx.Response(
                    200, headers={"content-type": "text/event-stream"},
                    text=sse_body(("Hi",)))
            return httpx.Response(200, json={
                "choices": [{"message": {"content": chat_content}}],
            })
        return httpx.Response(404, json={"error": f"no mock route: {path}"})

    return httpx.MockTransport(handler)


def _stream(transport) -> list[str]:
    return list(llm_client.stream_chat_completion(
        _cfg(), [{"role": "user", "content": "hi"}],
        sampler=SAMPLER, stop=[], transport=transport,
    ))


def _chat(transport) -> str:
    return llm_client.chat_completion(
        _cfg(), [{"role": "user", "content": "hi"}], transport=transport,
    )


def test_two_slots_pins_narrative_zero_and_aux_one():
    capture: list = []
    props_hits: list = []
    transport = make_v2_transport(props_body={"total_slots": 2}, capture=capture,
                                  props_hits=props_hits)
    assert _stream(transport) == ["Hi"]
    assert _chat(transport) == "ok"

    assert props_hits[0].url.path == "/props"
    assert str(props_hits[0].url) == "http://llm.test/props"
    assert props_hits[0].headers["authorization"] == "Bearer test-key"

    stream_payload, aux_payload = capture
    assert stream_payload["id_slot"] == 0
    assert stream_payload["cache_prompt"] is True
    assert aux_payload["id_slot"] == 1
    assert aux_payload["cache_prompt"] is True
    assert aux_payload["stream"] is False


@pytest.mark.parametrize("kwargs", [
    {"props_body": {"total_slots": 1}},
    {"props_status": 404},
    {"props_exc": httpx.ConnectError("mock: connection refused")},
    {"props_raw": b"not-json"},
])
def test_single_slot_or_props_failure_omits_id_slot(kwargs):
    capture: list = []
    transport = make_v2_transport(capture=capture, **kwargs)
    assert _stream(transport) == ["Hi"]
    assert _chat(transport) == "ok"
    assert len(capture) == 2
    for payload in capture:
        assert "id_slot" not in payload
        assert payload["cache_prompt"] is True


def test_slot_cache_probes_props_once():
    props_hits: list = []
    capture: list = []
    transport = make_v2_transport(props_body={"total_slots": 2}, capture=capture,
                                  props_hits=props_hits)
    assert _chat(transport) == "ok"
    assert _chat(transport) == "ok"
    assert len(props_hits) == 1
    assert capture[0]["id_slot"] == 1
    assert capture[1]["id_slot"] == 1


def test_invalidate_discovery_cache_forces_reprobe():
    props_hits: list = []
    transport = make_v2_transport(props_body={"total_slots": 2}, props_hits=props_hits)
    _chat(transport)
    assert len(props_hits) == 1
    llm_client.invalidate_discovery_cache()
    _chat(transport)
    assert len(props_hits) == 2


def test_slot_cache_reprobes_after_ttl(monkeypatch):
    props_hits: list = []
    transport = make_v2_transport(props_body={"total_slots": 2}, props_hits=props_hits)
    _chat(transport)
    assert len(props_hits) == 1
    # Envelhece o cache além do TTL sem chamar invalidate (llama-server pode
    # mudar de -np por fora).
    base = next(iter(llm_client._slot_cache))
    count, _ = llm_client._slot_cache[base]
    llm_client._slot_cache[base] = (count, time.monotonic() - llm_client._SLOT_CACHE_TTL_S - 1)
    _chat(transport)
    assert len(props_hits) == 2


def test_chat_completion_rejects_id_slot_kwarg():
    with pytest.raises(TypeError):
        llm_client.chat_completion(
            _cfg(), [{"role": "user", "content": "hi"}], id_slot=1,
        )
