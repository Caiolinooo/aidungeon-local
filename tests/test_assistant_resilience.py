"""Falha parcial do assistente não descarta o rascunho inteiro."""

from __future__ import annotations

import json

import httpx

from test_v2 import override_transport


def _models_or_chat(handler):
    def wrapped(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": [{"id": "mock-model"}]})
        if request.url.path.endswith("/chat/completions"):
            return handler(request)
        return httpx.Response(404, json={"error": request.url.path})
    return wrapped


def _post(client, transport, payload):
    override_transport(client, httpx.MockTransport(transport))
    try:
        return client.post("/api/assistant/generate", json=payload)
    finally:
        client.app.dependency_overrides.clear()


_DRAFT = {
    "title": "Porto",
    "description": "Uma cidade de cais.",
    "ai_instructions": "Seja concreto.",
    "plot_essentials": "Você é um ladrão do cais.",
    "authors_note": "Chuva.",
    "opening_text": "A maré baixou.",
    "tags": ["mar"],
    "story_cards": [{
        "title": "Lia",
        "type": "character",
        "triggers": ["Lia"],
        "entry": "Capitã do cais.",
    }],
}


def test_invalid_character_sheet_keeps_the_draft(client):
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        system = body["messages"][0]["content"]
        content = "not json" if "structured RPG character sheets" in system else json.dumps(_DRAFT)
        return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})

    resp = _post(client, _models_or_chat(handler), {
        "brief": "um porto sob tempestade",
        "web_search_mode": "off",
        "deep_characters": True,
        "card_count": 3,
    })
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["title"] == "Porto"
    assert data["story_cards"][0]["title"] == "Lia"


def test_empty_opening_survives_a_failed_regeneration(client):
    draft = dict(_DRAFT)
    draft["opening_text"] = ""

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        system = body["messages"][0]["content"]
        if "Regenerate or refine exactly ONE field" in system:
            content = "not json"
        else:
            content = json.dumps(draft)
        return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})

    resp = _post(client, _models_or_chat(handler), {
        "brief": "um porto sob tempestade",
        "web_search_mode": "off",
        "deep_characters": False,
        "card_count": 3,
    })
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["title"] == "Porto"
    assert data["opening_text"] == ""
    assert data["story_cards"]


def test_failed_cards_keep_the_scenario(client):
    draft = {key: value for key, value in _DRAFT.items() if key != "story_cards"}

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        system = body["messages"][0]["content"]
        if system.startswith("You are a master scenario architect"):
            content = json.dumps(draft)
        else:
            content = "not json"
        return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})

    resp = _post(client, _models_or_chat(handler), {
        "brief": "um porto sob tempestade",
        "web_search_mode": "off",
        "deep_characters": False,
        "card_count": 3,
    })
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["title"] == "Porto"
    assert data["story_cards"] == []
