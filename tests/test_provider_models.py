"""Testes de integração de provedores: presets, descoberta/pesquisa de modelos e uso."""

from __future__ import annotations

import base64
import json

import httpx
import pytest

from app import (
    db,
    deps,
    embeddings_client,
    image_client,
    llm_client,
    provider_models,
    settings_store,
    voice_client,
)


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


def test_gemini_stream_chat_completion_omits_frequency_penalty_and_return_progress():
    calls: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/chat/completions"):
            body = json.loads(request.content.decode("utf-8"))
            calls.append(body)
            sse = (
                'data: {"choices":[{"delta":{"content":"Olá "}}]}\n\n'
                'data: {"choices":[{"delta":{"content":"mundo!"},"finish_reason":"stop"}]}\n\n'
                "data: [DONE]\n\n"
            )
            return httpx.Response(200, text=sse, headers={"content-type": "text/event-stream"})
        return httpx.Response(404)

    cfg = {
        "base_url": "https://generativelanguage.googleapis.com/v1beta/openai",
        "api_key": "AIza-test",
        "model_alias": "gemini-2.5-flash",
        "disable_thinking": True,
        "context_size": 32768,
        "timeout_seconds": 30,
    }
    tokens = list(llm_client.stream_chat_completion(
        cfg,
        [{"role": "user", "content": "oi"}],
        sampler={"temperature": 0.8, "max_tokens": 120, "frequency_penalty": 0.1, "presence_penalty": 0.15},
        stop=[],
        transport=httpx.MockTransport(handler),
        role="narrative",
    ))
    assert "".join(tokens) == "Olá mundo!"
    assert len(calls) == 1
    sent = calls[0]
    assert sent["model"] == "gemini-2.5-flash"
    assert "return_progress" not in sent
    assert "frequency_penalty" not in sent
    assert "presence_penalty" not in sent
    assert "dry_multiplier" not in sent
    assert "cache_prompt" not in sent
    assert "chat_template_kwargs" not in sent
    assert "id_slot" not in sent


def test_custom_cloud_proxy_retries_once_when_http_400_rejects_unknown_fields():
    calls: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/props"):
            return httpx.Response(404)
        if request.url.path.endswith("/chat/completions"):
            body = json.loads(request.content.decode("utf-8"))
            calls.append(body)
            if "frequency_penalty" in body or "return_progress" in body or "cache_prompt" in body:
                return httpx.Response(
                    400,
                    json=[{
                        "error": {
                            "code": 400,
                            "message": (
                                'Invalid JSON payload received. Unknown name "frequency_penalty": '
                                'Cannot find field.\n'
                                'Invalid JSON payload received. Unknown name "return_progress": '
                                'Cannot find field.'
                            ),
                        }
                    }],
                )
            sse = (
                'data: {"choices":[{"delta":{"content":"Ok!"}}]}\n\n'
                "data: [DONE]\n\n"
            )
            return httpx.Response(200, text=sse, headers={"content-type": "text/event-stream"})
        return httpx.Response(404)

    cfg = {
        "base_url": "https://my-gemini-proxy.example.com/v1",
        "api_key": "proxy-key",
        "model_alias": "gemini-2.5-pro",
        "disable_thinking": True,
        "context_size": 32768,
        "timeout_seconds": 30,
    }
    tokens = list(llm_client.stream_chat_completion(
        cfg,
        [{"role": "user", "content": "teste"}],
        sampler={"temperature": 0.7, "max_tokens": 100, "frequency_penalty": 0.1},
        stop=[],
        transport=httpx.MockTransport(handler),
        role="narrative",
    ))
    assert "".join(tokens) == "Ok!"
    assert len(calls) == 2
    assert "return_progress" in calls[0]
    assert "frequency_penalty" in calls[0]
    assert "return_progress" not in calls[1]
    assert "frequency_penalty" not in calls[1]
    assert "cache_prompt" not in calls[1]
    assert "dry_multiplier" not in calls[1]
    assert "chat_template_kwargs" not in calls[1]


def test_openai_reasoning_model_proactively_uses_max_completion_tokens():
    calls: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/chat/completions"):
            body = json.loads(request.content.decode("utf-8"))
            calls.append(body)
            return httpx.Response(
                200,
                json={"choices": [{"message": {"content": "feito"}, "finish_reason": "stop"}]},
            )
        return httpx.Response(404)

    cfg = {
        "base_url": "https://api.openai.com/v1",
        "api_key": "sk-test",
        "model_alias": "o3-mini",
        "timeout_seconds": 30,
    }
    out = llm_client.chat_completion(
        cfg,
        [{"role": "user", "content": "oi"}],
        max_tokens=150,
        stop=["\n> You", "\n> you"],
        transport=httpx.MockTransport(handler),
    )
    assert out == "feito"
    assert len(calls) == 1
    assert "max_tokens" not in calls[0]
    assert calls[0]["max_completion_tokens"] == 150
    assert "temperature" not in calls[0]
    assert "stop" not in calls[0]


