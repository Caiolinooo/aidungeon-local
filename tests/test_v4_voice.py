"""Testes v0.4.0 — voz: TTS (Supertonic), STT (Whisper) e settings de voz."""

from __future__ import annotations

import json

import httpx

from app import db, deps

MP3_BYTES = b"ID3\x04\x00\x00\x00\x00\x00\x21fake-mp3-audio-bytes"


def make_voice_transport(capture: list | None = None, fail: str | None = None,
                         stt_text: str = "hello brave adventurer") -> httpx.MockTransport:
    """MockTransport da stack de voz da VM: /audio/speech + /audio/transcriptions.

    fail: 'connect' (queima conexão), 'auth' (401), 'http500'.
    capture: lista que recebe o payload JSON do TTS e o corpo multipart do STT.
    """
    def handler(request: httpx.Request) -> httpx.Response:
        if fail == "connect":
            raise httpx.ConnectError("mock: connection refused")
        path = request.url.path
        if path.endswith("/audio/speech"):
            if fail == "auth":
                return httpx.Response(401, json={"error": "unauthorized"})
            if fail == "http500":
                return httpx.Response(500, text="boom")
            if capture is not None:
                capture.append(json.loads(request.content.decode("utf-8")))
            return httpx.Response(200, content=MP3_BYTES,
                                  headers={"content-type": "audio/mpeg"})
        if path.endswith("/audio/transcriptions"):
            if fail == "auth":
                return httpx.Response(401, json={"error": "unauthorized"})
            if capture is not None:
                capture.append(request.content)
            return httpx.Response(200, json={"text": stt_text})
        return httpx.Response(404, json={"error": f"no mock route: {path}"})

    return httpx.MockTransport(handler)


def override_transport(client, transport) -> None:
    client.app.dependency_overrides[deps.get_http_transport] = lambda: transport


def set_stt_max_bytes(value: int) -> None:
    with db.session() as conn:
        db.set_setting(conn, "voice.stt_max_bytes", json.dumps(value))


# --------------------------------------------------------------------- voices
def test_list_voices(client):
    resp = client.get("/api/voices")
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["voices"] == ["M1", "M2", "M3", "F1", "F2", "F3"]
    assert data["narrator_voice"] == "M1"


# ------------------------------------------------------------------------ tts
def test_tts_ok_default_narrator_voice(client):
    capture: list = []
    override_transport(client, make_voice_transport(capture=capture))
    try:
        resp = client.post("/api/tts", json={"text": "The tavern door creaks open."})
    finally:
        client.app.dependency_overrides.clear()
    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"].startswith("audio/mpeg")
    assert resp.content == MP3_BYTES
    # voice omitida → narrator_voice (M1); payload no formato OpenAI TTS
    assert capture[0]["voice"] == "M1"
    assert capture[0]["model"] == "omnivoice"
    assert capture[0]["response_format"] == "mp3"
    assert capture[0]["input"] == "The tavern door creaks open."


def test_tts_explicit_voice(client):
    capture: list = []
    override_transport(client, make_voice_transport(capture=capture))
    try:
        resp = client.post("/api/tts", json={"text": "I am Ada.", "voice": "F2"})
    finally:
        client.app.dependency_overrides.clear()
    assert resp.status_code == 200, resp.text
    assert capture[0]["voice"] == "F2"


def test_tts_invalid_voice_400(client):
    override_transport(client, make_voice_transport())
    try:
        resp = client.post("/api/tts", json={"text": "hi", "voice": "X9"})
        assert resp.status_code == 400
        assert resp.json()["error"]["code"] == "invalid_voice"
        # string vazia também é inválida (não cai no narrador)
        resp = client.post("/api/tts", json={"text": "hi", "voice": ""})
        assert resp.status_code == 400
        assert resp.json()["error"]["code"] == "invalid_voice"
    finally:
        client.app.dependency_overrides.clear()


def test_tts_empty_and_too_long_text_400(client):
    override_transport(client, make_voice_transport())
    try:
        resp = client.post("/api/tts", json={"text": "   "})
        assert resp.status_code == 400
        assert resp.json()["error"]["code"] == "invalid_payload"

        resp = client.post("/api/tts", json={"text": "x" * 5501})
        assert resp.status_code == 400
        assert resp.json()["error"]["code"] == "text_too_long"
    finally:
        client.app.dependency_overrides.clear()


