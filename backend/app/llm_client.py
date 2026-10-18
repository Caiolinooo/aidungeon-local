"""Cliente do servidor llama.cpp (API OpenAI-compatível) da VM.

- Descoberta de modelo: `model_alias == "auto"` → `GET {base_url}/models`,
  usa o id do primeiro modelo e cacheia por (base_url, model_alias).
  `invalidate_discovery_cache()` é chamada quando base_url/model_alias mudam
  via `PUT /api/settings/llm`.
- Roles e slots (contrato C1, docs/agents/CONTRATOS.md): cada chamada declara
  um role. `GET {base}/props` (`base` = base_url sem `/v1`) informa
  `total_slots`; com ≥ 2 slots a narrativa fica no slot 0 com
  `cache_prompt=true` e as auxiliares nunca usam o slot 0. Com 4 slots:
  choices → 1, chronicle/summary → 2, image_prompt/assistant/meta → 3.
  Auxiliares vão com `cache_prompt=false`: slot fixado + prompt cache trava o
  llama.cpp em modelos híbridos (llama.cpp #28280).
- A narrativa vem em SSE; as auxiliares em JSON (`stream=false`). Cancelar
  (`cancel`) ou estourar o prazo do role derruba o socket; o llama-server
  percebe o cliente desconectado e libera o slot (vale para os dois modos).
- `disable_thinking=true` (default; OBRIGATÓRIO p/ Qwen3.x híbrido) → toda
  chamada inclui `chat_template_kwargs={"enable_thinking": false}`. Sem isso o
  servidor consome os tokens como `reasoning_content` e devolve content VAZIO.
- Erros: 401/403 → `LlmAuthError` ("auth_error"); falha de conexão →
  `LlmUnreachable` ("unreachable"); prazo → `LlmTimeout`; cancelada →
  `LlmCancelled`; breaker aberto → `LlmDegraded`; demais → `LlmError`.

A api_key NUNCA é escrita em logs/mensagens de erro.
"""

from __future__ import annotations

import collections
import json
import logging
import socket
import threading
import time
from typing import Any, Iterator

import httpx

log = logging.getLogger("aidungeon.llm")

PROBE_TIMEOUT = 15.0

ROLES = ("narrative", "choices", "chronicle", "summary",
         "image_prompt", "assistant", "meta")

# Slot de cada auxiliar quando o servidor tem 4+ slots.
_AUX_SLOTS_4 = {"choices": 1, "chronicle": 2, "summary": 2,
                "image_prompt": 3, "assistant": 3, "meta": 3}

DEFAULT_ROLE_TIMEOUTS = {"choices": 15.0, "chronicle": 45.0, "summary": 60.0,
                         "image_prompt": 20.0, "assistant": 90.0, "meta": 45.0}

# Cache de descoberta: (base_url, model_alias) -> id do modelo (ou None)
_discovery_cache: dict[tuple[str, str], str | None] = {}
# Cache de slots: base_url sem sufixo /v1 -> (total_slots, expira_em).
# 0 = um slot ou falha. A falha expira logo: sem slot conhecido as auxiliares
# vão sem id_slot e podem cair no slot 0, apagando o cache da narrativa.
_slot_cache: dict[str, tuple[int, float]] = {}
_SLOT_CACHE_TTL_S = 300.0
_SLOT_FAILURE_TTL_S = 30.0

# Últimas chamadas (role, slot, tempos do llama-server) para /api/health/llm.
_recent_calls: collections.deque = collections.deque(maxlen=200)
_recent_lock = threading.Lock()


class LlmError(Exception):
    code = "error"

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


class LlmUnreachable(LlmError):
    code = "unreachable"


class LlmAuthError(LlmError):
    code = "auth_error"


class LlmTimeout(LlmError):
    code = "llm_timeout"


class LlmCancelled(LlmError):
    code = "llm_cancelled"


class LlmDegraded(LlmError):
    code = "llm_degraded"


