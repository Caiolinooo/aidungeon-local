"""Missões, transições, autorização e gatilhos de estado."""

from __future__ import annotations

import json

from app import db, npc_bonds, users, world_map
from app.sim import quests, state, triggers

from conftest import TEST_AUTH_USER, make_adventure


def _login(client, username: str, password: str) -> None:
    res = client.post("/api/auth/login", json={"username": username, "password": password})
    assert res.status_code == 200, res.text
    client.headers["Authorization"] = f"Bearer {res.json()['token']}"


def _pack(client, adv_id: str) -> None:
    res = client.put(f"/api/adventures/{adv_id}/sim/pack", json={"pack": "d20"})
    assert res.status_code == 200, res.text


def _quest(client, adv_id: str, **body):
    res = client.post(f"/api/adventures/{adv_id}/sim/quests", json=body)
    assert res.status_code == 200, res.text
    return res.json()


def _stored_quest(adv_id: str) -> dict:
    with db.session() as conn:
        sim = db.get_adventure(conn, adv_id)["sim_state"]
    return sim["quests"][0]


def test_owner_crud_transitions_and_reload(client):
    adv = make_adventure(title="Missões", owner=TEST_AUTH_USER)
    created = _quest(
        client, adv["id"],
        title="A chave",
        description="Achar e usar",
        status="planned",
        stages=[{"title": "Achar"}, {"title": "Usar"}],
    )
    quest = created["quests"][0]
    assert quest["status"] == "planned"
    assert quest["progress"] == {"value": 0, "target": 2}
    assert [stage["title"] for stage in quest["stages"]] == ["Achar", "Usar"]

    denied = client.post(
        f"/api/adventures/{adv['id']}/sim/quests/{quest['id']}/transition",
        json={"status": "accepted"},
    )
    assert denied.status_code == 409
    assert denied.json()["error"]["code"] == "invalid_transition"

    available = client.post(
        f"/api/adventures/{adv['id']}/sim/quests/{quest['id']}/transition",
        json={"status": "available"},
    )
    assert available.status_code == 200, available.text
    quest_id = quest["id"]
    stage_id = available.json()["quests"][0]["stages"][0]["id"]
    accepted = client.post(
        f"/api/adventures/{adv['id']}/sim/quests/{quest_id}/transition",
        json={"status": "accepted"},
    )
    assert accepted.status_code == 200, accepted.text
    marked = client.post(
        f"/api/adventures/{adv['id']}/sim/quests/{quest_id}/transition",
        json={"stage_id": stage_id, "done": True},
    )
    assert marked.status_code == 200, marked.text
    body = marked.json()["quests"][0]
    assert body["status"] == "accepted"
    assert body["progress"]["value"] == 1
    assert body["stages"][0]["done"] is True

    missing = client.post(
        f"/api/adventures/{adv['id']}/sim/quests/nao-existe/transition",
        json={"status": "accepted"},
    )
    assert missing.status_code == 404

    with db.session() as conn:
        reloaded = db.get_adventure(conn, adv["id"])["sim_state"]["quests"][0]
    assert reloaded["status"] == "accepted"
    assert reloaded["progress"]["value"] == 1

    listed = client.get(f"/api/adventures/{adv['id']}/sim/quests")
    assert listed.status_code == 200
    assert listed.json()["quests"][0]["id"] == quest_id

    removed = client.delete(f"/api/adventures/{adv['id']}/sim/quests/{quest_id}")
    assert removed.status_code == 200, removed.text
    assert removed.json()["quests"] == []


def test_shared_player_cannot_edit_quests(client):
    adv = make_adventure(title="Mesa", owner=TEST_AUTH_USER)
    _quest(client, adv["id"], title="Do dono", status="available")
    with db.session() as conn:
        users.create_user(conn, "Mira", "mira-pass")
        db.share_adventure(conn, adv["id"], "Mira")
    _login(client, "Mira", "mira-pass")
    seen = client.get(f"/api/adventures/{adv['id']}/sim/quests")
    assert seen.status_code == 200
    assert seen.json()["quests"][0]["title"] == "Do dono"
    denied = client.post(
        f"/api/adventures/{adv['id']}/sim/quests",
        json={"title": "Intrusa"},
    )
    assert denied.status_code == 403
    assert denied.json()["error"]["code"] == "forbidden"
    quest_id = seen.json()["quests"][0]["id"]
    accept = client.post(
        f"/api/adventures/{adv['id']}/sim/quests/{quest_id}/transition",
        json={"status": "accepted"},
    )
    assert accept.status_code == 403


