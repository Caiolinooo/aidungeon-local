from __future__ import annotations

import json
import threading

import httpx
import pytest

from app import choices_service, db, deps, game_engine, llm_client
from conftest import add_action, make_adventure


@pytest.fixture()
def auth_token(admin_token):
    return admin_token


def test_cancel_during_stream_rolls_back_player_action(monkeypatch, tmp_db):
    adv = make_adventure()
    add_action(adv["id"], 0, "start", "A porta está fechada.")
    cancel = threading.Event()

    def stream(*args, **kwargs):
        assert kwargs.get("cancel") is cancel
        yield "texto parcial"
        assert cancel.wait(2)
        raise llm_client.LlmCancelled("llm call cancelled")

    monkeypatch.setattr(game_engine.llm_client, "stream_chat_completion", stream)
    events = game_engine.handle_action(adv["id"], "do", "abrir a porta", cancel=cancel)
    assert next(events)["event"] == "token"
    cancel.set()
    rest = list(events)

    assert rest[-1] == {"event": "error", "data": {
        "message": "llm call cancelled", "code": "llm_cancelled"}}
    with db.session() as conn:
        assert [a["type"] for a in db.list_actions(conn, adv["id"])] == ["start"]
    assert not game_engine.is_generation_active(adv["id"])



def test_adventure_generation_preferences_reach_narrator(tmp_db):
    from conftest import make_llm_transport
    adv = make_adventure()
    with db.session() as conn:
        db.update_adventure_ui_state(
            conn, adv["id"], {"_generation": {"max_tokens": 512, "temperature": 0.55}})
    add_action(adv["id"], 0, "start", "Abertura")
    captured = []

    events = list(game_engine.handle_action(
        adv["id"], "do", "esperar",
        transport=make_llm_transport(tokens=("Tudo quieto.",), capture=captured),
    ))

    assert events[-1]["event"] == "done"
    narrative = next(call for call in captured if call.get("stream"))
    assert narrative["max_tokens"] == 512
    assert narrative["temperature"] == 0.55

def test_cancelled_retry_preserves_previous_answer(monkeypatch, tmp_db):
    adv = make_adventure()
    add_action(adv["id"], 0, "start", "Abertura")
    add_action(adv["id"], 1, "continue", "Resposta anterior")
    monkeypatch.setattr(
        game_engine.llm_client, "stream_chat_completion",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("não deveria chamar")),
    )
    cancel = threading.Event()
    cancel.set()

    events = list(game_engine.handle_retry(adv["id"], cancel=cancel))

    assert events[-1]["data"]["code"] == "llm_cancelled"
    with db.session() as conn:
        actions = db.list_actions(conn, adv["id"])
        assert [a["text"] for a in actions] == ["Abertura", "Resposta anterior"]


def test_health_rejects_public_models_with_unauthorized_props(client):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": [{"id": "public-model"}]})
        if request.url.path == "/props":
            return httpx.Response(401, json={"error": "unauthorized"})
        return httpx.Response(404)

    client.app.dependency_overrides[deps.get_http_transport] = lambda: httpx.MockTransport(handler)
    try:
        response = client.get("/api/health/llm")
    finally:
        client.app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json()["status"] == "auth_error"
    assert response.json()["model_discovered"] == "public-model"


def test_json_schema_bad_request_falls_back_once(tmp_db):
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": [{"id": "mock"}]})
        if request.url.path.endswith("/chat/completions"):
            payload = json.loads(request.content)
            calls.append(payload)
            if "response_format" in payload:
                return httpx.Response(400, json={"error": "unsupported schema"})
            return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})
        return httpx.Response(404)

    cfg = {"base_url": "http://mock/v1", "model_alias": "auto", "timeout_seconds": 2}
    result = llm_client.chat_completion(
        cfg, [{"role": "user", "content": "hi"}],
        response_format={"type": "json_schema", "json_schema": {"name": "x", "schema": {}}},
        transport=httpx.MockTransport(handler),
    )

    assert result == "ok"
    assert len(calls) == 2
    assert "response_format" in calls[0]
    assert "response_format" not in calls[1]


def test_choices_use_schema_and_retry_wrong_language(tmp_db):
    scene = "Você está numa sala com uma porta."
    english = json.dumps([
        {"type": "do", "text": "open the door and look around"},
        {"type": "say", "text": "ask the guard about the door"},
        {"type": "story", "text": "the door opens with a sound"},
    ])
    portuguese = json.dumps([
        {"type": "do", "text": "abrir a porta"},
        {"type": "say", "text": "perguntar ao guarda"},
        {"type": "story", "text": "uma rajada entra na sala"},
    ])
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": [{"id": "mock"}]})
        if request.url.path.endswith("/chat/completions"):
            payload = json.loads(request.content)
            calls.append(payload)
            content = english if len(calls) == 1 else portuguese
            return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})
        return httpx.Response(404)

    entry = choices_service._Entry(key=(1, 0))
    cfg = {"base_url": "http://mock/v1", "model_alias": "auto", "timeout_seconds": 2}
    choices_service._run(
        "adv", entry, [{"type": "continue", "text": scene}], cfg,
        {"count": 3, "temperature": 0.8, "max_tokens": 220},
        httpx.MockTransport(handler),
    )

    assert entry.error is None
    assert entry.choices[0]["text"] == "abrir a porta"
    assert len(calls) == 2
    assert all(call["response_format"]["type"] == "json_schema" for call in calls)


def test_image_advanced_settings_round_trip_and_seed_reset(client):
    payload = {
        "seed": 42,
        "variation": 2,
        "cfg_scale": 6.5,
        "sampler_name": "DPM++ SDE",
        "scheduler": "Karras",
        "denoising_strength": 0.45,
        "timeout_seconds": 45,
        "sd_model_checkpoint": "checkpoint-test.safetensors",
        "openai_size": "1024x1024",
    }
    response = client.put("/api/settings/image", json=payload)
    assert response.status_code == 200, response.text
    for key, expected in payload.items():
        assert response.json()[key] == expected

    reset = client.put("/api/settings/image", json={"seed": None})
    assert reset.status_code == 200
    assert reset.json()["seed"] is None
    assert reset.json()["variation"] == 2


def test_context_budget_reserves_system_prompt_overhead(tmp_db):
    from app import context_builder
    budgets = {key: 0 for key in context_builder.FIXED_BUDGET_KEYS}
    budgets["response_reserve"] = 500
    budget = context_builder.recent_window_budget({
        "context_size": 32768,
        "budgets": budgets,
        "sampler": {"max_tokens": 300},
    })

    assert 0 < budget <= 32768 - 500 - context_builder.estimate_tokens(
        context_builder.BASE_SYSTEM_PROMPT)


def test_generation_preferences_stay_out_of_player_state(tmp_db):
    from app import context_builder
    rendered = context_builder.render_player_state({
        "_generation": {"max_tokens": 180},
        "stats": {"HP": 10},
    })
    assert "_generation" not in rendered
    assert "HP 10" in rendered