class StreamState:
    """O teto de tokens (`finish_reason` length) corta a frase no meio.

    `timings` guarda o bloco de tempos do llama-server da última resposta
    (prompt_n, cache_n, predicted_per_second...); `ttft_s` o tempo até o
    primeiro token."""

    def __init__(self) -> None:
        self.truncated = False
        self.timings: dict = {}
        self.ttft_s: float | None = None
        self.slot: int | None = None


def invalidate_discovery_cache() -> None:
    _discovery_cache.clear()
    _slot_cache.clear()


def invalidate_slot_cache() -> None:
    """O llama-server reiniciou (watchdog, troca de -np): relê `total_slots`."""
    _slot_cache.clear()


def cached_discovery(base_url: str, model_alias: str) -> str | None:
    return _discovery_cache.get((base_url.rstrip("/"), model_alias))


def recent_calls(limit: int = 50) -> list[dict]:
    with _recent_lock:
        return list(_recent_calls)[-limit:]


def _record_call(role: str, slot: int | None, state: StreamState,
                 started: float, outcome: str) -> None:
    timings = state.timings or {}
    entry = {
        "role": role,
        "slot": slot,
        "outcome": outcome,
        "at": time.time(),
        "total_s": round(time.monotonic() - started, 3),
        "ttft_s": round(state.ttft_s, 3) if state.ttft_s is not None else None,
        "prompt_n": timings.get("prompt_n"),
        "cache_n": timings.get("cache_n"),
        "predicted_n": timings.get("predicted_n"),
        "prompt_per_second": timings.get("prompt_per_second"),
        "predicted_per_second": timings.get("predicted_per_second"),
    }
    with _recent_lock:
        _recent_calls.append(entry)
    if role == "narrative" or outcome != "ok":
        log.info("llm %s slot=%s %s total=%.1fs ttft=%s prompt_n=%s cache_n=%s tok/s=%s",
                 role, slot, outcome, entry["total_s"], entry["ttft_s"],
                 entry["prompt_n"], entry["cache_n"], entry["predicted_per_second"])


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


# ---------------------------------------------------------------- slots
def _props_base(cfg: dict) -> str:
    """base_url sem barra final e sem o sufixo `/v1` (só esse sufixo)."""
    return cfg["base_url"].rstrip("/").removesuffix("/v1")


def server_slot_count(cfg: dict, transport: httpx.BaseTransport | None = None) -> int:
    """GET {base}/props → `total_slots`.

    Qualquer falha (exceção, HTTP ≠ 200, corpo não-JSON, valor ilegível)
    cacheia e devolve 0. A chave do cache é o base_url já normalizado.
    """
    base = _props_base(cfg)
    now = time.monotonic()
    cached = _slot_cache.get(base)
    if cached is not None and cached[1] > now:
        return cached[0]
    timeout = min(float(cfg.get("timeout_seconds", 180)), PROBE_TIMEOUT)
    count = 0
    ok = False
    try:
        with _make_client(cfg, transport, timeout) as client:
            resp = client.get(f"{base}/props", headers=_headers(cfg))
        if resp.status_code == 200:
            data = resp.json()
            count = int(data.get("total_slots") or 0)
            ok = True
    except Exception:
        count = 0
    ttl = _SLOT_CACHE_TTL_S if ok else _SLOT_FAILURE_TTL_S
    _slot_cache[base] = (count, now + ttl)
    return count


def normalize_role(role: str | None) -> str:
    if role == "aux" or not role:
        return "assistant"
    if role not in ROLES:
        raise ValueError(f"unknown llm role: {role}")
    return role


