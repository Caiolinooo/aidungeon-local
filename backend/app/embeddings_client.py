"""Cliente de embeddings OpenAI-compatível (llama-server na CPU, porta 8081).

- Modelo multilíngue padrão: Qwen3-Embedding-0.6B (1024 dimensões).
- Serialização via struct float32 BLOB (<Nf).
- Fallback tolerante a falhas: se base_url estiver indisponível, der timeout
  ou retornar erro, registra warning e retorna None sem levantar exceções.
- Suporta injeção de transport httpx para testes determinísticos.
"""

from __future__ import annotations

import logging
import math
import struct
from typing import Any

import httpx

log = logging.getLogger("aidungeon.embeddings")

DEFAULT_MODEL = "Qwen3-Embedding-0.6B"
DEFAULT_DIM = 1024
DEFAULT_TIMEOUT = 30.0


# ------------------------------------------------------------- vector math
def pack_vector(vec: list[float] | tuple[float, ...]) -> bytes:
    """Serializa lista de floats em bytes float32 little-endian (<Nf)."""
    if not vec:
        return b""
    return struct.pack(f"<{len(vec)}f", *vec)


def serialize_vector(vec: list[float] | tuple[float, ...]) -> bytes:
    """Alias para pack_vector."""
    return pack_vector(vec)


def unpack_vector(blob: bytes | None, expected_dim: int | None = None) -> list[float] | None:
    """Desserializa bytes float32 little-endian. Valida alinhamento e dimensão."""
    if not blob or not isinstance(blob, (bytes, bytearray)):
        return None
    if len(blob) % 4 != 0:
        return None
    dim = len(blob) // 4
    if expected_dim is not None and dim != expected_dim:
        return None
    try:
        return list(struct.unpack(f"<{dim}f", blob))
    except struct.error:
        return None


def deserialize_vector(blob: bytes | None) -> list[float]:
    """Alias para unpack_vector, retornando lista vazia em caso de falha."""
    vec = unpack_vector(blob)
    return vec if vec is not None else []


def cosine_similarity(u: list[float] | tuple[float, ...], v: list[float] | tuple[float, ...]) -> float:
    """Calcula a similaridade de cosseno entre dois vetores em [-1.0, 1.0]."""
    if not u or not v or len(u) != len(v):
        return 0.0
    dot = 0.0
    norm_u = 0.0
    norm_v = 0.0
    for a, b in zip(u, v):
        dot += a * b
        norm_u += a * a
        norm_v += b * b
    if not math.isfinite(dot) or not math.isfinite(norm_u) or not math.isfinite(norm_v):
        return 0.0
    if norm_u <= 0.0 or norm_v <= 0.0:
        return 0.0
    denom = math.sqrt(norm_u) * math.sqrt(norm_v)
    if not math.isfinite(denom) or denom <= 0.0:
        return 0.0
    val = dot / denom
    if not math.isfinite(val):
        return 0.0
    return max(-1.0, min(1.0, val))


