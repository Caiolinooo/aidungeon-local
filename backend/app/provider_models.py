"""Descoberta, pesquisa e presets de provedores de modelos (LLM, imagem, voz, embeddings).

Permite listar e pesquisar modelos diretamente das APIs dos provedores configurados
(OpenRouter, OpenAI, Groq, Together AI, Fireworks, DeepSeek, Mistral, xAI, Gemini,
Voyage, Jina, DeepInfra, Ollama, LM Studio, llama.cpp, SD WebUI, ComfyUI, Kokoro,
Speaches ou qualquer endpoint OpenAI-compatível).

A `api_key` NUNCA é logada nem devolvida nas respostas ou mensagens de erro.
"""

from __future__ import annotations

import logging
import re
from typing import Any
from urllib.parse import urlparse

import httpx

from app import settings_store

log = logging.getLogger("aidungeon.providers")

DISCOVER_TIMEOUT = 15.0

OPENAI_TTS_VOICES = [
    "alloy", "ash", "ballad", "coral", "echo", "fable",
    "nova", "onyx", "sage", "shimmer", "verse",
]

GROQ_TTS_VOICES = [
    "Arista-PlayAI", "Atlas-PlayAI", "Basil-PlayAI", "Briggs-PlayAI",
    "Calum-PlayAI", "Celeste-PlayAI", "Cheyenne-PlayAI", "Chip-PlayAI",
    "Cillian-PlayAI", "Deedee-PlayAI", "Fritz-PlayAI", "Gail-PlayAI",
    "Indigo-PlayAI", "Mamaw-PlayAI", "Mason-PlayAI", "Mikail-PlayAI",
    "Mitch-PlayAI", "Quinn-PlayAI",
]

KOKORO_VOICES = [
    "af_heart", "af_bella", "af_sarah", "am_adam", "am_michael",
    "bf_emma", "bm_george", "pf_dora", "pm_alex",
]

# Hosts OpenAI-compatíveis em nuvem conhecidos (usado também para o llm_client
# omitir extensões exclusivas de llama.cpp como dry_multiplier/id_slot).
CLOUD_OPENAI_HOSTS = frozenset({
    "api.openai.com",
    "openrouter.ai",
    "api.groq.com",
    "api.together.xyz",
    "api.fireworks.ai",
    "api.deepseek.com",
    "api.mistral.ai",
    "api.x.ai",
    "generativelanguage.googleapis.com",
    "api.cerebras.ai",
    "api.perplexity.ai",
    "api.cohere.com",
    "api.studio.nebius.com",
    "api.deepinfra.com",
    "api.novita.ai",
    "api.sambanova.ai",
    "api.voyageai.com",
    "api.jina.ai",
})


def is_cloud_openai_url(base_url: str) -> bool:
    """True quando `base_url` aponta para um provedor OpenAI em nuvem conhecido."""
    try:
        host = (urlparse(str(base_url or "").strip()).hostname or "").lower()
    except ValueError:
        return False
    if not host:
        return False
    return host in CLOUD_OPENAI_HOSTS or any(
        host.endswith("." + known) for known in CLOUD_OPENAI_HOSTS
    )


