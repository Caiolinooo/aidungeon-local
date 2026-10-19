"""Pesos de vínculo do NPC e o bloco que o narrador lê antes de escrever."""

from __future__ import annotations

import json

from app import context_builder as cb
from app import db, npc_assets, npc_bonds, npc_chronicle, settings_store

from conftest import add_action, make_adventure


def test_kindness_threat_and_gift_move_the_bonds():
    base = npc_bonds.read_bonds({})
    kind = npc_bonds.apply_deltas(base, "I thank you and comfort you")
    assert kind["trust"] > base["trust"]
    assert kind["relationship"] > base["relationship"]
    assert kind["fear"] < base["fear"]

    threat = npc_bonds.apply_deltas(base, "I threaten to kill you")
    assert threat["fear"] > base["fear"]
    assert threat["trust"] < base["trust"]
    assert threat["relationship"] < base["relationship"]

    gift = npc_bonds.apply_deltas(base, "I give you a gift")
    assert gift["desire"] > base["desire"]
    assert gift["relationship"] > base["relationship"]


def test_deltas_clamp_to_the_scale():
    high = npc_bonds.read_bonds({"bonds": {"trust": 98}})
    assert npc_bonds.apply_deltas(high, "thank you")["trust"] == 100
    low = npc_bonds.read_bonds({"bonds": {"trust": 3}})
    assert npc_bonds.apply_deltas(low, "I threaten to kill you")["trust"] == 0
    assert npc_bonds.apply_deltas(npc_bonds.read_bonds({}), "the door is shut")["trust"] == 40


def test_only_the_named_npc_changes(tmp_db):
    adv = make_adventure(title="Bonds")
    with db.session() as conn:
        mira = db.insert_card(
            conn, adv["id"], title="Mira", type="character",
            triggers=["Mira"], entry="Guarda do portão.")
        tom = db.insert_card(
            conn, adv["id"], title="Tom", type="character",
            triggers=["Tom"], entry="Cozinheiro.")
        changed = npc_bonds.apply_turn(
            conn, adv["id"], "I thank Mira for the help",
            "Tom stays by the stove and Mira nods.")
        assert changed == 1
        mira_sheet = npc_assets.parse_sheet(db.get_card(conn, adv["id"], mira["id"])["sheet"])
        tom_sheet = npc_assets.parse_sheet(db.get_card(conn, adv["id"], tom["id"])["sheet"])
    assert mira_sheet["bonds"]["trust"] > npc_bonds.DEFAULTS["trust"]
    assert "bonds" not in tom_sheet


def test_stance_follows_the_numbers():
    def bonds(**over):
        data = dict(npc_bonds.DEFAULTS)
        data.update(over)
        return data

    assert npc_bonds.stance_of(bonds()) == "resist"
    assert npc_bonds.stance_of(bonds(trust=70, relationship=60, fear=10)) == "help"
    assert npc_bonds.stance_of(bonds(fear=75, boldness=30)) == "flee"
    assert npc_bonds.stance_of(bonds(trust=10, boldness=70, relationship=20, fear=10)) == "attack"
    assert npc_bonds.stance_of(bonds(desire=80, trust=50, fear=10)) == "flirt"


