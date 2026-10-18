"""Cliente de voz (v0.4.0) — TTS Supertonic + STT Whisper via stack
OpenAI-compatível da VM. Espelha a estrutura de app/image_client.py.

Provider único (settings.voice, config efetiva de settings_store):
- TTS: POST {base_url}/audio/speech com Bearer, JSON {model: tts_model,
  voice, input, response_format: "mp3", instructions?} → 200 audio/mpeg
  (bytes devolvidos ao chamador sem tocar em disco — passthrough para a UI).
  `instructions` leva a direção emocional. O modelo drama-3 só entra se
  voice.drama3_enabled estiver ligado; senão o texto falado fica limpo.
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

from app.voice_emotion import direction_for

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
def _post_speech(cfg: dict, payload: dict, *,
                 transport: httpx.BaseTransport | None) -> bytes:
    timeout = float(cfg.get("timeout_seconds", 60))
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


def synthesize(cfg: dict, text: str, voice: str, *,
               emotion: str | None = None,
               transport: httpx.BaseTransport | None = None) -> bytes:
    """Sintetiza `text` com `voice` no provider efetivo; devolve bytes mp3.

    `emotion` vira o campo `instructions` (o audio_server atual aceita e
    ignora). Com `drama3_enabled`, o modelo pedido é o Drama-3 e a direção
    entra no texto, no formato dos colchetes. Se esse modelo recusar, a
    fala volta ao `tts_model` (Supertonic) sem a etiqueta — senão ele leria
    o colchete em voz alta. Levanta VoiceError subclasses.
    """
    instruction = direction_for(emotion) if emotion else None
    use_drama = bool(cfg.get("drama3_enabled"))
    payload = {
        "model": (cfg.get("drama3_model") or "drama-3") if use_drama else cfg["tts_model"],
        "voice": voice,
        "input": f"{instruction} {text}" if use_drama and instruction else text,
        "response_format": "mp3",
    }
    if instruction:
        payload["instructions"] = instruction
    try:
        return _post_speech(cfg, payload, transport=transport)
    except (VoiceAuthError, VoiceUnreachable):
        raise
    except VoiceError as exc:
        if not use_drama:
            raise
        log.warning("drama-3 indisponível (%s); fallback %s", exc.code, cfg.get("tts_model"))
        payload["model"] = cfg["tts_model"]
        payload["input"] = text
        return _post_speech(cfg, payload, transport=transport)


def transcribe(cfg: dict, filename: str, content_type: str, data: bytes, *,
               language: str | None = None,
               transport: httpx.BaseTransport | None = None) -> str:
    """Transcreve o áudio `data` no provider efetivo (multipart, model=stt_model);
    devolve o texto. Levanta VoiceError subclasses.

    `language` (ISO 639-1, opcional) é repassado no form. Sem ele o servidor
    de voz assume português e uma fala em inglês volta vazia ou lixo.
    """
    timeout = float(cfg.get("timeout_seconds", 60))
    files = {"file": (filename, data, content_type)}
    form = {"model": cfg["stt_model"]}
    if language:
        form["language"] = language
    with _make_client(transport, timeout) as client:
        try:
            resp = client.post(f"{_base(cfg)}/audio/transcriptions", files=files,
                               data=form,
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