def provider_presets() -> dict[str, list[dict[str, Any]]]:
    """Presets curados de provedores por modalidade (com defaults locais do settings.json)."""
    file_cfg = settings_store.load_file_settings()
    llm_default = str(file_cfg.get("llm", {}).get("base_url") or "http://127.0.0.1:1234/v1")
    voice_cfg = file_cfg.get("voice", {}) if isinstance(file_cfg.get("voice"), dict) else {}
    voice_default = str(voice_cfg.get("base_url") or "http://127.0.0.1:8000/v1")
    voice_default_voices = list(voice_cfg.get("voices") or ["M1", "M2", "M3", "F1", "F2", "F3"])
    emb_cfg = file_cfg.get("embeddings", {}) if isinstance(file_cfg.get("embeddings"), dict) else {}
    emb_default = str(emb_cfg.get("base_url") or "http://127.0.0.1:8091/v1")
    emb_model = str(emb_cfg.get("model") or "Qwen3-Embedding-0.6B")

    return {
        "llm": [
            {
                "id": "local_vm",
                "label": "Local / VM (llama.cpp)",
                "base_url": llm_default,
                "requires_api_key": False,
                "default_model": "auto",
            },
            {
                "id": "openrouter",
                "label": "OpenRouter",
                "base_url": "https://openrouter.ai/api/v1",
                "requires_api_key": True,
                "default_model": "openai/gpt-4o-mini",
            },
            {
                "id": "openai",
                "label": "OpenAI",
                "base_url": "https://api.openai.com/v1",
                "requires_api_key": True,
                "default_model": "gpt-4o-mini",
            },
            {
                "id": "groq",
                "label": "Groq",
                "base_url": "https://api.groq.com/openai/v1",
                "requires_api_key": True,
                "default_model": "llama-3.3-70b-versatile",
            },
            {
                "id": "together",
                "label": "Together AI",
                "base_url": "https://api.together.xyz/v1",
                "requires_api_key": True,
                "default_model": "meta-llama/Llama-3.3-70B-Instruct-Turbo",
            },
            {
                "id": "fireworks",
                "label": "Fireworks AI",
                "base_url": "https://api.fireworks.ai/inference/v1",
                "requires_api_key": True,
                "default_model": "accounts/fireworks/models/llama-v3p3-70b-instruct",
            },
            {
                "id": "deepseek",
                "label": "DeepSeek",
                "base_url": "https://api.deepseek.com/v1",
                "requires_api_key": True,
                "default_model": "deepseek-chat",
            },
            {
                "id": "mistral",
                "label": "Mistral AI",
                "base_url": "https://api.mistral.ai/v1",
                "requires_api_key": True,
                "default_model": "mistral-large-latest",
            },
            {
                "id": "xai",
                "label": "xAI (Grok)",
                "base_url": "https://api.x.ai/v1",
                "requires_api_key": True,
                "default_model": "grok-3-mini",
            },
            {
                "id": "gemini",
                "label": "Google Gemini",
                "base_url": "https://generativelanguage.googleapis.com/v1beta/openai",
                "requires_api_key": True,
                "default_model": "gemini-2.5-flash",
            },
            {
                "id": "ollama",
                "label": "Ollama (Local)",
                "base_url": "http://127.0.0.1:11434/v1",
                "requires_api_key": False,
                "default_model": "auto",
            },
            {
                "id": "lmstudio",
                "label": "LM Studio (Local)",
                "base_url": "http://127.0.0.1:1234/v1",
                "requires_api_key": False,
                "default_model": "auto",
            },
        ],
        "image": [
            {
                "id": "openai",
                "label": "OpenAI Images",
                "provider": "openai_compatible",
                "base_url": "https://api.openai.com/v1",
                "requires_api_key": True,
                "default_model": "gpt-image-1",
                "default_size": "1024x1024",
            },
            {
                "id": "together",
                "label": "Together AI (FLUX)",
                "provider": "openai_compatible",
                "base_url": "https://api.together.xyz/v1",
                "requires_api_key": True,
                "default_model": "black-forest-labs/FLUX.1-schnell-Free",
                "default_size": "1024x1024",
            },
            {
                "id": "xai",
                "label": "xAI (Grok Image)",
                "provider": "openai_compatible",
                "base_url": "https://api.x.ai/v1",
                "requires_api_key": True,
                "default_model": "grok-2-image",
                "default_size": "1024x1024",
            },
            {
                "id": "nebius",
                "label": "Nebius Studio",
                "provider": "openai_compatible",
                "base_url": "https://api.studio.nebius.com/v1",
                "requires_api_key": True,
                "default_model": "black-forest-labs/flux-schnell",
                "default_size": "1024x1024",
            },
            {
                "id": "fireworks",
                "label": "Fireworks AI",
                "provider": "openai_compatible",
                "base_url": "https://api.fireworks.ai/inference/v1",
                "requires_api_key": True,
                "default_model": "accounts/fireworks/models/flux-1-schnell-fp8",
                "default_size": "1024x1024",
            },
            {
                "id": "openrouter",
                "label": "OpenRouter Images",
                "provider": "openai_compatible",
                "base_url": "https://openrouter.ai/api/v1",
                "requires_api_key": True,
                "default_model": "google/gemini-2.5-flash-image-preview",
                "default_size": "1024x1024",
            },
            {
                "id": "gemini",
                "label": "Google Gemini (Imagen)",
                "provider": "openai_compatible",
                "base_url": "https://generativelanguage.googleapis.com/v1beta/openai",
                "requires_api_key": True,
                "default_model": "imagen-3.0-generate-002",
                "default_size": "1024x1024",
            },
            {
                "id": "local_openai",
                "label": "OpenAI-Compatível (Local)",
                "provider": "openai_compatible",
                "base_url": "http://127.0.0.1:8000/v1",
                "requires_api_key": False,
                "default_model": "sdxl",
                "default_size": "1024x1024",
            },
        ],
        "voice": [
            {
                "id": "local_vm",
                "label": "Local / VM (OmniVoice + Whisper)",
                "base_url": voice_default,
                "requires_api_key": False,
                "default_tts_model": str(voice_cfg.get("tts_model") or "omnivoice"),
                "default_stt_model": str(voice_cfg.get("stt_model") or "whisper-1"),
                "default_voices": voice_default_voices,
                "default_narrator_voice": str(voice_cfg.get("narrator_voice") or voice_default_voices[0]),
            },
            {
                "id": "openai",
                "label": "OpenAI Audio (TTS + Whisper)",
                "base_url": "https://api.openai.com/v1",
                "requires_api_key": True,
                "default_tts_model": "gpt-4o-mini-tts",
                "default_stt_model": "whisper-1",
                "default_voices": list(OPENAI_TTS_VOICES),
                "default_narrator_voice": "nova",
            },
            {
                "id": "groq",
                "label": "Groq Audio (Whisper + PlayAI)",
                "base_url": "https://api.groq.com/openai/v1",
                "requires_api_key": True,
                "default_tts_model": "playai-tts",
                "default_stt_model": "whisper-large-v3-turbo",
                "default_voices": list(GROQ_TTS_VOICES),
                "default_narrator_voice": "Fritz-PlayAI",
            },
            {
                "id": "deepinfra",
                "label": "DeepInfra Audio (Kokoro + Whisper)",
                "base_url": "https://api.deepinfra.com/v1/openai",
                "requires_api_key": True,
                "default_tts_model": "hexgrad/Kokoro-82M",
                "default_stt_model": "openai/whisper-large-v3-turbo",
                "default_voices": list(KOKORO_VOICES),
                "default_narrator_voice": "af_heart",
            },
            {
                "id": "kokoro_local",
                "label": "Kokoro-FastAPI (Local)",
                "base_url": "http://127.0.0.1:8880/v1",
                "requires_api_key": False,
                "default_tts_model": "kokoro",
                "default_stt_model": "whisper-1",
                "default_voices": list(KOKORO_VOICES),
                "default_narrator_voice": "pf_dora",
            },
            {
                "id": "speaches_local",
                "label": "Speaches / Faster-Whisper (Local)",
                "base_url": "http://127.0.0.1:8000/v1",
                "requires_api_key": False,
                "default_tts_model": "kokoro",
                "default_stt_model": "Systran/faster-whisper-small",
                "default_voices": list(KOKORO_VOICES),
                "default_narrator_voice": "af_heart",
            },
        ],
        "embeddings": [
            {
                "id": "local_vm",
                "label": "Local / VM (llama-server)",
                "base_url": emb_default,
                "requires_api_key": False,
                "default_model": emb_model,
            },
            {
                "id": "openai",
                "label": "OpenAI Embeddings",
                "base_url": "https://api.openai.com/v1",
                "requires_api_key": True,
                "default_model": "text-embedding-3-small",
            },
            {
                "id": "openrouter",
                "label": "OpenRouter Embeddings",
                "base_url": "https://openrouter.ai/api/v1",
                "requires_api_key": True,
                "default_model": "openai/text-embedding-3-small",
            },
            {
                "id": "together",
                "label": "Together AI Embeddings",
                "base_url": "https://api.together.xyz/v1",
                "requires_api_key": True,
                "default_model": "BAAI/bge-large-en-v1.5",
            },
            {
                "id": "fireworks",
                "label": "Fireworks AI Embeddings",
                "base_url": "https://api.fireworks.ai/inference/v1",
                "requires_api_key": True,
                "default_model": "nomic-ai/nomic-embed-text-v1.5",
            },
            {
                "id": "mistral",
                "label": "Mistral Embeddings",
                "base_url": "https://api.mistral.ai/v1",
                "requires_api_key": True,
                "default_model": "mistral-embed",
            },
            {
                "id": "gemini",
                "label": "Google Gemini Embeddings",
                "base_url": "https://generativelanguage.googleapis.com/v1beta/openai",
                "requires_api_key": True,
                "default_model": "gemini-embedding-001",
            },
            {
                "id": "voyage",
                "label": "Voyage AI",
                "base_url": "https://api.voyageai.com/v1",
                "requires_api_key": True,
                "default_model": "voyage-3-lite",
            },
            {
                "id": "jina",
                "label": "Jina AI",
                "base_url": "https://api.jina.ai/v1",
                "requires_api_key": True,
                "default_model": "jina-embeddings-v3",
            },
            {
                "id": "ollama",
                "label": "Ollama (Local)",
                "base_url": "http://127.0.0.1:11434/v1",
                "requires_api_key": False,
                "default_model": "nomic-embed-text",
            },
        ],
    }


