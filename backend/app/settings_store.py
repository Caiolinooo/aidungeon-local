"""Resolução de configuração: config/settings.json (contrato) + overrides do banco.

Precedência para cada campo de `llm`:
    override da tabela `settings` (chaves `llm.*`)  >  config/settings.json

Para `api_key` especificamente:
    override do banco  >  env `LLM_API_KEY`  >  arquivo `.env` (raiz do repo)
    >  `llm.api_key` do settings.json

A `api_key` efetiva NUNCA é logada e só sai da API mascarada (`mask_api_key`).
"""

from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path
from typing import Any

from app import db

SETTINGS_PATH = db.REPO_ROOT / "config" / "settings.json"

OVERRIDE_KEYS = (
    "llm.base_url", "llm.api_key", "llm.model_alias", "llm.context_size",
    "llm.max_tokens", "llm.streaming", "llm.disable_thinking", "llm.timeout_seconds",
    # sampler (expostos em GET/PUT /api/settings/llm)
    "sampler.temperature", "sampler.top_p", "sampler.min_p",
    "sampler.repetition_penalty", "sampler.presence_penalty", "sampler.frequency_penalty",
    "sampler.dry_multiplier", "sampler.dry_base", "sampler.dry_allowed_length",
    "sampler.dry_penalty_last_n", "sampler.retry_temperature_step", "sampler.max_retry_temperature",
    # imagem (v2; storage_dir é interno — não sai no PUT)
    "image.enabled", "image.provider", "image.sdwebui_base_url",
    "image.sdwebui_steps", "image.sdwebui_width", "image.sdwebui_height",
    "image.comfyui_base_url", "image.comfyui_steps", "image.comfyui_width",
    "image.comfyui_height",
    "image.negative_prompt", "image.openai_base_url", "image.openai_api_key",
    "image.openai_model", "image.openai_size", "image.style_suffix",
    "image.storage_dir", "image.timeout_seconds", "image.seed", "image.variation",
    "image.cfg_scale", "image.sampler_name", "image.scheduler",
    "image.denoising_strength", "image.sd_model_checkpoint",
    # voz (v0.4.0; voices/max_text_chars/stt_max_bytes são internos — não saem no PUT)
    "voice.enabled", "voice.base_url", "voice.api_key", "voice.tts_model",
    "voice.stt_model", "voice.narrator_voice", "voice.timeout_seconds",
    "voice.max_text_chars", "voice.stt_max_bytes",
    "voice.drama3_model", "voice.drama3_enabled",
    # embeddings (v0.8.0 / M2)
    "embeddings.enabled", "embeddings.base_url", "embeddings.model",
    "embeddings.timeout_seconds",
)

_file_cache: dict | None = None


# ------------------------------------------------------------ arquivo base
def load_file_settings(force: bool = False) -> dict:
    global _file_cache
    if _file_cache is None or force:
        _file_cache = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
    return _file_cache


def _dotenv_path() -> Path:
    return Path(os.environ.get("AIDUNGEON_ENV_PATH") or (db.REPO_ROOT / ".env"))


def _read_dotenv_key(name: str) -> str | None:
    """Parser mínimo de .env (KEY=VALUE), sem dependência externa."""
    path = _dotenv_path()
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            if key.strip() == name:
                return value.strip().strip('"').strip("'") or None
    except OSError:
        pass
    return None


def resolve_env_value(name: str) -> str | None:
    """Valor de `name` no ambiente ou no .env da raiz (nunca logar o valor)."""
    value = os.environ.get(name)
    if value:
        return value
    return _read_dotenv_key(name)


def resolve_env_api_key() -> str | None:
    """LLM_API_KEY do ambiente ou do .env da raiz (nunca logar o valor)."""
    env_name = load_file_settings().get("llm", {}).get("api_key_env", "LLM_API_KEY")
    return resolve_env_value(env_name)


