"""Cliente de voz (v0.4.0) — TTS Supertonic + STT Whisper via stack
OpenAI-compatível da VM. Espelha a estrutura de app/image_client.py.

Provider único (settings.voice, config efetiva de settings_store):
- TTS: POST {base_url}/audio/speech com Bearer, JSON {model: tts_model,
  voice, input, response_format: "mp3"} → 200 audio/mpeg (bytes devolvidos
  ao chamador sem tocar em disco — passthrough para a UI).
- STT: POST {base_url}/audio/transcriptions multipart (campo `file` com
  filename/content_type originais, data={model: stt_model}) → {"text": ...}
  (formato OpenAI whisper).

Erros: falha de conexão/timeout → VoiceUnreachable ("unreachable");
401/403 → VoiceAuthError ("auth_error"); demais → VoiceError. A api_key
NUNCA é escrita em logs/mensagens de erro.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

log = logging.getLogger("aidungeon.voice")


class VoiceError(Exception):
    code = "error"

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


class VoiceUnreachable(VoiceError):
    code = "unreachable"


class VoiceAuthError(VoiceError):
    code = "auth_error"


# ---------------------------------------------------------------- helpers
def _make_client(transport: httpx.BaseTransport | None, timeout: float) -> httpx.Client:
    t = httpx.Timeout(timeout, connect=min(10.0, timeout))
    kwargs: dict[str, Any] = {"timeout": t, "trust_env": False}
    if transport is not None:
        kwargs["transport"] = transport
    return httpx.Client(**kwargs)


def _headers(cfg: dict) -> dict[str, str]:
    """Authorization Bearer (se api_key efetiva); NUNCA logar o valor."""
    headers: dict[str, str] = {}
    if cfg.get("api_key"):
        headers["Authorization"] = f"Bearer {cfg['api_key']}"
    return headers


def _base(cfg: dict) -> str:
    base = (cfg.get("base_url") or "").rstrip("/")
    if not base:
        raise VoiceError("config_error: voice.base_url vazio")
    return base


# ------------------------------------------------------------------ público
def synthesize(cfg: dict, text: str, voice: str, *,
               transport: httpx.BaseTransport | None = None) -> bytes:
    """Sintetiza `text` com `voice` no provider efetivo; devolve bytes mp3.
    Levanta VoiceError subclasses."""
    timeout = float(cfg.get("timeout_seconds", 60))
    payload = {
        "model": cfg["tts_model"],
        "voice": voice,
        "input": text,
        "response_format": "mp3",
    }
    with _make_client(transport, timeout) as client:
        try:
            resp = client.post(f"{_base(cfg)}/audio/speech", json=payload,
                               headers=_headers(cfg))
        except httpx.TransportError as exc:
            raise VoiceUnreachable(f"voice provider unreachable: {type(exc).__name__}") from exc
    if resp.status_code in (401, 403):
        raise VoiceAuthError(f"auth_error: HTTP {resp.status_code} em audio/speech")
    if resp.status_code != 200:
        raise VoiceError(f"HTTP {resp.status_code} em audio/speech: {resp.text[:200]}")
    if not resp.content:
        raise VoiceError("resposta de audio/speech vazia")
    return resp.content


def transcribe(cfg: dict, filename: str, content_type: str, data: bytes, *,
               transport: httpx.BaseTransport | None = None) -> str:
    """Transcreve o áudio `data` no provider efetivo (multipart, model=stt_model);
    devolve o texto. Levanta VoiceError subclasses."""
    timeout = float(cfg.get("timeout_seconds", 60))
    files = {"file": (filename, data, content_type)}
    with _make_client(transport, timeout) as client:
        try:
            resp = client.post(f"{_base(cfg)}/audio/transcriptions", files=files,
                               data={"model": cfg["stt_model"]},
                               headers=_headers(cfg))
        except httpx.TransportError as exc:
            raise VoiceUnreachable(f"voice provider unreachable: {type(exc).__name__}") from exc
    if resp.status_code in (401, 403):
        raise VoiceAuthError(f"auth_error: HTTP {resp.status_code} em audio/transcriptions")
    if resp.status_code != 200:
        raise VoiceError(f"HTTP {resp.status_code} em audio/transcriptions: {resp.text[:200]}")
    try:
        body = resp.json()
    except ValueError as exc:
        raise VoiceError("resposta de audio/transcriptions não é JSON") from exc
    text = body.get("text") if isinstance(body, dict) else None
    if not isinstance(text, str):
        raise VoiceError("resposta de audio/transcriptions sem campo 'text'")
    return text
