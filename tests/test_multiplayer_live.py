"""Multiplayer ao vivo: ator, contexto, personagens e eventos."""

from __future__ import annotations

import json

from app import context_builder as cb
from app import db, events, game_engine, settings_store, users
from conftest import add_action, make_adventure, make_llm_transport
from test_v2 import make_v2_transport, override_transport


def _login(client, username, password):
    res = client.post("/api/auth/login", json={"username": username, "password": password})
    assert res.status_code == 200, res.text
    client.headers["Authorization"] = f"Bearer {res.json()['token']}"


def _maria(conn) -> None:
    users.create_user(conn, "maria", "maria-pass")


def _sheet(**over) -> dict:
    sheet = {
        "appearance": "cabelo preto curto e cicatriz no queixo",
        "body": "baixa e ágil",
        "clothing": "manto cinza",
        "status": "alive",
        "stats": {"str": 12, "dex": 16},
        "notes": "não confia em guardas",
    }
    sheet.update(over)
    return sheet


def _put_character(client, adventure_id, title, **sheet_over):
    return client.put(
        f"/api/adventures/{adventure_id}/character",
        json={"title": title, "sheet": _sheet(**sheet_over)},
    )


def _pull(sub) -> list:
    found = []
    while True:
        event = sub.get(timeout=0)
        if event is None:
            break
        found.append(event)
    return found


def _system(adv, actions) -> str:
    with db.session() as conn:
        cfg = settings_store.runtime_config(conn)
        built = cb.build_context(adv, actions, cfg, conn)
    return built.messages[0]["content"]


def test_actor_persisted_in_state_and_get(client):
    adv = client.post("/api/adventures", json={"title": "Mesa"}).json()
    aid = adv["id"]
    with db.session() as conn:
        legacy = db.insert_action(conn, aid, 0, "start", "Begin.")
        action = db.insert_action(conn, aid, 1, "do", "\n> You look\n", actor="maria")
    assert legacy["actor"] == ""
    assert action["actor"] == "maria"
    state = client.get(f"/api/adventures/{aid}").json()
    by_seq = {item["seq"]: item for item in state["actions"]}
    assert by_seq[1]["actor"] == "maria"
    assert by_seq[0]["actor"] == ""


def test_handle_action_records_actor(tmp_db):
    adv = make_adventure(title="Solo", owner="tester")
    add_action(adv["id"], 0, "start", "A gate.")
    transport = make_llm_transport(tokens=("The gate opens.",))
    events_out = list(game_engine.handle_action(
        adv["id"], "do", "push", transport=transport, user="maria"))
    player = next(item for item in events_out if item["event"] == "done")["data"]["player_action"]
    assert player["actor"] == "maria"
    with db.session() as conn:
        actions = db.list_actions(conn, adv["id"])
    assert actions[1]["actor"] == "maria"
    assert actions[0]["actor"] == ""
    assert actions[2]["actor"] == ""


def test_multiplayer_context_names_actors_single_stays_you(tmp_db):
    adv = make_adventure(title="Mesa", owner="Caio", plot_essentials="Uma taverna.")
    add_action(adv["id"], 0, "start", "The tavern is loud.")
    with db.session() as conn:
        db.insert_action(conn, adv["id"], 1, "do", "open the door", actor="maria")
        db.insert_action(conn, adv["id"], 2, "say", "hello", actor="Caio")
    with db.session() as conn:
        users.create_user(conn, "maria", "maria-pass")
        db.share_adventure(conn, adv["id"], "maria")
        actions = db.list_actions(conn, adv["id"])
        cfg = settings_store.runtime_config(conn)
        built = cb.build_context(adv, actions, cfg, conn)
    rendered = "\n".join(message["content"] for message in built.messages)
    assert "\n> maria: open the door\n" in rendered
    assert '\n> Caio says "hello"\n' in rendered
    assert "> You " not in rendered

    alone = make_adventure(title="Solo")
    add_action(alone["id"], 0, "start", "A road.")
    with db.session() as conn:
        db.insert_action(conn, alone["id"], 1, "do", "walk", actor="Caio")
    with db.session() as conn:
        actions = db.list_actions(conn, alone["id"])
        cfg = settings_store.runtime_config(conn)
        built = cb.build_context(alone, actions, cfg, conn)
    last = built.messages[-1]["content"]
    assert last.endswith("\n> You walk\n")
    assert "Caio:" not in last


