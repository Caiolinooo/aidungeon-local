"""Router de voz (v0.4.0): /voices, /tts, /stt e /settings/voice.

- GET  /voices          → vozes disponíveis + voz do narrador (config efetiva);
- POST /tts             → {"text", "voice"?} → bytes audio/mpeg (passthrough,
                          NÃO JSON); voice omitida = narrator_voice;
- POST /stt             → multipart campo `file` → {"text": ...} (repassa ao
                          provider /audio/transcriptions com model=stt_model);
- GET/PUT /settings/voice → config de voz (api_key SEMPRE mascarada).

Clones pertencem a quem os criou. O dono e o admin podem usá-los; participantes
de uma aventura compartilhada podem usar, informando adventure_id, os clones
do dono dessa aventura. Listar e apagar não expõe clones de outras pessoas.

Mapeamento de erros do provider (mesmo padrão de routes/images.py):
unreachable → 502 `unreachable`; 401/403 → 502 `auth_error`; demais → 502 `error`.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import sqlite3
import uuid
from collections.abc import Coroutine
from typing import Any, TypeVar

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.responses import Response, StreamingResponse
from starlette.requests import ClientDisconnect


from app import access, auth, db, npc_bonds, settings_store, voice_client, voice_emotion
from app.deps import get_http_transport
from app.errors import GameError
from app.schemas import TtsRequest, VoiceSettingsUpdate
from app.users import is_admin

log = logging.getLogger("aidungeon.voice")

router = APIRouter()

T = TypeVar("T")


async def _await_until_disconnect(request: Request, operation: Coroutine[Any, Any, T]) -> T:
    """Cancela o awaitable HTTP quando ASGI informa que o cliente desconectou."""
    task = asyncio.create_task(operation)
    try:
        while True:
            if await request.is_disconnected():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                raise ClientDisconnect()
            done, _ = await asyncio.wait((task,), timeout=0.1)
            if task in done:
                return task.result()
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


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
def put_voice_settings(body: VoiceSettingsUpdate,
                       info: auth.AuthInfo = Depends(access.require_admin)) -> dict:
    update = body.model_dump(exclude_unset=True)
    with db.session() as conn:
        narrator = update.get("narrator_voice")
        if narrator is not None and not _known_voice(conn, narrator):
            raise GameError.bad_request(
                "invalid_voice",
                f"unknown narrator_voice {narrator!r}")
        settings_store.apply_voice_update(conn, update)
        return _voice_settings_response(conn)



# --------------------------------------------------------------- /health/voice
@router.get("/health/voice")
def health_voice(transport: Any = Depends(get_http_transport)) -> dict:
    with db.session() as conn:
        cfg = settings_store.effective_voice_settings(conn)
    return voice_client.health(cfg, transport=transport)

# -------------------------------------------------------------------- /voices
def _known_voice(conn, name: str) -> bool:
    if not name:
        return False
    voices = settings_store.effective_voice_settings(conn)["voices"]
    if name in voices:
        return True
    return db.get_cloned_voice(conn, name) is not None


@router.get("/voices")
def list_voices(info: auth.AuthInfo = Depends(auth.require_auth)) -> dict:
    with db.session() as conn:
        cfg = settings_store.effective_voice_settings(conn)
        cloned = db.list_cloned_voices(
            conn, owner=None if is_admin(info.user) else info.user)
    return {
        "voices": cfg["voices"],
        "cloned": cloned,
        "narrator_voice": cfg["narrator_voice"],
    }


def _may_use_clone(conn, username: str, cloned: dict, adventure_id: str) -> bool:
    if access.can_manage(username, cloned["owner"]):
        return True
    if not adventure_id:
        return False
    adventure = db.get_adventure(conn, adventure_id)
    if not adventure or not db.can_access_adventure(conn, adventure, username):
        return False
    return bool(adventure["owner"]) and cloned["owner"] == adventure["owner"]


async def _ensure_clone(request: Request, cfg: dict, voice: str, cloned: dict | None,
                        transport: Any, *, force: bool = False) -> None:
    """Envia o RVQ uma vez por id+SHA256 neste processo. force reenvia."""
    if cloned is None:
        return
    rvq = cloned["rvq"]
    if force:
        voice_client.forget_clone_rvq(voice)
    elif voice_client.clone_rvq_is_cached(voice, rvq):
        return
    try:
        await _await_until_disconnect(
            request,
            voice_client.put_clone_rvq_async(
                cfg, voice, cloned["ref_text"], rvq, transport=transport),
        )
    except voice_client.VoiceError as exc:
        raise GameError(502, exc.code, exc.message) from exc


def _as_game_error(exc: voice_client.VoiceError) -> GameError:
    """clone_missing é interno: o cliente continua vendo `error`."""
    code = "error" if exc.code == "clone_missing" else exc.code
    return GameError(502, code, exc.message)


async def _retry_if_clone_missing(request: Request, cfg: dict, voice: str,
                                  cloned: dict | None, transport: Any,
                                  exc: voice_client.VoiceError, operation):
    """Reenvia o clone uma vez e repete a fala só se nenhum áudio foi entregue."""
    if cloned is None or not voice_client.is_missing_clone(exc):
        raise _as_game_error(exc) from exc
    if await request.is_disconnected():
        raise _as_game_error(exc) from exc
    await _ensure_clone(request, cfg, voice, cloned, transport, force=True)
    try:
        return await operation()
    except voice_client.VoiceError as again:
        raise _as_game_error(again) from again


# ----------------------------------------------------------------------- /tts
async def _prepare_tts(request: Request, body: TtsRequest,
                       info: auth.AuthInfo, transport: Any) -> tuple[dict, str, str, str, dict | None]:
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
    voice = cfg["narrator_voice"] if body.voice is None else body.voice
    adventure_id = (body.adventure_id or "").strip()
    narrator = cfg["narrator_voice"] or ""
    if adventure_id and narrator.startswith("cv_") \
            and voice in {"M1", "M2", "M3", "F1", "F2", "F3"}:
        voice = narrator
    with db.session() as conn:
        if not voice or not _known_voice(conn, voice):
            raise GameError.bad_request(
                "invalid_voice", f"unknown voice {body.voice!r}")
        cloned = db.get_cloned_voice(conn, voice) if voice.startswith("cv_") else None
        if cloned is not None and not _may_use_clone(conn, info.user, cloned, adventure_id):
            raise GameError(404, "voice_not_found", f"voice {voice} not found")
    await _ensure_clone(request, cfg, voice, cloned, transport)
    npc_emotion = None
    if adventure_id:
        with db.session() as conn:
            if db.get_adventure(conn, adventure_id):
                cards = db.list_cards(conn, adventure_id)
                npc_emotion = npc_bonds.npc_emotion_for_line(cards, text)
    emotion = voice_emotion.choose_emotion(
        text, npc_emotion=npc_emotion, hint=body.emotion)
    return cfg, text, voice, emotion, cloned


@router.post("/tts")
async def post_tts(request: Request, body: TtsRequest,
                   info: auth.AuthInfo = Depends(auth.require_auth),
                   transport: Any = Depends(get_http_transport)) -> Response:
    cfg, text, voice, emotion, cloned = await _prepare_tts(request, body, info, transport)

    async def speak() -> bytes:
        return await _await_until_disconnect(
            request,
            voice_client.synthesize_async(
                cfg, text, voice, emotion=emotion, transport=transport),
        )

    try:
        audio = await speak()
    except voice_client.VoiceError as exc:
        audio = await _retry_if_clone_missing(
            request, cfg, voice, cloned, transport, exc, speak)
    return Response(content=audio, media_type="audio/mpeg",
                    headers={"X-Voice-Emotion": emotion})


@router.post("/tts/stream")
async def post_tts_stream(request: Request, body: TtsRequest,
                          info: auth.AuthInfo = Depends(auth.require_auth),
                          transport: Any = Depends(get_http_transport)) -> StreamingResponse:
    cfg, text, voice, emotion, cloned = await _prepare_tts(request, body, info, transport)

    async def open_stream():
        return await _await_until_disconnect(
            request, asyncio.to_thread(
                voice_client.open_speech_stream,
                cfg, text, voice, emotion=emotion, transport=transport),
        )

    try:
        speech = await open_stream()
    except voice_client.VoiceError as exc:
        speech = await _retry_if_clone_missing(
            request, cfg, voice, cloned, transport, exc, open_stream)

    async def chunks():
        try:
            while True:
                chunk = await _await_until_disconnect(
                    request, asyncio.to_thread(voice_client.next_speech_chunk, speech))
                if chunk is None:
                    break
                yield chunk
        finally:
            await asyncio.to_thread(speech.close)

    return StreamingResponse(
        chunks(), media_type="audio/wav",
        headers={"X-Voice-Emotion": emotion, "Cache-Control": "no-store"})


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
        text = await _await_until_disconnect(
            request,
            voice_client.transcribe_async(
                cfg, file.filename or "audio",
                file.content_type or "application/octet-stream",
                data, language=lang, transport=transport),
        )
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
async def post_clone(request: Request,
                     file: UploadFile | None = File(None),
                     name: str = Form(""),
                     ref_text: str = Form(""),
                     info: auth.AuthInfo = Depends(auth.require_auth),
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
            transcript = (await _await_until_disconnect(
                request,
                voice_client.transcribe_async(
                    cfg, file.filename or "audio",
                    file.content_type or "application/octet-stream",
                    data, language="pt", transport=transport),
            )).strip()
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
        remote = await _await_until_disconnect(
            request,
            voice_client.register_clone_async(
                cfg, voice_id, clean_name, transcript,
                file.filename or "audio", data,
                content_type=file.content_type or "application/octet-stream",
                transport=transport),
        )
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
                rvq=rvq, sample_seconds=seconds, owner=info.user)
    except sqlite3.IntegrityError as exc:
        raise GameError.conflict("voice_name_taken", "Já existe uma voz com esse nome.") from exc
    return {"id": voice_id, "name": clean_name, "ref_text": transcript, "seconds": seconds}


@router.delete("/voices/{voice_id}")
def delete_voice(voice_id: str,
                 info: auth.AuthInfo = Depends(auth.require_auth),
                 transport: Any = Depends(get_http_transport)) -> dict:
    with db.session() as conn:
        row = db.get_cloned_voice(conn, voice_id)
        if row is None:
            raise GameError(404, "voice_not_found", f"voice {voice_id} not found")
        if not access.can_manage(info.user, row["owner"]):
            raise GameError(404, "voice_not_found", f"voice {voice_id} not found")
        db.delete_cloned_voice(conn, voice_id)
        db.clear_card_voice(conn, voice_id)
        cfg = settings_store.effective_voice_settings(conn)
        if cfg["narrator_voice"] == voice_id:
            settings_store.apply_voice_update(conn, {"narrator_voice": "M1"})
    voice_client.delete_clone_remote(cfg, voice_id, transport=transport)
    return {"id": voice_id}
