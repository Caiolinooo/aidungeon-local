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

import asyncio
import base64
import hashlib
import logging
import threading
from typing import Any

import httpx

from app.voice_emotion import direction_for

log = logging.getLogger("aidungeon.voice")


class VoiceError(Exception):
    code = "error"

    def __init__(self, message: str, code: str | None = None):
        super().__init__(message)
        self.message = message
        if code:
            self.code = code


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

def _make_async_client(transport: httpx.AsyncBaseTransport | None,
                       timeout: float) -> httpx.AsyncClient:
    t = httpx.Timeout(timeout, connect=min(10.0, timeout))
    kwargs: dict[str, Any] = {"timeout": t, "trust_env": False}
    if transport is not None:
        kwargs["transport"] = transport
    return httpx.AsyncClient(**kwargs)


# Um cliente por base_url/timeout. Transporte injetado (teste) continua
# privado e é fechado por quem o pediu. O compartilhado não fecha com a resposta.
_shared_lock = threading.Lock()
_shared_sync: dict[tuple[str, float], httpx.Client] = {}
_shared_async: dict[tuple[str, float, int], httpx.AsyncClient] = {}
_clone_sent: dict[str, str] = {}


def _borrow_sync(base_url: str, timeout: float,
                 transport: httpx.BaseTransport | None) -> tuple[httpx.Client, bool]:
    """Devolve (client, owns). owns=False é o pool thread-safe."""
    if transport is not None:
        return _make_client(transport, timeout), True
    key = (base_url, float(timeout))
    with _shared_lock:
        client = _shared_sync.get(key)
        if client is None or client.is_closed:
            client = _make_client(None, timeout)
            _shared_sync[key] = client
        return client, False


def _borrow_async(base_url: str, timeout: float,
                  transport: httpx.AsyncBaseTransport | None) -> tuple[httpx.AsyncClient, bool]:
    """Pool de AsyncClient no event loop corrente. Transporte injetado não entra."""
    if transport is not None:
        return _make_async_client(transport, timeout), True
    loop = asyncio.get_running_loop()
    key = (base_url, float(timeout), id(loop))
    with _shared_lock:
        client = _shared_async.get(key)
        if client is None or client.is_closed:
            client = _make_async_client(None, timeout)
            _shared_async[key] = client
        return client, False


def reset_voice_clients() -> None:
    """Fecha clientes sync do pool e esquece clones enviados. Só para testes."""
    with _shared_lock:
        sync_clients = list(_shared_sync.values())
        async_clients = list(_shared_async.values())
        _shared_sync.clear()
        _shared_async.clear()
        _clone_sent.clear()
    for client in sync_clients:
        try:
            client.close()
        except Exception:
            pass
    for client in async_clients:
        closer = getattr(client, "aclose", None)
        if closer is None:
            continue
        try:
            coro = closer()
            coro.close()
        except Exception:
            pass


def clone_rvq_digest(rvq: bytes) -> str:
    return hashlib.sha256(bytes(rvq)).hexdigest()


def clone_rvq_is_cached(voice_id: str, rvq: bytes) -> bool:
    digest = clone_rvq_digest(rvq)
    with _shared_lock:
        return _clone_sent.get(voice_id) == digest


def remember_clone_rvq(voice_id: str, rvq: bytes) -> None:
    digest = clone_rvq_digest(rvq)
    with _shared_lock:
        _clone_sent[voice_id] = digest


def forget_clone_rvq(voice_id: str) -> None:
    with _shared_lock:
        _clone_sent.pop(voice_id, None)


def is_missing_clone(exc: BaseException) -> bool:
    return (
        isinstance(exc, VoiceError)
        and not isinstance(exc, (VoiceAuthError, VoiceUnreachable))
        and exc.code == "clone_missing"
    )


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


class SpeechStream:
    """Resposta WAV aberta. Fecha a response; o client compartilhado fica vivo."""

    def __init__(self, client: httpx.Client, response: httpx.Response, *,
                 owns_client: bool):
        self._client = client
        self._response = response
        self._owns_client = owns_client
        self._iterator = response.iter_bytes()
        self._closed = False

    def __enter__(self) -> SpeechStream:
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def iter_bytes(self):
        return self._iterator

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self._response.close()
        finally:
            if self._owns_client:
                self._client.close()


_MISSING_CLONE_MARKERS = (
    "voice_not_found",
    "unknown_voice",
    "clone_missing",
    "voice not found",
    "unknown voice",
    "no such voice",
    "missing voice",
    "voice missing",
    "clone ausente",
    "voz não encontrada",
    "voz nao encontrada",
)


