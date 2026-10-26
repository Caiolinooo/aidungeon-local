"""Consequências persistentes, obediência e memória social."""

from __future__ import annotations

import json

from app import consequences, db, npc_bonds, world_facts


def test_compelled_servant_is_not_treated_as_rebellious():
    card = {
        "title": "Alok",
        "type": "character",
        "entry": "DJ e lacaio do protagonista.",
        "sheet": json.dumps({
            "personality": "obediente à instrução mental de Caio",
            "bonds": {"relationship": 6, "trust": 0, "distrust": 68,
                      "fear": 77, "desire": 15, "boldness": 56},
        }),
    }
    assert world_facts.obedience_of(card) == "obedient_compelled"
    assert world_facts._yields("Alok", card, "") is True
    bonds = npc_bonds.read_bonds({"bonds": {
        "relationship": 6, "trust": 0, "distrust": 68,
        "fear": 77, "desire": 15, "boldness": 56,
    }})
    assert npc_bonds.stance_of(bonds, obedience=world_facts.obedience_of(card)) == "help"


def test_reality_and_perception_effects_feed_images_and_adjudication():
    adventure = {
        "ui_state": {
            "world_effects": [
                {"kind": "reality", "target": "world",
                 "summary": "A praça virou um deserto de vidro.",
                 "visual_en": "a glass desert replacing the square"},
                {"kind": "perception", "target": "Alok",
                 "summary": "Alok acredita que Caio é um aliado.",
                 "visual_en": ""},
            ],
            "public_facts": ["Todos viram a praça mudar."],
            "private_facts": [{"npc": "Alok", "fact": "Ele foi instruído mentalmente."}],
        }
    }
    context = consequences.effect_context(adventure)
    assert "glass" not in context or "desert" in context
    assert "praça mudar" in context
    assert "Alok acredita" in consequences.effect_context(adventure, target="Alok")
    assert consequences.visual_context(adventure) == "a glass desert replacing the square"
    assert consequences.private_facts_for(adventure, "Alok") == ["Ele foi instruído mentalmente."]


def test_private_facts_are_saved_as_npc_memories(tmp_db):
    with db.session() as conn:
        adv = db.insert_adventure(conn, title="Costazul")
        card = db.insert_card(conn, adv["id"], title="Alok", type="character")
        result = consequences.apply_evaluation(conn, adv["id"], {
            "effects": [{
                "kind": "perception", "target": "Alok",
                "summary": "Alok esqueceu a traição.",
                "visual_en": "",
            }],
            "public_facts": ["Alok foi visto obedecendo."],
            "private_facts": [{"npc": "Alok", "fact": "Ele esqueceu a traição."}],
        })
        assert result["effects"] == 1
        memories = db.list_npc_memories(conn, adv["id"], card["id"])
        assert any("esqueceu" in row["text"] for row in memories)
        fresh = db.get_adventure(conn, adv["id"])
        assert consequences.active_effects(fresh, target="Alok")
        assert consequences.private_facts_for(fresh, "Alok") == ["Ele esqueceu a traição."]
