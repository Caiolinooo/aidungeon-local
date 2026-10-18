"""Testes de /api/settings/llm e /api/health/llm.

- api_key mascarada no GET (nunca em claro);
- PUT de base_url/model_alias invalida o cache de descoberta de modelo;
- health sonda /models em tempo real (ok / unreachable / auth_error).
"""

from __future__ import annotations

import json

from app import deps

from conftest import make_llm_transport


def test_get_settings_defaults(client):
    resp = client.get("/api/settings/llm")
    assert resp.status_code == 200
    data = resp.json()
    assert data["provider"] == "llamacpp"
    assert data["base_url"] == "http://vm.groupabz.com:1234/v1"
    assert data["api_key"] is None                       # settings.json tem ""
    assert data["model_alias"] == "auto"
    assert data["context_size"] == 65536
    assert data["max_tokens"] == 300
    assert data["streaming"] is True
    assert data["disable_thinking"] is True              # default do contrato
    assert data["timeout_seconds"] == 180
    assert "model_discovered" in data


def test_api_key_is_masked_and_never_plain(client):
    resp = client.put("/api/settings/llm", json={"api_key": "sk-secret-1234"})
    assert resp.status_code == 200
    body = resp.text
    assert "sk-secret-1234" not in body                  # nunca em claro
    masked = resp.json()["api_key"]
    assert masked == "sk-…1234"
    # GET subsequente também mascarado
    again = client.get("/api/settings/llm").json()
    assert again["api_key"] == "sk-…1234"
    assert "sk-secret-1234" not in json.dumps(again)


def test_api_key_empty_string_removes_it(client):
    client.put("/api/settings/llm", json={"api_key": "sk-secret-1234"})
    resp = client.put("/api/settings/llm", json={"api_key": ""})
    assert resp.status_code == 200
    assert resp.json()["api_key"] is None
    assert client.get("/api/settings/llm").json()["api_key"] is None


def test_put_partial_update_keeps_other_fields(client):
    client.put("/api/settings/llm", json={"max_tokens": 1200, "context_size": 98304, "streaming": False})
    data = client.get("/api/settings/llm").json()
    assert data["max_tokens"] == 1200
    assert data["context_size"] == 98304
    assert data["streaming"] is False
    assert data["base_url"] == "http://vm.groupabz.com:1234/v1"  # intacto
    assert data["disable_thinking"] is True                      # intacto


def test_put_disable_thinking_override(client):
    resp = client.put("/api/settings/llm", json={"disable_thinking": False})
    assert resp.json()["disable_thinking"] is False
    # e volta
    resp = client.put("/api/settings/llm", json={"disable_thinking": True})
    assert resp.json()["disable_thinking"] is True


def test_put_validation_error(client):
    resp = client.put("/api/settings/llm", json={"max_tokens": 50})  # min 100
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "validation_error"
    resp = client.put("/api/settings/llm", json={"context_size": 1024})  # min 4096
    assert resp.status_code == 422


def test_base_url_change_invalidates_discovery_cache(client):
    # 1) endpoint A descobre model-a
    client.app.dependency_overrides[deps.get_http_transport] = \
        lambda: make_llm_transport(model_id="model-a")
    resp = client.put("/api/settings/llm", json={"base_url": "http://a.example/v1"})
    assert resp.status_code == 200
    assert resp.json()["model_discovered"] == "model-a"

    # 2) troca para B: cache invalidado → redescobre model-b
    client.app.dependency_overrides[deps.get_http_transport] = \
        lambda: make_llm_transport(model_id="model-b")
    resp = client.put("/api/settings/llm", json={"base_url": "http://b.example/v1"})
    assert resp.json()["model_discovered"] == "model-b"

    # 3) sem mudar base_url: usa o cache (não bate no servidor de novo)
    client.app.dependency_overrides[deps.get_http_transport] = \
        lambda: make_llm_transport(model_id="model-c")
    assert client.get("/api/settings/llm").json()["model_discovered"] == "model-b"
    client.app.dependency_overrides.clear()