def _validate_base_url(base_url: str) -> str:
    cleaned = str(base_url or "").strip().rstrip("/")
    parsed = urlparse(cleaned)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise ValueError("URL base inválida (use http:// ou https://)")
    return cleaned


def _make_client(transport: httpx.BaseTransport | None, timeout: float = DISCOVER_TIMEOUT) -> httpx.Client:
    t = httpx.Timeout(timeout, connect=min(8.0, timeout))
    kwargs: dict[str, Any] = {"timeout": t, "trust_env": False}
    if transport is not None:
        kwargs["transport"] = transport
    return httpx.Client(**kwargs)


def _auth_headers(api_key: str | None, base_url: str = "") -> dict[str, str]:
    headers = {"Accept": "application/json"}
    if api_key and api_key.isascii() and api_key.isprintable():
        headers["Authorization"] = f"Bearer {api_key}"
    if "openrouter.ai" in (base_url or "").lower():
        headers["X-Title"] = "AI Dungeon Local"
    return headers


# --------------------------------------------------------------- classification
_RE_EMBED = re.compile(
    r"(?:embed|embedding|bge-|e5-|gte-|nomic-embed|mxbai-embed|snowflake-arctic-embed|"
    r"voyage-|jina-embeddings|text-embedding|mistral-embed)",
    re.IGNORECASE,
)
_RE_IMAGE = re.compile(
    r"(?:dall-e|gpt-image|imagen|flux|stable-diffusion|sdxl|sd3|playground-v|kandinsky|"
    r"midjourney|ideogram|recraft|grok-2-image|qwen-image|z-image|hidream|kolors|AuraFlow)",
    re.IGNORECASE,
)
_RE_STT = re.compile(
    r"(?:whisper|transcribe|distil-whisper|faster-whisper|canary-|parakeet|sensevoice|stt)",
    re.IGNORECASE,
)
_RE_TTS = re.compile(
    r"(?:^tts\b|-tts\b|_tts\b|/tts\b|omnivoice|kokoro|playai|orpheus|supertonic|chatterbox|"
    r"bark|xtts|f5-tts|fish-speech|eleven|speech-|audio-speech|cosyvoice|drama-3|sesame)",
    re.IGNORECASE,
)
_RE_OTHER_NON_LLM = re.compile(
    r"(?:moderation|omni-moderation|rerank|bge-reranker|sora|veo|ltxv|wan2|realtime)",
    re.IGNORECASE,
)


