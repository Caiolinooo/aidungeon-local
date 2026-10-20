"""Rulesets: builtins no seed, CRUD só do admin, apply em character_rules."""

from __future__ import annotations

from app import db, users

from conftest import TEST_AUTH_USER, make_adventure

BUILTIN_NAMES = {
    "D&D 5e",
    "Tormenta20",
    "GURPS",
    "Call of Cthulhu 7e",
    "FATE Core",
    "Savage Worlds",
}


def _rows(client) -> list[dict]:
    res = client.get("/api/rulesets")
    assert res.status_code == 200, res.text
    return res.json()


def _builtin(client, name: str = "D&D 5e") -> dict:
    return next(row for row in _rows(client) if row["name"] == name)


def _login(client, username: str, password: str) -> None:
    res = client.post("/api/auth/login", json={"username": username, "password": password})
    assert res.status_code == 200, res.text
    client.headers["Authorization"] = f"Bearer {res.json()['token']}"


def _as_caio(client) -> None:
    with db.session() as conn:
        conn.execute(
            "INSERT INTO users(username, password_hash, role, created_at) VALUES(?,?,?,?)",
            (users.ADMIN_USERNAME, users.password_hash("caio-pass"), "admin", db.utcnow()),
        )
    _login(client, users.ADMIN_USERNAME, "caio-pass")


def test_seed_lists_six_builtins_for_a_normal_user(client):
    found = {row["name"]: row for row in _rows(client)}
    assert BUILTIN_NAMES <= set(found)
    for name in BUILTIN_NAMES:
        row = found[name]
        assert row["builtin"] is True
        assert 800 <= len(row["rules"]) <= 1500


def test_get_ruleset_and_missing(client):
    row = _builtin(client, "FATE Core")
    got = client.get(f"/api/rulesets/{row['id']}")
    assert got.status_code == 200, got.text
    assert got.json()["name"] == "FATE Core"
    assert got.json()["builtin"] is True
    missing = client.get("/api/rulesets/nao-existe")
    assert missing.status_code == 404
    assert missing.json()["error"]["code"] == "not_found"


def test_mutations_require_admin(client):
    row = _builtin(client)
    created = client.post("/api/rulesets", json={"name": "Casa", "rules": "Regra da casa."})
    assert created.status_code == 403
    assert created.json()["error"]["code"] == "forbidden"
    updated = client.put(f"/api/rulesets/{row['id']}", json={"description": "no"})
    assert updated.status_code == 403
    assert updated.json()["error"]["code"] == "forbidden"
    deleted = client.delete(f"/api/rulesets/{row['id']}")
    assert deleted.status_code == 403
    assert deleted.json()["error"]["code"] == "forbidden"
    assert client.get(f"/api/rulesets/{row['id']}").status_code == 200


def test_admin_creates_updates_and_deletes(client):
    _as_caio(client)
    missing = client.post("/api/rulesets", json={"description": "sem nome"})
    assert missing.status_code == 422
    assert missing.json()["error"]["code"] == "validation"
    too_long = client.post("/api/rulesets", json={"name": "Longa", "rules": "x" * 4001})
    assert too_long.status_code == 422

    created = client.post("/api/rulesets", json={
        "name": "Casa",
        "description": "regras da mesa",
        "rules": "Na mesa, o narrador tem a palavra final.",
    })
    assert created.status_code == 201, created.text
    body = created.json()
    assert body["builtin"] is False
    assert body["name"] == "Casa"
    assert body["description"] == "regras da mesa"
    rid = body["id"]

    duplicate = client.post("/api/rulesets", json={"name": "Casa", "rules": "outra"})
    assert duplicate.status_code == 422
    assert duplicate.json()["error"]["code"] == "validation"

    updated = client.put(f"/api/rulesets/{rid}", json={"name": "Casa nova", "rules": "Texto novo."})
    assert updated.status_code == 200, updated.text
    assert updated.json()["name"] == "Casa nova"
    assert updated.json()["rules"] == "Texto novo."
    assert updated.json()["description"] == "regras da mesa"

    assert client.put("/api/rulesets/nao-existe", json={"name": "Z"}).status_code == 404
    removed = client.delete(f"/api/rulesets/{rid}")
    assert removed.status_code == 200
    assert removed.json()["deleted"] is True
    assert client.get(f"/api/rulesets/{rid}").status_code == 404
    assert client.delete("/api/rulesets/nao-existe").status_code == 404

    builtin = _builtin(client, "Savage Worlds")
    assert client.delete(f"/api/rulesets/{builtin['id']}").status_code == 200
    left = {row["name"] for row in _rows(client)}
    assert "Savage Worlds" not in left
    assert "D&D 5e" in left


