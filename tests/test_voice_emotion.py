"""Emoção da fala: a linha escolhe a tag; o Supertonic continua falando."""

from __future__ import annotations

import json

import httpx

from app import db, settings_store, voice_emotion

from conftest import make_adventure
from test_v4_voice import MP3_BYTES, override_transport


def test_emotion_comes_from_the_line_before_the_npc():
    assert voice_emotion.emotion_from_text("She shouted in anger") == "anger"
    assert voice_emotion.emotion_from_text("Ele sorriu de alegria") == "joy"
    assert voice_emotion.emotion_from_text("The door is wooden") is None
    assert voice_emotion.choose_emotion(
        "She looks at you.", npc_emotion="fear") == "fear"
    assert voice_emotion.choose_emotion(
        "She laughs with joy.", npc_emotion="fear") == "joy"
    assert voice_emotion.choose_emotion("Hello.", hint="happy") == "joy"
    assert voice_emotion.direction_for("fear").startswith("[frightened")


def _transport(capture: list, *, reject_drama: bool = False,
               fail_auth_on_drama: bool = False) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        capture.append(body)
        if body.get("model") == "drama-3":
            if fail_auth_on_drama:
                return httpx.Response(401, json={"error": "unauthorized"})
            if reject_drama:
                return httpx.Response(404, text="model not found")
        return httpx.Response(200, content=MP3_BYTES, headers={"content-type": "audio/mpeg"})

    return httpx.MockTransport(handler)


def test_tts_sends_emotion_but_supertonic_speaks_plain_text(client):
    capture: list = []
    override_transport(client, _transport(capture))
    try:
        resp = client.post("/api/tts", json={
            "text": "She shouted in anger at the gate.",
            "emotion": "joy",
        })
    finally:
        client.app.dependency_overrides.clear()
    assert resp.status_code == 200, resp.text
    assert resp.headers["x-voice-emotion"] == "anger"
    assert capture[0]["model"] == "supertonic-3"
    assert capture[0]["input"] == "She shouted in anger at the gate."
    assert capture[0]["instructions"].startswith("[angry")
    assert "drama-3" not in capture[0]["input"]


def test_tts_uses_npc_bonds_when_the_line_is_flat(client):
    adv = make_adventure(title="Voz")
    with db.session() as conn:
        db.insert_card(
            conn, adv["id"], title="Mira", type="character", triggers=["Mira"],
            entry="Guarda.",
            sheet=json.dumps({"bonds": {
                "relationship": 10, "trust": 10, "fear": 80,
                "desire": 10, "boldness": 20,
            }}),
        )
    capture: list = []
    override_transport(client, _transport(capture))
    try:
        resp = client.post("/api/tts", json={
            "text": "Mira looks at you.",
            "adventure_id": adv["id"],
        })
    finally:
        client.app.dependency_overrides.clear()
    assert resp.status_code == 200, resp.text
    assert resp.headers["x-voice-emotion"] == "fear"
    assert capture[0]["input"] == "Mira looks at you."


def test_drama3_falls_back_to_supertonic_when_the_model_is_missing(client):
    with db.session() as conn:
        settings_store._set_override(conn, "voice.drama3_enabled", True)
    capture: list = []
    override_transport(client, _transport(capture, reject_drama=True))
    try:
        resp = client.post("/api/tts", json={"text": "She is afraid."})
    finally:
        client.app.dependency_overrides.clear()
    assert resp.status_code == 200, resp.text
    assert resp.headers["x-voice-emotion"] == "fear"
    assert [item["model"] for item in capture] == ["drama-3", "supertonic-3"]
    assert capture[0]["input"].startswith("[frightened")
    assert capture[1]["input"] == "She is afraid."
    assert "instructions" in capture[1]


def test_drama3_auth_error_does_not_pretend_supertonic_worked(client):
    with db.session() as conn:
        settings_store._set_override(conn, "voice.drama3_enabled", True)
    capture: list = []
    override_transport(client, _transport(capture, fail_auth_on_drama=True))
    try:
        resp = client.post("/api/tts", json={"text": "Hello there."})
    finally:
        client.app.dependency_overrides.clear()
    assert resp.status_code == 502
    assert resp.json()["error"]["code"] == "auth_error"
    assert len(capture) == 1
