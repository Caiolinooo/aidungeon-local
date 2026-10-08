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
                       ("timeout_seconds", "llm.timeout_seconds")):
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
    }


def mask_api_key(key: str | None) -> str | None:
    """'sk-secret-1234' -> 'sk-…1234' (formato do exemplo do openapi.yaml)."""
    if not key:
        return None
    if len(key) <= 7:
        return "…" + key[-4:]
    return key[:3] + "…" + key[-4:]


# ---------------------------------------------------- config p/ game engine
def runtime_config(conn: sqlite3.Connection) -> dict:
    """Tudo que o context_builder/game_engine precisam (budgets, sampler, stops)."""
    cfg = load_file_settings()
    llm = effective_llm_settings(conn)
    sampler = dict(cfg.get("sampler", {}))
    sampler["max_tokens"] = llm["max_tokens"]
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
