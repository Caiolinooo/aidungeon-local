"""Eventos ao vivo de card, personagem, metadados e undo."""

from __future__ import annotations

import json

from app import db, events
from conftest import add_action, make_adventure
from test_v5 import clear_override, make_chat_transport, override_transport


def _pull(sub) -> list:
    found = []
    while True:
        event = sub.get(timeout=0)
        if event is None:
            break
        found.append(event)
    return found


def _card(adventure_id: str, **fields) -> dict:
    with db.session() as conn:
        return db.insert_card(
            conn, adventure_id,
            title=fields.pop("title", "Mira"),
            type=fields.pop("type", "character"),
            entry=fields.pop("entry", "uma maga"),
            sheet=fields.pop("sheet", ""),
        )


def test_create_update_delete_card_publish_card_changed(client):
    adv = make_adventure(title="Mesa")
    with events.subscribe(adv["id"]) as sub:
        created = client.post(
            f"/api/adventures/{adv['id']}/cards",
            json={"title": "Mira", "type": "character", "entry": "uma maga"},
        )
        assert created.status_code == 201, created.text
        card_id = created.json()["id"]
        updated = client.patch(
            f"/api/adventures/{adv['id']}/cards/{card_id}",
            json={"title": "Mira Voss"},
        )
        assert updated.status_code == 200, updated.text
        deleted = client.delete(f"/api/adventures/{adv['id']}/cards/{card_id}")
        assert deleted.status_code == 204, deleted.text
        got = _pull(sub)
    actions = [(event.type, event.data) for event in got]
    assert actions == [
        ("card_changed", {"card_id": card_id, "action": "created"}),
        ("card_changed", {"card_id": card_id, "action": "updated"}),
        ("card_changed", {"card_id": card_id, "action": "deleted"}),
    ]


def test_missing_card_does_not_publish(client):
    adv = make_adventure(title="Mesa")
    with events.subscribe(adv["id"]) as sub:
        missing = client.patch(
            f"/api/adventures/{adv['id']}/cards/nope",
            json={"title": "x"},
        )
        assert missing.status_code == 404
        gone = client.delete(f"/api/adventures/{adv['id']}/cards/nope")
        assert gone.status_code == 404
        assert _pull(sub) == []


def test_regenerate_publishes_regenerated(client):
    adv = make_adventure(title="Mesa")
    card = _card(adv["id"], sheet=json.dumps({"humor": "seco", "appearance": "tranca"}))
    override_transport(client, make_chat_transport(json.dumps({
        "value": "morde o sarcasmo",
    })))
    try:
        with events.subscribe(adv["id"]) as sub:
            resp = client.post(
                f"/api/adventures/{adv['id']}/cards/{card['id']}/regenerate",
                json={"scope": "field", "field": "humor"},
            )
            got = _pull(sub)
    finally:
        clear_override(client)
    assert resp.status_code == 200, resp.text
    assert [(event.type, event.data) for event in got] == [
        ("card_changed", {"card_id": card["id"], "action": "regenerated"}),
    ]


def test_regenerate_invalid_field_does_not_publish(client):
    adv = make_adventure(title="Mesa")
    card = _card(adv["id"])
    with events.subscribe(adv["id"]) as sub:
        resp = client.post(
            f"/api/adventures/{adv['id']}/cards/{card['id']}/regenerate",
            json={"scope": "field", "field": "entry"},
        )
        assert resp.status_code == 422
        assert resp.json()["error"]["code"] == "invalid_field"
        assert _pull(sub) == []


def test_put_character_publishes_username(client):
    adv = make_adventure(title="Mesa")
    with events.subscribe(adv["id"]) as sub:
        resp = client.put(
            f"/api/adventures/{adv['id']}/character",
            json={"title": "Kael", "sheet": {"appearance": "capa cinza"}},
        )
        assert resp.status_code == 200, resp.text
        got = _pull(sub)
    assert [(event.type, event.data) for event in got] == [
        ("character_changed", {"username": "tester"}),
    ]


def test_patch_adventure_publishes_changed_fields(client):
    adv = make_adventure(title="Mesa")
    with events.subscribe(adv["id"]) as sub:
        resp = client.patch(
            f"/api/adventures/{adv['id']}",
            json={"title": "Mesa nova", "turn_mode": True},
        )
        assert resp.status_code == 200, resp.text
        got = _pull(sub)
    assert len(got) == 1
    assert got[0].type == "adventure_updated"
    assert set(got[0].data["fields"]) == {"title", "turn_mode"}


def test_undo_publishes_after_commit(client):
    adv = make_adventure(title="Mesa")
    add_action(adv["id"], 0, "start", "Once.")
    add_action(adv["id"], 1, "say", "hi")
    with events.subscribe(adv["id"]) as sub:
        resp = client.post(f"/api/adventures/{adv['id']}/undo")
        assert resp.status_code == 200, resp.text
        got = _pull(sub)
    assert [(event.type, event.data) for event in got] == [("undo", {})]
    with db.session() as conn:
        actions = db.list_actions(conn, adv["id"])
    assert [action["type"] for action in actions] == ["start"]


def test_undo_conflict_does_not_publish(client):
    adv = make_adventure(title="Mesa")
    add_action(adv["id"], 0, "start", "Once.")
    with events.subscribe(adv["id"]) as sub:
        resp = client.post(f"/api/adventures/{adv['id']}/undo")
        assert resp.status_code == 409
        assert _pull(sub) == []