def _classify_model(raw: dict[str, Any], requested_kind: str) -> str:
    """Infere a modalidade principal do modelo a partir de metadados e ID."""
    model_id = str(raw.get("id") or "")
    raw_type = str(raw.get("type") or "").strip().lower()
    arch = raw.get("architecture") if isinstance(raw.get("architecture"), dict) else {}
    modality = str(arch.get("modality") or "").lower()
    out_mods = arch.get("output_modalities") if isinstance(arch.get("output_modalities"), list) else []
    out_mods_lower = {str(m).lower() for m in out_mods}

    if raw_type in ("embedding", "embeddings") or _RE_EMBED.search(model_id):
        return "embeddings"
    if raw_type in ("image", "image-generation") or "image" in out_mods_lower or "->image" in modality:
        return "image"
    if _RE_IMAGE.search(model_id) and "chat" not in raw_type:
        return "image"
    if raw_type in ("audio", "speech", "tts") or _RE_TTS.search(model_id):
        return "tts"
    if raw_type in ("transcription", "stt", "asr") or _RE_STT.search(model_id):
        return "stt"
    if _RE_OTHER_NON_LLM.search(model_id):
        return "other"
    if raw_type in ("chat", "language", "text", "completion", "llm"):
        return "llm"
    # Fallback quando servidor local devolve nomes arbitrários (ex.: modelo.gguf)
    if requested_kind in ("llm", "embeddings", "image"):
        return "llm" if requested_kind == "llm" else "other"
    return "other"