def test_prewrapped_you_is_the_acting_character(tmp_db):
    with db.session() as conn:
        users.create_user(conn, "ludmilla", "ludmilla-pass")
        _maria(conn)
    adv = make_adventure(
        title="Mesa", owner="ludmilla",
        plot_essentials="Protagonista — Nome: Sandra",
    )
    add_action(adv["id"], 0, "start", "O escritório.")
    with db.session() as conn:
        db.share_adventure(conn, adv["id"], "maria")
        db.upsert_player_character(
            conn, adv["id"], "ludmilla", title="Sandra, A Salamandra", sheet="{}",
        )
        db.upsert_player_character(
            conn, adv["id"], "maria", title="Ikit - O grilo ladrao", sheet="{}",
        )
        db.insert_action(
            conn, adv["id"], 1, "do",
            "\n> You tentar pegar os cristais\n", actor="maria",
        )
        actions = db.list_actions(conn, adv["id"])
        fresh = db.get_adventure(conn, adv["id"])
        cfg = settings_store.runtime_config(conn)
        built = cb.build_context(fresh, actions, cfg, conn)
    rendered = "\n".join(message["content"] for message in built.messages)
    assert "\n> Ikit - O grilo ladrao: tentar pegar os cristais\n" in rendered
    assert "> You " not in rendered
    system = built.messages[0]["content"]
    assert "maria = Ikit - O grilo ladrao" in system
    assert "you/você is only Ikit - O grilo ladrao" in system
    assert "ludmilla = Sandra, A Salamandra" in system


def test_players_block_tracks_character_edits_only(tmp_db):
    adv = make_adventure(title="Mesa", owner="Caio")
    add_action(adv["id"], 0, "start", "Night.")
    with db.session() as conn:
        users.create_user(conn, "maria", "maria-pass")
        db.share_adventure(conn, adv["id"], "maria")
        db.upsert_player_character(
            conn, adv["id"], "Caio", title="Kael",
            sheet=json.dumps(_sheet(appearance="olhos cinza, capa vermelha muito longa " * 8)),
        )
        db.upsert_player_character(
            conn, adv["id"], "maria", title="Lira",
            sheet=json.dumps(_sheet(appearance="tranças louras", status="injured",
                                    stats={"dex": 18})),
        )
        actions = db.list_actions(conn, adv["id"])
        fresh = db.get_adventure(conn, adv["id"])
    system = _system(fresh, actions)
    assert "[Players]" in system
    assert system.index("[Plot]") < system.index("[Players]") if "[Plot]" in system else True
    players = system.split("[Players]\n", 1)[1].split("\n\n", 1)[0]
    assert "Caio = Kael" in players
    assert "maria = Lira" in players
    assert "injured" in players
    assert "dex 18" in players
    assert len(players.split("Caio = Kael — ", 1)[1].split(";", 1)[0]) <= 120

    with db.session() as conn:
        db.insert_action(conn, adv["id"], 1, "continue", "A door creaks.", actor="")
        actions = db.list_actions(conn, adv["id"])
        fresh = db.get_adventure(conn, adv["id"])
    assert _system(fresh, actions).split("[Players]\n", 1)[1].split("\n\n", 1)[0] == players

    with db.session() as conn:
        db.upsert_player_character(
            conn, adv["id"], "maria", title="Lyra",
            sheet=json.dumps(_sheet(appearance="tranças louras", status="alive")),
        )
        fresh = db.get_adventure(conn, adv["id"])
    edited = _system(fresh, actions)
    assert "maria = Lyra" in edited
    assert "maria = Lira" not in edited

    with db.session() as conn:
        db.unshare_adventure(conn, adv["id"], "maria")
        fresh = db.get_adventure(conn, adv["id"])
    assert "[Players]" not in _system(fresh, actions)