# ------------------------------------------------------------- http client
def get_embedding(
    text: str,
    *,
    cfg: dict | None = None,
    settings: dict | None = None,
    conn: Any = None,
    transport: httpx.BaseTransport | None = None,
) -> list[float] | None:
    """Requisita vetor de embeddings via POST {base_url}/embeddings.

    Retorna list[float] ou None. NUNCA levanta exceção para o chamador.
    """
    clean_text = (text or "").strip()
    if not clean_text:
        return None

    resolved_cfg = cfg if cfg is not None else settings
    if resolved_cfg is None:
        try:
            from app import settings_store
            resolved_cfg = settings_store.effective_embeddings_settings(conn)
        except Exception:
            log.warning("embeddings_client: falha ao carregar configurações", exc_info=True)
            return None

    # Suporta tanto chave aninhada {"embeddings": {...}} quanto dict plano
    if isinstance(resolved_cfg, dict) and "embeddings" in resolved_cfg and isinstance(resolved_cfg["embeddings"], dict):
        resolved_cfg = resolved_cfg["embeddings"]

    if not resolved_cfg.get("enabled", True):
        return None

    base_url = (resolved_cfg.get("base_url") or "").rstrip("/")
    if not base_url:
        return None

    model = resolved_cfg.get("model") or DEFAULT_MODEL
    timeout = float(resolved_cfg.get("timeout_seconds", DEFAULT_TIMEOUT))

    url = f"{base_url}/embeddings"
    payload = {"model": model, "input": clean_text}
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    api_key = resolved_cfg.get("api_key")
    if isinstance(api_key, str) and api_key and api_key.isascii() and api_key.isprintable():
        headers["Authorization"] = f"Bearer {api_key}"
    if "openrouter.ai" in base_url.lower():
        headers["X-Title"] = "AI Dungeon Local"

    try:
        t = httpx.Timeout(timeout, connect=min(5.0, timeout))
        client_kwargs: dict[str, Any] = {"timeout": t, "trust_env": False}
        if transport is not None:
            client_kwargs["transport"] = transport

        with httpx.Client(**client_kwargs) as client:
            resp = client.post(url, json=payload, headers=headers)

        if resp.status_code != 200:
            log.warning("embeddings_client: HTTP %s em %s: %s",
                        resp.status_code, url, resp.text[:200])
            return None

        data = resp.json()
        items = data.get("data")
        if not items or not isinstance(items, list):
            log.warning("embeddings_client: resposta sem 'data' válida em %s", url)
            return None

        vec = items[0].get("embedding")
        if not vec or not isinstance(vec, list):
            return None

        return [float(x) for x in vec]

    except (httpx.TransportError, httpx.TimeoutException) as exc:
        log.warning("embeddings_client: serviço inacessível (%s: %s)",
                    type(exc).__name__, exc)
        return None
    except Exception as exc:
        log.warning("embeddings_client: erro inesperado (%s: %s)",
                    type(exc).__name__, exc, exc_info=True)
        return None


def health(cfg: dict, transport: httpx.BaseTransport | None = None) -> dict:
    """Sonda do serviço de embeddings (/models ou fallback /embeddings p/ Voyage/Jina)."""
    if not cfg.get("enabled", True):
        return {"status": "disabled", "details": "embeddings desativados"}
    base_url = str(cfg.get("base_url") or "").strip().rstrip("/")
    if not base_url:
        return {"status": "config_error", "details": "embeddings.base_url vazio"}
    headers = {"Accept": "application/json"}
    api_key = cfg.get("api_key")
    if isinstance(api_key, str) and api_key and api_key.isascii() and api_key.isprintable():
        headers["Authorization"] = f"Bearer {api_key}"
    if "openrouter.ai" in base_url.lower():
        headers["X-Title"] = "AI Dungeon Local"
    timeout = min(float(cfg.get("timeout_seconds", DEFAULT_TIMEOUT)), 10.0)
    t = httpx.Timeout(timeout, connect=min(5.0, timeout))
    client_kwargs: dict[str, Any] = {"timeout": t, "trust_env": False}
    if transport is not None:
        client_kwargs["transport"] = transport
    probe_url = (
        f"{base_url}/embeddings/models"
        if "openrouter.ai" in base_url.lower()
        else f"{base_url}/models"
    )
    try:
        with httpx.Client(**client_kwargs) as client:
            resp = client.get(probe_url, headers=headers)
            if resp.status_code == 404:
                # Provedores dedicados de embeddings (Voyage AI, Jina AI) não expõem /models.
                model = str(cfg.get("model") or DEFAULT_MODEL).strip()
                emb_headers = {**headers, "Content-Type": "application/json"}
                resp = client.post(
                    f"{base_url}/embeddings",
                    json={"model": model, "input": "ok"},
                    headers=emb_headers,
                )
        if resp.status_code in (401, 403):
            return {"status": "auth_error", "details": f"HTTP {resp.status_code}"}
        if resp.status_code != 200:
            return {"status": "error", "details": f"HTTP {resp.status_code}"}
        data = resp.json()
        if not isinstance(data, (dict, list)):
            return {"status": "error", "details": "resposta inválida"}
        return {"status": "ok", "details": None}
    except (httpx.TransportError, httpx.TimeoutException) as exc:
        return {"status": "unreachable", "details": type(exc).__name__}
    except Exception:
        return {"status": "error", "details": "falha ao verificar embeddings"}

