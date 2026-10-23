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

import base64
import logging
import sqlite3
import uuid
from typing import Any

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.responses import Response

from app import db, npc_bonds, settings_store, voice_client, voice_emotion
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
        "drama3_model": cfg["drama3_model"],
        "drama3_enabled": cfg["drama3_enabled"],
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
        if narrator is not None and not _known_voice(conn, narrator):
            raise GameError.bad_request(
                "invalid_voice",
                f"unknown narrator_voice {narrator!r}")
        settings_store.apply_voice_update(conn, update)
        return _voice_settings_response(conn)


# -------------------------------------------------------------------- /voices
def _known_voice(conn, name: str) -> bool:
    if not name:
        return False
    voices = settings_store.effective_voice_settings(conn)["voices"]
    if name in voices:
        return True
    return db.get_cloned_voice(conn, name) is not None


@router.get("/voices")
def list_voices() -> dict:
    with db.session() as conn:
        cfg = settings_store.effective_voice_settings(conn)
        cloned = db.list_cloned_voices(conn)
    return {
        "voices": cfg["voices"],
        "cloned": cloned,
        "narrator_voice": cfg["narrator_voice"],
    }


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
    with db.session() as conn:
        if not voice or not _known_voice(conn, voice):
            raise GameError.bad_request(
                "invalid_voice", f"unknown voice {body.voice!r}")
        cloned = db.get_cloned_voice(conn, voice) if voice.startswith("cv_") else None
    if cloned is not None:
        try:
            voice_client.put_clone_rvq(
                cfg, voice, cloned["ref_text"], cloned["rvq"], transport=transport)
        except voice_client.VoiceError as exc:
            raise GameError(502, exc.code, exc.message) from exc
    npc_emotion = None
    adventure_id = (body.adventure_id or "").strip()
    if adventure_id:
        with db.session() as conn:
            if db.get_adventure(conn, adventure_id):
                cards = db.list_cards(conn, adventure_id)
                npc_emotion = npc_bonds.npc_emotion_for_line(cards, text)
    emotion = voice_emotion.choose_emotion(
        text, npc_emotion=npc_emotion, hint=body.emotion)
    try:
        audio = voice_client.synthesize(
            cfg, text, voice, emotion=emotion, transport=transport)
    except voice_client.VoiceError as exc:
        raise GameError(502, exc.code, exc.message) from exc
    return Response(content=audio, media_type="audio/mpeg",
                    headers={"X-Voice-Emotion": emotion})


# ----------------------------------------------------------------------- /stt
@router.post("/stt")
async def post_stt(request: Request, file: UploadFile | None = File(None),
                   language: str | None = Form(None),
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
    lang = (language or "").strip().lower().split("-")[0]
    if len(lang) != 2 or not lang.isalpha():
        lang = None
    try:
        text = voice_client.transcribe(
            cfg, file.filename or "audio",
            file.content_type or "application/octet-stream",
            data, language=lang, transport=transport)
    except voice_client.VoiceError as exc:
        raise GameError(502, exc.code, exc.message) from exc
    return {"text": text}

CLONE_MAX_BYTES = 15 * 1024 * 1024
_CLONE_CODES = {"audio_too_short", "audio_too_long", "invalid_audio"}


def _voice_http(exc: voice_client.VoiceError) -> GameError:
    if exc.code in _CLONE_CODES:
        return GameError.bad_request(exc.code, exc.message)
    return GameError(502, exc.code, exc.message)


@router.post("/voices/clone")
async def post_clone(file: UploadFile | None = File(None),
                     name: str = Form(""),
                     ref_text: str = Form(""),
                     transport: Any = Depends(get_http_transport)) -> dict:
    """Grava uma voz clonada. Sem ref_text, o Whisper preenche o que foi dito."""
    clean_name = (name or "").strip()
    if not clean_name or len(clean_name) > 40:
        raise GameError.bad_request("invalid_payload", "name must be 1-40 characters")
    if file is None:
        raise GameError.bad_request("invalid_audio", "multipart field 'file' is required")
    data = await file.read()
    if not data or len(data) > CLONE_MAX_BYTES:
        raise GameError.bad_request("invalid_audio", "audio is empty or larger than 15 MiB")
    with db.session() as conn:
        cfg = settings_store.effective_voice_settings(conn)
        taken = any(
            row["name"].casefold() == clean_name.casefold()
            for row in db.list_cloned_voices(conn)
        )
    if taken:
        raise GameError.conflict("voice_name_taken", "Já existe uma voz com esse nome.")
    if not cfg["enabled"]:
        raise GameError.bad_request("voice_disabled",
                                    "voice is disabled (settings.voice.enabled)")
    transcript = (ref_text or "").strip()
    if not transcript:
        try:
            transcript = voice_client.transcribe(
                cfg, file.filename or "audio",
                file.content_type or "application/octet-stream",
                data, language="pt", transport=transport).strip()
        except voice_client.VoiceError as exc:
            raise _voice_http(exc) from exc
    if not transcript:
        raise GameError.bad_request(
            "ref_text_empty", "Digite o que foi dito nesse áudio.")
    transcript = transcript[:2000]
    voice_id = None
    with db.session() as conn:
        for _ in range(2):
            candidate = "cv_" + uuid.uuid4().hex[:8]
            if db.get_cloned_voice(conn, candidate) is None:
                voice_id = candidate
                break
    if voice_id is None:
        raise GameError(500, "error", "could not allocate voice id")
    try:
        remote = voice_client.register_clone(
            cfg, voice_id, clean_name, transcript,
            file.filename or "audio", data,
            content_type=file.content_type or "application/octet-stream",
            transport=transport)
    except voice_client.VoiceError as exc:
        raise _voice_http(exc) from exc
    try:
        rvq = base64.b64decode(remote["rvq_b64"], validate=True)
    except Exception as exc:
        raise GameError(502, "error", "rvq_b64 inválido") from exc
    if not rvq:
        raise GameError(502, "error", "rvq vazio")
    seconds = float(remote.get("seconds") or 0)
    try:
        with db.session() as conn:
            db.insert_cloned_voice(
                conn, voice_id=voice_id, name=clean_name, ref_text=transcript,
                rvq=rvq, sample_seconds=seconds)
    except sqlite3.IntegrityError as exc:
        raise GameError.conflict("voice_name_taken", "Já existe uma voz com esse nome.") from exc
    return {"id": voice_id, "name": clean_name, "ref_text": transcript, "seconds": seconds}


@router.delete("/voices/{voice_id}")
def delete_voice(voice_id: str,
                 transport: Any = Depends(get_http_transport)) -> dict:
    with db.session() as conn:
        row = db.get_cloned_voice(conn, voice_id)
        if row is None:
            raise GameError(404, "voice_not_found", f"voice {voice_id} not found")
        db.delete_cloned_voice(conn, voice_id)
        db.clear_card_voice(conn, voice_id)
        cfg = settings_store.effective_voice_settings(conn)
        if cfg["narrator_voice"] == voice_id:
            settings_store.apply_voice_update(conn, {"narrator_voice": "M1"})
    voice_client.delete_clone_remote(cfg, voice_id, transport=transport)
    return {"id": voice_id}
