"""Contas: o Caio vê tudo; cada pessoa só vê as próprias histórias."""

from __future__ import annotations

from app import db, users

from conftest import TEST_AUTH_PASSWORD, TEST_AUTH_USER


def _login(client, username, password):
    res = client.post("/api/auth/login", json={"username": username, "password": password})
    assert res.status_code == 200, res.text
    token = res.json()["token"]
    client.headers["Authorization"] = f"Bearer {token}"
    return token


def _as_caio(conn) -> None:
    users.create_user(conn, "Mira", "mira-pass")
    conn.execute(
        "INSERT INTO users(username, password_hash, role, created_at) VALUES(?,?,?,?)",
        (users.ADMIN_USERNAME, users.password_hash("caio-pass"), "admin", db.utcnow()),
    )


def test_people_see_only_their_stories_and_caio_sees_all(client):
    with db.session() as conn:
        _as_caio(conn)
        mine = db.insert_adventure(conn, title="Mira's tale", owner="Mira")
        his = db.insert_adventure(conn, title="Caio's tale", owner=users.ADMIN_USERNAME)

    _login(client, "Mira", "mira-pass")
    titles = {row["title"] for row in client.get("/api/adventures").json()}
    assert titles == {"Mira's tale"}
    assert client.get(f"/api/adventures/{his['id']}").status_code == 404
    assert client.get(f"/api/adventures/{mine['id']}").status_code == 200
    assert client.get("/api/users").status_code == 403

    _login(client, users.ADMIN_USERNAME, "caio-pass")
    titles = {row["title"] for row in client.get("/api/adventures").json()}
    assert {"Mira's tale", "Caio's tale"} <= titles
    assert client.get(f"/api/adventures/{mine['id']}").status_code == 200
    listed = client.get("/api/users").json()
    assert any(row["username"] == "Mira" for row in listed)
    assert client.delete(f"/api/users/{users.ADMIN_USERNAME}").status_code == 403


def test_admin_creates_resets_and_deletes_a_user(client):
    with db.session() as conn:
        conn.execute(
            "INSERT INTO users(username, password_hash, role, created_at) VALUES(?,?,?,?)",
            (users.ADMIN_USERNAME, users.password_hash("caio-pass"), "admin", db.utcnow()),
        )
    _login(client, users.ADMIN_USERNAME, "caio-pass")
    created = client.post("/api/users", json={"username": "Nilo", "password": "nilo-pass"})
    assert created.status_code == 201, created.text

    _login(client, "Nilo", "nilo-pass")
    adv = client.post("/api/adventures", json={"title": "Nilo only"}).json()
    assert client.get("/api/adventures").json()[0]["title"] == "Nilo only"

    _login(client, users.ADMIN_USERNAME, "caio-pass")
    assert client.put("/api/users/Nilo/password", json={"password": "nilo-new1"}).status_code == 200
    assert client.post("/api/auth/login",
                       json={"username": "Nilo", "password": "nilo-pass"}).status_code == 401
    _login(client, "Nilo", "nilo-new1")
    changed = client.post("/api/auth/password",
                          json={"current_password": "nilo-new1", "new_password": "nilo-new2"})
    assert changed.status_code == 200, changed.text

    _login(client, users.ADMIN_USERNAME, "caio-pass")
    assert client.delete("/api/users/Nilo").status_code == 200
    assert client.get(f"/api/adventures/{adv['id']}").status_code == 404
    _login(client, TEST_AUTH_USER, TEST_AUTH_PASSWORD)
    assert client.post("/api/auth/login",
                       json={"username": "Nilo", "password": "nilo-new2"}).status_code == 401


def test_new_adventure_belongs_to_the_signed_in_user(client):
    _login(client, TEST_AUTH_USER, TEST_AUTH_PASSWORD)
    adv = client.post("/api/adventures", json={"title": "Mine"}).json()
    assert adv["owner"] == TEST_AUTH_USER