def test_moment_is_only_in_the_final_user_message(tmp_db):
    adv = make_adventure(title="Momento", authors_note="NPCs têm vontade própria.")
    add_action(adv["id"], 0, "start", "Mira waits by the gate.")
    add_action(adv["id"], 1, "do", "wave at Mira")
    add_action(adv["id"], 2, "continue", "Mira waves back, afraid.")
    with db.session() as conn:
        db.insert_card(
            conn, adv["id"], title="Mira", type="character",
            triggers=["Mira"], entry="Guarda do portão.",
            sheet=json.dumps({"bonds": {"fear": 80, "boldness": 20, "trust": 20}}),
        )
        actions = db.list_actions(conn, adv["id"])
        cfg = settings_store.runtime_config(conn)
        early = cb.build_context(adv, actions[:2], cfg, conn)
        built = cb.build_context(adv, actions, cfg, conn)

    # A user final antiga não vira mensagem da janela: o marcador fica só no fim.
    assert npc_bonds.MOMENT_MARKER in early.messages[-1]["content"]
    assert built.messages[:-1] == [
        early.messages[0],
        {"role": "user", "content": "Mira waits by the gate."},
        {"role": "user", "content": "\n> You wave at Mira\n"},
    ]
    for message in built.messages[:-1]:
        assert npc_bonds.MOMENT_MARKER not in message["content"]
    final = built.messages[-1]["content"]
    assert npc_bonds.MOMENT_MARKER in final
    assert "Mira:" in final
    assert "stance flee" in final
    assert "emotion fear" in final
    # O prefixo estável (system + janela) não carrega o bloco.
    assert npc_bonds.MOMENT_MARKER not in built.messages[0]["content"]
    window = "".join(message["content"] for message in built.messages[1:-1])
    assert "Mira waits by the gate." in window
    assert npc_bonds.MOMENT_MARKER not in window


def test_next_turn_keeps_the_prefix_when_bonds_change(tmp_db):
    adv = make_adventure(title="Prefixo")
    add_action(adv["id"], 0, "start", "Mira stands in the yard.")
    add_action(adv["id"], 1, "do", "look at Mira")
    with db.session() as conn:
        card = db.insert_card(
            conn, adv["id"], title="Mira", type="character",
            triggers=["Mira"], entry="Guarda.")
        actions = db.list_actions(conn, adv["id"])
        cfg = settings_store.runtime_config(conn)
        before = cb.build_context(adv, actions, cfg, conn)
        npc_bonds.apply_turn(conn, adv["id"], "I threaten to kill Mira", "Mira steps back.")
        after = cb.build_context(adv, actions, cfg, conn)
        sheet = npc_assets.parse_sheet(db.get_card(conn, adv["id"], card["id"])["sheet"])
    assert before.messages[:-1] == after.messages[:-1]
    assert npc_bonds.MOMENT_MARKER in after.messages[-1]["content"]
    assert sheet["bonds"]["fear"] > npc_bonds.DEFAULTS["fear"]
    assert sheet["bonds"]["trust"] < npc_bonds.DEFAULTS["trust"]


def test_enrich_keeps_bonds():
    previous = {"appearance": "dark hair", "bonds": {"trust": 77, "fear": 10}}
    sheet = npc_assets._normalize_sheet(
        {"appearance": "other face", "personality": "quiet"}, previous)
    assert sheet["bonds"]["trust"] == 77
    assert sheet["appearance"] == "dark hair"


def test_chronicle_keeps_bonds(tmp_db):
    adv = make_adventure(title="Cronica")
    with db.session() as conn:
        card = db.insert_card(
            conn, adv["id"], title="Mira", type="character",
            triggers=["Mira"], entry="Guarda.",
            sheet=json.dumps({
                "notes": "guarda o portão",
                "bonds": {"trust": 70, "relationship": 40, "fear": 15,
                          "desire": 15, "boldness": 40},
            }),
        )
        cards = db.list_cards(conn, adv["id"])
        kind = npc_chronicle.apply_character(
            conn, adv["id"], cards,
            {"name": "Mira", "facts": ["viu o portao fechar a noite"]},
            set(),
        )
        assert kind == "updated"
        sheet = npc_assets.parse_sheet(db.get_card(conn, adv["id"], card["id"])["sheet"])
    assert sheet["bonds"]["trust"] == 70


def test_strip_leaked_moment():
    prose = "Mira recua.\n\n[NPC moment] Private cue: follow it, do not quote it.\nMira: emotion fear\n\nEla fecha a porta."
    assert npc_bonds.strip_leaked(prose) == "Mira recua.\n\nEla fecha a porta."
    assert npc_bonds.strip_leaked("Só prosa.") == "Só prosa."