def test_state_triggers_are_idempotent():
    sim = state.empty()
    key = state.actor_key("hero")
    sim["actors"][key] = {
        "name": "Aldric",
        "hp": 3,
        "resources": {"gold": 8, "san": 12, "rations": 2},
        "inventory": [{"name": "Chave", "qty": 1}],
    }
    sim["life"] = {
        "enabled": True,
        "relations": {"mira|aldric": 6},
        "npcs": {"mira": {"place": "praça"}},
    }
    cards = [{
        "id": "mira",
        "type": "character",
        "title": "Mira",
        "sheet": json.dumps({"bonds": {"trust": 55}, "relations": {"aldric": {"affinity": 70}}}),
    }]
    specs = [
        ("hp", {"fact": "hp", "op": "lte", "value": 4}),
        ("gold", {"fact": "gold", "op": "gte", "value": 8}),
        ("san", {"fact": "san", "op": "lte", "value": 20}),
        ("resource", {"fact": "resource", "name": "rations", "op": "gte", "value": 2}),
        ("item", {"fact": "item", "name": "chave"}),
        ("place", {"fact": "location", "place": "Praça", "card_id": "mira"}),
        ("life", {"fact": "relation", "a": "aldric", "b": "mira", "op": "gte", "value": 6}),
        ("bond", {"fact": "relation", "card_id": "Mira", "stat": "trust", "op": "gte", "value": 50}),
        ("affinity", {"fact": "relation", "card_id": "mira", "other_id": "aldric", "stat": "affinity", "op": "gte", "value": 70}),
    ]
    for name, clause in specs:
        quests.create(sim, {
            "title": name,
            "status": "hidden",
            "triggers": [{
                "when": [clause],
                "apply": [{"status": "available"}, {"progress_delta": 1}],
            }],
        })
    quests.create(sim, {
        "title": "já fechada",
        "status": "completed",
        "triggers": [{"when": [{"fact": "hp", "op": "lte", "value": 4}], "apply": [{"status": "hidden"}]}],
    })
    facts = triggers.build_facts(sim, user="hero", cards=cards, location={"id": "loc", "title": "Outro"})
    first = triggers.evaluate(sim, facts)
    second = triggers.evaluate(sim, facts)
    assert second == []
    by_title = {quest["title"]: quest for quest in sim["quests"]}
    for name, _clause in specs:
        assert by_title[name]["status"] == "available", name
        assert by_title[name]["progress"]["value"] == 1, name
    assert by_title["já fechada"]["status"] == "completed"
    assert len(first) == len(specs)
    closed = next(trigger for trigger in sim["triggers"] if trigger["apply"][0].get("status") == "hidden")
    assert closed["applied"] is False


