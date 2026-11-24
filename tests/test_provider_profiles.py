"""Testes de perfis de provedores: CRUD, reaproveitamento de API entre modalidades,
troca instantânea, captura do setup atual, sincronização e segurança de credenciais."""

from __future__ import annotations

import httpx
import pytest

from app import auth, db, deps, settings_store


@pytest.fixture()
def auth_token(admin_token):
    return admin_token


def test_non_admin_cannot_modify_profiles(client):
    cfg = auth.auth_config()
    tok = auth.make_token("player1", cfg["secret"], 24)
    user_hdr = {"Authorization": f"Bearer {tok}"}
    r = client.post("/api/settings/profiles", json={"name": "Nope"}, headers=user_hdr)
    assert r.status_code == 403


def test_list_profiles_empty_and_multimodal_templates(client):
    resp = client.get("/api/settings/profiles")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["profiles"] == []
    assert body["active"] == {"llm": None, "image": None, "voice": None, "embeddings": None}

    templates = {t["id"]: t for t in body["templates"]}
    assert set(templates["openai"]["capabilities"]) == {"llm", "image", "voice", "embeddings"}
    assert set(templates["together"]["capabilities"]) == {"llm", "image", "embeddings"}
    assert set(templates["groq"]["capabilities"]) == {"llm", "voice"}
    assert set(templates["openrouter"]["capabilities"]) == {"llm", "embeddings"}


def test_create_activate_reuse_single_api_key_across_all_four_modalities(client):
    secret = "sk-openai-shared-9999"
    resp = client.post("/api/settings/profiles", json={
        "name": "OpenAI Principal",
        "provider_id": "openai",
        "base_url": "https://api.openai.com/v1",
        "api_key": secret,
        "capabilities": ["llm", "image", "voice", "embeddings"],
        "llm": {"model_alias": "gpt-4o-mini"},
        "image": {"provider": "openai_compatible", "model": "gpt-image-1", "size": "1024x1024"},
        "voice": {
            "tts_model": "gpt-4o-mini-tts",
            "stt_model": "whisper-1",
            "narrator_voice": "nova",
            "voices": ["alloy", "nova", "shimmer"],
        },
        "embeddings": {"model": "text-embedding-3-small"},
    })
    assert resp.status_code == 200, resp.text
    data = resp.json()
    prof = data["profile"]
    prof_id = prof["id"]

    # Chave nunca sai em texto claro
    assert secret not in resp.text
    assert prof["api_key_set"] is True
    assert prof["api_key"] and "9999" in prof["api_key"]

    # Ativa em todas as 4 modalidades (body vazio usa todas as capabilities)
    act = client.post(f"/api/settings/profiles/{prof_id}/activate", json={})
    assert act.status_code == 200, act.text
    act_body = act.json()
    assert secret not in act.text
    assert set(act_body["activated"]) == {"llm", "image", "voice", "embeddings"}
    assert act_body["active"] == {
        "llm": prof_id,
        "image": prof_id,
        "voice": prof_id,
        "embeddings": prof_id,
    }

    # Confere no servidor que a MESMA api_key foi reaproveitada nas 4 modalidades
    with db.session() as conn:
        llm_cfg = settings_store.effective_llm_settings(conn)
        img_cfg = settings_store.effective_image_settings(conn)
        voice_cfg = settings_store.effective_voice_settings(conn)
        emb_cfg = settings_store.effective_embeddings_settings(conn)

    assert llm_cfg["base_url"] == "https://api.openai.com/v1"
    assert llm_cfg["api_key"] == secret
    assert llm_cfg["model_alias"] == "gpt-4o-mini"

    assert img_cfg["provider"] == "openai_compatible"
    assert img_cfg["openai_base_url"] == "https://api.openai.com/v1"
    assert img_cfg["openai_api_key"] == secret
    assert img_cfg["openai_model"] == "gpt-image-1"

    assert voice_cfg["base_url"] == "https://api.openai.com/v1"
    assert voice_cfg["api_key"] == secret
    assert voice_cfg["tts_model"] == "gpt-4o-mini-tts"
    assert voice_cfg["narrator_voice"] == "nova"
    assert "nova" in voice_cfg["voices"]

    assert emb_cfg["base_url"] == "https://api.openai.com/v1"
    assert emb_cfg["api_key"] == secret
    assert emb_cfg["model"] == "text-embedding-3-small"