def _is_recommended(kind_hint: str, requested_kind: str, task: str | None) -> bool:
    if requested_kind == "voice":
        if task == "tts":
            return kind_hint == "tts"
        if task == "stt":
            return kind_hint == "stt"
        return kind_hint in ("tts", "stt")
    return kind_hint == requested_kind


def _extract_models_list(payload: Any) -> list[dict[str, Any]]:
    """Extrai lista de objetos de modelo de respostas OpenAI, Together, OpenRouter ou Ollama."""
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if not isinstance(payload, dict):
        return []
    for key in ("data", "models", "items"):
        candidate = payload.get(key)
        if isinstance(candidate, list):
            return [item for item in candidate if isinstance(item, dict)]
    return []


def _normalize_openai_model(raw: dict[str, Any], requested_kind: str, task: str | None) -> dict[str, Any] | None:
    model_id = str(raw.get("id") or raw.get("name") or raw.get("model") or "").strip()
    if not model_id:
        return None
    name = str(raw.get("display_name") or raw.get("name") or model_id).strip()
    owned_by = str(raw.get("owned_by") or raw.get("organization") or "").strip()
    if not owned_by and "/" in model_id:
        owned_by = model_id.split("/", 1)[0]
    arch = raw.get("architecture") if isinstance(raw.get("architecture"), dict) else {}
    context_length = raw.get("context_length") or raw.get("context_window")
    if isinstance(raw.get("top_provider"), dict) and not context_length:
        context_length = raw["top_provider"].get("context_length")
    try:
        ctx_int = int(context_length) if context_length else None
    except (TypeError, ValueError):
        ctx_int = None

    modality = str(arch.get("modality") or raw.get("type") or "").strip()
    description = str(raw.get("description") or "").strip()
    if len(description) > 180:
        description = description[:177].rstrip() + "…"

    kind_hint = _classify_model(raw, requested_kind)
    return {
        "id": model_id,
        "name": name,
        "owned_by": owned_by or None,
        "context_length": ctx_int,
        "modality": modality or None,
        "description": description or None,
        "kind_hint": kind_hint,
        "recommended": _is_recommended(kind_hint, requested_kind, task),
    }