# -------------------------------------------------------------- overrides
def _get_override(conn: sqlite3.Connection, key: str) -> Any:
    raw = db.get_setting(conn, key)
    if raw is None:
        return None
    try:
        return json.loads(raw)
    except ValueError:
        return raw


def _set_override(conn: sqlite3.Connection, key: str, value: Any) -> None:
    db.set_setting(conn, key, json.dumps(value, ensure_ascii=False))


def _api_key_or_env(conn: sqlite3.Connection, key: str, env_name: str) -> str | None:
    """Override ASCII válido; legados mascarados/não ASCII caem para a env."""
    value = _get_override(conn, key)
    if isinstance(value, str) and value and value.isascii() and value.isprintable():
        return value
    return resolve_env_value(env_name) or None


def _usable_secret(value: Any) -> str | None:
    if isinstance(value, str) and value and value.isascii() and value.isprintable():
        return value
    return None


def _apply_key(conn: sqlite3.Connection, key: str, value: str | None) -> bool:
    """Grava a chave ("" apaga). A chave vira header HTTP: a mascarada que o
    front reenvia ("sk-…1234") e qualquer valor fora de ASCII imprimível são
    ignorados. Devolve se mudou algo."""
    if value is None:
        return False
    if value == "":
        db.delete_setting(conn, key)
        return True
    if _usable_secret(value) is None:
        return False
    _set_override(conn, key, value)
    return True


def apply_llm_update(conn: sqlite3.Connection, update: dict) -> set[str]:
    """Persiste overrides (campos omitidos permanecem). Devolve chaves mudadas.

    `api_key=""` remove a chave (apaga o override); omitir mantém.
    """
    changed: set[str] = set()
    for field, key in (("base_url", "llm.base_url"), ("model_alias", "llm.model_alias"),
                       ("context_size", "llm.context_size"), ("max_tokens", "llm.max_tokens"),
                       ("streaming", "llm.streaming"),
                       ("disable_thinking", "llm.disable_thinking"),
                       ("timeout_seconds", "llm.timeout_seconds"),
                       # sampler: overrides gravados como sampler.* (não llm.*)
                       ("temperature", "sampler.temperature"),
                       ("top_p", "sampler.top_p"),
                       ("min_p", "sampler.min_p"),
                       ("repetition_penalty", "sampler.repetition_penalty"),
                       ("presence_penalty", "sampler.presence_penalty"),
                       ("frequency_penalty", "sampler.frequency_penalty"),
                       ("dry_multiplier", "sampler.dry_multiplier"),
                       ("dry_base", "sampler.dry_base"),
                       ("dry_allowed_length", "sampler.dry_allowed_length"),
                       ("dry_penalty_last_n", "sampler.dry_penalty_last_n"),
                       ("retry_temperature_step", "sampler.retry_temperature_step"),
                       ("max_retry_temperature", "sampler.max_retry_temperature")):
        if field in update and update[field] is not None:
            _set_override(conn, key, update[field])
            changed.add(field)
    if _apply_key(conn, "llm.api_key", update.get("api_key")):
        changed.add("api_key")
    return changed