def test_character_roundtrip_and_validation(client):
    with db.session() as conn:
        _maria(conn)
    adv = client.post("/api/adventures", json={
        "title": "Mesa",
        "character_rules": "um nome e uma cicatriz",
    }).json()
    aid = adv["id"]
    assert adv["character_rules"] == "um nome e uma cicatriz"
    assert client.get(f"/api/adventures/{aid}/character").json()["card"] is None

    empty = client.put(f"/api/adventures/{aid}/character", json={"title": "   ", "sheet": {}})
    assert empty.status_code == 400
    assert empty.json()["error"]["code"] == "invalid_title"

    saved = client.put(f"/api/adventures/{aid}/character", json={
        "title": "Kael",
        "sheet": _sheet(),
        "portrait_prompt": "scarred rogue",
    })
    assert saved.status_code == 200, saved.text
    card = saved.json()["card"]
    assert card["title"] == "Kael"
    assert card["type"] == "character"
    assert card["player_username"] == "tester"
    assert card["use_for_character_creation"] is True
    assert json.loads(card["sheet"])["status"] == "alive"
    assert card["portrait_prompt"] == "scarred rogue"
    card_id = card["id"]

    again = _put_character(client, aid, "Kael Two", appearance="barba")
    assert again.status_code == 200, again.text
    updated = again.json()["card"]
    assert updated["id"] == card_id
    assert updated["title"] == "Kael Two"
    assert updated["portrait_prompt"] == "scarred rogue"
    assert json.loads(updated["sheet"])["appearance"] == "barba"

    got = client.get(f"/api/adventures/{aid}/character").json()
    assert got["card"]["id"] == card_id
    listed = client.get(f"/api/adventures/{aid}/characters").json()
    assert listed == [{"username": "tester", "card": got["card"]}]

    owner = client.headers["Authorization"]
    _login(client, "maria", "maria-pass")
    missing = client.put(f"/api/adventures/{aid}/character",
                         json={"title": "Intrusa", "sheet": {}})
    assert missing.status_code == 404

    client.headers["Authorization"] = owner
    client.post(f"/api/adventures/{aid}/sharing", json={"username": "maria"})
    _login(client, "maria", "maria-pass")
    hers = _put_character(client, aid, "Lira")
    assert hers.status_code == 200, hers.text
    both = client.get(f"/api/adventures/{aid}/characters").json()
    assert {row["username"] for row in both} == {"tester", "maria"}


def test_character_rules_on_create_and_owner_patch(client):
    with db.session() as conn:
        _maria(conn)
    created = client.post("/api/adventures", json={
        "title": "Regras", "character_rules": "herói com classe",
    }).json()
    assert created["character_rules"] == "herói com classe"
    aid = created["id"]
    patched = client.patch(f"/api/adventures/{aid}", json={"character_rules": "mago ou ladino"})
    assert patched.status_code == 200, patched.text
    assert patched.json()["character_rules"] == "mago ou ladino"

    owner = client.headers["Authorization"]
    client.post(f"/api/adventures/{aid}/sharing", json={"username": "maria"})
    _login(client, "maria", "maria-pass")
    denied = client.patch(f"/api/adventures/{aid}", json={"character_rules": "nada"})
    assert denied.status_code == 403
    client.headers["Authorization"] = owner
    assert client.get(f"/api/adventures/{aid}").json()["character_rules"] == "mago ou ladino"


def test_shared_action_requires_character_then_passes(client):
    with db.session() as conn:
        _maria(conn)
    override_transport(client, make_v2_transport())
    adv = client.post("/api/adventures", json={"title": "Mesa"}).json()
    aid = adv["id"]
    assert client.post(f"/api/adventures/{aid}/sharing",
                       json={"username": "maria"}).status_code == 201
    assert client.patch(f"/api/adventures/{aid}", json={"turn_mode": True}).status_code == 200

    blocked = client.post(f"/api/adventures/{aid}/action",
                          json={"type": "do", "text": "look"})
    assert blocked.status_code == 428
    assert blocked.json()["error"]["code"] == "character_required"
    assert client.post(f"/api/adventures/{aid}/continue").status_code == 428
    assert client.post(f"/api/adventures/{aid}/retry").status_code == 428

    assert _put_character(client, aid, "Kael").status_code == 200
    played = client.post(f"/api/adventures/{aid}/action",
                         json={"type": "do", "text": "look"})
    assert played.status_code == 200, played.text
    assert "event: done" in played.text
    state = client.get(f"/api/adventures/{aid}").json()
    player = next(action for action in state["actions"] if action["type"] == "do")
    assert player["actor"] == "tester"


def test_live_events_during_handle_action(tmp_db):
    adv = make_adventure(title="Ao vivo", owner="tester")
    add_action(adv["id"], 0, "start", "A hall.")
    transport = make_llm_transport(tokens=("The", " hall", " answers."))
    with events.subscribe(adv["id"]) as sub:
        done = list(game_engine.handle_action(
            adv["id"], "say", "who is there", transport=transport, user="tester"))
        captured = _pull(sub)
    kinds = [event.type for event in captured]
    assert "player_action" in kinds
    assert "token" in kinds
    assert "action" in kinds
    player = next(event for event in captured if event.type == "player_action")
    assert player.data["type"] == "say"
    assert player.data["actor"] == "tester"
    tokens = "".join(event.data["token"] for event in captured if event.type == "token")
    assert tokens == "The hall answers."
    narrator = next(event for event in captured if event.type == "action")
    assert narrator.data["type"] == "continue"
    assert narrator.data["text"] == "The hall answers."
    assert any(item["event"] == "done" for item in done)