def slot_for(cfg: dict, role: str,
             transport: httpx.BaseTransport | None = None) -> int | None:
    """Slot fixo do role, ou None (o servidor escolhe).

    - < 2 slots: None (um `id_slot` inexistente trava o llama.cpp).
    - narrativa: 0.
    - 2 slots: auxiliares no 1. 3 slots: choices no 1, o resto no 2.
    - 4+ slots: mapa do contrato C1.
    """
    role = normalize_role(role)
    total = server_slot_count(cfg, transport)
    if total < 2:
        return None
    if role == "narrative":
        return 0
    if total >= 4:
        return _AUX_SLOTS_4[role]
    if total == 3:
        return 1 if role == "choices" else 2
    return 1


def role_timeout(cfg: dict, role: str) -> float:
    """Prazo total (s) de uma auxiliar: `cfg.role_timeouts` ou o padrão C1."""
    role = normalize_role(role)
    overrides = cfg.get("role_timeouts") or {}
    try:
        value = float(overrides.get(role, DEFAULT_ROLE_TIMEOUTS.get(role, 60.0)))
    except (TypeError, ValueError):
        value = DEFAULT_ROLE_TIMEOUTS.get(role, 60.0)
    return max(1.0, value)


def scaled_timeout(cfg: dict, role: str, max_tokens: int, *,
                   tokens_per_s: float = 12.0, overhead_s: float = 30.0) -> float:
    """Prazo de uma saída longa (rascunho de cenário): o do role ou o tempo de
    gerar `max_tokens` com folga, o que for maior."""
    return max(role_timeout(cfg, role), max_tokens / tokens_per_s + overhead_s)


def _cache_prompt(cfg: dict, role: str) -> bool:
    if role == "narrative":
        return True
    return bool(cfg.get("aux_cache_prompt", False))


# ------------------------------------------------------------ cancelamento
def _stream_socket(stream: Any) -> socket.socket | None:
    if stream is None:
        return None
    try:
        sock = stream.get_extra_info("socket")
    except Exception:
        return None
    return sock if isinstance(sock, socket.socket) else None


class _CallGuard:
    """Vigia `cancel` e o prazo. Ao disparar, derruba o socket (shutdown): a
    leitura bloqueada acorda na hora e o llama-server solta o slot.

    O socket vem do trace `connection.connect_tcp.complete`: sem stream o
    llama-server só manda os headers no fim da geração, então esperar a
    resposta para pegar o socket deixaria o cancelamento sem efeito."""

    def __init__(self, cancel: threading.Event | None, deadline: float | None):
        self.cancel = cancel
        self.deadline = deadline
        self.reason: str | None = None
        self._sock: socket.socket | None = None
        self._done = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self.cancel is None and self.deadline is None:
            return
        self._thread = threading.Thread(target=self._run, name="llm-call-guard",
                                        daemon=True)
        self._thread.start()

    def attach(self, sock: socket.socket | None) -> None:
        if sock is None:
            return
        self._sock = sock
        if self.reason:
            self._shutdown()

    def trace(self, event: str, info: dict) -> None:
        if event == "connection.connect_tcp.complete":
            self.attach(_stream_socket(info.get("return_value")))

    def _run(self) -> None:
        while not self._done.wait(0.1):
            if self._expired():
                self._shutdown()
                return

    def _expired(self) -> bool:
        if self.reason:
            return True
        if self.cancel is not None and self.cancel.is_set():
            self.reason = "cancel"
        elif self.deadline is not None and time.monotonic() >= self.deadline:
            self.reason = "timeout"
        return self.reason is not None

    def _shutdown(self) -> None:
        sock = self._sock
        if sock is None:
            return
        try:
            sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass

    def check(self) -> None:
        if self._expired():
            raise self.error()

    def error(self) -> LlmError:
        if self.reason == "cancel":
            return LlmCancelled("llm call cancelled")
        return LlmTimeout("llm call timed out")

    def stop(self) -> None:
        self._done.set()


