"""Pesos de vínculo do NPC e o bloco que o narrador lê antes de escrever."""

from __future__ import annotations

import json

from app import context_builder as cb
from app import db, npc_assets, npc_bonds, npc_chronicle, settings_store

from conftest import add_action, make_adventure


def _update(card_id, target, delta, *, reason, evidence):
    return {
        "card_id": card_id,
        "target": target,
        "delta": delta,
        "reason": reason,
        "evidence": evidence,
    }


def test_material_deltas_persist_and_words_alone_do_not(tmp_db):
    adv = make_adventure(title="Bonds")
    with db.session() as conn:
        mira = db.insert_card(
            conn, adv["id"], title="Mira", type="character",
            triggers=["Mira"], entry="Guarda.")
        kind = npc_bonds.apply_adjudication(conn, adv["id"], {
            "relation_updates": [_update(
                mira["id"], "player",
                {"trust": 4, "relationship": 3, "fear": -2},
                reason="ela aceita a ajuda",
                evidence="obrigado pela ajuda",
            )],
        }, seq=1, player_text="Eu agradeço a ajuda da Mira")
        sheet = npc_assets.parse_sheet(db.get_card(conn, adv["id"], mira["id"])["sheet"])
    assert kind[0]["delta"] == {"trust": 4, "relationship": 3, "fear": -2}
    assert sheet["bonds"]["trust"] == npc_bonds.DEFAULTS["trust"] + 4
    assert sheet["bonds"]["relationship"] == npc_bonds.DEFAULTS["relationship"] + 3
    assert sheet["bonds"]["fear"] == npc_bonds.DEFAULTS["fear"] - 2
    with db.session() as conn:
        untouched = npc_bonds.apply_adjudication(conn, adv["id"], {
            "relation_updates": [_update(
                mira["id"], "player", {"trust": 8},
                reason="conversa fiada", evidence="oi, tudo bem?",
            )],
        }, seq=2, player_text="Oi Mira, tudo bem?")
        again = npc_assets.parse_sheet(db.get_card(conn, adv["id"], mira["id"])["sheet"])
    assert untouched == []
    assert again["bonds"]["trust"] == sheet["bonds"]["trust"]


def test_deltas_clamp_to_the_scale_and_to_the_turn(tmp_db):
    adv = make_adventure(title="Teto")
    with db.session() as conn:
        high = db.insert_card(
            conn, adv["id"], title="Alta", type="character", entry="A.",
            sheet=json.dumps({"bonds": {"trust": 98}}))
        mid = db.insert_card(
            conn, adv["id"], title="Meio", type="character", entry="B.",
            sheet=json.dumps({"bonds": {"trust": 40}}))
        low = db.insert_card(
            conn, adv["id"], title="Baixa", type="character", entry="C.",
            sheet=json.dumps({"bonds": {"trust": 3}}))
        npc_bonds.apply_adjudication(conn, adv["id"], {"relation_updates": [
            _update(high["id"], "player", {"trust": 50},
                     reason="ajuda", evidence="ela protegeu o grupo"),
            _update(mid["id"], "player", {"trust": 50},
                     reason="ajuda", evidence="ele salvou a criança"),
            _update(low["id"], "player", {"trust": -50},
                     reason="golpe", evidence="o soco quebrou o nariz"),
        ]}, seq=1, player_text="Eu ajudo e também soco")
        high_sheet = npc_assets.parse_sheet(db.get_card(conn, adv["id"], high["id"])["sheet"])
        mid_sheet = npc_assets.parse_sheet(db.get_card(conn, adv["id"], mid["id"])["sheet"])
        low_sheet = npc_assets.parse_sheet(db.get_card(conn, adv["id"], low["id"])["sheet"])
    assert high_sheet["bonds"]["trust"] == 100
    assert mid_sheet["bonds"]["trust"] == 60
    assert low_sheet["bonds"]["trust"] == 0


def test_only_the_named_npc_changes(tmp_db):
    adv = make_adventure(title="Bonds")
    with db.session() as conn:
        mira = db.insert_card(
            conn, adv["id"], title="Mira", type="character",
            triggers=["Mira"], entry="Guarda do portão.")
        tom = db.insert_card(
            conn, adv["id"], title="Tom", type="character",
            triggers=["Tom"], entry="Cozinheiro.")
        changed = npc_bonds.apply_adjudication(conn, adv["id"], {
            "relation_updates": [_update(
                mira["id"], "player", {"trust": 5},
                reason="ajuda", evidence="Mira agradece a proteção",
            )],
        }, seq=1, player_text="Eu protejo Mira")
        mira_sheet = npc_assets.parse_sheet(db.get_card(conn, adv["id"], mira["id"])["sheet"])
        tom_sheet = npc_assets.parse_sheet(db.get_card(conn, adv["id"], tom["id"])["sheet"])
    assert len(changed) == 1
    assert changed[0]["card_id"] == mira["id"]
    assert mira_sheet["bonds"]["trust"] == npc_bonds.DEFAULTS["trust"] + 5
    assert "bonds" not in tom_sheet