def _missing_clone_response(resp: httpx.Response) -> bool:
    """404 no speech, ou rejeição explícita de clone/voz ausente."""
    if resp.status_code == 404:
        return True
    if resp.status_code not in (400, 409, 422):
        return False
    blob = (resp.text or "")[:500].lower()
    return any(marker in blob for marker in _MISSING_CLONE_MARKERS)


def _speech_status_error(resp: httpx.Response) -> VoiceError:
    if resp.status_code in (401, 403):
        return VoiceAuthError(
            f"auth_error: HTTP {resp.status_code} em audio/speech")
    message = f"HTTP {resp.status_code} em audio/speech: {resp.text[:200]}"
    if _missing_clone_response(resp):
        return VoiceError(message, code="clone_missing")
    return VoiceError(message)


def _speech_payload(cfg: dict, text: str, voice: str, emotion: str | None,
                    response_format: str) -> dict:
    voice = voice or cfg["narrator_voice"]
    instruction = direction_for(emotion) if emotion else None
    payload = {
        "model": cfg["tts_model"],
        "voice": voice,
        "input": text,
        "response_format": response_format,
    }
    if instruction:
        payload["instructions"] = instruction
    return payload


def open_speech_stream(cfg: dict, text: str, voice: str, *,
                       emotion: str | None = None,
                       transport: httpx.BaseTransport | None = None) -> SpeechStream:
    """Abre OmniVoice WAV sem buffering nem fallback para Drama-3."""
    payload = _speech_payload(cfg, text, voice, emotion, "wav")
    timeout = float(cfg.get("timeout_seconds", 60))
    client, owns = _borrow_sync(_base(cfg), timeout, transport)
    response: httpx.Response | None = None
    handed = False
    try:
        response = client.send(client.build_request(
            "POST", f"{_base(cfg)}/audio/speech", json=payload,
            headers=_headers(cfg)), stream=True)
        if response.status_code in (401, 403) or response.status_code != 200:
            response.read()
            raise _speech_status_error(response)
        stream = SpeechStream(client, response, owns_client=owns)
        handed = True
        return stream
    except httpx.TransportError as exc:
        raise VoiceUnreachable(
            f"voice provider unreachable: {type(exc).__name__}") from exc
    finally:
        if not handed:
            if response is not None:
                response.close()
            if owns:
                client.close()


def next_speech_chunk(stream: SpeechStream) -> bytes | None:
    """Lê um chunk em thread; None representa EOF sem vazar StopIteration."""
    try:
        return next(stream.iter_bytes())
    except StopIteration:
        return None

# ------------------------------------------------------------------ público
def _finish_speech(resp: httpx.Response) -> bytes:
    if resp.status_code in (401, 403) or resp.status_code != 200:
        raise _speech_status_error(resp)
    if not resp.content:
        raise VoiceError("resposta de audio/speech vazia")
    return resp.content


def _post_speech(cfg: dict, payload: dict, *,
                 transport: httpx.BaseTransport | None) -> bytes:
    timeout = float(cfg.get("timeout_seconds", 60))
    client, owns = _borrow_sync(_base(cfg), timeout, transport)
    try:
        try:
            resp = client.post(f"{_base(cfg)}/audio/speech", json=payload,
                               headers=_headers(cfg))
        except httpx.TransportError as exc:
            raise VoiceUnreachable(f"voice provider unreachable: {type(exc).__name__}") from exc
        return _finish_speech(resp)
    finally:
        if owns:
            client.close()


async def _post_speech_async(cfg: dict, payload: dict, *,
                             transport: httpx.AsyncBaseTransport | None) -> bytes:
    timeout = float(cfg.get("timeout_seconds", 60))
    client, owns = _borrow_async(_base(cfg), timeout, transport)
    try:
        try:
            resp = await client.post(f"{_base(cfg)}/audio/speech", json=payload,
                                     headers=_headers(cfg))
        except httpx.TransportError as exc:
            raise VoiceUnreachable(f"voice provider unreachable: {type(exc).__name__}") from exc
        return _finish_speech(resp)
    finally:
        if owns:
            await client.aclose()


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