# ---------------------------------------------------------------- geração
def build_chat_payload(cfg: dict, messages: list[dict], *, sampler: dict,
                       stop: list[str], model: str,
                       transport: httpx.BaseTransport | None = None,
                       role: str = "narrative") -> dict:
    """Payload de /chat/completions (inclui o flag anti-thinking do Qwen3.x)."""
    role = normalize_role(role)
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
        "cache_prompt": _cache_prompt(cfg, role),
    }
    slot = slot_for(cfg, role, transport)
    if slot is not None:
        payload["id_slot"] = slot
    if cfg.get("disable_thinking", True):
        payload["chat_template_kwargs"] = {"enable_thinking": False}
    return payload


def _iter_content(cfg: dict, payload: dict, *, transport: httpx.BaseTransport | None,
                  read_timeout: float, guard: _CallGuard,
                  state: StreamState) -> Iterator[str]:
    """POST /chat/completions e devolve o conteúdo pedaço a pedaço.

    Aceita SSE (stream) e JSON (servidor ou mock que ignora `stream`)."""
    base = cfg["base_url"].rstrip("/")
    started = time.monotonic()
    saw_content = False
    saw_reasoning = False
    finished = False
    try:
        with _make_client(cfg, transport, read_timeout) as client:
            with client.stream("POST", f"{base}/chat/completions",
                               json=payload, headers=_headers(cfg),
                               extensions={"trace": guard.trace}) as resp:
                guard.attach(_stream_socket(resp.extensions.get("network_stream")))
                if resp.status_code in (401, 403):
                    resp.read()
                    raise LlmAuthError(f"auth_error: HTTP {resp.status_code} em /chat/completions")
                if resp.status_code != 200:
                    resp.read()
                    raise LlmError(f"HTTP {resp.status_code} em /chat/completions:"
                                   f" {resp.text[:200]}")
                ctype = resp.headers.get("content-type", "")
                if "event-stream" not in ctype:
                    resp.read()
                    guard.check()
                    try:
                        data = resp.json()
                        msg = (data.get("choices") or [{}])[0].get("message") or {}
                    except (ValueError, IndexError, AttributeError) as exc:
                        raise LlmError("resposta de /chat/completions inválida "
                                       "(sem choices[0].message)") from exc
                    if isinstance(data.get("timings"), dict):
                        state.timings = data["timings"]
                    choice = (data.get("choices") or [{}])[0]
                    if choice.get("finish_reason") in ("length", "max_tokens"):
                        state.truncated = True
                    finished = True
                    content = msg.get("content") or ""
                    if content:
                        state.ttft_s = time.monotonic() - started
                        yield content
                        return
                    if msg.get("reasoning_content"):
                        saw_reasoning = True
                else:
                    for line in resp.iter_lines():
                        guard.check()
                        if not line or not line.startswith("data:"):
                            continue
                        data = line[len("data:"):].strip()
                        if data == "[DONE]":
                            finished = True
                            break
                        try:
                            chunk = json.loads(data)
                        except ValueError:
                            continue
                        if isinstance(chunk.get("timings"), dict):
                            state.timings = chunk["timings"]
                        for choice in chunk.get("choices") or []:
                            delta = choice.get("delta") or {}
                            token = delta.get("content")
                            if token:
                                if not saw_content:
                                    state.ttft_s = time.monotonic() - started
                                saw_content = True
                                yield token
                            elif delta.get("reasoning_content"):
                                saw_reasoning = True
                            if choice.get("finish_reason"):
                                finished = True
                            if choice.get("finish_reason") in ("length", "max_tokens"):
                                state.truncated = True
                    if not finished and guard.reason:
                        # Socket derrubado pelo guard: o corpo pode ter terminado
                        # "limpo", mas a resposta está incompleta.
                        raise guard.error()
    except httpx.TimeoutException as exc:
        if guard.reason:
            raise guard.error() from exc
        raise LlmTimeout(f"llm call timed out: {type(exc).__name__}") from exc
    except httpx.TransportError as exc:
        if guard.reason:
            raise guard.error() from exc
        # Servidor caiu ou reiniciou (watchdog, outro -np): relê os slots.
        _slot_cache.pop(_props_base(cfg), None)
        raise LlmUnreachable(f"llm unreachable: {type(exc).__name__}") from exc

    if not saw_content and saw_reasoning:
        # Fallback defensivo: Qwen3.x respondeu só com reasoning_content —
        # quase certeza de chat_template_kwargs.enable_thinking não aplicado.
        log.warning("LLM devolveu content vazio com reasoning_content presente; "
                    "verifique llm.disable_thinking (chat_template_kwargs.enable_thinking)")
        raise LlmError("config_error: resposta com content vazio (só reasoning_content); "
                       "ative llm.disable_thinking para modelos Qwen3.x híbridos")


