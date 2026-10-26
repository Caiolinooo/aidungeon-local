"""Testes de /api/settings/llm e /api/health/llm.

- api_key mascarada no GET (nunca em claro);
- PUT de /api/settings/llm sempre invalida descoberta e sonda de slots;
- PUT de base_url/model_alias ainda refaz a descoberta de modelo;
- health sonda /models em tempo real (ok / unreachable / auth_error).
"""

from __future__ import annotations

import json

import httpx
import pytest

from app import db, deps, llm_client, settings_store

from conftest import make_llm_transport


@pytest.fixture()
def auth_token(admin_token):
    return admin_token


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


def test_stored_masked_voice_key_falls_through_to_env(monkeypatch):
    monkeypatch.setenv("VOICE_API_KEY", "sk-real-voice-key-9999")
    with db.session() as conn:
        db.set_setting(conn, "voice.api_key", json.dumps("sk-…9999"))
        cfg = settings_store.effective_voice_settings(conn)
    assert cfg["api_key"] == "sk-real-voice-key-9999"


def test_api_key_empty_string_removes_it(client):
    client.put("/api/settings/llm", json={"api_key": "sk-secret-1234"})
    resp = client.put("/api/settings/llm", json={"api_key": ""})
    assert resp.status_code == 200
    assert resp.json()["api_key"] is None
    assert client.get("/api/settings/llm").json()["api_key"] is None


@pytest.mark.parametrize(("path", "field", "key"), [
    ("/api/settings/llm", "api_key", "llm.api_key"),
    ("/api/settings/image", "openai_api_key", "image.openai_api_key"),
    ("/api/settings/voice", "api_key", "voice.api_key"),
])
@pytest.mark.parametrize("echoed", ["sk-…1234", "chave-ção", "sk-new\r\nX-Evil: 1"])
def test_masked_or_non_ascii_key_is_ignored(client, path, field, key, echoed):
    assert client.put(path, json={field: "sk-secret-1234"}).status_code == 200
    resp = client.put(path, json={field: echoed})
    assert resp.status_code == 200, resp.text
    assert resp.json()[field] == "sk-…1234"
    with db.session() as conn:
        assert settings_store._get_override(conn, key) == "sk-secret-1234"
    resp = client.put(path, json={field: "sk-other-5678"})
    assert resp.json()[field] == "sk-…5678"


@pytest.mark.parametrize(("key", "env_name", "getter", "field"), [
    ("llm.api_key", "LLM_API_KEY", settings_store.effective_llm_settings, "api_key"),
    ("image.openai_api_key", "IMAGE_API_KEY", settings_store.effective_image_settings, "openai_api_key"),
    ("voice.api_key", "VOICE_API_KEY", settings_store.effective_voice_settings, "api_key"),
])
def test_legacy_masked_override_falls_back_to_environment(
        client, monkeypatch, key, env_name, getter, field):
    monkeypatch.setenv(env_name, "sk-env-valid-1234")
    with db.session() as conn:
        db.set_setting(conn, key, json.dumps("sk-…1234"))
        cfg = getter(conn)
    assert cfg[field] == "sk-env-valid-1234"


def test_put_partial_update_keeps_other_fields(client):
    client.put("/api/settings/llm", json={"max_tokens": 1200, "context_size": 98304, "streaming": False})
    data = client.get("/api/settings/llm").json()
    assert data["max_tokens"] == 1200
    assert data["context_size"] == 98304  # abaixo do cap: passa intacto
    # Acima do cap: clamp de leitura (context_size_limits.max)
    client.put("/api/settings/llm", json={"context_size": 262144})
    assert client.get("/api/settings/llm").json()["context_size"] == 106496
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


def test_api_key_only_put_reprobes_slot_count(client):
    """PUT só de api_key limpa o cache de slots: o próximo chat sonda /props de novo."""
    props_hits: list = []

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/props":
            props_hits.append(request)
            return httpx.Response(200, json={"total_slots": 2})
        if path.endswith("/models"):
            return httpx.Response(200, json={
                "object": "list",
                "data": [{"id": "mock-model", "object": "model"}],
            })
        if path.endswith("/chat/completions"):
            return httpx.Response(200, json={
                "choices": [{"message": {"content": "ok"}}],
            })
        return httpx.Response(404, json={"error": path})

    transport = httpx.MockTransport(handler)
    with db.session() as conn:
        cfg = settings_store.effective_llm_settings(conn)
    assert llm_client.chat_completion(
        cfg, [{"role": "user", "content": "hi"}], transport=transport) == "ok"
    assert len(props_hits) == 1

    client.app.dependency_overrides[deps.get_http_transport] = lambda: transport
    try:
        resp = client.put("/api/settings/llm", json={"api_key": "sk-rotated-9999"})
    finally:
        client.app.dependency_overrides.clear()
    assert resp.status_code == 200
    assert "sk-rotated-9999" not in resp.text
    assert len(props_hits) == 1  # o PUT não sonda slots; só invalida o cache

    with db.session() as conn:
        cfg = settings_store.effective_llm_settings(conn)
    assert cfg["api_key"] == "sk-rotated-9999"
    assert llm_client.chat_completion(
        cfg, [{"role": "user", "content": "hi"}], transport=transport) == "ok"
    assert len(props_hits) == 2


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



def test_image_comfyui_settings_roundtrip(client):
    resp = client.put("/api/settings/image", json={
        "provider": "comfyui",
        "comfyui_base_url": "http://127.0.0.1:8188",
        "comfyui_steps": 8,
        "comfyui_width": 1024,
        "comfyui_height": 1024,
    })
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["provider"] == "comfyui"
    assert data["comfyui_steps"] == 8
    assert (data["comfyui_width"], data["comfyui_height"]) == (1024, 1024)

    with db.session() as conn:
        cfg = settings_store.effective_image_settings(conn)
    # Modelo/VAE/CLIP vêm só do settings.json (internos, não passam pela API).
    assert cfg["comfyui_unet"] == "fineviewQWEN_qw21TurboFixed_3299367.safetensors"
    assert cfg["comfyui_clip"] == "qwen3vl_8b_int8_convrot.safetensors"
    assert cfg["comfyui_clip_type"] == "qwen_image"
    assert cfg["comfyui_vae"] == "texture_fix_vae_for_qwen_image_2.1_bf16.safetensors"
    assert cfg["comfyui_sampler"] == "euler"
    assert cfg["comfyui_shift"] == 1.31
    assert cfg["comfyui_sigmas"] == [
        1.0, 0.978453, 0.95418, 0.926626, 0.89508,
        0.845148, 0.704534, 0.414568, 0.0]


def test_comfyui_steps_must_match_sigmas(client):
    response = client.put("/api/settings/image", json={"comfyui_steps": 12})
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "validation_error"
    assert client.get("/api/settings/image").json()["comfyui_steps"] == 8