# ----------------------------------------------------------- config efetiva
def effective_llm_settings(conn: sqlite3.Connection) -> dict:
    """Config efetiva do servidor LLM (arquivo + overrides + env/.env)."""
    cfg = load_file_settings()
    llm = cfg.get("llm", {})
    sampler = cfg.get("sampler", {})

    def ov(key: str, default: Any) -> Any:
        value = _get_override(conn, key)
        return default if value is None else value

    api_key = _api_key_or_env(conn, "llm.api_key", llm.get("api_key_env", "LLM_API_KEY"))
    if not api_key:
        api_key = llm.get("api_key") or None

    effective = {
        "provider": llm.get("provider", "llamacpp"),
        "base_url": ov("llm.base_url", llm.get("base_url", "")),
        "api_key": api_key or None,
        "model_alias": ov("llm.model_alias", llm.get("model_alias", "auto")),
        "context_size": int(ov("llm.context_size", llm.get("context_size", 8192))),
        "max_tokens": int(ov("llm.max_tokens", sampler.get("max_tokens", 300))),
        "streaming": bool(ov("llm.streaming", llm.get("streaming", True))),
        "disable_thinking": bool(ov("llm.disable_thinking", llm.get("disable_thinking", True))),
        "timeout_seconds": int(ov("llm.timeout_seconds", llm.get("timeout_seconds", 180))),
        # sampler efetivo (override sampler.* > settings.json sampler)
        "temperature": float(ov("sampler.temperature", sampler.get("temperature", 0.85))),
        "top_p": float(ov("sampler.top_p", sampler.get("top_p", 1.0))),
        "min_p": float(ov("sampler.min_p", sampler.get("min_p", 0.08))),
        "repetition_penalty": float(ov("sampler.repetition_penalty",
                                       sampler.get("repetition_penalty", 1.02))),
        "presence_penalty": float(ov("sampler.presence_penalty", sampler.get("presence_penalty", 0.15))),
        "frequency_penalty": float(ov("sampler.frequency_penalty", sampler.get("frequency_penalty", 0.10))),
        "dry_multiplier": float(ov("sampler.dry_multiplier", sampler.get("dry_multiplier", 0.8))),
        "dry_base": float(ov("sampler.dry_base", sampler.get("dry_base", 1.75))),
        "dry_allowed_length": int(ov("sampler.dry_allowed_length", sampler.get("dry_allowed_length", 2))),
        "dry_penalty_last_n": int(ov("sampler.dry_penalty_last_n", sampler.get("dry_penalty_last_n", -1))),
        "retry_temperature_step": float(ov("sampler.retry_temperature_step", sampler.get("retry_temperature_step", 0.05))),
        "max_retry_temperature": float(ov("sampler.max_retry_temperature", sampler.get("max_retry_temperature", 1.15))),
    }
    # Clamp on read: VM DB may store 262144, which exceeds per-slot n_ctx.
    # Never write the stored override back down.
    cap = int(llm.get("context_size_limits", {}).get("max", 106496))
    effective["context_size"] = min(effective["context_size"], cap)
    scheduler = cfg.get("scheduler", {}) or {}
    effective["role_timeouts"] = dict(scheduler.get("timeouts", {}) or {})
    effective["aux_cache_prompt"] = bool(scheduler.get("aux_cache_prompt", False))
    effective["scheduler"] = _scheduler_settings(scheduler)
    return effective


def _scheduler_settings(raw: dict) -> dict:
    """Bloco `scheduler` para o llm_client, com os caminhos absolutos
    (relativos à raiz do repo)."""
    out = {key: value for key, value in raw.items() if key not in ("timeouts", "note")}
    for key in ("stall_hint_path", "watchdog_status_path"):
        raw_path = str(raw.get(key) or "").strip()
        if raw_path:
            path = Path(raw_path)
            out[key] = str(path if path.is_absolute() else db.REPO_ROOT / path)
    return out


def mask_api_key(key: str | None) -> str | None:
    """'sk-secret-1234' -> 'sk-…1234' (formato do exemplo do openapi.yaml)."""
    if not key:
        return None
    if len(key) <= 7:
        return "…" + key[-4:]
    return key[:3] + "…" + key[-4:]