def test_apply_replace_and_append_caps_at_4000(client):
    adv = make_adventure(title="Mesa", owner=TEST_AUTH_USER)
    dnd = _builtin(client, "D&D 5e")
    url = f"/api/adventures/{adv['id']}/rulesets/apply"

    replaced = client.post(url, json={"ruleset_id": dnd["id"]})
    assert replaced.status_code == 200, replaced.text
    assert replaced.json()["id"] == adv["id"]
    assert replaced.json()["character_rules"] == dnd["rules"]

    gurps = _builtin(client, "GURPS")
    appended = client.post(url, json={"ruleset_id": gurps["id"], "mode": "append"})
    assert appended.status_code == 200, appended.text
    assert appended.json()["character_rules"] == dnd["rules"] + "\n\n" + gurps["rules"]

    with db.session() as conn:
        db.update_adventure(conn, adv["id"], character_rules="A" * 3900)
    capped = client.post(url, json={"ruleset_id": gurps["id"], "mode": "append"})
    assert capped.status_code == 200, capped.text
    text = capped.json()["character_rules"]
    assert len(text) == 4000
    assert text.startswith("A" * 3900)
    assert text[3900:3902] == "\n\n"


def test_apply_append_on_empty_joins_with_blank_line(client):
    adv = make_adventure(title="Vazia", owner=TEST_AUTH_USER)
    dnd = _builtin(client)
    res = client.post(
        f"/api/adventures/{adv['id']}/rulesets/apply",
        json={"ruleset_id": dnd["id"], "mode": "append"},
    )
    assert res.status_code == 200, res.text
    assert res.json()["character_rules"] == "\n\n" + dnd["rules"]


def test_apply_missing_ruleset_is_404(client):
    adv = make_adventure(title="Mine", owner=TEST_AUTH_USER)
    res = client.post(
        f"/api/adventures/{adv['id']}/rulesets/apply",
        json={"ruleset_id": "nao-existe"},
    )
    assert res.status_code == 404
    assert res.json()["error"]["code"] == "not_found"


def test_apply_by_stranger_is_404(client):
    adv = make_adventure(title="Alheia", owner="Outro")
    dnd = _builtin(client)
    res = client.post(
        f"/api/adventures/{adv['id']}/rulesets/apply",
        json={"ruleset_id": dnd["id"], "mode": "replace"},
    )
    assert res.status_code == 404
    assert res.json()["error"]["code"] == "not_found"


def test_apply_by_shared_player_is_403(client):
    adv = make_adventure(title="Mesa", owner=TEST_AUTH_USER)
    with db.session() as conn:
        users.create_user(conn, "Mira", "mira-pass")
        db.share_adventure(conn, adv["id"], "Mira")
    _login(client, "Mira", "mira-pass")
    dnd = _builtin(client)
    res = client.post(
        f"/api/adventures/{adv['id']}/rulesets/apply",
        json={"ruleset_id": dnd["id"]},
    )
    assert res.status_code == 403
    assert res.json()["error"]["code"] == "forbidden"


def test_apply_bad_mode_is_422(client):
    adv = make_adventure(title="Mine", owner=TEST_AUTH_USER)
    res = client.post(
        f"/api/adventures/{adv['id']}/rulesets/apply",
        json={"ruleset_id": "dnd-5e", "mode": "merge"},
    )
    assert res.status_code == 422
