"""Tests ensuring all domain mutations emit durable world_events."""

from __future__ import annotations

import pytest
from app import db
from conftest import make_adventure


def test_cards_and_memory_emit_world_events(client):
    adv = make_adventure(title="Event coverage")
    adv_id = adv["id"]

    # 1. Create Story Card
    res = client.post(f"/api/adventures/{adv_id}/cards", json={
        "title": "Old Mill",
        "type": "location",
        "entry": "A creaky old windmill on the hill.",
        "triggers": ["mill", "windmill"]
    })
    assert res.status_code == 201, res.text
    card_id = res.json()["id"]

    # 2. Update Story Card
    res = client.patch(f"/api/adventures/{adv_id}/cards/{card_id}", json={
        "entry": "A burnt windmill on the hill."
    })
    assert res.status_code == 200, res.text

    # 3. Bulk Update
    res = client.post(f"/api/adventures/{adv_id}/cards/bulk", json={
        "action": "pin",
        "card_ids": [card_id]
    })
    assert res.status_code == 200, res.text

    # 4. Memory / Plot Essentials
    res = client.put(f"/api/adventures/{adv_id}/memory", json={
        "plot_essentials": "Caio is seeking ancient artifacts.",
        "authors_note": "Grimdark tone"
    })
    assert res.status_code == 200, res.text

    # 5. Delete Card
    res = client.delete(f"/api/adventures/{adv_id}/cards/{card_id}")
    assert res.status_code == 204, res.text

    # Verify world_events table has recorded durable events
    with db.session() as conn:
        events = [
            row["type"] for row in conn.execute(
                "SELECT type FROM world_events WHERE adventure_id=? ORDER BY event_id ASC",
                (adv_id,)
            ).fetchall()
        ]

    assert "card_created" in events
    assert "card_updated" in events
    assert "plot_updated" in events
    assert "card_deleted" in events
