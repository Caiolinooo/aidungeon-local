"""Rotas /sim: dono, pack, dado e Modo Vida desligado."""

from __future__ import annotations

from app import db, users

from conftest import TEST_AUTH_USER, make_adventure


def _login(client, username: str, password: str) -> None:
    res = client.post("/api/auth/login", json={"username": username, "password": password})
    assert res.status_code == 200, res.text
    client.headers["Authorization"] = f"Bearer {res.json()['token']}"


def test_pack_by_shared_player_is_403(client):
    adv = make_adventure(title="Mesa", owner=TEST_AUTH_USER)
    with db.session() as conn:
        users.create_user(conn, "Mira", "mira-pass")
        db.share_adventure(conn, adv["id"], "Mira")
    _login(client, "Mira", "mira-pass")
    res = client.put(f"/api/adventures/{adv['id']}/sim/pack", json={"pack": "d20"})
    assert res.status_code == 403
    assert res.json()["error"]["code"] == "forbidden"


def test_unknown_pack_is_422(client):
    adv = make_adventure(title="Mine", owner=TEST_AUTH_USER)
    res = client.put(f"/api/adventures/{adv['id']}/sim/pack", json={"pack": "desconhecido"})
    assert res.status_code == 422
    ok = client.put(f"/api/adventures/{adv['id']}/sim/pack", json={"pack": "d100"})
    assert ok.status_code == 200, ok.text
    assert ok.json()["pack"] == "d100"
    assert ok.json()["actor"] is not None


def test_roll_3d6_is_in_range(client):
    adv = make_adventure(title="Mine", owner=TEST_AUTH_USER)
    res = client.post(f"/api/adventures/{adv['id']}/sim/roll", json={"expr": "3d6"})
    assert res.status_code == 200, res.text
    assert 3 <= res.json()["total"] <= 18
    bad = client.post(f"/api/adventures/{adv['id']}/sim/roll", json={"expr": "abc"})
    assert bad.status_code == 422
    assert bad.json()["error"]["code"] == "invalid_dice"


def test_advance_while_life_is_off_is_409(client):
    adv = make_adventure(title="Mine", owner=TEST_AUTH_USER)
    res = client.post(
        f"/api/adventures/{adv['id']}/sim/life/advance",
        json={"minutes": 60, "activity": "sleep"},
    )
    assert res.status_code == 409
    assert res.json()["error"]["code"] == "life_disabled"
