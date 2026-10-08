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
    "sampler.temperature", "sampler.top_p", "sampler.repetition_penalty",
    # imagem (v2; storage_dir/timeout_seconds são internos — não saem no PUT)
    "image.enabled", "image.provider", "image.sdwebui_base_url",
    "image.sdwebui_steps", "image.sdwebui_width", "image.sdwebui_height",
    "image.negative_prompt", "image.openai_base_url", "image.openai_api_key",
    "image.openai_model", "image.openai_size", "image.style_suffix",
    "image.storage_dir", "image.timeout_seconds",
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
                       ("repetition_penalty", "sampler.repetition_penalty")):
        if field in update and update[field] is not None:
            _set_override(conn, key, update[field])
            changed.add(field)
    if "api_key" in update and update["api_key"] is not None:
        if update["api_key"] == "":
            db.delete_setting(conn, "llm.api_key")
        else:
            _set_override(conn, "llm.api_key", update["api_key"])
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

    api_key = _get_override(conn, "llm.api_key")
    if not api_key:
        api_key = resolve_env_api_key() or llm.get("api_key") or None

    return {
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
        "top_p": float(ov("sampler.top_p", sampler.get("top_p", 0.95))),
        "repetition_penalty": float(ov("sampler.repetition_penalty",
                                       sampler.get("repetition_penalty", 1.08))),
    }


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
    openai = image.get("providers", {}).get("openai_compatible", {})
    prompt_builder = image.get("prompt_builder", {})

    def ov(key: str, default: Any) -> Any:
        value = _get_override(conn, key)
        return default if value is None else value

    api_key = _get_override(conn, "image.openai_api_key")
    if not api_key:
        api_key = resolve_env_value(openai.get("api_key_env", "IMAGE_API_KEY")) or None

    return {
        # ---- shape público (ImageSettings) ----
        "enabled": bool(ov("image.enabled", image.get("enabled", True))),
        "provider": ov("image.provider", image.get("provider", "sdwebui")),
        "sdwebui_base_url": ov("image.sdwebui_base_url", sdwebui.get("base_url", "")),
        "sdwebui_steps": int(ov("image.sdwebui_steps", sdwebui.get("steps", 20))),
        "sdwebui_width": int(ov("image.sdwebui_width", sdwebui.get("width", 512))),
        "sdwebui_height": int(ov("image.sdwebui_height", sdwebui.get("height", 512))),
        "negative_prompt": ov("image.negative_prompt", sdwebui.get("negative_prompt", "")),
        "openai_base_url": ov("image.openai_base_url", openai.get("base_url", "")),
        "openai_api_key": api_key,
        "openai_model": ov("image.openai_model", openai.get("model", "")),
        "openai_size": ov("image.openai_size", openai.get("size", "512x512")),
        "style_suffix": ov("image.style_suffix", prompt_builder.get("style_suffix", "")),
        # ---- internos ----
        "storage_dir": ov("image.storage_dir", image.get("storage_dir", "data/images")),
        "timeout_seconds": int(ov("image.timeout_seconds",
                                  image.get("timeout_seconds", 300))),
        "txt2img_path": sdwebui.get("txt2img_path", "/sdapi/v1/txt2img"),
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
                       ("negative_prompt", "image.negative_prompt"),
                       ("openai_base_url", "image.openai_base_url"),
                       ("openai_model", "image.openai_model"),
                       ("openai_size", "image.openai_size"),
                       ("style_suffix", "image.style_suffix")):
        if field in update and update[field] is not None:
            _set_override(conn, key, update[field])
            changed.add(field)
    if "openai_api_key" in update and update["openai_api_key"] is not None:
        if update["openai_api_key"] == "":
            db.delete_setting(conn, "image.openai_api_key")
        else:
            _set_override(conn, "image.openai_api_key", update["openai_api_key"])
        changed.add("openai_api_key")
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
    sampler["repetition_penalty"] = llm["repetition_penalty"]
    return {
        "context_size": llm["context_size"],
        "budgets": dict(cfg.get("context", {}).get("budgets", {})),
        "stop_sequences": list(cfg.get("stop_sequences", ["\n> You", "\n> you"])),
        "sampler": sampler,
        "memory_bank": dict(cfg.get("memory_bank", {})),
        "story_cards": dict(cfg.get("story_cards", {})),
        "llm": llm,
    }


def get_stop_sequences() -> list[str]:
    return list(load_file_settings().get("stop_sequences", ["\n> You", "\n> you"]))