def _filter_and_sort(
    models: list[dict[str, Any]],
    query: str,
    *,
    only_matching: bool,
    limit: int,
) -> tuple[list[dict[str, Any]], int]:
    # Deduplica preservando ordem de chegada
    seen: set[str] = set()
    unique: list[dict[str, Any]] = []
    for item in models:
        mid = item["id"]
        if mid not in seen:
            seen.add(mid)
            unique.append(item)

    tokens = [tok.lower() for tok in (query or "").strip().split() if tok]

    def matches_query(item: dict[str, Any]) -> bool:
        if not tokens:
            return True
        haystack = " ".join(
            str(item.get(k) or "")
            for k in ("id", "name", "owned_by", "modality", "description", "kind_hint")
        ).lower()
        return all(tok in haystack for tok in tokens)

    matched = [item for item in unique if matches_query(item)]
    recommended = [item for item in matched if item.get("recommended")]

    # Se o filtro por modalidade zeraria tudo (ex.: servidor local com nome customizado),
    # devolve todos os modelos que bateram com a busca.
    if only_matching and recommended:
        pool = recommended
    else:
        pool = matched

    # Recomendados primeiro, depois ordem alfabética por id
    pool.sort(key=lambda item: (0 if item.get("recommended") else 1, item["id"].lower()))
    total = len(pool)
    return pool[:limit], total


def _default_voices_for_url(base_url: str) -> list[str]:
    low = (base_url or "").lower()
    if "api.openai.com" in low:
        return list(OPENAI_TTS_VOICES)
    if "api.groq.com" in low:
        return list(GROQ_TTS_VOICES)
    if "deepinfra.com" in low or "kokoro" in low or ":8880" in low:
        return list(KOKORO_VOICES)
    return []


def _discover_voices(client: httpx.Client, base: str, headers: dict[str, str]) -> list[str]:
    """Tenta puxar vozes de /audio/voices ou /voices; se não existir, usa fallback conhecido."""
    for path in ("/audio/voices", "/voices"):
        try:
            resp = client.get(f"{base}{path}", headers=headers)
        except httpx.TransportError:
            continue
        if resp.status_code != 200:
            continue
        try:
            data = resp.json()
        except ValueError:
            continue
        raw_list: list[Any] = []
        if isinstance(data, list):
            raw_list = data
        elif isinstance(data, dict):
            for key in ("voices", "data", "items"):
                if isinstance(data.get(key), list):
                    raw_list = data[key]
                    break
        voices: list[str] = []
        for entry in raw_list:
            if isinstance(entry, str) and entry.strip():
                voices.append(entry.strip())
            elif isinstance(entry, dict):
                vid = str(entry.get("id") or entry.get("voice_id") or entry.get("name") or "").strip()
                if vid:
                    voices.append(vid)
        if voices:
            return list(dict.fromkeys(voices))
    return _default_voices_for_url(base)


def _discover_sdwebui(client: httpx.Client, base: str) -> list[dict[str, Any]]:
    resp = client.get(f"{base}/sdapi/v1/sd-models", headers={"Accept": "application/json"})
    if resp.status_code in (401, 403):
        raise PermissionError(f"HTTP {resp.status_code}")
    if resp.status_code != 200:
        raise RuntimeError(f"HTTP {resp.status_code} em /sdapi/v1/sd-models")
    data = resp.json()
    if not isinstance(data, list):
        raise RuntimeError("resposta de /sdapi/v1/sd-models inválida")
    models: list[dict[str, Any]] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        title = str(item.get("title") or item.get("model_name") or "").strip()
        if not title:
            continue
        models.append({
            "id": title,
            "name": str(item.get("model_name") or title).strip(),
            "owned_by": "sdwebui",
            "context_length": None,
            "modality": "text->image",
            "description": str(item.get("filename") or "") or None,
            "kind_hint": "image",
            "recommended": True,
        })
    return models