def test_a_refused_kiss_costs_trust_instead_of_desire(tmp_db):
    adv = make_adventure(title="Recusa")
    with db.session() as conn:
        card = db.insert_card(
            conn, adv["id"], title="Elara", type="character",
            triggers=["Elara"], entry="Uma mulher.")
        npc_bonds.apply_adjudication(conn, adv["id"], {
            "relation_updates": [_update(
                card["id"], "player",
                {"desire": 10, "trust": -6, "fear": 6},
                reason="ela recusa o beijo",
                evidence="Elara recusa e dá um passo para trás",
            )],
        }, seq=1, player_text="Eu beijo Elara")
        sheet = npc_assets.parse_sheet(db.get_card(conn, adv["id"], card["id"])["sheet"])
    assert sheet["bonds"]["desire"] == npc_bonds.DEFAULTS["desire"]
    assert sheet["bonds"]["fear"] == npc_bonds.DEFAULTS["fear"] + 6
    assert sheet["bonds"]["trust"] == npc_bonds.DEFAULTS["trust"] - 6


def test_moment_demands_a_visible_action():
    card = {"type": "character", "title": "Elara", "triggers": ["Elara"], "sheet": ""}
    block = npc_bonds.format_moment([card], "Elara está na pista.")
    assert "visible action" in block
    assert "stance resist" in block
    assert "do not instantly undress" in block


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
        npc_bonds.apply_adjudication(conn, adv["id"], {
            "relation_updates": [{
                "card_id": card["id"],
                "target": "player",
                "delta": {"fear": 8, "trust": -6},
                "reason": "ameaça",
                "evidence": "ele ameaçou matar",
            }],
        }, seq=2, player_text="I threaten to kill Mira")
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


def test_small_talk_and_proximity_leave_bonds_at_zero(tmp_db):
    adv = make_adventure(title="Papo")
    with db.session() as conn:
        mira = db.insert_card(
            conn, adv["id"], title="Mira", type="character", entry="Guarda.")
        changes = npc_bonds.apply_adjudication(conn, adv["id"], {
            "relation_updates": [
                _update(mira["id"], "player", {"trust": 5, "relationship": 3},
                         reason="cumprimento", evidence="oi, tudo bem?"),
                _update(mira["id"], "player", {"trust": 2},
                         reason="está por perto", evidence="Mira fica perto"),
            ],
        }, seq=1, player_text="Oi Mira, tudo bem?")
        sheet = npc_assets.parse_sheet(db.get_card(conn, adv["id"], mira["id"])["sheet"])
    assert changes == []
    assert "bonds" not in sheet


def test_material_harm_persists_as_a_small_delta(tmp_db):
    adv = make_adventure(title="Dano")
    with db.session() as conn:
        mira = db.insert_card(
            conn, adv["id"], title="Mira", type="character", entry="Guarda.")
        changes = npc_bonds.apply_adjudication(conn, adv["id"], {
            "reactions": [{
                "card_id": mira["id"],
                "npc": "Mira",
                "reaction": "Ela recua com a mão no rosto.",
                "emotion": "fear",
                "pose": "recoiling, hand on cheek",
            }],
            "relation_updates": [_update(
                mira["id"], "player", {"fear": 6, "trust": -4},
                reason="apanhou", evidence="o soco acertou o rosto",
            )],
        }, seq=4, player_text="Eu soco Mira")
        sheet = npc_assets.parse_sheet(db.get_card(conn, adv["id"], mira["id"])["sheet"])
    assert changes[0]["delta"] == {"fear": 6, "trust": -4}
    assert sheet["bonds"]["fear"] == npc_bonds.DEFAULTS["fear"] + 6
    assert sheet["bonds"]["trust"] == npc_bonds.DEFAULTS["trust"] - 4
    assert sheet["visual_state"] == {
        "action": "Ela recua com a mão no rosto.",
        "emotion": "fear",
        "pose": "recoiling, hand on cheek",
        "seq": 4,
    }
    assert sheet["expression"] == "afraid"
    assert sheet["bond_log"][-1]["seq"] == 4
    assert sheet["bond_log"][-1]["delta"] == {"fear": 6, "trust": -4}
    assert "soco" in sheet["bond_log"][-1]["evidence"]


def test_npc_to_npc_delta_is_directional(tmp_db):
    adv = make_adventure(title="Entre eles")
    with db.session() as conn:
        mira = db.insert_card(
            conn, adv["id"], title="Mira", type="character", entry="Guarda.")
        bram = db.insert_card(
            conn, adv["id"], title="Bram", type="character", entry="Mercador.")
        changes = npc_bonds.apply_adjudication(conn, adv["id"], {
            "relation_updates": [_update(
                mira["id"], bram["id"],
                {"trust": -5, "fear": 4, "affinity": -6, "relationship": 9},
                reason="Bram empurrou Mira",
                evidence="Bram empurrou Mira contra a porta",
            )],
        }, seq=7, player_text="Eu observo")
        mira_sheet = npc_assets.parse_sheet(db.get_card(conn, adv["id"], mira["id"])["sheet"])
        bram_sheet = npc_assets.parse_sheet(db.get_card(conn, adv["id"], bram["id"])["sheet"])
    assert changes[0]["target"] == bram["id"]
    assert changes[0]["delta"] == {"trust": -5, "fear": 4, "affinity": -6}
    assert "relationship" not in changes[0]["delta"]
    relation = mira_sheet["relations"][bram["id"]]
    assert relation["trust"] == 35
    assert relation["fear"] == 14
    assert relation["affinity"] == 24
    assert relation["history"][-1].startswith("seq 7 ")
    assert "empurrou" in relation["history"][-1]
    assert "relations" not in bram_sheet
    assert "bonds" not in mira_sheet


