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


def test_mixed_negated_and_person_specific_emotions():
    assert voice_emotion.emotion_from_text(
        "Elara está furiosa, mas sorri para não preocupar Caio.") == "anger"
    assert voice_emotion.emotion_from_text(
        "Elara está aliviada, quase chorando de felicidade.") == "joy"
    assert voice_emotion.emotion_from_text("Elara recusa com calma, sem raiva.") is None
    assert voice_emotion.emotion_from_text("Ela não sente nada.") is None
    assert voice_emotion.emotion_from_text("Elara sorri, ainda de luto.") == "sadness"
    assert voice_emotion.emotion_from_text(
        "Lexi gosta dele, mas finge indiferença.") == "tenderness"
    text = "Elara está feliz, mas Lexi tem medo."
    emotions = voice_emotion.emotions_for_subjects(
        text, {"elara": ["Elara"], "lexi": ["Lexi"]})
    assert emotions["elara"] == "joy"
    assert emotions["lexi"] == "fear"
    assert voice_emotion.emotions_for_subjects(
        "You ask Lexi whether she trusts you. She likes you but pretends indifference.",
        {"lexi": ["Lexi"]},
    )["lexi"] == "tenderness"


def test_negation_is_local_and_new_vocabulary_is_not_ambiguous():
    assert voice_emotion.emotion_from_text("She feels no fear, only terror.") == "fear"
    assert voice_emotion.emotion_from_text("no fear only terror") == "fear"
    assert voice_emotion.emotion_from_text("She is not relieved.") is None
    assert voice_emotion.emotion_from_text("She is not sorry.") is None
    assert voice_emotion.emotion_from_text("without anger") is None
    assert voice_emotion.emotion_from_text("Elara não está ansiosa.") is None
    assert voice_emotion.emotion_from_text("She is nervous.") == "fear"
    assert voice_emotion.emotion_from_text("She is not nervous.") is None
    assert voice_emotion.emotion_from_text("She is grieving.") == "sadness"
    assert voice_emotion.emotion_from_text("She shows remorse.") == "sadness"
    assert voice_emotion.emotion_from_text("She shows affection.") == "tenderness"
    assert voice_emotion.emotion_from_text("Elara is like a sister to you.") is None
    assert voice_emotion.emotion_from_text("She doesn't like you.") is None
    assert voice_emotion.emotion_from_text("Elara desculpa Caio e sorri.") == "joy"


def test_subject_resolution_handles_coordination_contrast_aliases_and_pronouns():
    subjects = {"elara": ["Elara"], "lexi": ["Lexi"], "caio": ["Caio"]}
    resolved = voice_emotion.emotions_for_subjects(
        "Elara is happy and Lexi is afraid.", subjects)
    assert resolved == {"elara": "joy", "lexi": "fear"}
    assert voice_emotion.emotions_for_subjects(
        "Elara is happy and Lexi became afraid.", subjects) == {
            "elara": "joy", "lexi": "fear",
        }
    assert voice_emotion.emotions_for_subjects(
        "Elara está furiosa, mas ela sorri.", subjects)["elara"] == "anger"
    assert voice_emotion.emotions_for_subjects(
        "Elara está feliz, mas com medo.", subjects)["elara"] == "fear"
    assert voice_emotion.emotions_for_subjects(
        "Elara talks to Lexi. She is angry.", subjects) == {}
    assert voice_emotion.emotions_for_subjects(
        "You ask Elara and Lexi about trust. She is furious.", subjects) == {}
    assert voice_emotion.emotions_for_subjects(
        "Elara and Lexi are happy. She is afraid.", subjects) == {
            "elara": "joy", "lexi": "joy",
        }
    assert voice_emotion.emotions_for_subjects(
        "Elara asks Caio a question. He is furious.", subjects) == {}
    assert voice_emotion.emotions_for_subjects(
        "A Donzela da Noite is happy. Lexi is furious.",
        {"lexi": ["A Donzela da Noite", "Lexi"]},
    )["lexi"] == "anger"


def test_tts_uses_last_npcs_attributed_emotion_before_global_text_and_hint(client):
    adv = make_adventure(title="TTS por pessoa")
    with db.session() as conn:
        for name in ("Elara", "Lexi"):
            db.insert_card(conn, adv["id"], title=name, type="character", sheet="{}")
    capture: list = []
    override_transport(client, _transport(capture))
    try:
        resp = client.post("/api/tts", json={
            "text": "Elara is happy and Lexi is afraid.",
            "adventure_id": adv["id"], "emotion": "joy",
        })
    finally:
        client.app.dependency_overrides.clear()
    assert resp.status_code == 200
    assert resp.headers["x-voice-emotion"] == "fear"


def test_tts_neutral_last_npc_does_not_inherit_another_npcs_joy(client):
    adv = make_adventure(title="TTS neutro")
    with db.session() as conn:
        for name in ("Elara", "Lexi"):
            db.insert_card(conn, adv["id"], title=name, type="character", sheet="{}")
    capture: list = []
    override_transport(client, _transport(capture))
    try:
        resp = client.post("/api/tts", json={
            "text": "Elara is happy. Lexi waits.",
            "adventure_id": adv["id"], "emotion": "joy",
        })
    finally:
        client.app.dependency_overrides.clear()
    assert resp.status_code == 200
    assert resp.headers["x-voice-emotion"] == "neutral"


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
    assert capture[0]["model"] == "omnivoice"
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
    assert [item["model"] for item in capture] == ["drama-3", "omnivoice"]
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
