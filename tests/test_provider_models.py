"""Testes de integração de provedores: presets, descoberta/pesquisa de modelos e uso."""

from __future__ import annotations

import base64
import json

import httpx
import pytest

from app import db, deps, embeddings_client, image_client, llm_client, settings_store


@pytest.fixture()
def auth_token(admin_token):
    return admin_token


def test_get_provider_presets_has_all_four_modalities(client):
    resp = client.get("/api/settings/providers")
    assert resp.status_code == 200, resp.text
    providers = resp.json()["providers"]
    for kind in ("llm", "image", "voice", "embeddings"):
        assert kind in providers
        assert len(providers[kind]) >= 4
        assert all(p.get("id") and p.get("base_url") for p in providers[kind])


def test_discover_llm_models_openrouter_and_search(client):
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json={
                "data": [
                    {
                        "id": "anthropic/claude-3.7-sonnet",
                        "name": "Anthropic: Claude 3.7 Sonnet",
                        "context_length": 200000,
                        "architecture": {"modality": "text+image->text"},
                    },
                    {
                        "id": "openai/text-embedding-3-small",
                        "name": "OpenAI Embedding Small",
                        "type": "embedding",
                    },
                    {
                        "id": "meta-llama/llama-3.3-70b-instruct",
                        "name": "Meta Llama 3.3 70B",
                        "context_length": 131072,
                        "architecture": {"modality": "text->text"},
                    },
                ],
            })
        return httpx.Response(404)

    client.app.dependency_overrides[deps.get_http_transport] = lambda: httpx.MockTransport(handler)
    try:
        resp = client.post("/api/settings/models/discover", json={
            "kind": "llm",
            "base_url": "https://openrouter.ai/api/v1",
            "api_key": "sk-or-test-key-9999",
            "query": "claude sonnet",
        })
    finally:
        client.app.dependency_overrides.clear()

    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["status"] == "ok"
    assert data["total_available"] == 3
    assert data["total"] == 1
    assert data["models"][0]["id"] == "anthropic/claude-3.7-sonnet"
    assert data["models"][0]["context_length"] == 200000
    assert data["models"][0]["recommended"] is True
    assert captured[0].headers["authorization"] == "Bearer sk-or-test-key-9999"


def test_discover_together_raw_array_and_image_classification(client):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json=[
                {"id": "black-forest-labs/FLUX.1-schnell-Free", "type": "image", "organization": "BFL"},
                {"id": "meta-llama/Llama-3.3-70B-Instruct-Turbo", "type": "chat", "organization": "Meta"},
            ])
        return httpx.Response(404)

    client.app.dependency_overrides[deps.get_http_transport] = lambda: httpx.MockTransport(handler)
    try:
        resp = client.post("/api/settings/models/discover", json={
            "kind": "image",
            "provider": "openai_compatible",
            "base_url": "https://api.together.xyz/v1",
            "api_key": "tok-together-1234",
            "only_matching": True,
        })
    finally:
        client.app.dependency_overrides.clear()

    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["status"] == "ok"
    assert [m["id"] for m in data["models"]] == ["black-forest-labs/FLUX.1-schnell-Free"]
    assert data["models"][0]["kind_hint"] == "image"
    assert data["models"][0]["recommended"] is True


