"""Fatos repetidos, fatos passageiros e corte por frase nas fichas e consequências."""

from __future__ import annotations

from app import consequences, db, npc_chronicle, scene, text_facts
from tests.conftest import make_adventure


def test_near_duplicates_are_the_same_fact():
    a = "The group perceives a golden spark dripping from the Fada's palm to illuminate the path."
    b = "The group perceives a golden spark dripping from the Fada's palm to illuminate the path ahead."
    assert text_facts.same_fact(a, b)
    assert text_facts.same_fact("Ele perdeu a espada.", "ele perdeu a espada")
    assert not text_facts.same_fact("Bruno is the blacksmith of the village.",
                                    "Marina is the mechanic of the village.")
    assert not text_facts.same_fact("He lost.", "He lost his sword in the river.")   # curta demais
    # uma palavra de diferença pode ser o fato oposto
    assert not text_facts.same_fact("Bruno hates Marina since the fire at the forge.",
                                    "Bruno loves Marina since the fire at the forge.")
    assert not text_facts.same_fact("", "x")


def test_clothing_and_posture_of_the_moment_are_not_sheet_facts():
    """A ficha do Bruno virou "He is wearing a grey wool coat. He is wearing a
    brown leather hat. He is walking ahead of the group. He is walking…"."""
    for text in ("He is wearing a grey wool coat over his undershirt.",
                 "He put on a grey wool coat.", "He is walking ahead of the group.",
                 "Ela está vestindo uma capa amarela.", "Ele tirou o chapéu.",
                 "She took off her boots."):
        assert text_facts.is_transient(text), text
    for text in ("He lost his left eye in the fight.", "Ela jurou lealdade ao rei.",
                 "Bruno is the village blacksmith."):
        assert not text_facts.is_transient(text), text
    notes = npc_chronicle._append_unique(
        "Bruno is the village blacksmith.",
        ["He is wearing a grey wool coat.", "He swore to protect Marina.",
         "He swore to protect Marina!", "He is walking ahead of the group."],
        800, keep="recent", skip_transient=True)
    assert notes == "Bruno is the village blacksmith. He swore to protect Marina."


def test_the_cap_cuts_whole_sentences():
    base = "Texto que o jogador escreveu no card. Segunda frase do jogador."
    facts = [f"Fato novo número {index} sobre a vida dele na aldeia." for index in range(40)]
    entry = text_facts.append(base, facts, 600, keep="head")
    assert len(entry) <= 600
    assert entry.startswith("Texto que o jogador escreveu no card.")     # o começo fica
    assert entry.endswith("Fato novo número 39 sobre a vida dele na aldeia.")   # e o mais novo
    assert all(part.endswith(".") for part in text_facts.sentences(entry))
    recent = text_facts.fit(" ".join(facts), 200)
    assert len(recent) <= 200 and recent.endswith("número 39 sobre a vida dele na aldeia.")
    assert "número 0 " not in recent


def test_a_new_faction_cannot_take_a_characters_name(tmp_db):
    """O registro pós-turno criou uma facção com o nome de um personagem."""
    adv = make_adventure(plot_essentials="[Name: Caio]")
    with db.session() as conn:
        db.insert_card(conn, adv["id"], type="character", title="Marina Teste",
                       triggers=["Marina"], entry="Mecânica.")
        pool: list[dict] = []
        assert npc_chronicle.apply_world(
            conn, adv["id"], pool, {"name": "Marina", "summary": "Um grupo."},
            "faction", set()) is None
        assert npc_chronicle.apply_world(
            conn, adv["id"], pool, {"name": "Os Vigias", "aliases": ["marina teste"],
                                    "summary": "Um grupo."}, "faction", set()) is None
        assert npc_chronicle.apply_world(
            conn, adv["id"], pool, {"name": "Guilda dos Ferreiros", "summary": "Um grupo."},
            "faction", set()) == "created"
        titles = [card["title"] for card in db.list_cards(conn, adv["id"])]
    assert titles.count("Marina") == 0 and "Guilda dos Ferreiros" in titles


def test_consequences_do_not_repeat_themselves():
    spark = "The group perceives a golden spark dripping from the Fada's palm to illuminate the path"
    adventure = {"ui_state": {
        "world_effects": [
            {"kind": "perception", "target": "everyone_present", "summary": spark + ".",
             "duration": "persistent"},
            {"kind": "perception", "target": "everyone_present", "summary": spark + " ahead.",
             "duration": "persistent"},
            {"kind": "injury", "target": "Bruno", "summary": "Bruno has a cut on the right forearm.",
             "duration": "persistent"},
        ],
        "public_facts": ["Bruno has a cut on the right forearm", "Marina carries the lantern."],
    }}
    text = consequences.effect_context(adventure)
    assert text.count("golden spark") == 1
    assert "path ahead" in text                      # fica a versão mais recente
    assert text.count("cut on the right forearm") == 1
    assert "Witnesses know: Marina carries the lantern." in text


def test_a_name_without_the_accent_is_the_same_person():
    assert scene._last_mention("Jose entra na sala e Marina o segue.", "José") == 0
    assert scene._last_mention("A Íris observa.", "Iris") == 2
    assert scene._last_mention("Josefina entra.", "José") == -1