def test_switch_profiles_without_losing_keys_and_rotate_key_propagates(client):
    openai_key = "sk-openai-keep-1111"
    or_key = "sk-openrouter-keep-2222"

    r1 = client.post("/api/settings/profiles", json={
        "name": "OpenAI",
        "provider_id": "openai",
        "base_url": "https://api.openai.com/v1",
        "api_key": openai_key,
        "capabilities": ["llm", "embeddings"],
        "llm": {"model_alias": "gpt-4o-mini"},
        "embeddings": {"model": "text-embedding-3-small"},
    })
    openai_id = r1.json()["profile"]["id"]

    r2 = client.post("/api/settings/profiles", json={
        "name": "OpenRouter",
        "provider_id": "openrouter",
        "base_url": "https://openrouter.ai/api/v1",
        "api_key": or_key,
        "capabilities": ["llm"],
        "llm": {"model_alias": "anthropic/claude-3.7-sonnet"},
    })
    or_id = r2.json()["profile"]["id"]

    # Ativa OpenAI em LLM + Embeddings
    assert client.post(f"/api/settings/profiles/{openai_id}/activate", json={}).status_code == 200

    # Troca SÓ LLM para OpenRouter
    sw = client.post(f"/api/settings/profiles/{or_id}/activate", json={"kinds": ["llm"]})
    assert sw.status_code == 200
    assert sw.json()["active"]["llm"] == or_id
    assert sw.json()["active"]["embeddings"] == openai_id

    with db.session() as conn:
        assert settings_store.effective_llm_settings(conn)["api_key"] == or_key
        assert settings_store.effective_llm_settings(conn)["model_alias"] == "anthropic/claude-3.7-sonnet"
        assert settings_store.effective_embeddings_settings(conn)["api_key"] == openai_key

    # Volta LLM para OpenAI em 1 clique — chave e modelo originais intactos
    back = client.post(f"/api/settings/profiles/{openai_id}/activate", json={"kinds": ["llm"]})
    assert back.status_code == 200
    with db.session() as conn:
        assert settings_store.effective_llm_settings(conn)["api_key"] == openai_key
        assert settings_store.effective_llm_settings(conn)["model_alias"] == "gpt-4o-mini"

    # Rotaciona a chave no perfil OpenAI -> propaga automaticamente para LLM e Embeddings ativos
    rotated = "sk-openai-rotated-7777"
    upd = client.put(f"/api/settings/profiles/{openai_id}", json={"api_key": rotated})
    assert upd.status_code == 200, upd.text
    assert rotated not in upd.text
    assert set(upd.json()["synced"]) == {"llm", "embeddings"}
    with db.session() as conn:
        assert settings_store.effective_llm_settings(conn)["api_key"] == rotated
        assert settings_store.effective_embeddings_settings(conn)["api_key"] == rotated


def test_tab_save_syncs_model_to_active_profile_and_unlinks_on_host_change(client):
    r1 = client.post("/api/settings/profiles", json={
        "name": "OpenAI",
        "base_url": "https://api.openai.com/v1",
        "api_key": "sk-openai-sync-3333",
        "capabilities": ["llm"],
        "llm": {"model_alias": "gpt-4o-mini"},
    })
    openai_id = r1.json()["profile"]["id"]
    client.post(f"/api/settings/profiles/{openai_id}/activate", json={"kinds": ["llm"]})

    # Troca o modelo na aba LLM e salva -> perfil lembra o modelo novo
    put = client.put("/api/settings/llm", json={"model_alias": "gpt-4.1-mini"})
    assert put.status_code == 200

    profs = client.get("/api/settings/profiles").json()
    assert profs["active"]["llm"] == openai_id
    assert profs["profiles"][0]["llm"]["model_alias"] == "gpt-4.1-mini"

    # Se trocar URL para outro host na aba, desvincula o perfil ativo sem corromper o perfil salvo
    put2 = client.put("/api/settings/llm", json={"base_url": "https://api.deepseek.com/v1", "model_alias": "deepseek-chat"})
    assert put2.status_code == 200
    profs2 = client.get("/api/settings/profiles").json()
    assert profs2["active"]["llm"] is None
    assert profs2["profiles"][0]["base_url"] == "https://api.openai.com/v1"
    assert profs2["profiles"][0]["llm"]["model_alias"] == "gpt-4.1-mini"