def test_discover_sdwebui_and_comfyui_image_models(client):
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/sdapi/v1/sd-models"):
            return httpx.Response(200, json=[
                {"title": "juggernautXL.safetensors [abc123]", "model_name": "juggernautXL"},
                {"title": "ponyDiffusion.safetensors [def456]", "model_name": "ponyDiffusion"},
            ])
        if path.endswith("/object_info/UNETLoader"):
            return httpx.Response(200, json={
                "UNETLoader": {"input": {"required": {"unet_name": [["flux-2-klein-4b-fp8.safetensors"]]}}}
            })
        if path.endswith("/object_info/UnetLoaderGGUF"):
            return httpx.Response(200, json={
                "UnetLoaderGGUF": {"input": {"required": {"unet_name": [["z_image_turbo-Q4_K_M.gguf"]]}}}
            })
        if path.endswith("/object_info/CheckpointLoaderSimple"):
            return httpx.Response(200, json={
                "CheckpointLoaderSimple": {"input": {"required": {"ckpt_name": [["ltx-video.safetensors"]]}}}
            })
        return httpx.Response(404)

    client.app.dependency_overrides[deps.get_http_transport] = lambda: httpx.MockTransport(handler)
    try:
        sd = client.post("/api/settings/models/discover", json={
            "kind": "image",
            "provider": "sdwebui",
            "base_url": "http://127.0.0.1:7860",
            "query": "juggernaut",
        })
        comfy = client.post("/api/settings/models/discover", json={
            "kind": "image",
            "provider": "comfyui",
            "base_url": "http://127.0.0.1:8188",
        })
    finally:
        client.app.dependency_overrides.clear()

    assert sd.status_code == 200
    assert [m["id"] for m in sd.json()["models"]] == ["juggernautXL.safetensors [abc123]"]
    assert comfy.status_code == 200
    ids = {m["id"] for m in comfy.json()["models"]}
    assert {"flux-2-klein-4b-fp8.safetensors", "z_image_turbo-Q4_K_M.gguf", "ltx-video.safetensors"} <= ids


def test_discover_voice_models_and_voices_and_save_external_voice(client):
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/models"):
            return httpx.Response(200, json={
                "data": [
                    {"id": "gpt-4o-mini-tts"},
                    {"id": "whisper-1"},
                    {"id": "gpt-4o"},
                ]
            })
        if path.endswith("/audio/voices"):
            return httpx.Response(404)
        if path.endswith("/voices"):
            return httpx.Response(404)
        return httpx.Response(404)

    client.app.dependency_overrides[deps.get_http_transport] = lambda: httpx.MockTransport(handler)
    try:
        resp = client.post("/api/settings/models/discover", json={
            "kind": "voice",
            "base_url": "https://api.openai.com/v1",
            "api_key": "sk-openai-voice-1234",
            "only_matching": True,
        })
    finally:
        client.app.dependency_overrides.clear()

    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["status"] == "ok"
    assert {m["id"] for m in data["models"]} == {"gpt-4o-mini-tts", "whisper-1"}
    assert "nova" in data["voices"]
    assert "alloy" in data["voices"]

    # Salvar vozes externas + narrator_voice externa funciona sem 400 invalid_voice
    put = client.put("/api/settings/voice", json={
        "base_url": "https://api.openai.com/v1",
        "tts_model": "gpt-4o-mini-tts",
        "stt_model": "whisper-1",
        "voices": data["voices"],
        "narrator_voice": "nova",
    })
    assert put.status_code == 200, put.text
    assert put.json()["narrator_voice"] == "nova"
    assert "nova" in put.json()["voices"]


def test_discover_embeddings_openrouter_endpoint_and_api_key_usage(client):
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        if request.url.path.endswith("/embeddings/models"):
            return httpx.Response(200, json={
                "data": [
                    {"id": "openai/text-embedding-3-small", "name": "OpenAI Embedding Small"},
                    {"id": "qwen/qwen3-embedding-8b", "name": "Qwen3 Embedding 8B"},
                ]
            })
        if request.url.path.endswith("/embeddings"):
            return httpx.Response(200, json={
                "data": [{"embedding": [0.1, 0.2, 0.3]}],
            })
        return httpx.Response(404)

    # Salvar chave de embeddings: resposta deve vir mascarada e nunca em claro
    put = client.put("/api/settings/embeddings", json={
        "base_url": "https://openrouter.ai/api/v1",
        "model": "qwen/qwen3-embedding-8b",
        "api_key": "sk-emb-secret-4321",
    })
    assert put.status_code == 200, put.text
    assert "sk-emb-secret-4321" not in put.text
    assert put.json()["api_key"] == "sk-…4321"
    assert put.json()["api_key_set"] is True

    transport = httpx.MockTransport(handler)
    client.app.dependency_overrides[deps.get_http_transport] = lambda: transport
    try:
        # Discover sem enviar api_key no body usa a chave salva para o mesmo base_url
        disc = client.post("/api/settings/models/discover", json={
            "kind": "embeddings",
            "query": "qwen3",
        })
        health = client.get("/api/health/embeddings")
    finally:
        client.app.dependency_overrides.clear()

    assert disc.status_code == 200
    assert disc.json()["status"] == "ok"
    assert [m["id"] for m in disc.json()["models"]] == ["qwen/qwen3-embedding-8b"]
    assert captured[0].url.path.endswith("/embeddings/models")
    assert captured[0].headers["authorization"] == "Bearer sk-emb-secret-4321"
    assert health.status_code == 200
    assert health.json()["status"] == "ok"

    # embeddings_client.get_embedding envia Authorization Bearer
    with db.session() as conn:
        vec = embeddings_client.get_embedding("hello world", conn=conn, transport=transport)
    assert vec == pytest.approx([0.1, 0.2, 0.3])
    assert captured[-1].headers["authorization"] == "Bearer sk-emb-secret-4321"

    # Limpar chave
    cleared = client.put("/api/settings/embeddings", json={"api_key": ""})
    assert cleared.status_code == 200
    assert cleared.json()["api_key"] is None
    assert cleared.json()["api_key_set"] is False


