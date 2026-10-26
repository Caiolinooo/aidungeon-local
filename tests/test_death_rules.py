"""Morte persistente na narrativa e no elenco visual."""

from __future__ import annotations

import json

from app import death_rules, scene


def _card(title="Alok", status="dead"):
    return {
        "id": "c1",
        "title": title,
        "type": "character",
        "sheet": json.dumps({"status": status}),
    }


def test_dead_npc_cannot_speak_or_work():
    card = _card()
    text = "Alok ajusta os controles e sorri para você."
    assert death_rules.dead_acting_alive([card], text) == ["Alok"]


def test_corpse_description_is_allowed():
    card = _card()
    text = "O corpo sem cabeça de Alok permanece no chão."
    assert death_rules.dead_acting_alive([card], text) == []


def test_dead_npc_is_removed_from_scene_cast():
    dead = _card()
    alive = _card("Maria", "alive")
    alive["id"] = "c2"
    cast = scene._cast([dead, alive], "Alok e Maria estão na praça.")
    assert [card["title"] for card in cast] == ["Maria"]


def test_resurrection_requires_explicit_character_event():
    card = _card()
    assert death_rules.resurrection_established("Alok ressuscitou.", card) is True
    assert death_rules.resurrection_established("Alok continua trabalhando.", card) is False
    assert death_rules.resurrection_established("Outra pessoa ressuscitou.", card) is False
