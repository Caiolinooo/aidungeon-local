"""Regeneração de ficha: merge por campo vazio, campo isolado, campo proibido."""

from __future__ import annotations

import json

from app import db
from conftest import make_adventure
from test_v5 import clear_override, make_chat_transport, override_transport


def _card(adventure_id: str, sheet: dict) -> dict:
    with db.session() as conn:
        return db.insert_card(
            conn, adventure_id, type="character", title="Ada Vane",
            entry="Sky captain.", sheet=json.dumps(sheet),
        )


def test_regenerate_all_keeps_filled_humor_and_fills_lifestyle(client):
    adv = make_adventure(title="Skyward", description="Airships.")
    card = _card(adv["id"], {
        "humor": "dry jokes about storms",
        "appearance": "ash braid",
        "notes": "fears lightning",
    })
    body = json.dumps({
        "personality": "loyal and sharp",
        "humor": "SHOULD NOT APPLY",
        "speech": "",
        "goals": "",
        "background": "",
        "lifestyle": "wakes on the bridge",
        "habits": "",
        "likes": "",
        "sentiments": "",
    })
    override_transport(client, make_chat_transport(body))
    try:
        resp = client.post(
            f"/api/adventures/{adv['id']}/cards/{card['id']}/regenerate",
            json={"scope": "all", "overwrite": False},
        )
    finally:
        clear_override(client)
    assert resp.status_code == 200, resp.text
    payload = resp.json()
    sheet = json.loads(payload["card"]["sheet"])
    assert sheet["humor"] == "dry jokes about storms"
    assert sheet["lifestyle"] == "wakes on the bridge"
    assert sheet["appearance"] == "ash braid"
    assert sheet["notes"] == "fears lightning"
    assert "humor" in payload["skipped"]
    assert "lifestyle" in payload["regenerated"]
    assert "personality" in payload["regenerated"]


def test_regenerate_entry_is_rejected(client):
    adv = make_adventure()
    card = _card(adv["id"], {"humor": "dry"})
    resp = client.post(
        f"/api/adventures/{adv['id']}/cards/{card['id']}/regenerate",
        json={"scope": "field", "field": "entry"},
    )
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "invalid_field"
    with db.session() as conn:
        stored = json.loads(db.get_card(conn, adv["id"], card["id"])["sheet"])
    assert stored["humor"] == "dry"


def test_regenerate_humor_replaces_the_old_value(client):
    adv = make_adventure()
    card = _card(adv["id"], {"humor": "dry jokes", "appearance": "ash braid"})
    override_transport(client, make_chat_transport(json.dumps({
        "value": "bites sarcasm at the crew",
    })))
    try:
        resp = client.post(
            f"/api/adventures/{adv['id']}/cards/{card['id']}/regenerate",
            json={"scope": "field", "field": "humor"},
        )
    finally:
        clear_override(client)
    assert resp.status_code == 200, resp.text
    sheet = json.loads(resp.json()["card"]["sheet"])
    assert sheet["humor"] == "bites sarcasm at the crew"
    assert sheet["appearance"] == "ash braid"
    assert resp.json()["regenerated"] == ["humor"]


def test_regenerate_empty_value_keeps_the_old_field(client):
    adv = make_adventure()
    card = _card(adv["id"], {"humor": "dry jokes"})
    override_transport(client, make_chat_transport(json.dumps({"value": "  "})))
    try:
        resp = client.post(
            f"/api/adventures/{adv['id']}/cards/{card['id']}/regenerate",
            json={"scope": "field", "field": "humor"},
        )
    finally:
        clear_override(client)
    assert resp.status_code == 200, resp.text
    sheet = json.loads(resp.json()["card"]["sheet"])
    assert sheet["humor"] == "dry jokes"
    assert resp.json()["skipped"] == ["humor"]


def test_regenerate_unknown_card_404(client):
    adv = make_adventure()
    resp = client.post(
        f"/api/adventures/{adv['id']}/cards/missing/regenerate",
        json={"scope": "field", "field": "humor"},
    )
    assert resp.status_code == 404
