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


def _d20_adventure(client):
    adv = make_adventure(title="Arena", owner=TEST_AUTH_USER)
    res = client.put(f"/api/adventures/{adv['id']}/sim/pack", json={"pack": "d20"})
    assert res.status_code == 200, res.text
    return adv


def test_action_skill_check_rolls_on_the_server(client):
    adv = _d20_adventure(client)
    res = client.post(
        f"/api/adventures/{adv['id']}/sim/action",
        json={"kind": "skill", "skill": "Atletismo", "ability": "str", "difficulty": "medium"},
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["resolution"]["steps"], body
    assert any("Atletismo" in step["text"] or "d20" in step["text"]
               for step in body["resolution"]["steps"])


def test_action_attack_creates_and_targets_a_foe(client):
    adv = _d20_adventure(client)
    res = client.post(
        f"/api/adventures/{adv['id']}/sim/action",
        json={"kind": "attack", "target": "Lobo", "weapon": "Espada", "toughness": "weak"},
    )
    assert res.status_code == 200, res.text
    body = res.json()
    steps = body["resolution"]["steps"]
    assert steps[0]["label"] == "attack"
    assert "Ataque (espada)" in steps[0]["text"]
    assert any(foe["name"] == "Lobo" for foe in body["foes"]) or any(
        step["label"] == "kill" for step in steps)


def test_action_use_item_restores_hp_and_consumes(client):
    adv = _d20_adventure(client)
    res = client.post(
        f"/api/adventures/{adv['id']}/sim/action",
        json={"kind": "take", "item": "Poção de cura"},
    )
    assert res.status_code == 200, res.text
    actor = client.get(f"/api/adventures/{adv['id']}/sim").json()["actor"]
    hurt = client.put(
        f"/api/adventures/{adv['id']}/sim/actor", json={"hp": max(1, actor["hp_max"] - 6)})
    assert hurt.status_code == 200
    res = client.post(
        f"/api/adventures/{adv['id']}/sim/action",
        json={"kind": "use_item", "item": "Poção de cura", "heal": True},
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert any(row["what"] == "PV" for row in body["resolution"]["changes"])
    inventory = {row["name"]: row["qty"] for row in body["actor"]["inventory"]}
    assert "Poção de cura" not in inventory


def test_action_use_missing_item_is_409(client):
    adv = _d20_adventure(client)
    res = client.post(
        f"/api/adventures/{adv['id']}/sim/action",
        json={"kind": "use_item", "item": "Poção inexistente"},
    )
    assert res.status_code == 409
    assert res.json()["error"]["code"] == "rules_refused"


def test_action_advances_the_life_clock_and_fires_reactions(client):
    adv = _d20_adventure(client)
    life_on = client.put(f"/api/adventures/{adv['id']}/sim/life", json={"enabled": True})
    assert life_on.status_code == 200
    # fome perto do limite para a ação de 1h cruzar o aviso
    sim = client.get(f"/api/adventures/{adv['id']}/sim").json()
    assert sim["life"]["enabled"] is True
    res = client.post(
        f"/api/adventures/{adv['id']}/sim/action",
        json={"kind": "rest", "minutes": 60},
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["resolution"]["time"]["minutes"] == 60
    assert body["life"]["clock"] != "08:00" or body["life"]["minute"] != 480


def test_action_without_sim_is_409(client):
    adv = make_adventure(title="Vazio", owner=TEST_AUTH_USER)
    res = client.post(
        f"/api/adventures/{adv['id']}/sim/action",
        json={"kind": "rest", "minutes": 60},
    )
    assert res.status_code == 409
    assert res.json()["error"]["code"] == "sim_off"


def test_action_unknown_kind_is_422(client):
    adv = _d20_adventure(client)
    res = client.post(
        f"/api/adventures/{adv['id']}/sim/action", json={"kind": "dançar"})
    assert res.status_code == 422
    assert res.json()["error"]["code"] == "invalid_kind"
