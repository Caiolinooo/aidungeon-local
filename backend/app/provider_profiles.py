"""Perfis salvos de provedores de IA (LLM, Imagem, Voz e Embeddings).

Permite salvar múltiplos provedores com suas respectivas credenciais (API keys),
URLs e modelos favoritos, alternar entre eles sem perder configurações e
reaproveitar a mesma API key em todas as modalidades suportadas pelo provedor
(ex.: OpenAI em LLM + Imagem + Voz + Embeddings; Together/Fireworks em LLM +
Imagem + Embeddings; Groq em LLM + Voz; OpenRouter/Mistral/Gemini em LLM +
Embeddings).

Persistência: tabela `settings` nas chaves `providers.profiles` e
`providers.active` (sem migration de schema).
Segurança: `api_key` NUNCA é logada e SEMPRE sai mascarada nas respostas da API.
"""

from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlparse

from app import db, llm_client, provider_models, settings_store
from app.errors import GameError

PROFILES_KEY = "providers.profiles"
ACTIVE_KEY = "providers.active"
VALID_KINDS = ("llm", "image", "voice", "embeddings")
VALID_IMAGE_PROVIDERS = ("openai_compatible", "comfyui", "sdwebui")


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _norm_url(url: str | None) -> str:
    return str(url or "").strip().rstrip("/").lower()


def _url_host(url: str | None) -> str:
    try:
        parsed = urlparse(str(url or "").strip())
        host = (parsed.hostname or "").lower()
        port = parsed.port
        return f"{host}:{port}" if port else host
    except ValueError:
        return ""