def _discover_comfyui(client: httpx.Client, base: str) -> list[dict[str, Any]]:
    models: list[dict[str, Any]] = []
    nodes = (
        ("UNETLoader", "unet_name", "unet"),
        ("UnetLoaderGGUF", "unet_name", "unet_gguf"),
        ("CheckpointLoaderSimple", "ckpt_name", "checkpoint"),
    )
    any_ok = False
    last_status = None
    for node_name, field_name, tag in nodes:
        resp = client.get(f"{base}/object_info/{node_name}", headers={"Accept": "application/json"})
        last_status = resp.status_code
        if resp.status_code in (401, 403):
            raise PermissionError(f"HTTP {resp.status_code}")
        if resp.status_code != 200:
            continue
        any_ok = True
        try:
            data = resp.json()
        except ValueError:
            continue
        node_info = data.get(node_name) if isinstance(data, dict) else None
        if not isinstance(node_info, dict):
            continue
        req = (node_info.get("input") or {}).get("required") or {}
        spec = req.get(field_name)
        names = spec[0] if isinstance(spec, list) and spec and isinstance(spec[0], list) else []
        for name in names:
            if not isinstance(name, str) or not name.strip():
                continue
            models.append({
                "id": name.strip(),
                "name": name.strip(),
                "owned_by": f"comfyui/{tag}",
                "context_length": None,
                "modality": "text->image",
                "description": f"Node {node_name}",
                "kind_hint": "image",
                "recommended": True,
            })
    if not any_ok and last_status is not None:
        raise RuntimeError(f"HTTP {last_status} em /object_info")
    return models


_STATIC_PROVIDER_MODELS: dict[str, list[dict[str, Any]]] = {
    "api.voyageai.com": [
        {"id": "voyage-3.5", "name": "Voyage 3.5", "owned_by": "voyageai", "type": "embedding"},
        {"id": "voyage-3.5-lite", "name": "Voyage 3.5 Lite", "owned_by": "voyageai", "type": "embedding"},
        {"id": "voyage-3-large", "name": "Voyage 3 Large", "owned_by": "voyageai", "type": "embedding"},
        {"id": "voyage-3-lite", "name": "Voyage 3 Lite", "owned_by": "voyageai", "type": "embedding"},
        {"id": "voyage-code-3", "name": "Voyage Code 3", "owned_by": "voyageai", "type": "embedding"},
        {"id": "voyage-multilingual-2", "name": "Voyage Multilingual 2", "owned_by": "voyageai", "type": "embedding"},
    ],
    "api.jina.ai": [
        {"id": "jina-embeddings-v3", "name": "Jina Embeddings v3", "owned_by": "jina", "type": "embedding"},
        {"id": "jina-embeddings-v2-base-en", "name": "Jina Embeddings v2 Base EN", "owned_by": "jina", "type": "embedding"},
        {"id": "jina-clip-v2", "name": "Jina CLIP v2", "owned_by": "jina", "type": "embedding"},
    ],
}


def _static_fallback_models(base: str, kind: str, task: str | None) -> list[dict[str, Any]]:
    host = (urlparse(base).hostname or "").lower()
    raw_items = _STATIC_PROVIDER_MODELS.get(host)
    if not raw_items:
        return []
    out: list[dict[str, Any]] = []
    for item in raw_items:
        norm = _normalize_openai_model(item, kind, task)
        if norm is not None:
            out.append(norm)
    return out