def test_tts_unreachable_502(client):
    override_transport(client, make_voice_transport(fail="connect"))
    try:
        resp = client.post("/api/tts", json={"text": "hello"})
    finally:
        client.app.dependency_overrides.clear()
    assert resp.status_code == 502
    assert resp.json()["error"]["code"] == "unreachable"


def test_tts_voice_disabled_400(client):
    assert client.put("/api/settings/voice", json={"enabled": False}).status_code == 200
    override_transport(client, make_voice_transport())
    try:
        resp = client.post("/api/tts", json={"text": "hello"})
    finally:
        client.app.dependency_overrides.clear()
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "voice_disabled"


# ------------------------------------------------------------------------ stt
def test_stt_ok_multipart(client):
    capture: list = []
    override_transport(client, make_voice_transport(capture=capture))
    try:
        resp = client.post("/api/stt", files={
            "file": ("clip.webm", b"fake-webm-audio", "audio/webm"),
        })
    finally:
        client.app.dependency_overrides.clear()
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"text": "hello brave adventurer"}
    # multipart repassado com filename/content_type originais + model=stt_model
    body = capture[0]
    assert b'filename="clip.webm"' in body
    assert b"audio/webm" in body
    assert b"whisper-1" in body


def test_stt_forwards_language(client):
    capture: list = []
    override_transport(client, make_voice_transport(capture=capture))
    try:
        resp = client.post(
            "/api/stt",
            files={"file": ("clip.webm", b"fake-webm-audio", "audio/webm")},
            data={"language": "en-US"},
        )
    finally:
        client.app.dependency_overrides.clear()
    assert resp.status_code == 200, resp.text
    body = capture[0]
    assert b'name="language"' in body
    assert b"\r\n\r\nen\r\n" in body or b"\nen\n" in body


def test_stt_missing_file_400(client):
    override_transport(client, make_voice_transport())
    try:
        resp = client.post("/api/stt")
    finally:
        client.app.dependency_overrides.clear()
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "invalid_payload"


def test_stt_too_large_413(client):
    set_stt_max_bytes(16)
    override_transport(client, make_voice_transport())
    try:
        resp = client.post("/api/stt", files={
            "file": ("clip.mp3", b"x" * 100, "audio/mpeg"),
        })
    finally:
        client.app.dependency_overrides.clear()
    assert resp.status_code == 413
    assert resp.json()["error"]["code"] == "audio_too_large"


# ------------------------------------------------------------------- settings
def test_voice_settings_get_masks_api_key(client, monkeypatch):
    monkeypatch.setenv("VOICE_API_KEY", "voice-secret-1234")
    resp = client.get("/api/settings/voice")
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["api_key"] == "voi…1234"
    assert data["api_key_set"] is True
    assert data["base_url"] == "https://vm.groupabz.com/voice/v1"
    assert data["tts_model"] == "omnivoice"
    assert data["stt_model"] == "whisper-1"
    assert data["voices"] == ["M1", "M2", "M3", "F1", "F2", "F3"]

    monkeypatch.delenv("VOICE_API_KEY", raising=False)
    data = client.get("/api/settings/voice").json()
    assert data["api_key"] is None
    assert data["api_key_set"] is False


def test_voice_settings_put_persists_override(client):
    resp = client.put("/api/settings/voice",
                      json={"narrator_voice": "F2", "timeout_seconds": 90})
    assert resp.status_code == 200, resp.text
    assert resp.json()["narrator_voice"] == "F2"
    assert resp.json()["timeout_seconds"] == 90
    # GET /voices reflete o override persistido no banco
    assert client.get("/api/voices").json()["narrator_voice"] == "F2"

    # api_key: setar mascara no GET; "" limpa o override (cai p/ env)
    resp = client.put("/api/settings/voice", json={"api_key": "abc12345"})
    assert resp.json()["api_key"] == "abc…2345"
    assert resp.json()["api_key_set"] is True
    resp = client.put("/api/settings/voice", json={"api_key": ""})
    assert resp.json()["api_key_set"] is False


def test_voice_settings_put_invalid_narrator_400(client):
    resp = client.put("/api/settings/voice", json={"narrator_voice": "X9"})
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "invalid_voice"
    # nada foi persistido
    assert client.get("/api/settings/voice").json()["narrator_voice"] == "M1"