def test_discover_does_not_leak_saved_key_to_different_host_or_errors(client):
    captured: list[httpx.Request] = []
    secret = "sk-saved-secret-7777"

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(401, text=f"bad key {secret}")

    assert client.put("/api/settings/llm", json={
        "base_url": "https://api.openai.com/v1",
        "api_key": secret,
    }).status_code == 200

    client.app.dependency_overrides[deps.get_http_transport] = lambda: httpx.MockTransport(handler)
    try:
        # Outro host sem passar chave: NÃO deve enviar a chave salva de api.openai.com
        resp = client.post("/api/settings/models/discover", json={
            "kind": "llm",
            "base_url": "https://evil.example.com/v1",
        })
    finally:
        client.app.dependency_overrides.clear()

    assert resp.status_code == 200
    assert resp.json()["status"] == "auth_error"
    assert secret not in resp.text
    assert "authorization" not in captured[0].headers


def test_cloud_openai_llm_payload_omits_llamacpp_extensions():
    cfg = {
        "base_url": "https://openrouter.ai/api/v1",
        "api_key": "sk-or-123",
        "model_alias": "openai/gpt-4o-mini",
        "disable_thinking": True,
        "context_size": 32768,
    }
    payload = llm_client.build_chat_payload(
        cfg,
        [{"role": "user", "content": "hi"}],
        sampler={"temperature": 0.7, "max_tokens": 200, "dry_multiplier": 0.8},
        stop=[],
        model="openai/gpt-4o-mini",
    )
    assert payload["model"] == "openai/gpt-4o-mini"
    assert "dry_multiplier" not in payload
    assert "repeat_penalty" not in payload
    assert "cache_prompt" not in payload
    assert "chat_template_kwargs" not in payload
    assert "id_slot" not in payload
    assert "stop" not in payload


def test_openai_image_retries_without_response_format_for_gpt_image_1(tmp_path):
    calls: list[dict] = []
    png_b64 = base64.b64encode(b"\x89PNG\r\n\x1a\nfake").decode("ascii")

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        calls.append(body)
        if "response_format" in body:
            return httpx.Response(400, json={"error": {"message": "Unknown parameter: response_format"}})
        return httpx.Response(200, json={"data": [{"b64_json": png_b64}]})

    cfg = {
        "provider": "openai_compatible",
        "openai_base_url": "https://api.openai.com/v1",
        "openai_api_key": "sk-test",
        "openai_model": "gpt-image-1",
        "openai_size": "1024x1024",
        "storage_dir": str(tmp_path),
        "timeout_seconds": 30,
    }
    result = image_client.generate_image(
        cfg,
        "a medieval tavern",
        adventure_id="adv1",
        image_id="img1",
        transport=httpx.MockTransport(handler),
    )
    assert len(calls) == 2
    assert "response_format" in calls[0]
    assert "response_format" not in calls[1]
    assert result["provider"] == "openai_compatible"