async def synthesize_async(cfg: dict, text: str, voice: str, *,
                           emotion: str | None = None,
                           transport: httpx.AsyncBaseTransport | None = None) -> bytes:
    """Versão assíncrona de synthesize; cancelar a coroutine fecha o POST HTTP."""
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
        return await _post_speech_async(cfg, payload, transport=transport)
    except (VoiceAuthError, VoiceUnreachable):
        raise
    except VoiceError as exc:
        if not use_drama:
            raise
        log.warning("drama-3 indisponível (%s); fallback %s", exc.code, cfg.get("tts_model"))
        payload["model"] = cfg["tts_model"]
        payload["input"] = text
        return await _post_speech_async(cfg, payload, transport=transport)


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


async def transcribe_async(cfg: dict, filename: str, content_type: str, data: bytes, *,
                           language: str | None = None,
                           transport: httpx.AsyncBaseTransport | None = None) -> str:
    """Versão assíncrona de transcribe; cancelar a coroutine fecha o POST HTTP."""
    timeout = float(cfg.get("timeout_seconds", 60))
    files = {"file": (filename, data, content_type)}
    form = {"model": cfg["stt_model"]}
    if language:
        form["language"] = language
    async with _make_async_client(transport, timeout) as client:
        try:
            resp = await client.post(f"{_base(cfg)}/audio/transcriptions", files=files,
                                     data=form, headers=_headers(cfg))
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


PROBE_TIMEOUT = 5.0


def probe_models(cfg: dict, *,
                 transport: httpx.BaseTransport | None = None) -> list[dict]:
    """Sonda não gerativa GET /models; endpoint ausente ou inválido é erro."""
    timeout = min(float(cfg.get("timeout_seconds", 60)), PROBE_TIMEOUT)
    try:
        with _make_client(transport, timeout) as client:
            resp = client.get(f"{_base(cfg)}/models", headers=_headers(cfg))
    except httpx.TransportError as exc:
        raise VoiceUnreachable(f"voice provider unreachable: {type(exc).__name__}") from exc
    if resp.status_code in (401, 403):
        raise VoiceAuthError(f"auth_error: HTTP {resp.status_code} em /models")
    if resp.status_code != 200:
        raise VoiceError(f"HTTP {resp.status_code} em /models")
    try:
        body = resp.json()
    except ValueError as exc:
        raise VoiceError("resposta de /models não é JSON") from exc
    models = body.get("data") if isinstance(body, dict) else None
    if not isinstance(models, list):
        raise VoiceError("resposta de /models sem lista 'data'")
    return models


def health(cfg: dict, *, transport: httpx.BaseTransport | None = None) -> dict:
    """Resultado sanitizado para health; nunca ecoa corpo do provider ou segredos."""
    try:
        probe_models(cfg, transport=transport)
    except VoiceAuthError as exc:
        return {"status": "auth_error", "details": exc.message}
    except VoiceUnreachable as exc:
        return {"status": "unreachable", "details": exc.message}
    except VoiceError as exc:
        return {"status": "error", "details": exc.message}
    except Exception as exc:
        log.warning("sonda de voz falhou: %s", type(exc).__name__)
        return {"status": "error", "details": f"voice health probe failed: {type(exc).__name__}"}
    return {"status": "ok", "details": None}

_CLONE_PROVIDER_CODES = {"audio_too_short", "audio_too_long", "invalid_audio"}


def _raise_provider(resp: httpx.Response, what: str) -> None:
    """401/403 vira auth. 400 com código conhecido preserva o código. O resto é erro."""
    if resp.status_code in (401, 403):
        raise VoiceAuthError(f"auth_error: HTTP {resp.status_code} em {what}")
    if resp.status_code == 200:
        return
    code = "error"
    message = f"HTTP {resp.status_code} em {what}: {resp.text[:200]}"
    try:
        body = resp.json()
    except ValueError:
        body = None
    err = body.get("error") if isinstance(body, dict) else None
    if isinstance(err, dict) and err.get("code") in _CLONE_PROVIDER_CODES:
        code = str(err["code"])
        message = str(err.get("message") or message)
    raise VoiceError(message, code=code)


