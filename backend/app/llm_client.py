"""Cliente do servidor llama.cpp (API OpenAI-compatível) da VM.

- Descoberta de modelo: `model_alias == "auto"` → `GET {base_url}/models`,
  usa o id do primeiro modelo e cacheia por (base_url, model_alias).
  `invalidate_discovery_cache()` é chamada quando base_url/model_alias mudam
  via `PUT /api/settings/llm`.
- Geração: `POST {base_url}/chat/completions` com `stream=true`, repassando
  tokens SSE um a um. Header `Authorization: Bearer <api_key>` quando houver.
- Modo Turbo: `cache_prompt=true` sempre. `id_slot` só entra no payload
  quando `GET {base}/props` (`base` = base_url sem o sufixo `/v1`) reporta
  `total_slots >= 2`: narrativa → 0, auxiliar (`chat_completion`) → 1.
  Menos de 2 slots, HTTP ≠ 200, JSON inválido ou exceção → chave omitida
  (um `id_slot` inexistente trava o llama.cpp até o timeout).
- `disable_thinking=true` (default; OBRIGATÓRIO p/ Qwen3.x híbrido) → toda
  chamada inclui `chat_template_kwargs={"enable_thinking": false}`. Sem isso o
  servidor consome os tokens como `reasoning_content` e devolve content VAZIO.
- Erros: 401/403 → `LlmAuthError` ("auth_error"); falha de conexão →
  `LlmUnreachable` ("unreachable"); demais → `LlmError`.

A api_key NUNCA é escrita em logs/mensagens de erro.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Iterator

import httpx

log = logging.getLogger("aidungeon.llm")

PROBE_TIMEOUT = 15.0

# Cache de descoberta: (base_url, model_alias) -> id do modelo (ou None)
_discovery_cache: dict[tuple[str, str], str | None] = {}
# Cache de slots: base_url sem sufixo /v1 -> total_slots (0 = um slot ou falha)
_slot_cache: dict[str, int] = {}


class LlmError(Exception):
    code = "error"

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


class LlmUnreachable(LlmError):
    code = "unreachable"


class LlmAuthError(LlmError):
    code = "auth_error"


class StreamState:
    """O teto de tokens (`finish_reason` length) corta a frase no meio."""

    def __init__(self) -> None:
        self.truncated = False


def invalidate_discovery_cache() -> None:
    _discovery_cache.clear()
    _slot_cache.clear()


def cached_discovery(base_url: str, model_alias: str) -> str | None:
    return _discovery_cache.get((base_url.rstrip("/"), model_alias))


# ---------------------------------------------------------------- helpers
def _headers(cfg: dict) -> dict[str, str]:
    headers = {"Accept": "application/json"}
    if cfg.get("api_key"):
        headers["Authorization"] = f"Bearer {cfg['api_key']}"
    return headers


def _make_client(cfg: dict, transport: httpx.BaseTransport | None,
                 timeout: float) -> httpx.Client:
    t = httpx.Timeout(timeout, connect=min(10.0, timeout))
    kwargs: dict[str, Any] = {"timeout": t, "trust_env": False}
    if transport is not None:
        kwargs["transport"] = transport
    return httpx.Client(**kwargs)


def probe_models(cfg: dict, transport: httpx.BaseTransport | None = None) -> list[dict]:
    """GET {base_url}/models em tempo real. Levanta LlmError subclasses."""
    base = cfg["base_url"].rstrip("/")
    timeout = min(float(cfg.get("timeout_seconds", 180)), PROBE_TIMEOUT)
    try:
        with _make_client(cfg, transport, timeout) as client:
            resp = client.get(f"{base}/models", headers=_headers(cfg))
    except httpx.TransportError as exc:
        raise LlmUnreachable(f"llm unreachable: {type(exc).__name__}") from exc
    if resp.status_code in (401, 403):
        raise LlmAuthError(f"auth_error: HTTP {resp.status_code} em /models")
    if resp.status_code != 200:
        raise LlmError(f"HTTP {resp.status_code} em /models: {resp.text[:200]}")
    try:
        data = resp.json()
    except ValueError as exc:
        raise LlmError("resposta de /models não é JSON") from exc
    models = data.get("data")
    if not isinstance(models, list):
        raise LlmError("resposta de /models sem lista 'data'")
    return models


def resolve_model(cfg: dict, transport: httpx.BaseTransport | None = None) -> str | None:
    """Modelo usado na geração: alias explícito ou descoberta cacheada."""
    alias = (cfg.get("model_alias") or "auto").strip()
    if alias and alias != "auto":
        return alias
    base = cfg["base_url"].rstrip("/")
    key = (base, "auto")
    if key not in _discovery_cache:
        models = probe_models(cfg, transport)
        _discovery_cache[key] = models[0].get("id") if models else None
    return _discovery_cache[key]


def health(cfg: dict, transport: httpx.BaseTransport | None = None) -> dict:
    """Sonda /models em tempo real → schema LlmHealth (sempre devolve dict)."""
    base = cfg["base_url"].rstrip("/")
    alias = cfg.get("model_alias") or "auto"
    result = {
        "status": "ok",
        "provider": cfg.get("provider", "llamacpp"),
        "base_url": cfg.get("base_url"),
        "model_alias": alias,
        "model_discovered": None,
        "model_loaded": False,
        "details": None,
    }
    try:
        models = probe_models(cfg, transport)
    except LlmError as exc:
        result["status"] = exc.code
        result["details"] = exc.message
        return result
    except Exception as exc:  # defesa extra — health nunca derruba a API
        result["status"] = "error"
        result["details"] = f"{type(exc).__name__}: {exc}"
        return result

    result["model_loaded"] = len(models) > 0
    if alias == "auto":
        discovered = models[0].get("id") if models else None
        _discovery_cache[(base, "auto")] = discovered
        result["model_discovered"] = discovered
        if discovered is None:
            result["details"] = "servidor respondeu, mas nenhum modelo carregado"
    else:
        result["model_discovered"] = alias
        if models and not any(m.get("id") == alias for m in models):
            result["details"] = f"alias '{alias}' não consta em /models"
    return result


# ---------------------------------------------------------------- geração
def _props_base(cfg: dict) -> str:
    """base_url sem barra final e sem o sufixo `/v1` (só esse sufixo)."""
    return cfg["base_url"].rstrip("/").removesuffix("/v1")


def server_slot_count(cfg: dict, transport: httpx.BaseTransport | None = None) -> int:
    """GET {base}/props → `total_slots`.

    Qualquer falha (exceção, HTTP ≠ 200, corpo não-JSON, valor ilegível)
    cacheia e devolve 0. A chave do cache é o base_url já normalizado.
    """
    base = _props_base(cfg)
    if base in _slot_cache:
        return _slot_cache[base]
    timeout = min(float(cfg.get("timeout_seconds", 180)), PROBE_TIMEOUT)
    count = 0
    try:
        with _make_client(cfg, transport, timeout) as client:
            resp = client.get(f"{base}/props", headers=_headers(cfg))
        if resp.status_code == 200:
            data = resp.json()
            count = int(data.get("total_slots") or 0)
    except Exception:
        count = 0
    _slot_cache[base] = count
    return count


def slot_for(cfg: dict, role: str,
             transport: httpx.BaseTransport | None = None) -> int | None:
    """0 narrativa, 1 auxiliar; None se o servidor tem menos de 2 slots."""
    if server_slot_count(cfg, transport) < 2:
        return None
    if role == "narrative":
        return 0
    if role == "aux":
        return 1
    return None


def build_chat_payload(cfg: dict, messages: list[dict], *, sampler: dict,
                       stop: list[str], model: str,
                       transport: httpx.BaseTransport | None = None) -> dict:
    """Payload de /chat/completions (inclui o flag anti-thinking do Qwen3.x)."""
    payload: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "stream": True,
        "temperature": sampler.get("temperature", 0.85),
        "top_p": sampler.get("top_p", 1.0),
        "min_p": sampler.get("min_p", 0.08),
        # llama.cpp ignora "repetition_penalty" (nome HF); a chave efetiva é "repeat_penalty"
        "repeat_penalty": sampler.get("repetition_penalty", 1.02),
        "presence_penalty": sampler.get("presence_penalty", 0.15),
        "frequency_penalty": sampler.get("frequency_penalty", 0.10),
        "dry_multiplier": sampler.get("dry_multiplier", 0.8),
        "dry_base": sampler.get("dry_base", 1.75),
        "dry_allowed_length": int(sampler.get("dry_allowed_length", 2)),
        "dry_penalty_last_n": int(sampler.get("dry_penalty_last_n", -1)),
        "dry_sequence_breakers": list(sampler.get("dry_sequence_breakers", ["\n", ":", "\"", "*"])),
        "max_tokens": int(sampler.get("max_tokens", 300)),
        "stop": list(stop),
        "cache_prompt": True,
    }
    slot = slot_for(cfg, "narrative", transport)
    if slot is not None:
        payload["id_slot"] = slot
    if cfg.get("disable_thinking", True):
        payload["chat_template_kwargs"] = {"enable_thinking": False}
    return payload


def stream_chat_completion(cfg: dict, messages: list[dict], *, sampler: dict,
                           stop: list[str],
                           transport: httpx.BaseTransport | None = None,
                           state: StreamState | None = None) -> Iterator[str]:
    """Stream de tokens via SSE. Levanta LlmError/LlmUnreachable/LlmAuthError."""
    model = resolve_model(cfg, transport)
    if not model:
        raise LlmError("nenhum modelo descoberto em /models (model_alias=auto)")
    payload = build_chat_payload(cfg, messages, sampler=sampler, stop=stop, model=model,
                                 transport=transport)
    base = cfg["base_url"].rstrip("/")
    timeout = float(cfg.get("timeout_seconds", 180))

    saw_content = False
    saw_reasoning = False
    try:
        with _make_client(cfg, transport, timeout) as client:
            with client.stream("POST", f"{base}/chat/completions",
                               json=payload, headers=_headers(cfg)) as resp:
                if resp.status_code in (401, 403):
                    resp.read()
                    raise LlmAuthError(f"auth_error: HTTP {resp.status_code} em /chat/completions")
                if resp.status_code != 200:
                    resp.read()
                    raise LlmError(f"HTTP {resp.status_code} em /chat/completions:"
                                   f" {resp.text[:200]}")
                for line in resp.iter_lines():
                    if not line or not line.startswith("data:"):
                        continue
                    data = line[len("data:"):].strip()
                    if data == "[DONE]":
                        break
                    try:
                        chunk = json.loads(data)
                    except ValueError:
                        continue
                    for choice in chunk.get("choices") or []:
                        delta = choice.get("delta") or {}
                        token = delta.get("content")
                        if token:
                            saw_content = True
                            yield token
                        elif delta.get("reasoning_content"):
                            saw_reasoning = True
                        if state is not None and choice.get("finish_reason") in (
                            "length", "max_tokens",
                        ):
                            state.truncated = True
    except httpx.TransportError as exc:
        raise LlmUnreachable(f"llm unreachable: {type(exc).__name__}") from exc

    if not saw_content and saw_reasoning:
        # Fallback defensivo: Qwen3.x respondeu só com reasoning_content —
        # quase certeza de chat_template_kwargs.enable_thinking não aplicado.
        log.warning("LLM devolveu content vazio com reasoning_content presente; "
                    "verifique llm.disable_thinking (chat_template_kwargs.enable_thinking)")
        raise LlmError("config_error: resposta com content vazio (só reasoning_content); "
                       "ative llm.disable_thinking para modelos Qwen3.x híbridos")


def chat_completion(cfg: dict, messages: list[dict], *, temperature: float = 0.3,
                    max_tokens: int = 200, stop: list[str] | None = None,
                    transport: httpx.BaseTransport | None = None) -> str:
    """Chamada única SEM stream (tarefas auxiliares; slot 1 se há ≥2 slots).

    Mesmas regras de erro/descoberta do stream; devolve
    `choices[0].message.content` (stripped). Levanta LlmError subclasses.
    """
    model = resolve_model(cfg, transport)
    if not model:
        raise LlmError("nenhum modelo descoberto em /models (model_alias=auto)")
    payload: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "stream": False,
        "temperature": temperature,
        "max_tokens": int(max_tokens),
        "stop": list(stop or []),
        "cache_prompt": True,
    }
    slot = slot_for(cfg, "aux", transport)
    if slot is not None:
        payload["id_slot"] = slot
    if cfg.get("disable_thinking", True):
        payload["chat_template_kwargs"] = {"enable_thinking": False}
    base = cfg["base_url"].rstrip("/")
    timeout = float(cfg.get("timeout_seconds", 180))

    try:
        with _make_client(cfg, transport, timeout) as client:
            resp = client.post(f"{base}/chat/completions",
                               json=payload, headers=_headers(cfg))
    except httpx.TransportError as exc:
        raise LlmUnreachable(f"llm unreachable: {type(exc).__name__}") from exc
    if resp.status_code in (401, 403):
        raise LlmAuthError(f"auth_error: HTTP {resp.status_code} em /chat/completions")
    if resp.status_code != 200:
        raise LlmError(f"HTTP {resp.status_code} em /chat/completions:"
                       f" {resp.text[:200]}")
    try:
        data = resp.json()
        msg = (data.get("choices") or [{}])[0].get("message") or {}
    except (ValueError, IndexError, AttributeError) as exc:
        raise LlmError("resposta de /chat/completions inválida (sem choices[0].message)") from exc
    content = (msg.get("content") or "").strip()
    if not content and msg.get("reasoning_content"):
        raise LlmError("config_error: resposta com content vazio (só reasoning_content); "
                       "ative llm.disable_thinking para modelos Qwen3.x híbridos")
    return content