def test_custom_gateway_multistep_400_422_retry_and_stop_limit():
    calls: list[dict] = []
    many_stops = [f"\n> Stop{i}" for i in range(10)]

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/props"):
            return httpx.Response(404)
        if request.url.path.endswith("/chat/completions"):
            body = json.loads(request.content.decode("utf-8"))
            calls.append(body)
            if "stop" in body and len(body["stop"]) > 5:
                return httpx.Response(
                    400,
                    json=[{
                        "error": {
                            "code": 400,
                            "message": (
                                "* GenerateContentRequest.generation_config.stop_sequences: "
                                "the number of stop_sequences must not exceed 5\n"
                            ),
                            "status": "INVALID_ARGUMENT",
                        }
                    }],
                )
            if "max_tokens" in body:
                return httpx.Response(
                    422,
                    json={
                        "error": {
                            "message": (
                                "Unsupported parameter: 'max_tokens' is not supported. "
                                "Use 'max_completion_tokens' instead."
                            ),
                        }
                    },
                )
            return httpx.Response(
                200,
                json={"choices": [{"message": {"content": "recuperado"}, "finish_reason": "stop"}]},
            )
        return httpx.Response(404)

    cfg = {
        "base_url": "https://my-custom-gateway.example.com/v1",
        "api_key": "gw-key",
        "model_alias": "strict-model",
        "timeout_seconds": 30,
    }
    out = llm_client.chat_completion(
        cfg,
        [{"role": "user", "content": "oi"}],
        max_tokens=150,
        stop=many_stops,
        transport=httpx.MockTransport(handler),
    )
    assert out == "recuperado"
    assert len(calls) == 3
    assert len(calls[0]["stop"]) == 10
    assert len(calls[1]["stop"]) == 4
    assert "max_tokens" not in calls[2]
    assert calls[2]["max_completion_tokens"] == 150


def test_cloud_llm_proactively_caps_stop_sequences_to_4_while_local_keeps_all():
    many_stops = [
        "\n> You", "\n> you", "\n> Alice", "\nAlice:", "\n>Alice",
        "\nPlayer:", "\nUser:", "\n<lore>", "\n</lore>", "\n[Moment]",
    ]
    for url in (
        "https://generativelanguage.googleapis.com/v1beta/openai",
        "https://api.openai.com/v1",
        "https://api.groq.com/openai/v1",
        "https://openrouter.ai/api/v1",
    ):
        payload = llm_client.build_chat_payload(
            {"base_url": url, "api_key": "k", "model_alias": "m"},
            [{"role": "user", "content": "hi"}],
            sampler={"max_tokens": 100},
            stop=many_stops,
            model="m",
        )
        assert len(payload["stop"]) == 4
        assert payload["stop"] == many_stops[:4]

    local_payload = llm_client.build_chat_payload(
        {"base_url": "http://127.0.0.1:8080/v1", "api_key": "", "model_alias": "local"},
        [{"role": "user", "content": "hi"}],
        sampler={"max_tokens": 100},
        stop=many_stops,
        model="local",
    )
    assert len(local_payload["stop"]) == len(many_stops)


def test_local_llamacpp_stream_still_sends_return_progress():
    calls: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/props"):
            return httpx.Response(200, json={"total_slots": 4})
        if request.url.path.endswith("/chat/completions"):
            body = json.loads(request.content.decode("utf-8"))
            calls.append(body)
            sse = 'data: {"choices":[{"delta":{"content":"local"}}]}\n\ndata: [DONE]\n\n'
            return httpx.Response(200, text=sse, headers={"content-type": "text/event-stream"})
        return httpx.Response(404)

    cfg = {
        "base_url": "http://127.0.0.1:8080/v1",
        "api_key": "",
        "model_alias": "qwen3-local",
        "disable_thinking": True,
        "context_size": 32768,
        "timeout_seconds": 30,
    }
    tokens = list(llm_client.stream_chat_completion(
        cfg,
        [{"role": "user", "content": "oi"}],
        sampler={"temperature": 0.8, "max_tokens": 100},
        stop=[],
        transport=httpx.MockTransport(handler),
        role="narrative",
    ))
    assert "".join(tokens) == "local"
    assert len(calls) == 1
    assert calls[0]["return_progress"] is True
    assert "cache_prompt" in calls[0]
    assert "dry_multiplier" in calls[0]