def test_discover_reuses_saved_profile_key_without_leaking_to_other_hosts(client):
    or_key = "sk-or-discover-5555"
    r = client.post("/api/settings/profiles", json={
        "name": "OpenRouter",
        "provider_id": "openrouter",
        "base_url": "https://openrouter.ai/api/v1",
        "api_key": or_key,
        "capabilities": ["llm"],
    })
    or_id = r.json()["profile"]["id"]

    seen_auth: list[tuple[str, str | None]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_auth.append((str(request.url), request.headers.get("authorization")))
        return httpx.Response(200, json={"data": [{"id": "openai/gpt-4o-mini"}]})

    client.app.dependency_overrides[deps.get_http_transport] = lambda: httpx.MockTransport(handler)
    try:
        # 1. Por profile_id explícito
        d1 = client.post("/api/settings/models/discover", json={"kind": "llm", "profile_id": or_id})
        assert d1.status_code == 200 and d1.json()["status"] == "ok"
        assert seen_auth[-1] == ("https://openrouter.ai/api/v1/models", f"Bearer {or_key}")

        # 2. Por base_url idêntica a um perfil salvo (mesmo sem profile_id)
        d2 = client.post("/api/settings/models/discover", json={
            "kind": "llm",
            "base_url": "https://openrouter.ai/api/v1",
        })
        assert d2.status_code == 200 and d2.json()["status"] == "ok"
        assert seen_auth[-1] == ("https://openrouter.ai/api/v1/models", f"Bearer {or_key}")

        # 3. Host diferente com profile_id -> NUNCA envia a chave do perfil para outro host
        d3 = client.post("/api/settings/models/discover", json={
            "kind": "llm",
            "profile_id": or_id,
            "base_url": "https://evil.example/v1",
        })
        assert d3.status_code == 200
        assert seen_auth[-1] == ("https://evil.example/v1/models", None)
    finally:
        client.app.dependency_overrides.pop(deps.get_http_transport, None)


def test_capture_current_preserves_distinct_local_keys_and_restores(client):
    # Configura chaves distintas em LLM e Voz (como na VM local)
    assert client.put("/api/settings/llm", json={
        "base_url": "http://vm.local:1234/v1",
        "api_key": "local-llm-secret-aaa",
        "model_alias": "qwen-local",
    }).status_code == 200
    assert client.put("/api/settings/voice", json={
        "base_url": "http://127.0.0.1:8001/v1",
        "api_key": "local-voice-secret-bbb",
        "tts_model": "omnivoice",
        "stt_model": "whisper-1",
    }).status_code == 200
    assert client.put("/api/settings/embeddings", json={
        "base_url": "http://127.0.0.1:8091/v1",
        "model": "Qwen3-Embedding-0.6B",
    }).status_code == 200

    cap = client.post("/api/settings/profiles/capture-current", json={"name": "Local VM"})
    assert cap.status_code == 200, cap.text
    assert "local-llm-secret-aaa" not in cap.text
    assert "local-voice-secret-bbb" not in cap.text
    local_id = cap.json()["profile"]["id"]

    # Troca para outro provedor (sobrescreve tudo)
    ext = client.post("/api/settings/profiles", json={
        "name": "Groq",
        "base_url": "https://api.groq.com/openai/v1",
        "api_key": "gsk-external-9999",
        "capabilities": ["llm", "voice"],
        "llm": {"model_alias": "llama-3.3-70b-versatile"},
        "voice": {"tts_model": "playai-tts", "narrator_voice": "Fritz-PlayAI", "voices": ["Fritz-PlayAI"]},
    }).json()["profile"]["id"]
    client.post(f"/api/settings/profiles/{ext}/activate", json={})

    # Restaura o perfil capturado "Local VM" -> ambas as chaves locais distintas voltam certinhas!
    restored = client.post(f"/api/settings/profiles/{local_id}/activate", json={})
    assert restored.status_code == 200, restored.text
    with db.session() as conn:
        assert settings_store.effective_llm_settings(conn)["base_url"] == "http://vm.local:1234/v1"
        assert settings_store.effective_llm_settings(conn)["api_key"] == "local-llm-secret-aaa"
        assert settings_store.effective_voice_settings(conn)["base_url"] == "http://127.0.0.1:8001/v1"
        assert settings_store.effective_voice_settings(conn)["api_key"] == "local-voice-secret-bbb"


def test_delete_profile_clears_active_reference(client):
    r = client.post("/api/settings/profiles", json={
        "name": "Temp",
        "base_url": "https://api.openai.com/v1",
        "api_key": "sk-temp-1234",
        "capabilities": ["llm"],
    })
    pid = r.json()["profile"]["id"]
    client.post(f"/api/settings/profiles/{pid}/activate", json={"kinds": ["llm"]})

    deleted = client.delete(f"/api/settings/profiles/{pid}")
    assert deleted.status_code == 200
    body = deleted.json()
    assert body["profiles"] == []
    assert body["active"]["llm"] is None