def test_resolve_travel_and_relation_fire_triggers(client):
    adv = make_adventure(title="Gatilhos", owner=TEST_AUTH_USER)
    _pack(client, adv["id"])
    hurt = client.put(f"/api/adventures/{adv['id']}/sim/actor", json={"hp": 2, "gold": 5})
    assert hurt.status_code == 200, hurt.text
    _quest(
        client, adv["id"],
        title="Ferido",
        status="hidden",
        triggers=[{"when": [{"fact": "hp", "op": "lte", "value": 3}], "apply": [{"status": "available"}]}],
    )
    _quest(
        client, adv["id"],
        title="Sem ouro",
        status="hidden",
        triggers=[{"when": [{"fact": "gold", "op": "lte", "value": 0}], "apply": [{"status": "available"}]}],
    )
    _quest(
        client, adv["id"],
        title="Tem corda",
        status="hidden",
        triggers=[{"when": [{"fact": "item", "name": "Corda"}], "apply": [{"status": "available"}, {"progress_delta": 1}]}],
    )
    rested = client.post(
        f"/api/adventures/{adv['id']}/sim/action",
        json={"kind": "rest", "minutes": 5},
    )
    assert rested.status_code == 200, rested.text
    bought = client.post(
        f"/api/adventures/{adv['id']}/sim/action",
        json={"kind": "buy", "item": "pão", "gold": 5},
    )
    assert bought.status_code == 200, bought.text
    taken = client.post(
        f"/api/adventures/{adv['id']}/sim/action",
        json={"kind": "take", "item": "Corda"},
    )
    assert taken.status_code == 200, taken.text
    taken_again = client.post(
        f"/api/adventures/{adv['id']}/sim/action",
        json={"kind": "take", "item": "Corda"},
    )
    assert taken_again.status_code == 200, taken_again.text
    by_title = {quest["title"]: quest for quest in taken_again.json()["quests"]}
    assert by_title["Ferido"]["status"] == "available"
    assert by_title["Sem ouro"]["status"] == "available"
    assert by_title["Tem corda"]["status"] == "available"
    assert by_title["Tem corda"]["progress"]["value"] == 1
    cord = next(row for row in taken_again.json()["actor"]["inventory"] if row["name"] == "Corda")
    assert cord["qty"] == 2

    _quest(
        client, adv["id"],
        title="Na praça",
        status="hidden",
        triggers=[{
            "when": [{"fact": "location", "title": "Praça"}],
            "apply": [{"status": "available"}, {"progress_delta": 1}],
        }],
    )
    triggers.install_hooks()
    with db.session() as conn:
        beach = db.insert_card(conn, adv["id"], type="location", title="Praia", entry="areia")
        square = db.insert_card(conn, adv["id"], type="location", title="Praça", entry="feira")
        world_map.ensure_link(conn, adv["id"], beach["id"], square["id"])
        world_map.set_current_location(conn, adv["id"], beach["id"])
        world_map.travel(conn, adv["id"], square["id"])
        world_map.travel(conn, adv["id"], beach["id"])
        world_map.travel(conn, adv["id"], square["id"])
    listed = client.get(f"/api/adventures/{adv['id']}/sim/quests")
    square_quest = next(quest for quest in listed.json()["quests"] if quest["title"] == "Na praça")
    assert square_quest["status"] == "available"
    assert square_quest["progress"]["value"] == 1
    square_trigger = next(
        trigger for trigger in listed.json()["triggers"]
        if any(effect.get("quest_id") == square_quest["id"] for effect in trigger["apply"])
    )
    assert square_trigger["applied"] is True

    _quest(
        client, adv["id"],
        title="Confiança",
        status="hidden",
        triggers=[{
            "when": [{"fact": "relation", "card_id": "Mira", "stat": "trust", "op": "gte", "value": 48}],
            "apply": [{"status": "available"}],
        }],
    )
    with db.session() as conn:
        mira = db.insert_card(conn, adv["id"], type="character", title="Mira", entry="amiga")
        update = {"card_id": mira["id"], "target": "player", "delta": {"trust": 8}}
        changed = npc_bonds.apply_adjudication(conn, adv["id"], {
            "relation_updates": [update],
        }, 1, player_text="obrigado Mira", actor_name="Hero")
        assert len(changed) == 1
        changed_again = npc_bonds.apply_adjudication(conn, adv["id"], {
            "relation_updates": [update],
        }, 2, player_text="obrigado Mira", actor_name="Hero")
        assert len(changed_again) == 1
    listed = client.get(f"/api/adventures/{adv['id']}/sim/quests")
    trust = next(quest for quest in listed.json()["quests"] if quest["title"] == "Confiança")
    assert trust["status"] == "available"
    with db.session() as conn:
        reloaded = db.get_adventure(conn, adv["id"])["sim_state"]
    assert any(quest["title"] == "Confiança" and quest["status"] == "available" for quest in reloaded["quests"])


