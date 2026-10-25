"""Vozes clonadas: gravar, falar, narrador e apagar."""

from __future__ import annotations

import base64
import json

import httpx

from app import auth, db, deps, settings_store, users
from conftest import TEST_AUTH_SECRET, TEST_AUTH_USER

RVQ = base64.b64encode(b"rvq-bytes").decode("ascii")


def _transport(capture: list, *, fail: str | None = None) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/audio/voices") and request.method == "POST":
            if fail == "short":
                return httpx.Response(400, json={
                    "error": {"code": "audio_too_short", "message": "curto"},
                })
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
        if "/audio/voices/" in path and request.method == "DELETE":
            capture.append(("delete", path.rsplit("/", 1)[-1]))
            return httpx.Response(200, json={"id": path.rsplit("/", 1)[-1]})
        if path.endswith("/audio/speech"):
            capture.append(("speech", json.loads(request.content.decode())))
            return httpx.Response(200, content=b"ID3fake", headers={"content-type": "audio/mpeg"})
        return httpx.Response(404, json={"error": f"no mock route: {path}"})

    return httpx.MockTransport(handler)


def _use(client, transport) -> None:
    client.app.dependency_overrides[deps.get_http_transport] = lambda: transport


def _as_admin(client) -> None:
    token = auth.make_token(users.ADMIN_USERNAME, TEST_AUTH_SECRET, ttl_h=1)
    client.headers["Authorization"] = f"Bearer {token}"


def _clone(client, name="Clara", ref_text="Olá, esta é a minha voz."):
    return client.post(
        "/api/voices/clone",
        data={"name": name, "ref_text": ref_text},
        files={"file": ("voz.wav", b"RIFFfake-wav", "audio/wav")},
    )


def test_clone_is_listed_apart_from_builtins(client):
    capture: list = []
    _use(client, _transport(capture))
    try:
        resp = _clone(client)
    finally:
        client.app.dependency_overrides.clear()
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["name"] == "Clara"
    assert body["id"].startswith("cv_")
    assert len(body["id"]) == 11
    with db.session() as conn:
        assert db.get_cloned_voice(conn, body["id"])["owner"] == TEST_AUTH_USER
    listed = client.get("/api/voices")
    assert listed.status_code == 200
    data = listed.json()
    assert data["voices"] == ["M1", "M2", "M3", "F1", "F2", "F3"]
    assert {"id": body["id"], "name": "Clara"} in data["cloned"]


def test_duplicate_name_is_conflict(client):
    capture: list = []
    _use(client, _transport(capture))
    try:
        first = _clone(client, name="Clara")
        second = _clone(client, name="clara")
    finally:
        client.app.dependency_overrides.clear()
    assert first.status_code == 200, first.text
    assert second.status_code == 409, second.text
    assert second.json()["error"]["code"] == "voice_name_taken"


def test_provider_short_audio_is_400(client):
    _use(client, _transport([], fail="short"))
    try:
        resp = _clone(client, name="Curta")
    finally:
        client.app.dependency_overrides.clear()
    assert resp.status_code == 400, resp.text
    assert resp.json()["error"]["code"] == "audio_too_short"


def test_narrator_accepts_cloned_voice_and_rejects_unknown(client):
    capture: list = []
    _use(client, _transport(capture))
    try:
        created = _clone(client, name="Narradora")
        assert created.status_code == 200, created.text
        voice_id = created.json()["id"]
        _as_admin(client)
        ok = client.put("/api/settings/voice", json={"narrator_voice": voice_id})
        bad = client.put("/api/settings/voice", json={"narrator_voice": "cv_00000000"})
    finally:
        client.app.dependency_overrides.clear()
    assert ok.status_code == 200, ok.text
    assert ok.json()["narrator_voice"] == voice_id
    assert bad.status_code == 400
    assert bad.json()["error"]["code"] == "invalid_voice"


def test_tts_pushes_rvq_and_uses_cloned_id(client):
    capture: list = []
    _use(client, _transport(capture))
    try:
        created = _clone(client, name="Fala")
        assert created.status_code == 200, created.text
        voice_id = created.json()["id"]
        spoken = client.post("/api/tts", json={"text": "O vento sopra.", "voice": voice_id})
    finally:
        client.app.dependency_overrides.clear()
    assert spoken.status_code == 200, spoken.text
    puts = [item for item in capture if item[0] == "put"]
    speeches = [item for item in capture if item[0] == "speech"]
    assert len(puts) == 1
    assert puts[0][1]["rvq_b64"] == RVQ
    assert speeches[0][1]["voice"] == voice_id
    assert speeches[0][1]["model"] == "omnivoice"


def test_delete_clears_cards_and_resets_narrator(client):
    capture: list = []
    _use(client, _transport(capture))
    try:
        created = _clone(client, name="Temporaria")
        assert created.status_code == 200, created.text
        voice_id = created.json()["id"]
        with db.session() as conn:
            settings_store.apply_voice_update(conn, {"narrator_voice": voice_id})
            adventure = db.insert_adventure(conn, title="Voz")
            card = db.insert_card(conn, adventure["id"], title="NPC", voice=voice_id)
            adventure_id = adventure["id"]
            card_id = card["id"]
        removed = client.delete(f"/api/voices/{voice_id}")
    finally:
        client.app.dependency_overrides.clear()
    assert removed.status_code == 200, removed.text
    with db.session() as conn:
        assert db.get_cloned_voice(conn, voice_id) is None
        assert db.get_card(conn, adventure_id, card_id)["voice"] == ""
        assert settings_store.effective_voice_settings(conn)["narrator_voice"] == "M1"
    assert ("delete", voice_id) in capture