# ------------------------------------------------------- config de imagem (v2)
def effective_image_settings(conn: sqlite3.Connection) -> dict:
    """Config efetiva de geração de imagens (arquivo + overrides + env/.env).

    As chaves públicas seguem o schema ImageSettings do openapi.yaml; as
    internas (storage_dir, timeout_seconds, txt2img_path, prompt_builder)
    são consumidas pelo image_client/rotas e não saem na API.

    Precedência da openai_api_key: override do banco > env/.env nomeada por
    image.providers.openai_compatible.api_key_env > None.
    """
    cfg = load_file_settings()
    image = cfg.get("image", {})
    sdwebui = image.get("providers", {}).get("sdwebui", {})
    comfyui = image.get("providers", {}).get("comfyui", {})
    openai = image.get("providers", {}).get("openai_compatible", {})
    prompt_builder = image.get("prompt_builder", {})

    def ov(key: str, default: Any) -> Any:
        value = _get_override(conn, key)
        return default if value is None else value

    api_key = _api_key_or_env(
        conn, "image.openai_api_key", openai.get("api_key_env", "IMAGE_API_KEY"))

    return {
        # ---- shape público (ImageSettings) ----
        "enabled": bool(ov("image.enabled", image.get("enabled", True))),
        "provider": ov("image.provider", image.get("provider", "sdwebui")),
        "sdwebui_base_url": ov("image.sdwebui_base_url", sdwebui.get("base_url", "")),
        "sdwebui_steps": int(ov("image.sdwebui_steps", sdwebui.get("steps", 20))),
        "sdwebui_width": int(ov("image.sdwebui_width", sdwebui.get("width", 512))),
        "sdwebui_height": int(ov("image.sdwebui_height", sdwebui.get("height", 512))),
        "comfyui_base_url": ov("image.comfyui_base_url", comfyui.get("base_url", "")),
        "comfyui_steps": int(ov("image.comfyui_steps", comfyui.get("steps", 15))),
        "comfyui_width": int(ov("image.comfyui_width", comfyui.get("width", 1024))),
        "comfyui_height": int(ov("image.comfyui_height", comfyui.get("height", 1024))),
        "negative_prompt": ov("image.negative_prompt", sdwebui.get("negative_prompt", "")),
        "openai_base_url": ov("image.openai_base_url", openai.get("base_url", "")),
        "openai_api_key": api_key,
        "openai_model": ov("image.openai_model", openai.get("model", "")),
        "openai_size": ov("image.openai_size", openai.get("size", "512x512")),
        "style_suffix": ov("image.style_suffix", prompt_builder.get("style_suffix", "")),
        "seed": ov("image.seed", image.get("seed", sdwebui.get("seed"))),
        "variation": int(ov("image.variation", image.get("variation", 0))),
        "cfg_scale": float(ov("image.cfg_scale", sdwebui.get("cfg_scale", 5))),
        "sampler_name": ov("image.sampler_name", sdwebui.get("sampler_name", "DPM++ 2M")),
        "scheduler": ov("image.scheduler", sdwebui.get("scheduler", "Karras")),
        "denoising_strength": float(ov(
            "image.denoising_strength", sdwebui.get("denoising_strength", 0.35))),
        # ---- internos ----
        "storage_dir": ov("image.storage_dir", image.get("storage_dir", "data/images")),
        "timeout_seconds": int(ov("image.timeout_seconds",
                                  image.get("timeout_seconds", 300))),
        "txt2img_path": sdwebui.get("txt2img_path", "/sdapi/v1/txt2img"),
        "img2img_path": sdwebui.get("img2img_path", "/sdapi/v1/img2img"),
        "sd_model_checkpoint": ov(
            "image.sd_model_checkpoint", sdwebui.get("sd_model_checkpoint", "")),
        # ComfyUI: nomes de arquivo/sampler vêm só do settings.json (internos).
        "comfyui_unet": comfyui.get("unet", ""),
        "comfyui_clip": comfyui.get("clip", ""),
        "comfyui_clip_type": comfyui.get("clip_type", "qwen_image_2_1"),
        "comfyui_clip_device": comfyui.get("clip_device", "default"),
        "comfyui_vae": comfyui.get("vae", ""),
        "comfyui_sampler": comfyui.get("sampler", "euler"),
        "comfyui_scheduler": comfyui.get("scheduler", "normal"),
        "comfyui_shift": float(comfyui.get("shift", 1.31)),
        "comfyui_poll_interval_s": float(comfyui.get("poll_interval_s", 1.0)),
        "prompt_builder": dict(prompt_builder),
    }


