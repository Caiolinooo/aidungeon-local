"""Autorização de propriedade: cenários, settings globais e clones de voz."""

from __future__ import annotations

import base64
import sqlite3

import httpx
import pytest

from app import auth, db, deps, users
from conftest import TEST_AUTH_SECRET, TEST_AUTH_USER


def _as(client, username: str) -> None:
    token = auth.make_token(username, TEST_AUTH_SECRET, ttl_h=1)
    client.headers["Authorization"] = f"Bearer {token}"


def _voice_transport() -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        if "/audio/voices/" in request.url.path and request.method == "PUT":
            return httpx.Response(200, json={"id": "voice"})
        if "/audio/voices/" in request.url.path and request.method == "DELETE":
            return httpx.Response(200, json={"id": "voice"})
        if request.url.path.endswith("/audio/speech"):
            return httpx.Response(200, content=b"ID3fake")
        return httpx.Response(404)

    return httpx.MockTransport(handler)


def _insert_voice(voice_id: str, name: str, owner: str) -> None:
    with db.session() as conn:
        db.insert_cloned_voice(
            conn, voice_id=voice_id, name=name, ref_text="texto",
            rvq=base64.b64decode(base64.b64encode(b"rvq")), sample_seconds=1,
            owner=owner,
        )


def test_scenario_owner_other_user_and_admin(client):
    created = client.post("/api/scenarios", json={"title": "Original"})
    assert created.status_code == 201, created.text
    scenario_id = created.json()["id"]
    assert created.json()["owner"] == TEST_AUTH_USER

    _as(client, "other")
    assert client.get(f"/api/scenarios/{scenario_id}").status_code == 200
    adventure = client.post("/api/adventures", json={"scenario_id": scenario_id})
    assert adventure.status_code == 201, adventure.text
    for method in ("patch", "put", "delete"):
        response = getattr(client, method)(
            f"/api/scenarios/{scenario_id}",
            **({} if method == "delete" else {"json": {"title": "Invasão"}}),
        )
        assert response.status_code == 403, response.text

    _as(client, TEST_AUTH_USER)
    assert client.patch(
        f"/api/scenarios/{scenario_id}", json={"title": "Do dono"},
    ).status_code == 200
    assert client.put(
        f"/api/scenarios/{scenario_id}", json={"description": "Editável"},
    ).status_code == 200

    _as(client, users.ADMIN_USERNAME)
    assert client.patch(
        f"/api/scenarios/{scenario_id}", json={"title": "Do admin"},
    ).status_code == 200
    assert client.delete(f"/api/scenarios/{scenario_id}").status_code == 200
    _as(client, "other")
    assert client.get(f"/api/adventures/{adventure.json()['id']}").status_code == 200


@pytest.mark.parametrize(("path", "payload", "field"), [
    ("/api/settings/llm", {"max_tokens": 400}, "max_tokens"),
    ("/api/settings/image", {"enabled": False}, "enabled"),
    ("/api/settings/voice", {"enabled": False}, "enabled"),
    ("/api/settings/embeddings", {"model": "owner-test-model"}, "model"),
    ("/api/settings/scripting", {"input_modifier": "return text"}, "input_modifier"),
])
def test_global_settings_require_admin(client, path, payload, field):
    before = client.get(path).json()[field]
    response = client.put(path, json=payload)
    assert response.status_code == 403, response.text
    assert client.get(path).json()[field] == before

    _as(client, users.ADMIN_USERNAME)
    response = client.put(path, json=payload)
    assert response.status_code == 200, response.text
    assert response.json()[field] == payload[field]


def test_clone_listing_use_and_delete_are_isolated(client):
    _insert_voice("cv_owner001", "Do dono", TEST_AUTH_USER)
    _insert_voice("cv_other001", "De outro", "other")
    client.app.dependency_overrides[deps.get_http_transport] = _voice_transport
    try:
        own = client.get("/api/voices").json()["cloned"]
        assert own == [{"id": "cv_owner001", "name": "Do dono"}]
        assert client.post(
            "/api/tts", json={"text": "permitido", "voice": "cv_owner001"},
        ).status_code == 200

        _as(client, "other")
        other = client.get("/api/voices").json()["cloned"]
        assert other == [{"id": "cv_other001", "name": "De outro"}]
        assert client.post(
            "/api/tts", json={"text": "negado", "voice": "cv_owner001"},
        ).status_code == 404
        assert client.delete("/api/voices/cv_owner001").status_code == 404
        with db.session() as conn:
            assert db.get_cloned_voice(conn, "cv_owner001") is not None

        _as(client, users.ADMIN_USERNAME)
        listed = client.get("/api/voices").json()["cloned"]
        assert {row["id"] for row in listed} == {"cv_owner001", "cv_other001"}
        assert client.post(
            "/api/tts", json={"text": "admin", "voice": "cv_owner001"},
        ).status_code == 200
        assert client.delete("/api/voices/cv_owner001").status_code == 200
    finally:
        client.app.dependency_overrides.clear()


def test_shared_adventure_can_use_only_its_owners_clone(client):
    _insert_voice("cv_owner001", "Do dono", TEST_AUTH_USER)
    _insert_voice("cv_other001", "De outro", "other")
    _insert_voice("cv_third001", "De terceiro", "third")
    with db.session() as conn:
        users.create_user(conn, "other", "other-pass")
        adventure = db.insert_adventure(conn, title="Compartilhada", owner=TEST_AUTH_USER)
        db.share_adventure(conn, adventure["id"], "other")
    client.app.dependency_overrides[deps.get_http_transport] = _voice_transport
    try:
        _as(client, "other")
        allowed = client.post("/api/tts", json={
            "text": "voz legítima", "voice": "cv_owner001",
            "adventure_id": adventure["id"],
        })
        assert allowed.status_code == 200, allowed.text
        denied = client.post("/api/tts", json={
            "text": "voz isolada", "voice": "cv_third001",
            "adventure_id": adventure["id"],
        })
        assert denied.status_code == 404, denied.text
    finally:
        client.app.dependency_overrides.clear()


def test_migration_029_adds_owners_and_backfills_admin():
    conn = sqlite3.connect(":memory:")
    conn.executescript("""
        CREATE TABLE scenarios(id TEXT PRIMARY KEY, ownerless TEXT);
        CREATE TABLE cloned_voices(id TEXT PRIMARY KEY);
        INSERT INTO scenarios VALUES('legacy-scenario', 'kept');
        INSERT INTO cloned_voices VALUES('legacy-voice');
    """)
    migration = db.MIGRATIONS_DIR / "029_ownership.sql"
    conn.executescript(migration.read_text(encoding="utf-8"))
    assert conn.execute(
        "SELECT owner, ownerless FROM scenarios WHERE id='legacy-scenario'",
    ).fetchone() == (users.ADMIN_USERNAME, "kept")
    assert conn.execute(
        "SELECT owner FROM cloned_voices WHERE id='legacy-voice'",
    ).fetchone()[0] == users.ADMIN_USERNAME
    conn.close()