def register_clone(cfg: dict, voice_id: str, name: str, ref_text: str,
                   filename: str, data: bytes, *,
                   content_type: str = "application/octet-stream",
                   transport: httpx.BaseTransport | None = None) -> dict:
    """POST {base}/audio/voices. Devolve o JSON do servidor, inclusive rvq_b64."""
    timeout = float(cfg.get("timeout_seconds", 60))
    files = {"file": (filename or "audio", data, content_type)}
    form = {"voice_id": voice_id, "name": name, "ref_text": ref_text}
    with _make_client(transport, timeout) as client:
        try:
            resp = client.post(f"{_base(cfg)}/audio/voices", files=files, data=form,
                               headers=_headers(cfg))
        except httpx.TransportError as exc:
            raise VoiceUnreachable(f"voice provider unreachable: {type(exc).__name__}") from exc
    _raise_provider(resp, "audio/voices")
    try:
        body = resp.json()
    except ValueError as exc:
        raise VoiceError("resposta de audio/voices não é JSON") from exc
    if not isinstance(body, dict) or not isinstance(body.get("rvq_b64"), str) or not body["rvq_b64"]:
        raise VoiceError("resposta de audio/voices sem rvq_b64")
    return body


async def register_clone_async(cfg: dict, voice_id: str, name: str, ref_text: str,
                                filename: str, data: bytes, *,
                                content_type: str = "application/octet-stream",
                                transport: httpx.AsyncBaseTransport | None = None) -> dict:
    """Versão assíncrona de register_clone; cancelar fecha o POST HTTP."""
    timeout = float(cfg.get("timeout_seconds", 60))
    files = {"file": (filename or "audio", data, content_type)}
    form = {"voice_id": voice_id, "name": name, "ref_text": ref_text}
    async with _make_async_client(transport, timeout) as client:
        try:
            resp = await client.post(f"{_base(cfg)}/audio/voices", files=files, data=form,
                                     headers=_headers(cfg))
        except httpx.TransportError as exc:
            raise VoiceUnreachable(f"voice provider unreachable: {type(exc).__name__}") from exc
    _raise_provider(resp, "audio/voices")
    try:
        body = resp.json()
    except ValueError as exc:
        raise VoiceError("resposta de audio/voices não é JSON") from exc
    if not isinstance(body, dict) or not isinstance(body.get("rvq_b64"), str) or not body["rvq_b64"]:
        raise VoiceError("resposta de audio/voices sem rvq_b64")
    return body


def put_clone_rvq(cfg: dict, voice_id: str, ref_text: str, rvq: bytes, *,
                  transport: httpx.BaseTransport | None = None) -> None:
    """Regrava o .rvq na VM antes da fala. O arquivo cabe em poucos KB."""
    timeout = float(cfg.get("timeout_seconds", 60))
    payload = {"ref_text": ref_text, "rvq_b64": base64.b64encode(rvq).decode("ascii")}
    with _make_client(transport, timeout) as client:
        try:
            resp = client.put(f"{_base(cfg)}/audio/voices/{voice_id}", json=payload,
                              headers=_headers(cfg))
        except httpx.TransportError as exc:
            raise VoiceUnreachable(f"voice provider unreachable: {type(exc).__name__}") from exc
    _raise_provider(resp, f"audio/voices/{voice_id}")
    remember_clone_rvq(voice_id, rvq)


async def put_clone_rvq_async(cfg: dict, voice_id: str, ref_text: str, rvq: bytes, *,
                              transport: httpx.AsyncBaseTransport | None = None) -> None:
    """Regrava o clone antes do TTS sem bloquear o cancelamento da requisição."""
    timeout = float(cfg.get("timeout_seconds", 60))
    payload = {"ref_text": ref_text, "rvq_b64": base64.b64encode(rvq).decode("ascii")}
    async with _make_async_client(transport, timeout) as client:
        try:
            resp = await client.put(f"{_base(cfg)}/audio/voices/{voice_id}", json=payload,
                                    headers=_headers(cfg))
        except httpx.TransportError as exc:
            raise VoiceUnreachable(f"voice provider unreachable: {type(exc).__name__}") from exc
    _raise_provider(resp, f"audio/voices/{voice_id}")
    remember_clone_rvq(voice_id, rvq)


def delete_clone_remote(cfg: dict, voice_id: str, *,
                        transport: httpx.BaseTransport | None = None) -> None:
    """Apaga o arquivo na VM. Falha só é logada: a linha local já saiu."""
    forget_clone_rvq(voice_id)
    timeout = float(cfg.get("timeout_seconds", 60))
    try:
        with _make_client(transport, timeout) as client:
            resp = client.delete(f"{_base(cfg)}/audio/voices/{voice_id}",
                                 headers=_headers(cfg))
    except httpx.TransportError as exc:
        log.warning("não apaguei a voz %s no servidor: %s", voice_id, type(exc).__name__)
        return
    if resp.status_code not in (200, 204, 404):
        log.warning("não apaguei a voz %s no servidor: HTTP %s", voice_id, resp.status_code)