def apply_image_update(conn: sqlite3.Connection, update: dict) -> set[str]:
    """Persiste overrides de imagem (campos omitidos permanecem).

    `openai_api_key=""` remove o override (cai para a env); omitir mantém.
    """
    changed: set[str] = set()
    for field, key in (("enabled", "image.enabled"), ("provider", "image.provider"),
                       ("sdwebui_base_url", "image.sdwebui_base_url"),
                       ("sdwebui_steps", "image.sdwebui_steps"),
                       ("sdwebui_width", "image.sdwebui_width"),
                       ("sdwebui_height", "image.sdwebui_height"),
                       ("comfyui_base_url", "image.comfyui_base_url"),
                       ("comfyui_steps", "image.comfyui_steps"),
                       ("comfyui_width", "image.comfyui_width"),
                       ("comfyui_height", "image.comfyui_height"),
                       ("negative_prompt", "image.negative_prompt"),
                       ("openai_base_url", "image.openai_base_url"),
                       ("openai_model", "image.openai_model"),
                       ("openai_size", "image.openai_size"),
                       ("style_suffix", "image.style_suffix"),
                       ("variation", "image.variation"),
                       ("cfg_scale", "image.cfg_scale"),
                       ("sampler_name", "image.sampler_name"),
                       ("scheduler", "image.scheduler"),
                       ("denoising_strength", "image.denoising_strength"),
                       ("timeout_seconds", "image.timeout_seconds"),
                       ("sd_model_checkpoint", "image.sd_model_checkpoint")):
        if field in update and update[field] is not None:
            _set_override(conn, key, update[field])
            changed.add(field)
    if "seed" in update:
        if update["seed"] is None:
            db.delete_setting(conn, "image.seed")
        else:
            _set_override(conn, "image.seed", update["seed"])
        changed.add("seed")
    if _apply_key(conn, "image.openai_api_key", update.get("openai_api_key")):
        changed.add("openai_api_key")
    return changed


# --------------------------------------------------------- config de voz (v4)
def effective_voice_settings(conn: sqlite3.Connection) -> dict:
    """Config efetiva de voz TTS/STT (arquivo + overrides + env/.env).

    As chaves públicas seguem o schema VoiceSettings do openapi.yaml; as
    internas (max_text_chars, stt_max_bytes) são consumidas pelas rotas e
    não saem no PUT (mas saem no GET, para a UI validar antes de enviar).

    Precedência da api_key: override do banco > env/.env nomeada por
    voice.api_key_env (default VOICE_API_KEY) > None.
    """
    cfg = load_file_settings()
    voice = cfg.get("voice", {})

    def ov(key: str, default: Any) -> Any:
        value = _get_override(conn, key)
        return default if value is None else value

    api_key = _api_key_or_env(conn, "voice.api_key", voice.get("api_key_env", "VOICE_API_KEY"))

    return {
        "enabled": bool(ov("voice.enabled", voice.get("enabled", True))),
        "base_url": ov("voice.base_url", voice.get("base_url", "")),
        "api_key": api_key,
        "tts_model": ov("voice.tts_model", voice.get("tts_model", "")),
        "stt_model": ov("voice.stt_model", voice.get("stt_model", "")),
        "narrator_voice": ov("voice.narrator_voice", voice.get("narrator_voice", "M1")),
        "voices": list(voice.get("voices", [])),
        "timeout_seconds": int(ov("voice.timeout_seconds",
                                  voice.get("timeout_seconds", 60))),
        "max_text_chars": int(ov("voice.max_text_chars",
                                 voice.get("max_text_chars", 4096))),
        "stt_max_bytes": int(ov("voice.stt_max_bytes",
                                voice.get("stt_max_bytes", 10485760))),
        "drama3_model": ov("voice.drama3_model", voice.get("drama3_model", "drama-3")),
        "drama3_enabled": bool(ov("voice.drama3_enabled",
                                  voice.get("drama3_enabled", False))),
    }


