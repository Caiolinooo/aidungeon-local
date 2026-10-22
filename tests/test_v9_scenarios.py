"""v0.9 — PATCH preserva os outros campos; DELETE desvincula a aventura."""

from __future__ import annotations


def _scenario_body() -> dict:
    return {
        "title": "Harbor Draft",
        "description": "A harbor city.",
        "ai_instructions": "Be terse.",
        "plot_essentials": "You are a dock thief.",
        "authors_note": "Rain.",
        "opening_text": "The tide is out.",
        "image_style": "ink sketch",
        "tags": ["sea"],
        "story_cards": [{
            "title": "Harbor",
            "type": "location",
            "triggers": ["harbor"],
            "entry": "Wet stone.",
        }],
        "third_person": True,
    }


def test_patch_scenario_preserves_other_fields(client):
    created = client.post("/api/scenarios", json=_scenario_body())
    assert created.status_code == 201, created.text
    scenario = created.json()

    patched = client.patch(
        f"/api/scenarios/{scenario['id']}", json={"title": "Renamed Harbor"},
    )
    assert patched.status_code == 200, patched.text
    body = patched.json()
    assert body["title"] == "Renamed Harbor"
    assert body["description"] == "A harbor city."
    assert body["ai_instructions"] == "Be terse."
    assert body["plot_essentials"] == "You are a dock thief."
    assert body["authors_note"] == "Rain."
    assert body["opening_text"] == "The tide is out."
    assert body["image_style"] == "ink sketch"
    assert body["tags"] == ["sea"]
    assert body["story_cards"][0]["title"] == "Harbor"
    assert body["story_cards"][0]["entry"] == "Wet stone."
    assert body["third_person"] is True

    got = client.get(f"/api/scenarios/{scenario['id']}")
    assert got.status_code == 200
    assert got.json()["title"] == "Renamed Harbor"
    assert got.json()["description"] == "A harbor city."
    assert got.json()["opening_text"] == "The tide is out."


def test_delete_scenario_then_get_404(client):
    created = client.post("/api/scenarios", json=_scenario_body())
    assert created.status_code == 201, created.text
    scenario_id = created.json()["id"]

    deleted = client.delete(f"/api/scenarios/{scenario_id}")
    assert deleted.status_code == 200, deleted.text
    assert client.get(f"/api/scenarios/{scenario_id}").status_code == 404


def test_delete_scenario_unlinks_adventure(client):
    created = client.post("/api/scenarios", json=_scenario_body())
    assert created.status_code == 201, created.text
    scenario = created.json()

    made = client.post("/api/adventures", json={"scenario_id": scenario["id"]})
    assert made.status_code == 201, made.text
    adventure = made.json()
    assert adventure["scenario_id"] == scenario["id"]
    assert adventure["title"] == "Harbor Draft"

    deleted = client.delete(f"/api/scenarios/{scenario['id']}")
    assert deleted.status_code == 200, deleted.text
    assert client.get(f"/api/scenarios/{scenario['id']}").status_code == 404

    got = client.get(f"/api/adventures/{adventure['id']}")
    assert got.status_code == 200, got.text
    body = got.json()
    assert body["scenario_id"] is None
    assert body["title"] == "Harbor Draft"
    assert body["actions"][0]["type"] == "start"
    assert body["actions"][0]["text"] == "The tide is out."


def test_create_scenario_requires_auth(tmp_db):
    from fastapi.testclient import TestClient

    from app.main import app
    with TestClient(app) as raw:
        res = raw.post("/api/scenarios", json=_scenario_body())
    assert res.status_code == 401
