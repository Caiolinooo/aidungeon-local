"""Cliente de voz compartilhado, cache de clone e PCM idêntico."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json

import httpx
import pytest

from app import deps, voice_client
from app.voice_client import VoiceError

RVQ = base64.b64encode(b"rvq-bytes").decode("ascii")
MP3 = b"ID3same-pcm"
WAV = b"RIFF\x24\x00\x00\x00WAVEfmt same-pcm"
CFG = {
    "base_url": "http://voice.test",
    "timeout_seconds": 60,
    "tts_model": "omnivoice",
    "narrator_voice": "F2",
    "api_key": "",
}


def _read_wav(cfg: dict, *, transport: httpx.BaseTransport | None = None) -> tuple[bytes, httpx.Response]:
    stream = voice_client.open_speech_stream(
        cfg, "mesma fala", "F2", emotion="joy", transport=transport)
    parts: list[bytes] = []
    while True:
        chunk = voice_client.next_speech_chunk(stream)
        if chunk is None:
            break
        parts.append(chunk)
    response = stream._response
    stream.close()
    return b"".join(parts), response


def test_clone_cache_is_voice_id_plus_sha256() -> None:
    voice_client.reset_voice_clients()
    rvq = b"rvq-bytes"
    try:
        assert voice_client.clone_rvq_digest(rvq) == hashlib.sha256(rvq).hexdigest()
        assert voice_client.clone_rvq_is_cached("cv_a", rvq) is False
        voice_client.remember_clone_rvq("cv_a", rvq)
        assert voice_client.clone_rvq_is_cached("cv_a", rvq) is True
        assert voice_client.clone_rvq_is_cached("cv_b", rvq) is False
        assert voice_client.clone_rvq_is_cached("cv_a", b"other") is False
        voice_client.forget_clone_rvq("cv_a")
        assert voice_client.clone_rvq_is_cached("cv_a", rvq) is False
    finally:
        voice_client.reset_voice_clients()


def test_shared_client_stays_open_and_pcm_matches(monkeypatch: pytest.MonkeyPatch) -> None:
    voice_client.reset_voice_clients()
    calls: list[dict] = []
    shared: list[httpx.Client] = []
    owned: list[httpx.Client] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode())
        calls.append(body)
        if body.get("response_format") == "wav":
            return httpx.Response(200, content=WAV)
        return httpx.Response(200, content=MP3)

    mock = httpx.MockTransport(handler)
    real_sync = voice_client._make_client

    def make_sync(transport, timeout):
        client = real_sync(mock if transport is None else transport, timeout)
        (shared if transport is None else owned).append(client)
        return client

    monkeypatch.setattr(voice_client, "_make_client", make_sync)
    try:
        first = voice_client.synthesize(CFG, "mesma fala", "F2", emotion="joy")
        second = voice_client.synthesize(CFG, "mesma fala", "F2", emotion="joy")
        assert first == second == MP3
        assert calls[0] == calls[1]
        assert calls[0]["voice"] == "F2"
        assert calls[0]["input"] == "mesma fala"
        assert calls[0]["response_format"] == "mp3"
        assert len(shared) == 1
        assert shared[0].is_closed is False

        wav_a, response_a = _read_wav(CFG)
        wav_b, response_b = _read_wav(CFG)
        assert wav_a == wav_b == WAV
        assert response_a.is_closed is True
        assert response_b.is_closed is True
        wav_calls = [body for body in calls if body["response_format"] == "wav"]
        assert wav_calls[0] == wav_calls[1]
        assert wav_calls[0]["voice"] == "F2"
        assert wav_calls[0]["input"] == "mesma fala"
        assert len(shared) == 1
        assert shared[0].is_closed is False

        other = httpx.MockTransport(handler)
        owned_wav, _owned_response = _read_wav(CFG, transport=other)
        assert owned_wav == WAV
        assert len(owned) == 1
        assert owned[0].is_closed is True
        assert shared[0].is_closed is False
    finally:
        voice_client.reset_voice_clients()


def test_missing_clone_does_not_close_shared_client(monkeypatch: pytest.MonkeyPatch) -> None:
    voice_client.reset_voice_clients()
    shared: list[httpx.Client] = []
    seen = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["n"] += 1
        if seen["n"] == 1:
            return httpx.Response(404, text="voice not found")
        return httpx.Response(200, content=MP3)

    mock = httpx.MockTransport(handler)
    real_sync = voice_client._make_client

    def make_sync(transport, timeout):
        client = real_sync(mock if transport is None else transport, timeout)
        shared.append(client)
        return client

    monkeypatch.setattr(voice_client, "_make_client", make_sync)
    try:
        with pytest.raises(VoiceError) as exc:
            voice_client.synthesize(CFG, "ola", "F2")
        assert exc.value.code == "clone_missing"
        assert len(shared) == 1
        assert shared[0].is_closed is False
        assert voice_client.synthesize(CFG, "ola", "F2") == MP3
        assert len(shared) == 1
        assert shared[0].is_closed is False
    finally:
        voice_client.reset_voice_clients()


def test_async_speech_reuses_client(monkeypatch: pytest.MonkeyPatch) -> None:
    voice_client.reset_voice_clients()
    calls: list[dict] = []
    created: list[httpx.AsyncClient] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(json.loads(request.content.decode()))
        return httpx.Response(200, content=MP3)

    mock = httpx.MockTransport(handler)
    real_async = voice_client._make_async_client

    def make_async(transport, timeout):
        client = real_async(mock if transport is None else transport, timeout)
        if transport is None:
            created.append(client)
        return client

    monkeypatch.setattr(voice_client, "_make_async_client", make_async)

    async def scenario() -> None:
        first = await voice_client.synthesize_async(CFG, "mesma fala", "F2", emotion="joy")
        second = await voice_client.synthesize_async(CFG, "mesma fala", "F2", emotion="joy")
        assert first == second == MP3

    try:
        asyncio.run(scenario())
        assert calls[0] == calls[1]
        assert calls[0]["input"] == "mesma fala"
        assert calls[0]["voice"] == "F2"
        assert len(created) == 1
        assert created[0].is_closed is False
    finally:
        voice_client.reset_voice_clients()


def _voice_calls(capture: list[tuple]) -> tuple[list, list]:
    puts = [item for item in capture if item[0] == "put"]
    speeches = [item[1] for item in capture if item[0] == "speech"]
    return puts, speeches


def _clone_transport(capture: list[tuple], *, speech_status) -> httpx.MockTransport:
    """speech_status: int fixo, ou callable(n, body) -> Response."""

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/audio/voices") and request.method == "POST":
            return httpx.Response(200, json={
                "id": "ignored",
                "name": "n",
                "ref_text": "t",
                "seconds": 2.5,
                "rvq_b64": RVQ,
            })
        if "/audio/voices/" in path and request.method == "PUT":
            capture.append(("put", json.loads(request.content.decode())))
            return httpx.Response(200, json={"id": path.rsplit("/", 1)[-1]})
        if path.endswith("/audio/speech"):
            body = json.loads(request.content.decode())
            capture.append(("speech", body))
            if callable(speech_status):
                return speech_status(len([item for item in capture if item[0] == "speech"]), body)
            if speech_status != 200:
                return httpx.Response(speech_status, text="boom")
            content = WAV if body.get("response_format") == "wav" else MP3
            media = "audio/wav" if body.get("response_format") == "wav" else "audio/mpeg"
            return httpx.Response(200, content=content, headers={"content-type": media})
        if "/audio/voices/" in path and request.method == "DELETE":
            return httpx.Response(200, json={"id": path.rsplit("/", 1)[-1]})
        return httpx.Response(404, json={"error": f"no mock route: {path}"})

    return httpx.MockTransport(handler)


def _use(client, transport: httpx.MockTransport) -> None:
    client.app.dependency_overrides[deps.get_http_transport] = lambda: transport


def _clone(client, name: str = "Clara"):
    return client.post(
        "/api/voices/clone",
        data={"name": name, "ref_text": "Olá, esta é a minha voz."},
        files={"file": ("voz.wav", b"RIFFfake-wav", "audio/wav")},
    )


def test_second_tts_skips_clone_and_keeps_payload(client) -> None:
    voice_client.reset_voice_clients()
    capture: list[tuple] = []
    _use(client, _clone_transport(capture, speech_status=200))
    try:
        created = _clone(client, name="Reuso")
        assert created.status_code == 200, created.text
        voice_id = created.json()["id"]
        first = client.post("/api/tts", json={"text": "O vento sopra.", "voice": voice_id})
        second = client.post("/api/tts", json={"text": "O vento sopra.", "voice": voice_id})
        assert voice_client.clone_rvq_is_cached(voice_id, base64.b64decode(RVQ)) is True
        removed = client.delete(f"/api/voices/{voice_id}")
        assert removed.status_code == 200, removed.text
        assert voice_client.clone_rvq_is_cached(voice_id, base64.b64decode(RVQ)) is False
    finally:
        client.app.dependency_overrides.clear()
        voice_client.reset_voice_clients()
    assert first.status_code == 200, first.text
    assert second.status_code == 200, second.text
    assert first.content == second.content == MP3
    puts, speeches = _voice_calls(capture)
    assert len(puts) == 1
    assert puts[0][1]["rvq_b64"] == RVQ
    assert len(speeches) == 2
    assert speeches[0] == speeches[1]
    assert speeches[0]["voice"] == voice_id
    assert speeches[0]["input"] == "O vento sopra."


def test_missing_clone_is_resent_once(client) -> None:
    voice_client.reset_voice_clients()
    capture: list[tuple] = []

    def speech_status(n: int, body: dict) -> httpx.Response:
        if n == 1:
            return httpx.Response(404, text="voice not found")
        assert body["voice"].startswith("cv_")
        assert body["input"] == "O vento sopra."
        return httpx.Response(200, content=MP3, headers={"content-type": "audio/mpeg"})

    _use(client, _clone_transport(capture, speech_status=speech_status))
    try:
        created = _clone(client, name="Sumiu")
        assert created.status_code == 200, created.text
        voice_id = created.json()["id"]
        spoken = client.post("/api/tts", json={"text": "O vento sopra.", "voice": voice_id})
    finally:
        client.app.dependency_overrides.clear()
        voice_client.reset_voice_clients()
    assert spoken.status_code == 200, spoken.text
    assert spoken.content == MP3
    puts, speeches = _voice_calls(capture)
    assert len(puts) == 2
    assert len(speeches) == 2
    assert speeches[0] == speeches[1]
    assert speeches[0]["voice"] == voice_id


def test_clone_rejection_retries_stream_once(client) -> None:
    voice_client.reset_voice_clients()
    capture: list[tuple] = []

    def speech_status(n: int, body: dict) -> httpx.Response:
        if n == 1:
            return httpx.Response(
                400,
                json={"error": {"code": "voice_not_found", "message": "voice not found"}},
            )
        return httpx.Response(200, content=WAV, headers={"content-type": "audio/wav"})

    _use(client, _clone_transport(capture, speech_status=speech_status))
    try:
        created = _clone(client, name="Stream")
        assert created.status_code == 200, created.text
        voice_id = created.json()["id"]
        spoken = client.post("/api/tts/stream", json={"text": "O vento sopra.", "voice": voice_id})
    finally:
        client.app.dependency_overrides.clear()
        voice_client.reset_voice_clients()
    assert spoken.status_code == 200, spoken.text
    assert spoken.content == WAV
    puts, speeches = _voice_calls(capture)
    assert len(puts) == 2
    assert len(speeches) == 2
    assert speeches[0] == speeches[1]
    assert speeches[0]["response_format"] == "wav"
    assert speeches[0]["voice"] == voice_id


def test_speech_error_does_not_resend_or_retry(client) -> None:
    voice_client.reset_voice_clients()
    capture: list[tuple] = []
    _use(client, _clone_transport(capture, speech_status=500))
    try:
        created = _clone(client, name="Quebra")
        assert created.status_code == 200, created.text
        voice_id = created.json()["id"]
        spoken = client.post("/api/tts", json={"text": "O vento sopra.", "voice": voice_id})
        again = client.post("/api/tts/stream", json={"text": "O vento sopra.", "voice": voice_id})
    finally:
        client.app.dependency_overrides.clear()
        voice_client.reset_voice_clients()
    assert spoken.status_code == 502, spoken.text
    assert spoken.json()["error"]["code"] == "error"
    assert again.status_code == 502, again.text
    puts, speeches = _voice_calls(capture)
    # O 500 não é clone ausente: um PUT no primeiro TTS e o cache segura o segundo.
    assert len(puts) == 1
    assert len(speeches) == 2


def test_persistent_missing_clone_retries_only_once(client) -> None:
    voice_client.reset_voice_clients()
    capture: list[tuple] = []
    _use(client, _clone_transport(capture, speech_status=404))
    try:
        created = _clone(client, name="Ausente")
        assert created.status_code == 200, created.text
        voice_id = created.json()["id"]
        spoken = client.post("/api/tts", json={"text": "O vento sopra.", "voice": voice_id})
    finally:
        client.app.dependency_overrides.clear()
        voice_client.reset_voice_clients()
    assert spoken.status_code == 502, spoken.text
    assert spoken.json()["error"]["code"] == "error"
    puts, speeches = _voice_calls(capture)
    assert len(puts) == 2
    assert len(speeches) == 2