def stream_chat_completion(cfg: dict, messages: list[dict], *, sampler: dict,
                           stop: list[str],
                           transport: httpx.BaseTransport | None = None,
                           state: StreamState | None = None,
                           role: str = "narrative",
                           cancel: threading.Event | None = None,
                           timeout_s: float | None = None) -> Iterator[str]:
    """Stream de tokens via SSE. Levanta LlmError e subclasses."""
    role = normalize_role(role)
    state = state if state is not None else StreamState()
    model = resolve_model(cfg, transport)
    if not model:
        raise LlmError("nenhum modelo descoberto em /models (model_alias=auto)")
    payload = build_chat_payload(cfg, messages, sampler=sampler, stop=stop, model=model,
                                 transport=transport, role=role)
    state.slot = payload.get("id_slot")
    read_timeout = float(cfg.get("timeout_seconds", 180))
    deadline = time.monotonic() + timeout_s if timeout_s else None
    guard = _CallGuard(cancel, deadline)
    guard.start()
    started = time.monotonic()
    outcome = "error"
    try:
        yield from _iter_content(cfg, payload, transport=transport,
                                 read_timeout=read_timeout, guard=guard, state=state)
        outcome = "ok"
    except GeneratorExit:
        outcome = "aborted"
        raise
    except LlmCancelled:
        outcome = "cancelled"
        raise
    except LlmTimeout:
        outcome = "timeout"
        raise
    finally:
        guard.stop()
        _record_call(role, state.slot, state, started, outcome)


def chat_completion(cfg: dict, messages: list[dict], *, role: str = "assistant",
                    timeout_s: float | None = None,
                    cancel: threading.Event | None = None,
                    temperature: float = 0.3,
                    max_tokens: int = 200, stop: list[str] | None = None,
                    transport: httpx.BaseTransport | None = None,
                    state: StreamState | None = None) -> str:
    """Chamada auxiliar: devolve o texto inteiro (stripped).

    Slot e `cache_prompt` seguem o role (C1). `timeout_s` (padrão: prazo do
    role) é o tempo total; `cancel` aborta a qualquer momento. Levanta
    LlmError e subclasses.
    """
    role = normalize_role(role)
    state = state if state is not None else StreamState()
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
        "cache_prompt": _cache_prompt(cfg, role),
    }
    slot = slot_for(cfg, role, transport)
    if slot is not None:
        payload["id_slot"] = slot
    state.slot = slot
    if cfg.get("disable_thinking", True):
        payload["chat_template_kwargs"] = {"enable_thinking": False}

    total = float(timeout_s) if timeout_s else role_timeout(cfg, role)
    guard = _CallGuard(cancel, time.monotonic() + total)
    guard.start()
    started = time.monotonic()
    outcome = "error"
    parts: list[str] = []
    try:
        for piece in _iter_content(cfg, payload, transport=transport,
                                   read_timeout=total + 1.0, guard=guard, state=state):
            parts.append(piece)
        outcome = "ok"
    except LlmCancelled:
        outcome = "cancelled"
        raise
    except LlmTimeout:
        outcome = "timeout"
        raise
    finally:
        guard.stop()
        _record_call(role, slot, state, started, outcome)
    return "".join(parts).strip()