def _discover_openai_compatible(
    client: httpx.Client,
    base: str,
    headers: dict[str, str],
    kind: str,
    task: str | None,
) -> list[dict[str, Any]]:
    candidate_urls: list[str] = []
    if kind == "embeddings" and "openrouter.ai" in base.lower():
        candidate_urls.append(f"{base}/embeddings/models")
    candidate_urls.append(f"{base}/models")
    # Ollama nativo quando o usuário coloca http://host:11434/v1 ou http://host:11434
    ollama_root = base[:-3] if base.endswith("/v1") else base
    candidate_urls.append(f"{ollama_root}/api/tags")

    last_error: Exception | None = None
    all_not_found = True
    for idx, url in enumerate(candidate_urls):
        try:
            resp = client.get(url, headers=headers)
        except httpx.TransportError as exc:
            last_error = exc
            all_not_found = False
            if idx == 0:
                raise
            continue
        if resp.status_code in (401, 403):
            raise PermissionError(f"HTTP {resp.status_code}")
        if resp.status_code == 404:
            last_error = RuntimeError(f"HTTP 404 em {urlparse(url).path}")
            if idx + 1 < len(candidate_urls):
                continue
            break
        all_not_found = False
        if resp.status_code != 200:
            raise RuntimeError(f"HTTP {resp.status_code} em {urlparse(url).path}")
        try:
            payload = resp.json()
        except ValueError as exc:
            raise RuntimeError("resposta do provedor não é JSON válido") from exc
        raw_items = _extract_models_list(payload)
        models: list[dict[str, Any]] = []
        for item in raw_items:
            normalized = _normalize_openai_model(item, kind, task)
            if normalized is not None:
                # OpenRouter /embeddings/models devolve apenas modelos de embedding
                if url.endswith("/embeddings/models"):
                    normalized["kind_hint"] = "embeddings"
                    normalized["recommended"] = True
                models.append(normalized)
        return models
    if all_not_found:
        fallback = _static_fallback_models(base, kind, task)
        if fallback:
            return fallback
    if last_error is not None:
        raise last_error
    return []


def discover_models(
    *,
    kind: str,
    base_url: str,
    api_key: str | None = None,
    provider: str | None = None,
    query: str = "",
    task: str | None = None,
    only_matching: bool = False,
    limit: int = 120,
    transport: httpx.BaseTransport | None = None,
) -> dict[str, Any]:
    """Consulta a API do provedor e devolve modelos (e vozes, se kind=='voice').

    Nunca expõe `api_key` em logs ou mensagens de erro.
    """
    try:
        base = _validate_base_url(base_url)
    except ValueError as exc:
        return {
            "status": "config_error",
            "detail": str(exc),
            "kind": kind,
            "provider": provider,
            "models": [],
            "total": 0,
            "voices": [],
        }

    headers = _auth_headers(api_key, base)
    voices: list[str] = []
    try:
        with _make_client(transport) as client:
            if kind == "image" and provider == "sdwebui":
                raw_models = _discover_sdwebui(client, base)
            elif kind == "image" and provider == "comfyui":
                raw_models = _discover_comfyui(client, base)
            else:
                raw_models = _discover_openai_compatible(client, base, headers, kind, task)
                if kind == "voice":
                    voices = _discover_voices(client, base, headers)
    except PermissionError:
        return {
            "status": "auth_error",
            "detail": "Falha de autenticação (401/403). Verifique a chave da API.",
            "kind": kind,
            "provider": provider,
            "models": [],
            "total": 0,
            "voices": _default_voices_for_url(base) if kind == "voice" else [],
        }
    except httpx.TransportError as exc:
        return {
            "status": "unreachable",
            "detail": f"Provedor inacessível ({type(exc).__name__}).",
            "kind": kind,
            "provider": provider,
            "models": [],
            "total": 0,
            "voices": _default_voices_for_url(base) if kind == "voice" else [],
        }
    except Exception as exc:
        log.warning("provider_models: falha ao consultar %s (%s)", kind, type(exc).__name__)
        return {
            "status": "error",
            "detail": str(exc)[:200] or "Erro ao consultar modelos no provedor.",
            "kind": kind,
            "provider": provider,
            "models": [],
            "total": 0,
            "voices": _default_voices_for_url(base) if kind == "voice" else [],
        }

    filtered, total = _filter_and_sort(
        raw_models, query, only_matching=only_matching, limit=limit
    )
    return {
        "status": "ok",
        "detail": None,
        "kind": kind,
        "provider": provider,
        "models": filtered,
        "total": total,
        "total_available": len(raw_models),
        "voices": voices,
    }