def test_explicit_model_alias_skips_discovery(client):
    resp = client.put("/api/settings/llm", json={"model_alias": "meu-gguf"})
    assert resp.json()["model_alias"] == "meu-gguf"


def test_health_ok_with_model(client):
    client.app.dependency_overrides[deps.get_http_transport] = \
        lambda: make_llm_transport(model_id="gguf-ativo")
    resp = client.get("/api/health/llm")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ok"
    assert data["model_discovered"] == "gguf-ativo"
    assert data["model_loaded"] is True
    assert data["provider"] == "llamacpp"
    client.app.dependency_overrides.clear()


def test_health_unreachable_still_200(client):
    client.app.dependency_overrides[deps.get_http_transport] = \
        lambda: make_llm_transport(fail="connect")
    resp = client.get("/api/health/llm")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "unreachable"
    assert data["model_discovered"] is None
    assert data["model_loaded"] is False
    client.app.dependency_overrides.clear()


def test_health_auth_error(client):
    client.app.dependency_overrides[deps.get_http_transport] = \
        lambda: make_llm_transport(fail="auth")
    resp = client.get("/api/health/llm")
    assert resp.json()["status"] == "auth_error"
    client.app.dependency_overrides.clear()


def test_put_and_get_advanced_sampler_settings(client):
    # Verify defaults
    initial = client.get("/api/settings/llm").json()
    assert initial["min_p"] == 0.08
    assert initial["top_p"] == 1.0
    assert initial["repetition_penalty"] == 1.02
    assert initial["presence_penalty"] == 0.15
    assert initial["frequency_penalty"] == 0.10
    assert initial["dry_multiplier"] == 0.8
    assert initial["dry_base"] == 1.75
    assert initial["dry_allowed_length"] == 2
    assert initial["dry_penalty_last_n"] == -1
    assert initial["retry_temperature_step"] == 0.05
    assert initial["max_retry_temperature"] == 1.15

    # Update sampler settings
    update_payload = {
        "min_p": 0.12,
        "presence_penalty": 0.20,
        "frequency_penalty": 0.15,
        "dry_multiplier": 1.0,
        "dry_base": 1.80,
        "dry_allowed_length": 3,
        "dry_penalty_last_n": 512,
        "retry_temperature_step": 0.08,
        "max_retry_temperature": 1.20,
    }
    resp = client.put("/api/settings/llm", json=update_payload)
    assert resp.status_code == 200
    data = resp.json()
    assert data["min_p"] == 0.12
    assert data["presence_penalty"] == 0.20
    assert data["frequency_penalty"] == 0.15
    assert data["dry_multiplier"] == 1.0
    assert data["dry_base"] == 1.80
    assert data["dry_allowed_length"] == 3
    assert data["dry_penalty_last_n"] == 512
    assert data["retry_temperature_step"] == 0.08
    assert data["max_retry_temperature"] == 1.20

    # Verify GET returns the overrides
    get_data = client.get("/api/settings/llm").json()
    assert get_data["min_p"] == 0.12
    assert get_data["presence_penalty"] == 0.20
    assert get_data["dry_multiplier"] == 1.0


def test_embeddings_settings_endpoint(client):
    resp = client.get("/api/settings/embeddings")
    assert resp.status_code == 200
    data = resp.json()
    assert "enabled" in data
    assert "model" in data

    put_resp = client.put("/api/settings/embeddings", json={"model": "Qwen3-Custom-Embedding", "timeout_seconds": 45})
    assert put_resp.status_code == 200
    assert put_resp.json()["model"] == "Qwen3-Custom-Embedding"
    assert put_resp.json()["timeout_seconds"] == 45


def test_scripting_settings_endpoint(client):
    resp = client.get("/api/settings/scripting")
    assert resp.status_code == 200
    assert "input_modifier" in resp.json()

    put_resp = client.put(
        "/api/settings/scripting",
        json={"input_modifier": "def modify(action): return action", "output_modifier": "def modify(text): return text"},
    )
    assert put_resp.status_code == 200
    assert "def modify(action)" in put_resp.json()["input_modifier"]
    assert "def modify(text)" in put_resp.json()["output_modifier"]