def test_player_card_is_not_an_npc_update(tmp_db):
    adv = make_adventure(title="Jogador")
    with db.session() as conn:
        player = db.insert_card(
            conn, adv["id"], title="Caio", type="character",
            player_username="caio", entry="O protagonista.",
            sheet=json.dumps({"expression": "calm"}))
        mira = db.insert_card(
            conn, adv["id"], title="Mira", type="character", entry="Guarda.")
        changes = npc_bonds.apply_adjudication(conn, adv["id"], {
            "actor_pose": "fist raised",
            "relation_updates": [
                _update(player["id"], "player", {"trust": -10},
                         reason="golpe", evidence="o soco"),
                _update(mira["id"], player["id"], {"trust": -10},
                         reason="golpe", evidence="o soco"),
                _update(player["id"], mira["id"], {"affinity": -5},
                         reason="golpe", evidence="o soco"),
            ],
        }, seq=3, player_text="Eu soco Mira", actor_name="Caio")
        player_sheet = npc_assets.parse_sheet(db.get_card(conn, adv["id"], player["id"])["sheet"])
        mira_sheet = npc_assets.parse_sheet(db.get_card(conn, adv["id"], mira["id"])["sheet"])
    assert changes == []
    assert "bonds" not in player_sheet
    assert "relations" not in player_sheet
    assert "bonds" not in mira_sheet
    assert "relations" not in mira_sheet
    assert player_sheet["visual_state"]["action"] == "Eu soco Mira"
    assert player_sheet["visual_state"]["pose"] == "fist raised"
    assert player_sheet["visual_state"]["emotion"] == ""
    assert player_sheet["visual_state"]["seq"] == 3
    assert player_sheet["expression"] == "calm"


def test_public_payload_omits_secrets(tmp_db):
    adv = make_adventure(title="Segredo")
    secret = "Ela quer o trono em segredo e não pode dizer"
    with db.session() as conn:
        mira = db.insert_card(
            conn, adv["id"], title="Mira", type="character", entry="Guarda.")
        changes = npc_bonds.apply_adjudication(conn, adv["id"], {
            "cue": secret,
            "relation_updates": [{
                "card_id": mira["id"],
                "target": "player",
                "delta": {"trust": -4},
                "reason": secret,
                "evidence": "o soco no rosto",
                "hidden_motive": "usurpar o trono",
                "goals": "o plano secreto",
            }],
        }, seq=5, player_text="Eu soco Mira")
        sheet = npc_assets.parse_sheet(db.get_card(conn, adv["id"], mira["id"])["sheet"])
    assert set(changes[0]) == {"card_id", "name", "target", "delta", "reason", "evidence"}
    rendered = json.dumps(changes[0], ensure_ascii=False)
    assert "trono" not in rendered
    assert "usurpar" not in rendered
    assert "secreto" not in rendered
    assert changes[0]["evidence"] == "o soco no rosto"
    assert changes[0]["reason"] == ""
    assert "trono" not in json.dumps(sheet["bond_log"], ensure_ascii=False)


def test_alignment_follows_the_bonds_after_a_delta(tmp_db):
    adv = make_adventure(title="Lado")
    with db.session() as conn:
        mira = db.insert_card(
            conn, adv["id"], title="Mira", type="character", entry="Guarda.",
            sheet=json.dumps({"alignment": "neutro"}))
        npc_bonds.apply_adjudication(conn, adv["id"], {
            "relation_updates": [_update(
                mira["id"], "player", {"trust": 20, "relationship": 20},
                reason="ela lutou ao lado", evidence="Mira protegeu o jogador",
            )],
        }, seq=1, player_text="Eu luto com Mira")
        sheet = npc_assets.parse_sheet(db.get_card(conn, adv["id"], mira["id"])["sheet"])
    assert sheet["alignment"] == "aliado"
    assert sheet["bonds"]["trust"] == npc_bonds.DEFAULTS["trust"] + 20


def test_strip_leaked_moment():
    prose = "Mira recua.\n\n[NPC moment] Private cue: follow it, do not quote it.\nMira: emotion fear\n\nEla fecha a porta."
    assert npc_bonds.strip_leaked(prose) == "Mira recua.\n\nEla fecha a porta."
    assert npc_bonds.strip_leaked("Só prosa.") == "Só prosa."