def apply_voice_update(conn: sqlite3.Connection, update: dict) -> set[str]:
    """Persiste overrides de voz (campos omitidos permanecem).

    `api_key=""` remove o override (cai para a env); omitir mantém.
    """
    changed: set[str] = set()
    for field, key in (("enabled", "voice.enabled"), ("base_url", "voice.base_url"),
                       ("tts_model", "voice.tts_model"), ("stt_model", "voice.stt_model"),
                       ("narrator_voice", "voice.narrator_voice"),
                       ("timeout_seconds", "voice.timeout_seconds")):
        if field in update and update[field] is not None:
            _set_override(conn, key, update[field])
            changed.add(field)
    if _apply_key(conn, "voice.api_key", update.get("api_key")):
        changed.add("api_key")
    return changed


# ---------------------------------------------------------------- embeddings
def effective_embeddings_settings(conn: sqlite3.Connection | None = None) -> dict:
    """Config efetiva do cliente de embeddings (arquivo + overrides SQLite)."""
    cfg = load_file_settings()
    emb = cfg.get("embeddings", {})

    def ov(key: str, default: Any) -> Any:
        if conn is None:
            return default
        value = _get_override(conn, key)
        return default if value is None else value

    return {
        "enabled": bool(ov("embeddings.enabled", emb.get("enabled", True))),
        "base_url": str(ov("embeddings.base_url", emb.get("base_url", "http://127.0.0.1:8081/v1"))),
        "model": str(ov("embeddings.model", emb.get("model", "Qwen3-Embedding-0.6B"))),
        "timeout_seconds": int(ov("embeddings.timeout_seconds", emb.get("timeout_seconds", 30))),
    }


def apply_embeddings_update(conn: sqlite3.Connection, update: dict) -> set[str]:
    """Persiste overrides de embeddings."""
    changed: set[str] = set()
    for field, key in (
        ("enabled", "embeddings.enabled"),
        ("base_url", "embeddings.base_url"),
        ("model", "embeddings.model"),
        ("timeout_seconds", "embeddings.timeout_seconds"),
    ):
        if field in update and update[field] is not None:
            _set_override(conn, key, update[field])
            changed.add(field)
    return changed


# ---------------------------------------------------- config p/ game engine
def runtime_config(conn: sqlite3.Connection) -> dict:
    """Tudo que o context_builder/game_engine precisam (budgets, sampler, stops)."""
    cfg = load_file_settings()
    llm = effective_llm_settings(conn)
    sampler = dict(cfg.get("sampler", {}))
    sampler["max_tokens"] = llm["max_tokens"]
    sampler["temperature"] = llm["temperature"]
    sampler["top_p"] = llm["top_p"]
    sampler["min_p"] = llm["min_p"]
    sampler["repetition_penalty"] = llm["repetition_penalty"]
    sampler["presence_penalty"] = llm["presence_penalty"]
    sampler["frequency_penalty"] = llm["frequency_penalty"]
    sampler["dry_multiplier"] = llm["dry_multiplier"]
    sampler["dry_base"] = llm["dry_base"]
    sampler["dry_allowed_length"] = llm["dry_allowed_length"]
    sampler["dry_penalty_last_n"] = llm["dry_penalty_last_n"]
    sampler["retry_temperature_step"] = llm["retry_temperature_step"]
    sampler["max_retry_temperature"] = llm["max_retry_temperature"]
    return {
        "context_size": llm["context_size"],
        "budgets": dict(cfg.get("context", {}).get("budgets", {})),
        "dynamic_scaling": dict(cfg.get("context", {}).get("dynamic_scaling", {})),
        "stop_sequences": list(cfg.get("stop_sequences", ["\n> You", "\n> you"])),
        "sampler": sampler,
        "memory_bank": dict(cfg.get("memory_bank", {})),
        "embeddings": effective_embeddings_settings(conn),
        "story_cards": dict(cfg.get("story_cards", {})),
        "llm": llm,
    }


def get_stop_sequences() -> list[str]:
    return list(load_file_settings().get("stop_sequences", ["\n> You", "\n> you"]))
