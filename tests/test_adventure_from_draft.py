"""POST /api/adventures/from-draft grava cenário, aventura, ficha e ruleset juntos."""

from __future__ import annotations


def test_from_draft_creates_adventure_character_and_rules(client):
    rules = client.get("/api/rulesets")
    assert rules.status_code == 200, rules.text
    fate = next(row for row in rules.json() if row["name"] == "FATE Core")

    created = client.post("/api/adventures/from-draft", json={
        "draft": {
            "title": "Porto",
            "description": "Uma cidade de cais.",
            "opening_text": "A maré baixou.",
            "story_cards": [{
                "title": "Cais",
                "type": "location",
                "triggers": ["cais"],
                "entry": "Pedra molhada.",
            }],
            "third_person": True,
        },
        "character": {
            "title": "Lia",
            "sheet": {"appearance": "casaco escuro", "class": "ladina"},
        },
        "ruleset_id": fate["id"],
    })
    assert created.status_code == 201, created.text
    body = created.json()
    assert body["title"] == "Porto"
    assert body["character_rules"] == fate["rules"]
    assert body["scenario_id"]

    got = client.get(f"/api/adventures/{body['id']}")
    assert got.status_code == 200, got.text
    state = got.json()
    assert state["character_rules"] == fate["rules"]
    assert any(card["title"] == "Lia" for card in state["story_cards"])
    assert any(card["title"] == "Cais" for card in state["story_cards"])

    scenario = client.get(f"/api/scenarios/{state['scenario_id']}")
    assert scenario.status_code == 200, scenario.text
    assert scenario.json()["id"] == state["scenario_id"]
    assert scenario.json()["title"] == "Porto"


def test_from_draft_requires_auth(tmp_db):
    from fastapi.testclient import TestClient

    from app.main import app
    with TestClient(app) as raw:
        res = raw.post("/api/adventures/from-draft", json={"draft": {"title": "X"}})
    assert res.status_code == 401