def _slugify(text: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", (text or "").strip().lower()).strip("-")
    return (slug[:48] or "profile")


def _load_raw_profiles(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    raw = db.get_setting(conn, PROFILES_KEY)
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
    except ValueError:
        return []
    if not isinstance(parsed, list):
        return []
    out: list[dict[str, Any]] = []
    for item in parsed:
        if isinstance(item, dict) and isinstance(item.get("id"), str) and item["id"].strip():
            out.append(item)
    return out


def _save_raw_profiles(conn: sqlite3.Connection, profiles: list[dict[str, Any]]) -> None:
    db.set_setting(conn, PROFILES_KEY, json.dumps(profiles, ensure_ascii=False))


def _load_active_map(conn: sqlite3.Connection) -> dict[str, str | None]:
    raw = db.get_setting(conn, ACTIVE_KEY)
    base: dict[str, str | None] = {k: None for k in VALID_KINDS}
    if not raw:
        return base
    try:
        parsed = json.loads(raw)
    except ValueError:
        return base
    if isinstance(parsed, dict):
        for k in VALID_KINDS:
            val = parsed.get(k)
            if isinstance(val, str) and val.strip():
                base[k] = val.strip()
    return base


def _save_active_map(conn: sqlite3.Connection, active: dict[str, str | None]) -> None:
    cleaned = {k: (active.get(k) or None) for k in VALID_KINDS}
    db.set_setting(conn, ACTIVE_KEY, json.dumps(cleaned, ensure_ascii=False))


def _clean_voices(raw: Any) -> list[str]:
    if not isinstance(raw, list):
        return []
    return list(dict.fromkeys(
        str(v).strip() for v in raw if isinstance(v, str) and str(v).strip()
    ))


def _normalize_capabilities(raw: Any) -> list[str]:
    if not isinstance(raw, list):
        return ["llm"]
    caps = [k for k in VALID_KINDS if k in raw]
    return caps or ["llm"]


def _merge_secret(existing: str | None, incoming: str | None) -> str | None:
    """Mesma semântica de _apply_key: None mantém, '' apaga, ASCII salva, mascarada ignora."""
    if incoming is None:
        return existing
    if incoming == "":
        return None
    usable = settings_store._usable_secret(incoming)
    if usable is None:
        return existing
    return usable


def _mask_block(block: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(block, dict):
        return {"api_key": None, "api_key_set": False}
    out = dict(block)
    secret = out.get("api_key")
    out["api_key"] = settings_store.mask_api_key(secret)
    out["api_key_set"] = bool(secret)
    return out


def mask_profile(profile: dict[str, Any]) -> dict[str, Any]:
    """Versão segura para resposta HTTP — todas as chaves sempre mascaradas."""
    secret = profile.get("api_key")
    out: dict[str, Any] = {
        "id": profile["id"],
        "name": str(profile.get("name") or profile["id"]),
        "provider_id": str(profile.get("provider_id") or "custom"),
        "base_url": str(profile.get("base_url") or ""),
        "api_key": settings_store.mask_api_key(secret),
        "api_key_set": bool(secret),
        "capabilities": _normalize_capabilities(profile.get("capabilities")),
        "updated_at": str(profile.get("updated_at") or ""),
    }
    for kind in VALID_KINDS:
        out[kind] = _mask_block(profile.get(kind))
    return out


def profile_templates() -> list[dict[str, Any]]:
    """Catálogo unificado de provedores com suporte multi-modalidade para criação rápida de perfis."""
    file_cfg = settings_store.load_file_settings()
    llm_cfg = file_cfg.get("llm", {}) if isinstance(file_cfg.get("llm"), dict) else {}
    img_cfg = file_cfg.get("image", {}) if isinstance(file_cfg.get("image"), dict) else {}
    img_provs = img_cfg.get("providers", {}) if isinstance(img_cfg.get("providers"), dict) else {}
    voice_cfg = file_cfg.get("voice", {}) if isinstance(file_cfg.get("voice"), dict) else {}
    emb_cfg = file_cfg.get("embeddings", {}) if isinstance(file_cfg.get("embeddings"), dict) else {}

    llm_default_url = str(llm_cfg.get("base_url") or "http://127.0.0.1:1234/v1")
    comfy_default_url = str((img_provs.get("comfyui") or {}).get("base_url") or "http://127.0.0.1:8188")
    sdwebui_default_url = str((img_provs.get("sdwebui") or {}).get("base_url") or "http://127.0.0.1:7860")
    voice_default_url = str(voice_cfg.get("base_url") or "http://127.0.0.1:8000/v1")
    voice_default_voices = _clean_voices(voice_cfg.get("voices")) or ["M1", "M2", "M3", "F1", "F2", "F3"]
    emb_default_url = str(emb_cfg.get("base_url") or "http://127.0.0.1:8091/v1")
    emb_default_model = str(emb_cfg.get("model") or "Qwen3-Embedding-0.6B")

    return [
        {
            "id": "openai",
            "name": "OpenAI",
            "base_url": "https://api.openai.com/v1",
            "requires_api_key": True,
            "capabilities": ["llm", "image", "voice", "embeddings"],
            "llm": {"model_alias": "gpt-4o-mini"},
            "image": {"provider": "openai_compatible", "model": "gpt-image-1", "size": "1024x1024"},
            "voice": {
                "tts_model": "gpt-4o-mini-tts",
                "stt_model": "whisper-1",
                "narrator_voice": "nova",
                "voices": list(provider_models.OPENAI_TTS_VOICES),
            },
            "embeddings": {"model": "text-embedding-3-small"},
        },
        {
            "id": "openrouter",
            "name": "OpenRouter",
            "base_url": "https://openrouter.ai/api/v1",
            "requires_api_key": True,
            "capabilities": ["llm", "embeddings"],
            "llm": {"model_alias": "openai/gpt-4o-mini"},
            "embeddings": {"model": "openai/text-embedding-3-small"},
        },
        {
            "id": "together",
            "name": "Together AI",
            "base_url": "https://api.together.xyz/v1",
            "requires_api_key": True,
            "capabilities": ["llm", "image", "embeddings"],
            "llm": {"model_alias": "meta-llama/Llama-3.3-70B-Instruct-Turbo"},
            "image": {
                "provider": "openai_compatible",
                "model": "black-forest-labs/FLUX.1-schnell-Free",
                "size": "1024x1024",
            },
            "embeddings": {"model": "BAAI/bge-large-en-v1.5"},
        },
        {
            "id": "fireworks",
            "name": "Fireworks AI",
            "base_url": "https://api.fireworks.ai/inference/v1",
            "requires_api_key": True,
            "capabilities": ["llm", "image", "embeddings"],
            "llm": {"model_alias": "accounts/fireworks/models/llama-v3p3-70b-instruct"},
            "image": {
                "provider": "openai_compatible",
                "model": "accounts/fireworks/models/flux-1-schnell-fp8",
                "size": "1024x1024",
            },
            "embeddings": {"model": "nomic-ai/nomic-embed-text-v1.5"},
        },
        {
            "id": "groq",
            "name": "Groq",
            "base_url": "https://api.groq.com/openai/v1",
            "requires_api_key": True,
            "capabilities": ["llm", "voice"],
            "llm": {"model_alias": "llama-3.3-70b-versatile"},
            "voice": {
                "tts_model": "playai-tts",
                "stt_model": "whisper-large-v3-turbo",
                "narrator_voice": "Fritz-PlayAI",
                "voices": list(provider_models.GROQ_TTS_VOICES),
            },
        },
        {
            "id": "mistral",
            "name": "Mistral AI",
            "base_url": "https://api.mistral.ai/v1",
            "requires_api_key": True,
            "capabilities": ["llm", "embeddings"],
            "llm": {"model_alias": "mistral-large-latest"},
            "embeddings": {"model": "mistral-embed"},
        },
        {
            "id": "xai",
            "name": "xAI (Grok)",
            "base_url": "https://api.x.ai/v1",
            "requires_api_key": True,
            "capabilities": ["llm", "image"],
            "llm": {"model_alias": "grok-3-mini"},
            "image": {"provider": "openai_compatible", "model": "grok-2-image", "size": "1024x1024"},
        },
        {
            "id": "deepinfra",
            "name": "DeepInfra",
            "base_url": "https://api.deepinfra.com/v1/openai",
            "requires_api_key": True,
            "capabilities": ["llm", "voice", "embeddings"],
            "llm": {"model_alias": "meta-llama/Llama-3.3-70B-Instruct"},
            "voice": {
                "tts_model": "hexgrad/Kokoro-82M",
                "stt_model": "openai/whisper-large-v3-turbo",
                "narrator_voice": "af_heart",
                "voices": list(provider_models.KOKORO_VOICES),
            },
            "embeddings": {"model": "BAAI/bge-large-en-v1.5"},
        },
        {
            "id": "gemini",
            "name": "Google Gemini",
            "base_url": "https://generativelanguage.googleapis.com/v1beta/openai",
            "requires_api_key": True,
            "capabilities": ["llm", "embeddings"],
            "llm": {"model_alias": "gemini-2.5-flash"},
            "embeddings": {"model": "text-embedding-004"},
        },
        {
            "id": "deepseek",
            "name": "DeepSeek",
            "base_url": "https://api.deepseek.com/v1",
            "requires_api_key": True,
            "capabilities": ["llm"],
            "llm": {"model_alias": "deepseek-chat"},
        },
        {
            "id": "nebius",
            "name": "Nebius Studio",
            "base_url": "https://api.studio.nebius.com/v1",
            "requires_api_key": True,
            "capabilities": ["llm", "image"],
            "llm": {"model_alias": "meta-llama/Llama-3.3-70B-Instruct"},
            "image": {
                "provider": "openai_compatible",
                "model": "black-forest-labs/flux-schnell",
                "size": "1024x1024",
            },
        },
        {
            "id": "voyage",
            "name": "Voyage AI",
            "base_url": "https://api.voyageai.com/v1",
            "requires_api_key": True,
            "capabilities": ["embeddings"],
            "embeddings": {"model": "voyage-3-lite"},
        },
        {
            "id": "jina",
            "name": "Jina AI",
            "base_url": "https://api.jina.ai/v1",
            "requires_api_key": True,
            "capabilities": ["embeddings"],
            "embeddings": {"model": "jina-embeddings-v3"},
        },
        {
            "id": "ollama",
            "name": "Ollama (Local)",
            "base_url": "http://127.0.0.1:11434/v1",
            "requires_api_key": False,
            "capabilities": ["llm", "embeddings"],
            "llm": {"model_alias": "auto"},
            "embeddings": {"model": "nomic-embed-text"},
        },
        {
            "id": "lmstudio",
            "name": "LM Studio (Local)",
            "base_url": "http://127.0.0.1:1234/v1",
            "requires_api_key": False,
            "capabilities": ["llm", "embeddings"],
            "llm": {"model_alias": "auto"},
            "embeddings": {"model": "text-embedding-nomic-embed-text-v1.5"},
        },
        {
            "id": "local_vm",
            "name": "Local / VM",
            "base_url": llm_default_url,
            "requires_api_key": False,
            "capabilities": ["llm", "image", "voice", "embeddings"],
            "llm": {"base_url": llm_default_url, "model_alias": "auto"},
            "image": {"provider": "comfyui", "base_url": comfy_default_url},
            "voice": {
                "base_url": voice_default_url,
                "tts_model": str(voice_cfg.get("tts_model") or "omnivoice"),
                "stt_model": str(voice_cfg.get("stt_model") or "whisper-1"),
                "narrator_voice": str(voice_cfg.get("narrator_voice") or voice_default_voices[0]),
                "voices": voice_default_voices,
            },
            "embeddings": {"base_url": emb_default_url, "model": emb_default_model},
        },
        {
            "id": "comfyui",
            "name": "ComfyUI (Local)",
            "base_url": comfy_default_url,
            "requires_api_key": False,
            "capabilities": ["image"],
            "image": {"provider": "comfyui", "base_url": comfy_default_url},
        },
        {
            "id": "sdwebui",
            "name": "SD WebUI (Local)",
            "base_url": sdwebui_default_url,
            "requires_api_key": False,
            "capabilities": ["image"],
            "image": {"provider": "sdwebui", "base_url": sdwebui_default_url},
        },
        {
            "id": "kokoro_local",
            "name": "Kokoro-FastAPI (Local)",
            "base_url": "http://127.0.0.1:8880/v1",
            "requires_api_key": False,
            "capabilities": ["voice"],
            "voice": {
                "tts_model": "kokoro",
                "stt_model": "whisper-1",
                "narrator_voice": "pf_dora",
                "voices": list(provider_models.KOKORO_VOICES),
            },
        },
        {
            "id": "custom",
            "name": "Provedor customizado",
            "base_url": "",
            "requires_api_key": False,
            "capabilities": ["llm"],
        },
    ]


def effective_profile_url(profile: dict[str, Any], kind: str) -> str:
    block = profile.get(kind) if isinstance(profile.get(kind), dict) else {}
    return str(block.get("base_url") or profile.get("base_url") or "").strip()


def effective_profile_key(profile: dict[str, Any], kind: str) -> str | None:
    """Reaproveita a api_key principal do perfil quando a modalidade não define override."""
    block = profile.get(kind) if isinstance(profile.get(kind), dict) else {}
    override = settings_store._usable_secret(block.get("api_key"))
    if override is not None:
        return override
    return settings_store._usable_secret(profile.get("api_key"))


def list_profiles(conn: sqlite3.Connection) -> dict[str, Any]:
    raw = _load_raw_profiles(conn)
    ids = {p["id"] for p in raw}
    active = _load_active_map(conn)
    active = {k: (v if v in ids else None) for k, v in active.items()}
    return {
        "profiles": [mask_profile(p) for p in raw],
        "active": active,
        "templates": profile_templates(),
    }


def get_raw_profile(conn: sqlite3.Connection, profile_id: str) -> dict[str, Any] | None:
    for item in _load_raw_profiles(conn):
        if item["id"] == profile_id:
            return item
    return None


def find_api_key_for_url(
    conn: sqlite3.Connection,
    kind: str,
    base_url: str,
    profile_id: str | None = None,
) -> tuple[str | None, str | None]:
    """Encontra chave de um perfil salvo sem vazar segredos para outro host.

    Retorna `(api_key, resolved_base_url)`.
    1. Se `profile_id` é informado: usa a chave desse perfil quando `base_url` está
       vazio ou aponta para o mesmo host do perfil.
    2. Senão, se `base_url` normalizada bate exatamente com algum perfil salvo
       (seja na `base_url` da modalidade ou na `base_url` principal), reutiliza a
       chave daquele perfil.
    """
    profiles = _load_raw_profiles(conn)
    norm_req = _norm_url(base_url)
    host_req = _url_host(base_url)

    if profile_id:
        for prof in profiles:
            if prof["id"] != profile_id:
                continue
            prof_url = effective_profile_url(prof, kind)
            prof_host = _url_host(prof_url)
            if not norm_req:
                return effective_profile_key(prof, kind), prof_url
            if prof_host and host_req == prof_host:
                return effective_profile_key(prof, kind), base_url
            # Host diferente do perfil — não envia a chave do perfil para outro host.
            return None, base_url

    if norm_req:
        for prof in profiles:
            cand_urls = {
                _norm_url(effective_profile_url(prof, kind)),
                _norm_url(prof.get("base_url")),
            }
            if norm_req in cand_urls:
                key = effective_profile_key(prof, kind)
                if key:
                    return key, base_url
    return None, base_url


def _current_key_for_kind(conn: sqlite3.Connection, kind: str) -> str | None:
    if kind == "llm":
        return settings_store._usable_secret(settings_store.effective_llm_settings(conn).get("api_key"))
    if kind == "image":
        return settings_store._usable_secret(settings_store.effective_image_settings(conn).get("openai_api_key"))
    if kind == "voice":
        return settings_store._usable_secret(settings_store.effective_voice_settings(conn).get("api_key"))
    if kind == "embeddings":
        return settings_store._usable_secret(settings_store.effective_embeddings_settings(conn).get("api_key"))
    return None


def _merge_kind_block(
    kind: str,
    existing: dict[str, Any] | None,
    incoming: dict[str, Any] | None,
) -> dict[str, Any]:
    base = dict(existing) if isinstance(existing, dict) else {}
    if not isinstance(incoming, dict):
        return base

    if "base_url" in incoming and incoming["base_url"] is not None:
        base["base_url"] = str(incoming["base_url"]).strip()
    if "api_key" in incoming:
        merged_key = _merge_secret(base.get("api_key"), incoming.get("api_key"))
        if merged_key:
            base["api_key"] = merged_key
        else:
            base.pop("api_key", None)

    if kind == "llm":
        if incoming.get("model_alias") is not None:
            base["model_alias"] = str(incoming["model_alias"]).strip()
    elif kind == "image":
        if incoming.get("provider") in VALID_IMAGE_PROVIDERS:
            base["provider"] = incoming["provider"]
        if incoming.get("model") is not None:
            base["model"] = str(incoming["model"]).strip()
        if incoming.get("size") is not None:
            base["size"] = str(incoming["size"]).strip()
        if incoming.get("sd_model_checkpoint") is not None:
            base["sd_model_checkpoint"] = str(incoming["sd_model_checkpoint"]).strip()
    elif kind == "voice":
        if incoming.get("tts_model") is not None:
            base["tts_model"] = str(incoming["tts_model"]).strip()
        if incoming.get("stt_model") is not None:
            base["stt_model"] = str(incoming["stt_model"]).strip()
        if incoming.get("narrator_voice") is not None:
            base["narrator_voice"] = str(incoming["narrator_voice"]).strip()
        if incoming.get("voices") is not None:
            cleaned = _clean_voices(incoming["voices"])
            if cleaned:
                base["voices"] = cleaned
    elif kind == "embeddings":
        if incoming.get("model") is not None:
            base["model"] = str(incoming["model"]).strip()

    return base


def _apply_profile_to_kind(conn: sqlite3.Connection, profile: dict[str, Any], kind: str) -> None:
    url = effective_profile_url(profile, kind)
    key = effective_profile_key(profile, kind)
    block = profile.get(kind) if isinstance(profile.get(kind), dict) else {}

    if kind == "llm":
        update: dict[str, Any] = {"api_key": key or ""}
        if url:
            update["base_url"] = url
        model_alias = str(block.get("model_alias") or "").strip()
        if model_alias:
            update["model_alias"] = model_alias
        settings_store.apply_llm_update(conn, update)
        llm_client.invalidate_discovery_cache()

    elif kind == "image":
        img_provider = str(block.get("provider") or "openai_compatible")
        if img_provider not in VALID_IMAGE_PROVIDERS:
            img_provider = "openai_compatible"
        update = {"provider": img_provider}
        if img_provider == "comfyui":
            if url:
                update["comfyui_base_url"] = url
        elif img_provider == "sdwebui":
            if url:
                update["sdwebui_base_url"] = url
            ckpt = str(block.get("sd_model_checkpoint") or "").strip()
            if ckpt:
                update["sd_model_checkpoint"] = ckpt
        else:
            if url:
                update["openai_base_url"] = url
            update["openai_api_key"] = key or ""
            model = str(block.get("model") or "").strip()
            if model:
                update["openai_model"] = model
            size = str(block.get("size") or "").strip()
            if size:
                update["openai_size"] = size
        settings_store.apply_image_update(conn, update)

    elif kind == "voice":
        update = {"api_key": key or ""}
        if url:
            update["base_url"] = url
        tts_model = str(block.get("tts_model") or "").strip()
        if tts_model:
            update["tts_model"] = tts_model
        stt_model = str(block.get("stt_model") or "").strip()
        if stt_model:
            update["stt_model"] = stt_model
        voices = _clean_voices(block.get("voices"))
        narrator = str(block.get("narrator_voice") or "").strip()
        if narrator and voices and narrator not in voices:
            voices = [narrator, *voices]
        elif narrator and not voices:
            voices = [narrator]
        if voices:
            update["voices"] = voices
        if narrator:
            update["narrator_voice"] = narrator
        elif voices:
            update["narrator_voice"] = voices[0]
        settings_store.apply_voice_update(conn, update)

    elif kind == "embeddings":
        update = {"api_key": key or ""}
        if url:
            update["base_url"] = url
        model = str(block.get("model") or "").strip()
        if model:
            update["model"] = model
        settings_store.apply_embeddings_update(conn, update)


def _unique_id(base_slug: str, existing_ids: set[str]) -> str:
    if base_slug not in existing_ids:
        return base_slug
    idx = 2
    while f"{base_slug}-{idx}" in existing_ids:
        idx += 1
    return f"{base_slug}-{idx}"


def upsert_profile(
    conn: sqlite3.Connection,
    data: dict[str, Any],
    *,
    profile_id: str | None = None,
    sync_active: bool = True,
) -> tuple[dict[str, Any], list[str]]:
    """Cria ou atualiza um perfil de provedor.

    Retorna `(perfil_mascarado, modalidades_sincronizadas)`.
    """
    profiles = _load_raw_profiles(conn)
    existing_idx = next((i for i, p in enumerate(profiles) if p["id"] == profile_id), None) if profile_id else None

    if profile_id is not None and existing_idx is None:
        raise GameError.not_found("profile_not_found", f"Perfil {profile_id!r} não encontrado.")

    existing = profiles[existing_idx] if existing_idx is not None else {}

    name = str(data.get("name") if data.get("name") is not None else existing.get("name") or "").strip()
    if not name:
        raise GameError.bad_request("invalid_profile", "Nome do perfil é obrigatório.")

    provider_id = str(
        data.get("provider_id")
        if data.get("provider_id") is not None
        else existing.get("provider_id") or "custom"
    ).strip() or "custom"

    base_url = str(
        data.get("base_url")
        if data.get("base_url") is not None
        else existing.get("base_url") or ""
    ).strip()

    if existing_idx is not None:
        final_id = existing["id"]
    else:
        requested_id = str(data.get("id") or "").strip()
        base_slug = _slugify(requested_id or name or provider_id)
        final_id = _unique_id(base_slug, {p["id"] for p in profiles})

    capabilities = _normalize_capabilities(
        data.get("capabilities")
        if data.get("capabilities") is not None
        else existing.get("capabilities")
    )

    api_key = _merge_secret(existing.get("api_key"), data.get("api_key"))
    inherit_from = data.get("inherit_key_from")
    if api_key is None and inherit_from in VALID_KINDS:
        api_key = _current_key_for_kind(conn, inherit_from)

    profile: dict[str, Any] = {
        "id": final_id,
        "name": name,
        "provider_id": provider_id,
        "base_url": base_url,
        "capabilities": capabilities,
        "updated_at": _now_iso(),
    }
    if api_key:
        profile["api_key"] = api_key

    for kind in VALID_KINDS:
        merged_block = _merge_kind_block(kind, existing.get(kind), data.get(kind))
        if merged_block:
            profile[kind] = merged_block

    if existing_idx is not None:
        profiles[existing_idx] = profile
    else:
        profiles.append(profile)
    _save_raw_profiles(conn, profiles)

    synced: list[str] = []
    if sync_active:
        active = _load_active_map(conn)
        for kind in VALID_KINDS:
            if active.get(kind) == final_id and kind in capabilities:
                _apply_profile_to_kind(conn, profile, kind)
                synced.append(kind)

    return mask_profile(profile), synced


def delete_profile(conn: sqlite3.Connection, profile_id: str) -> dict[str, Any]:
    profiles = _load_raw_profiles(conn)
    remaining = [p for p in profiles if p["id"] != profile_id]
    if len(remaining) == len(profiles):
        raise GameError.not_found("profile_not_found", f"Perfil {profile_id!r} não encontrado.")
    _save_raw_profiles(conn, remaining)

    active = _load_active_map(conn)
    changed_active = False
    for kind in VALID_KINDS:
        if active.get(kind) == profile_id:
            active[kind] = None
            changed_active = True
    if changed_active:
        _save_active_map(conn, active)

    return list_profiles(conn)


def activate_profile(
    conn: sqlite3.Connection,
    profile_id: str,
    kinds: list[str] | None = None,
) -> tuple[list[str], dict[str, str | None]]:
    """Ativa o perfil nas modalidades pedidas (ou em todas as capabilities do perfil)."""
    profile = get_raw_profile(conn, profile_id)
    if profile is None:
        raise GameError.not_found("profile_not_found", f"Perfil {profile_id!r} não encontrado.")

    caps = _normalize_capabilities(profile.get("capabilities"))
    target_kinds = [k for k in (kinds or caps) if k in VALID_KINDS]
    if not target_kinds:
        raise GameError.bad_request("invalid_kinds", "Nenhuma modalidade válida informada.")

    active = _load_active_map(conn)
    activated: list[str] = []
    for kind in target_kinds:
        _apply_profile_to_kind(conn, profile, kind)
        active[kind] = profile_id
        activated.append(kind)

    _save_active_map(conn, active)
    return activated, active


def capture_current_as_profile(
    conn: sqlite3.Connection,
    *,
    name: str,
    profile_id: str | None = None,
    kinds: list[str] | None = None,
    activate: bool = True,
) -> dict[str, Any]:
    """Captura o setup atual de uma ou todas as modalidades em um perfil salvo.

    Se todas as modalidades capturadas que possuem chave usarem a mesma `api_key`,
    ela fica centralizada no nível principal do perfil; se diferirem (ex.: VM local
    com LLM_API_KEY diferente de VOICE_API_KEY), preserva cada chave no seu bloco.
    """
    target_kinds = [k for k in (kinds or list(VALID_KINDS)) if k in VALID_KINDS]
    if not target_kinds:
        raise GameError.bad_request("invalid_kinds", "Nenhuma modalidade válida para capturar.")

    llm_cfg = settings_store.effective_llm_settings(conn) if "llm" in target_kinds else None
    img_cfg = settings_store.effective_image_settings(conn) if "image" in target_kinds else None
    voice_cfg = settings_store.effective_voice_settings(conn) if "voice" in target_kinds else None
    emb_cfg = settings_store.effective_embeddings_settings(conn) if "embeddings" in target_kinds else None

    keys_by_kind: dict[str, str | None] = {}
    urls_by_kind: dict[str, str] = {}
    payload: dict[str, Any] = {
        "name": name.strip() or "Setup atual",
        "provider_id": "custom",
        "capabilities": target_kinds,
    }

    if llm_cfg is not None:
        url = str(llm_cfg.get("base_url") or "").strip()
        urls_by_kind["llm"] = url
        keys_by_kind["llm"] = settings_store._usable_secret(llm_cfg.get("api_key"))
        payload["llm"] = {
            "base_url": url,
            "model_alias": str(llm_cfg.get("model_alias") or "auto"),
        }

    if img_cfg is not None:
        prov = str(img_cfg.get("provider") or "comfyui")
        if prov == "comfyui":
            url = str(img_cfg.get("comfyui_base_url") or "").strip()
            img_key = None
            img_block: dict[str, Any] = {"provider": "comfyui", "base_url": url}
        elif prov == "sdwebui":
            url = str(img_cfg.get("sdwebui_base_url") or "").strip()
            img_key = None
            img_block = {
                "provider": "sdwebui",
                "base_url": url,
                "sd_model_checkpoint": str(img_cfg.get("sd_model_checkpoint") or ""),
            }
        else:
            url = str(img_cfg.get("openai_base_url") or "").strip()
            img_key = settings_store._usable_secret(img_cfg.get("openai_api_key"))
            img_block = {
                "provider": "openai_compatible",
                "base_url": url,
                "model": str(img_cfg.get("openai_model") or ""),
                "size": str(img_cfg.get("openai_size") or "1024x1024"),
            }
        urls_by_kind["image"] = url
        keys_by_kind["image"] = img_key
        payload["image"] = img_block

    if voice_cfg is not None:
        url = str(voice_cfg.get("base_url") or "").strip()
        urls_by_kind["voice"] = url
        keys_by_kind["voice"] = settings_store._usable_secret(voice_cfg.get("api_key"))
        payload["voice"] = {
            "base_url": url,
            "tts_model": str(voice_cfg.get("tts_model") or ""),
            "stt_model": str(voice_cfg.get("stt_model") or ""),
            "narrator_voice": str(voice_cfg.get("narrator_voice") or ""),
            "voices": list(voice_cfg.get("voices") or []),
        }

    if emb_cfg is not None:
        url = str(emb_cfg.get("base_url") or "").strip()
        urls_by_kind["embeddings"] = url
        keys_by_kind["embeddings"] = settings_store._usable_secret(emb_cfg.get("api_key"))
        payload["embeddings"] = {
            "base_url": url,
            "model": str(emb_cfg.get("model") or ""),
        }

    # Define URL principal e limpa base_url redundante nos blocos iguais à principal
    non_empty_urls = [u for u in urls_by_kind.values() if u]
    primary_url = urls_by_kind.get("llm") or (non_empty_urls[0] if non_empty_urls else "")
    payload["base_url"] = primary_url
    for k in target_kinds:
        if k in payload and _norm_url(payload[k].get("base_url")) == _norm_url(primary_url):
            payload[k]["base_url"] = ""

    # Centraliza api_key se todas as modalidades com chave compartilharem a mesma
    distinct_keys = list(dict.fromkeys(v for v in keys_by_kind.values() if v))
    if len(distinct_keys) == 1:
        payload["api_key"] = distinct_keys[0]
    elif len(distinct_keys) > 1:
        payload["api_key"] = keys_by_kind.get("llm") or distinct_keys[0]
        for k, k_val in keys_by_kind.items():
            if k_val and k_val != payload["api_key"]:
                payload[k]["api_key"] = k_val

    masked, _ = upsert_profile(conn, payload, profile_id=profile_id, sync_active=False)

    if activate:
        active = _load_active_map(conn)
        for k in target_kinds:
            active[k] = masked["id"]
        _save_active_map(conn, active)

    return masked


def sync_active_profile_from_settings(
    conn: sqlite3.Connection,
    kind: str,
    update: dict[str, Any],
) -> None:
    """Mantém o perfil ativo em sincronia quando o usuário salva alterações numa aba.

    - Se a URL mudou para outro host, desvincula o perfil ativo daquela modalidade
      (não corrompe o perfil salvo anterior).
    - Se o host continua igual (ou a URL não mudou), salva no perfil ativo o novo
      modelo / vozes / tamanho (e chave nova, se enviada) para lembrar na próxima troca.
    """
    if kind not in VALID_KINDS:
        return
    active = _load_active_map(conn)
    prof_id = active.get(kind)
    if not prof_id:
        return

    profiles = _load_raw_profiles(conn)
    idx = next((i for i, p in enumerate(profiles) if p["id"] == prof_id), None)
    if idx is None:
        return

    profile = profiles[idx]
    prof_url = effective_profile_url(profile, kind)
    prof_host = _url_host(prof_url)

    incoming_url: str | None = None
    if kind == "llm" and update.get("base_url") is not None:
        incoming_url = str(update["base_url"]).strip()
    elif kind == "voice" and update.get("base_url") is not None:
        incoming_url = str(update["base_url"]).strip()
    elif kind == "embeddings" and update.get("base_url") is not None:
        incoming_url = str(update["base_url"]).strip()
    elif kind == "image":
        block_prov = (profile.get("image") or {}).get("provider") or "openai_compatible"
        incoming_prov = update.get("provider")
        if incoming_prov and incoming_prov != block_prov:
            active[kind] = None
            _save_active_map(conn, active)
            return
        if block_prov == "comfyui" and update.get("comfyui_base_url") is not None:
            incoming_url = str(update["comfyui_base_url"]).strip()
        elif block_prov == "sdwebui" and update.get("sdwebui_base_url") is not None:
            incoming_url = str(update["sdwebui_base_url"]).strip()
        elif block_prov == "openai_compatible" and update.get("openai_base_url") is not None:
            incoming_url = str(update["openai_base_url"]).strip()

    if incoming_url is not None and prof_host and _url_host(incoming_url) != prof_host:
        active[kind] = None
        _save_active_map(conn, active)
        return

    block = dict(profile.get(kind)) if isinstance(profile.get(kind), dict) else {}
    changed = False

    def _sync_key(new_val: Any) -> None:
        nonlocal changed
        new_key = settings_store._usable_secret(new_val)
        if not new_key:
            return
        if block.get("api_key"):
            block["api_key"] = new_key
        else:
            profile["api_key"] = new_key
        changed = True

    if kind == "llm":
        if update.get("model_alias"):
            block["model_alias"] = str(update["model_alias"]).strip()
            changed = True
        _sync_key(update.get("api_key"))
    elif kind == "image":
        prov = str(block.get("provider") or update.get("provider") or "openai_compatible")
        if prov == "openai_compatible":
            if update.get("openai_model"):
                block["model"] = str(update["openai_model"]).strip()
                changed = True
            if update.get("openai_size"):
                block["size"] = str(update["openai_size"]).strip()
                changed = True
            _sync_key(update.get("openai_api_key"))
        elif prov == "sdwebui" and update.get("sd_model_checkpoint"):
            block["sd_model_checkpoint"] = str(update["sd_model_checkpoint"]).strip()
            changed = True
    elif kind == "voice":
        if update.get("tts_model"):
            block["tts_model"] = str(update["tts_model"]).strip()
            changed = True
        if update.get("stt_model"):
            block["stt_model"] = str(update["stt_model"]).strip()
            changed = True
        if update.get("narrator_voice"):
            block["narrator_voice"] = str(update["narrator_voice"]).strip()
            changed = True
        if update.get("voices") is not None:
            cleaned = _clean_voices(update["voices"])
            if cleaned:
                block["voices"] = cleaned
                changed = True
        _sync_key(update.get("api_key"))
    elif kind == "embeddings":
        if update.get("model"):
            block["model"] = str(update["model"]).strip()
            changed = True
        _sync_key(update.get("api_key"))

    if changed:
        profile[kind] = block
        profile["updated_at"] = _now_iso()
        profiles[idx] = profile
        _save_raw_profiles(conn, profiles)
