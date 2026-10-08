"""Router de voz (v0.4.0): /voices, /tts, /stt e /settings/voice.

- GET  /voices          → vozes disponíveis + voz do narrador (config efetiva);
- POST /tts             → {"text", "voice"?} → bytes audio/mpeg (passthrough,
                          NÃO JSON); voice omitida = narrator_voice;
- POST /stt             → multipart campo `file` → {"text": ...} (repassa ao
                          provider /audio/transcriptions com model=stt_model);
- GET/PUT /settings/voice → config de voz (api_key SEMPRE mascarada).

Mapeamento de erros do provider (mesmo padrão de routes/images.py):
unreachable → 502 `unreachable`; 401/403 → 502 `auth_error`; demais → 502 `error`.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, File, Request, UploadFile
from fastapi.responses import Response

from app import db, settings_store, voice_client
from app.deps import get_http_transport
from app.errors import GameError
from app.schemas import TtsRequest, VoiceSettingsUpdate

log = logging.getLogger("aidungeon.voice")

router = APIRouter()


# ------------------------------------------------------------ settings/voice
def _voice_settings_response(conn) -> dict:
    """Shape VoiceSettings do openapi (api_key SEMPRE mascarada)."""
    cfg = settings_store.effective_voice_settings(conn)
    return {
        "enabled": cfg["enabled"],
        "base_url": cfg["base_url"],
        "tts_model": cfg["tts_model"],
        "stt_model": cfg["stt_model"],
        "narrator_voice": cfg["narrator_voice"],
        "voices": cfg["voices"],
        "timeout_seconds": cfg["timeout_seconds"],
        "max_text_chars": cfg["max_text_chars"],
        "stt_max_bytes": cfg["stt_max_bytes"],
        "api_key": settings_store.mask_api_key(cfg["api_key"]),
        "api_key_set": bool(cfg["api_key"]),
    }


@router.get("/settings/voice")
def get_voice_settings() -> dict:
    with db.session() as conn:
        return _voice_settings_response(conn)


@router.put("/settings/voice")
def put_voice_settings(body: VoiceSettingsUpdate) -> dict:
    update = body.model_dump(exclude_unset=True)
    with db.session() as conn:
        narrator = update.get("narrator_voice")
        if narrator is not None:
            voices = settings_store.effective_voice_settings(conn)["voices"]
            if narrator not in voices:
                raise GameError.bad_request(
                    "invalid_voice",
                    f"unknown narrator_voice {narrator!r}; expected one of {voices}")
        settings_store.apply_voice_update(conn, update)
        return _voice_settings_response(conn)


# -------------------------------------------------------------------- /voices
@router.get("/voices")
def list_voices() -> dict:
    with db.session() as conn:
        cfg = settings_store.effective_voice_settings(conn)
    return {"voices": cfg["voices"], "narrator_voice": cfg["narrator_voice"]}


# ----------------------------------------------------------------------- /tts
@router.post("/tts")
def post_tts(body: TtsRequest,
             transport: Any = Depends(get_http_transport)) -> Response:
    with db.session() as conn:
        cfg = settings_store.effective_voice_settings(conn)
    if not cfg["enabled"]:
        raise GameError.bad_request("voice_disabled",
                                    "voice is disabled (settings.voice.enabled)")
    text = body.text.strip()
    if not text:
        raise GameError.bad_request("invalid_payload", "text must not be empty")
    if len(text) > int(cfg["max_text_chars"]):
        raise GameError.bad_request(
            "text_too_long",
            f"text exceeds max_text_chars ({cfg['max_text_chars']})")
    # voice omitida (null) → narrator_voice; string vazia é inválida (400)
    voice = cfg["narrator_voice"] if body.voice is None else body.voice
    if not voice or voice not in cfg["voices"]:
        raise GameError.bad_request(
            "invalid_voice", f"unknown voice {body.voice!r}; expected one of {cfg['voices']}")
    try:
        audio = voice_client.synthesize(cfg, text, voice, transport=transport)
    except voice_client.VoiceError as exc:
        raise GameError(502, exc.code, exc.message) from exc
    return Response(content=audio, media_type="audio/mpeg")


# ----------------------------------------------------------------------- /stt
@router.post("/stt")
async def post_stt(request: Request, file: UploadFile | None = File(None),
                   transport: Any = Depends(get_http_transport)) -> dict:
    with db.session() as conn:
        cfg = settings_store.effective_voice_settings(conn)
    if not cfg["enabled"]:
        raise GameError.bad_request("voice_disabled",
                                    "voice is disabled (settings.voice.enabled)")
    if file is None:
        raise GameError.bad_request("invalid_payload",
                                    "multipart field 'file' is required")
    max_bytes = int(cfg["stt_max_bytes"])
    content_length = request.headers.get("content-length")
    if content_length:
        try:
            if int(content_length) > max_bytes:
                raise GameError(413, "audio_too_large",
                                f"audio exceeds stt_max_bytes ({max_bytes})")
        except ValueError:
            pass  # Content-Length malformado — o tamanho real é checado abaixo
    data = await file.read()
    if len(data) > max_bytes:
        raise GameError(413, "audio_too_large",
                        f"audio exceeds stt_max_bytes ({max_bytes})")
    try:
        text = voice_client.transcribe(
            cfg, file.filename or "audio",
            file.content_type or "application/octet-stream",
            data, transport=transport)
    except voice_client.VoiceError as exc:
        raise GameError(502, exc.code, exc.message) from exc
    return {"text": text}