def test_openrouter_and_xai_image_generation(tmp_path):
    png_bytes = b"\x89PNG\r\n\x1a\nfake-image"
    png_b64 = base64.b64encode(png_bytes).decode("ascii")
    or_calls: list[tuple[str, dict]] = []

    def or_handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        or_calls.append((request.url.path, body))
        return httpx.Response(200, json={
            "choices": [{
                "message": {
                    "role": "assistant",
                    "content": "Here is your image",
                    "images": [{"image_url": {"url": f"data:image/png;base64,{png_b64}"}}],
                }
            }]
        })

    or_cfg = {
        "provider": "openai_compatible",
        "openai_base_url": "https://openrouter.ai/api/v1",
        "openai_api_key": "sk-or-test",
        "openai_model": "google/gemini-2.5-flash-image-preview",
        "openai_size": "1024x1024",
        "storage_dir": str(tmp_path),
        "timeout_seconds": 30,
    }
    res_or = image_client.generate_image(
        or_cfg, "a castle at dusk", adventure_id="adv1", image_id="or1",
        transport=httpx.MockTransport(or_handler),
    )
    assert res_or["provider"] == "openai_compatible"
    assert or_calls[0][0].endswith("/chat/completions")
    assert or_calls[0][1]["modalities"] == ["image", "text"]

    # xAI omits `size` proactively
    xai_calls: list[dict] = []

    def xai_handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        xai_calls.append(body)
        return httpx.Response(200, json={"data": [{"b64_json": png_b64}]})

    xai_cfg = {
        "provider": "openai_compatible",
        "openai_base_url": "https://api.x.ai/v1",
        "openai_api_key": "xai-test",
        "openai_model": "grok-2-image",
        "openai_size": "1024x1024",
        "storage_dir": str(tmp_path),
        "timeout_seconds": 30,
    }
    res_xai = image_client.generate_image(
        xai_cfg, "a cyberpunk alley", adventure_id="adv1", image_id="xai1",
        transport=httpx.MockTransport(xai_handler),
    )
    assert res_xai["provider"] == "openai_compatible"
    assert "size" not in xai_calls[0]


def test_groq_tts_uses_wav_skips_drama3_and_normalizes_stt_language():
    calls: list[dict] = []
    wav_bytes = b"RIFF\x24\x00\x00\x00WAVEfmt "

    def tts_handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        calls.append(body)
        if "instructions" in body:
            return httpx.Response(400, json={"error": {"message": "Unknown field: instructions"}})
        assert body["response_format"] == "wav"
        assert body["model"] == "playai-tts"
        return httpx.Response(200, content=wav_bytes, headers={"content-type": "audio/wav"})

    cfg = {
        "base_url": "https://api.groq.com/openai/v1",
        "api_key": "gsk-test",
        "tts_model": "playai-tts",
        "stt_model": "whisper-large-v3-turbo",
        "narrator_voice": "Fritz-PlayAI",
        "drama3_enabled": True,
        "drama3_model": "drama-3",
        "timeout_seconds": 30,
    }
    audio = voice_client.synthesize(
        cfg, "Olá aventureiro", "Fritz-PlayAI", emotion="tenso",
        transport=httpx.MockTransport(tts_handler),
    )
    assert audio == wav_bytes
    assert len(calls) == 2
    assert calls[0]["model"] == "playai-tts"
    assert calls[0]["response_format"] == "wav"
    assert "instructions" not in calls[1]

    # STT normalizes BCP-47 locale "pt-BR" -> "pt" for Whisper
    captured_stt: list[bytes] = []

    def stt_handler(request: httpx.Request) -> httpx.Response:
        captured_stt.append(request.content)
        return httpx.Response(200, json={"text": "fala transcrita"})

    text = voice_client.transcribe(
        cfg, "audio.webm", "audio/webm", b"fake-audio", language="pt-BR",
        transport=httpx.MockTransport(stt_handler),
    )
    assert text == "fala transcrita"
    assert b'name="language"\r\n\r\npt\r\n' in captured_stt[0]


def test_voyage_embeddings_health_and_discover_fallback_when_models_404():
    paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        if request.url.path.endswith("/embeddings"):
            return httpx.Response(200, json={"data": [{"embedding": [0.1, 0.2]}]})
        return httpx.Response(404)

    transport = httpx.MockTransport(handler)
    cfg = {
        "enabled": True,
        "base_url": "https://api.voyageai.com/v1",
        "api_key": "pa-test",
        "model": "voyage-3-lite",
        "timeout_seconds": 10,
    }
    h = embeddings_client.health(cfg, transport=transport)
    assert h["status"] == "ok"
    assert any(p.endswith("/embeddings") for p in paths)

    disc = provider_models.discover_models(
        kind="embeddings",
        base_url="https://api.voyageai.com/v1",
        api_key="pa-test",
        transport=transport,
    )
    assert disc["status"] == "ok"
    assert any(m["id"] == "voyage-3-lite" for m in disc["models"])