def test_advance_evaluates_an_existing_relation(client):
    adv = make_adventure(title="Relógio", owner=TEST_AUTH_USER)
    _pack(client, adv["id"])
    enabled = client.put(f"/api/adventures/{adv['id']}/sim/life", json={"enabled": True})
    assert enabled.status_code == 200, enabled.text
    _quest(
        client, adv["id"],
        title="Amizade",
        status="available",
        triggers=[{
            "when": [{"fact": "relation", "a": "ana", "b": "bia", "op": "gte", "value": 4}],
            "apply": [{"status": "accepted"}],
        }],
    )
    with db.session() as conn:
        sim = state.load(db.get_adventure(conn, adv["id"]))
        sim["life"]["relations"] = {"ana|bia": 4}
        db.set_sim_state(conn, adv["id"], sim)
    advanced = client.post(
        f"/api/adventures/{adv['id']}/sim/life/advance",
        json={"minutes": 30, "activity": "social"},
    )
    assert advanced.status_code == 200, advanced.text
    quest = next(row for row in advanced.json()["quests"] if row["title"] == "Amizade")
    assert quest["status"] == "accepted"
    again = client.post(
        f"/api/adventures/{adv['id']}/sim/life/advance",
        json={"minutes": 30, "activity": "social"},
    )
    assert again.status_code == 200, again.text
    quest = next(row for row in again.json()["quests"] if row["title"] == "Amizade")
    assert quest["status"] == "accepted"


def test_invalid_trigger_is_422(client):
    adv = make_adventure(title="Ruim", owner=TEST_AUTH_USER)
    res = client.post(
        f"/api/adventures/{adv['id']}/sim/quests",
        json={"title": "Vazia", "triggers": [{"when": [{"fact": "item"}], "apply": [{"status": "available"}]}]},
    )
    assert res.status_code == 422
    assert res.json()["error"]["code"] == "invalid_trigger"


def test_direct_transition_rules():
    sim = state.empty()
    quest = quests.create(sim, {"title": "Cadeia", "status": "available"})
    quests.transition(sim, quest["id"], {"status": "accepted"})
    try:
        quests.transition(sim, quest["id"], {"status": "hidden"})
    except quests.QuestError as exc:
        assert exc.code == "invalid_transition"
    else:
        raise AssertionError("transição inválida")
    quests.transition(sim, quest["id"], {"status": "failed"})
    quests.transition(sim, quest["id"], {"status": "planned"})
    assert sim["quests"][0]["status"] == "planned"


def test_share_hides_secret_quests_and_triggers(client):
    adv = make_adventure(title="Véu", owner=TEST_AUTH_USER)
    specs = (
        ("Oculta", "hidden"),
        ("Planejada", "planned"),
        ("Aberta", "available"),
        ("Aceita", "accepted"),
        ("Feita", "completed"),
        ("Falhou", "failed"),
    )
    for title, status in specs:
        body = {"title": title, "status": status}
        if status == "available":
            body["triggers"] = [{
                "when": [{"fact": "hp", "op": "lte", "value": 2}],
                "apply": [{"status": "accepted"}],
            }]
        _quest(client, adv["id"], **body)

    def assert_owner(payload: dict) -> None:
        assert {row["title"] for row in payload["quests"]} == {title for title, _ in specs}
        assert any(row["status"] == "hidden" for row in payload["quests"])
        assert any(row["status"] == "planned" for row in payload["quests"])
        assert payload["triggers"]
        assert payload["triggers"][0]["when"][0]["fact"] == "hp"

    def assert_share(payload: dict) -> None:
        assert {row["status"] for row in payload["quests"]} == {
            "available", "accepted", "completed", "failed",
        }
        assert {row["title"] for row in payload["quests"]} == {
            "Aberta", "Aceita", "Feita", "Falhou",
        }
        assert payload["triggers"] == []

    for path in ("/sim/quests", "/sim"):
        seen = client.get(f"/api/adventures/{adv['id']}{path}")
        assert seen.status_code == 200, seen.text
        assert_owner(seen.json())

    with db.session() as conn:
        users.create_user(conn, "Mira", "mira-pass")
        db.share_adventure(conn, adv["id"], "Mira")
    _login(client, "Mira", "mira-pass")
    for path in ("/sim/quests", "/sim"):
        seen = client.get(f"/api/adventures/{adv['id']}{path}")
        assert seen.status_code == 200, seen.text
        assert_share(seen.json())
        blob = str(seen.json()["quests"]) + str(seen.json()["triggers"])
        assert "Oculta" not in blob
        assert "Planejada" not in blob
