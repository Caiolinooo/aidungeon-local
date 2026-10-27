"""Inícios nomeados: legado vira o padrão, a escolha entra na action start, e a ordem persiste."""

from __future__ import annotations

import json
import sqlite3


def _scenario_body() -> dict:
    return {
        "title": "Harbor Draft",
        "description": "A harbor city.",
        "opening_text": "The tide is out.",
    }


def _two_starts() -> list[dict]:
    return [
        {"id": "dock", "title": "Dock", "text": "The tide is out."},
        {"id": "alley", "title": "Alley", "text": "You wake in an alley."},
    ]


def test_legacy_opening_migrates_to_default_start(client, tmp_db):
    created = client.post("/api/scenarios", json=_scenario_body())
    assert created.status_code == 201, created.text
    scenario_id = created.json()["id"]

    conn = sqlite3.connect(tmp_db)
    conn.execute("UPDATE scenarios SET story_starts='[]' WHERE id=?", (scenario_id,))
    conn.commit()
    conn.close()

    from app import db
    db.init_db()

    conn = sqlite3.connect(tmp_db)
    row = conn.execute(
        "SELECT opening_text, story_starts FROM scenarios WHERE id=?",
        (scenario_id,),
    ).fetchone()
    conn.close()
    assert row[0] == "The tide is out."
    stored = json.loads(row[1])
    assert stored[0]["title"] == "Opening"
    assert stored[0]["text"] == "The tide is out."

    got = client.get(f"/api/scenarios/{scenario_id}")
    assert got.status_code == 200, got.text
    body = got.json()
    assert body["opening_text"] == "The tide is out."
    assert body["story_starts"][0]["id"] == stored[0]["id"]
    assert body["story_starts"][0]["text"] == "The tide is out."

    made = client.post("/api/adventures", json={"scenario_id": scenario_id})
    assert made.status_code == 201, made.text
    assert made.json()["actions"][0]["type"] == "start"
    assert made.json()["actions"][0]["text"] == "The tide is out."


def test_adventure_uses_non_default_start_exactly(client):
    created = client.post("/api/scenarios", json={
        **_scenario_body(),
        "story_starts": _two_starts(),
    })
    assert created.status_code == 201, created.text
    scenario = created.json()
    assert scenario["opening_text"] == "The tide is out."
    assert [item["id"] for item in scenario["story_starts"]] == ["dock", "alley"]

    chosen = client.post("/api/adventures", json={
        "scenario_id": scenario["id"],
        "start_id": "alley",
    })
    assert chosen.status_code == 201, chosen.text
    assert chosen.json()["actions"][0]["type"] == "start"
    assert chosen.json()["actions"][0]["text"] == "You wake in an alley."

    default = client.post("/api/adventures", json={
        "scenario_id": scenario["id"],
        "start_id": "dock",
    })
    assert default.status_code == 201, default.text
    assert default.json()["actions"][0]["text"] == "The tide is out."

    omitted = client.post("/api/adventures", json={"scenario_id": scenario["id"]})
    assert omitted.status_code == 201, omitted.text
    assert omitted.json()["actions"][0]["text"] == "The tide is out."

    missing = client.post("/api/adventures", json={
        "scenario_id": scenario["id"],
        "start_id": "missing",
    })
    assert missing.status_code == 400, missing.text
    assert missing.json()["error"]["code"] == "unknown_start"


def test_story_starts_persist_reorder_and_clone(client):
    created = client.post("/api/scenarios", json={
        **_scenario_body(),
        "story_starts": _two_starts(),
    })
    assert created.status_code == 201, created.text
    scenario_id = created.json()["id"]
    reordered = [
        {"id": "alley", "title": "Alley", "text": "You wake in an alley."},
        {"id": "dock", "title": "Dock", "text": "The tide is out."},
    ]
    patched = client.patch(f"/api/scenarios/{scenario_id}", json={"story_starts": reordered})
    assert patched.status_code == 200, patched.text
    assert [item["text"] for item in patched.json()["story_starts"]] == [
        "You wake in an alley.",
        "The tide is out.",
    ]
    assert patched.json()["opening_text"] == "You wake in an alley."

    removed = client.patch(f"/api/scenarios/{scenario_id}", json={
        "story_starts": [reordered[0]],
    })
    assert removed.status_code == 200, removed.text
    assert [item["id"] for item in removed.json()["story_starts"]] == ["alley"]

    got = client.get(f"/api/scenarios/{scenario_id}")
    assert got.status_code == 200
    assert got.json()["story_starts"] == removed.json()["story_starts"]
    assert got.json()["opening_text"] == "You wake in an alley."
    assert got.json()["description"] == "A harbor city."

    cloned = client.post("/api/scenarios", json={
        **got.json(),
        "title": "Harbor Draft (copy)",
        "id": None,
    })
    assert cloned.status_code == 201, cloned.text
    copy = cloned.json()
    assert copy["id"] != scenario_id
    assert [(item["id"], item["title"], item["text"]) for item in copy["story_starts"]] == [
        ("alley", "Alley", "You wake in an alley."),
    ]
    assert copy["opening_text"] == "You wake in an alley."


def test_story_starts_reject_blank_and_duplicate(client):
    blank_title = client.post("/api/scenarios", json={
        **_scenario_body(),
        "story_starts": [{"title": "  ", "text": "Hello"}],
    })
    assert blank_title.status_code == 422, blank_title.text

    blank_text = client.post("/api/scenarios", json={
        **_scenario_body(),
        "story_starts": [{"title": "Hello", "text": "   "}],
    })
    assert blank_text.status_code == 422, blank_text.text

    duplicated = client.post("/api/scenarios", json={
        **_scenario_body(),
        "story_starts": [
            {"id": "same", "title": "One", "text": "Alpha"},
            {"id": "same", "title": "Two", "text": "Beta"},
        ],
    })
    assert duplicated.status_code == 422, duplicated.text

    created = client.post("/api/scenarios", json={
        **_scenario_body(),
        "story_starts": _two_starts(),
    })
    assert created.status_code == 201, created.text
    cleared = client.patch(
        f"/api/scenarios/{created.json()['id']}",
        json={"opening_text": "   "},
    )
    assert cleared.status_code == 400, cleared.text
    assert cleared.json()["error"]["code"] == "invalid_story_start"


def test_from_draft_keeps_chosen_start(client):
    created = client.post("/api/adventures/from-draft", json={
        "draft": {
            "title": "Porto",
            "opening_text": "ignored when starts exist",
            "story_starts": _two_starts(),
        },
        "start_id": "alley",
    })
    assert created.status_code == 201, created.text
    state = created.json()
    assert state["actions"][0]["text"] == "You wake in an alley."
    scenario = client.get(f"/api/scenarios/{state['scenario_id']}")
    assert scenario.status_code == 200, scenario.text
    body = scenario.json()
    assert body["opening_text"] == "The tide is out."
    assert [item["id"] for item in body["story_starts"]] == ["dock", "alley"]
