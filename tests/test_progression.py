"""Progressão, skills, níveis e relações entre NPCs."""

from __future__ import annotations

from app import progression


def test_xp_unlocks_levels_and_skills():
    sheet = {}
    updated = progression.add_xp(sheet, 105)
    current = progression.progression_of(updated)
    assert current["level"] == 2
    assert current["xp"] == 105
    assert "persuade" in current["unlocked"]
    assert progression.can_use(updated, "persuade")
    assert not progression.can_use(updated, "absolute_command")


def test_high_level_unlocks_stronger_abilities():
    updated = progression.add_xp({}, 1300)
    current = progression.progression_of(updated)
    assert current["level"] == 14
    assert "absolute_command" in current["unlocked"]
    assert "reality_dominion" in current["unlocked"]
    assert "legendary_blow" in current["unlocked"]


def test_npc_relationships_are_persistent_and_directional():
    sheet = {}
    updated = progression.update_relation(
        sheet, "npc2", trust=20, fear=5, affinity=15, event="Ajudou na fuga",
    )
    relation = progression.relation_with(updated, "npc2")
    assert relation["trust"] == 60
    assert relation["fear"] == 15
    assert relation["affinity"] == 45
    assert relation["history"] == ["Ajudou na fuga"]
    other = progression.update_relation(updated, "npc2", trust=-10, event="Traiu")
    assert progression.relation_with(other, "npc2")["trust"] == 50
def test_relationship_context_describes_npc_links():
    import json
    cards = [
        {"id": "a", "title": "Alok", "sheet": "{}"},
        {"id": "b", "title": "Maria", "sheet": "{}"},
    ]
    cards[0]["sheet"] = json.dumps(
        progression.update_relation(cards[0]["sheet"], "b", trust=30, affinity=20, event="Parceria")
    )
    context = progression.relationship_context(cards)
    assert "Alok" in context
    assert "Maria" in context
    assert "Parceria" in context
