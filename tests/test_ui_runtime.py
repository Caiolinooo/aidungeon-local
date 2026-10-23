"""Runtime do painel dinâmico: HUD, ações contextuais e valores por turno."""

from __future__ import annotations

import copy
import json

from app import db, game_engine, npc_chronicle, ui_runtime
from conftest import add_action, make_adventure

PROFILE = {
    "tabs": [{"id": "do", "label": "Ação"}],
    "widgets": [
        {"id": "stats", "type": "stats", "label": "Stats", "stats": [
            {"name": "HP", "value": 20, "max": 20, "min": 0},
            {"name": "Mana", "value": 5, "max": 10, "min": 0},
        ]},
        {"id": "bag", "type": "inventory", "label": "Bolsa",
         "items": [{"name": "Corda", "qty": 1}]},
        {"id": "sheet", "type": "character", "label": "Ficha",
         "fields": [{"name": "Objetivo", "value": "Sobreviver"}]},
    ],
}


def test_normalize_hud_and_actions_caps_and_dedup():
    hud = ui_runtime.normalize_hud([
        {"label": "Local", "value": "Sala", "tone": "info"},
        {"label": "local", "value": "Duplicado"},
        {"label": "", "value": "sem nome"},
        *[{"label": f"F{i}", "value": "x"} for i in range(10)],
    ])
    assert len(hud) == ui_runtime.MAX_HUD
    assert hud[0] == {"label": "Local", "value": "Sala", "tone": "info"}
    assert [item["label"] for item in hud].count("Local") == 1

    actions = ui_runtime.normalize_actions([
        {"label": "Perguntar", "type": "say", "text": "Pergunto sobre a marca."},
        {"type": "invalid", "text": "Observo a porta."},
        {"type": "do", "text": "  Observo a porta.  "},
        {"type": "do", "text": ""},
    ])
    assert actions[0]["type"] == "say"
    assert actions[1] == {"label": "Observo a porta.", "type": "do",
                          "text": "Observo a porta."}
    assert len(actions) == 2


def test_seed_only_keeps_non_empty_runtime():
    assert ui_runtime.seed({"widgets": [], "hud": [], "actions": []}) == {}
    seeded = ui_runtime.seed({
        "hud": [{"label": "Clima", "value": "Chuva"}],
        "actions": [{"label": "Correr", "type": "do", "text": "Corro."}],
    })
    assert seeded[ui_runtime.HUD_KEY][0]["label"] == "Clima"
    assert seeded[ui_runtime.ACTIONS_KEY][0]["text"] == "Corro."


def test_apply_stats_inventory_fields_hud_and_actions():
    state = ui_runtime.seed(PROFILE) | {
        "stats": {"HP": 20, "Mana": 5},
        "bag": [{"name": "Corda", "qty": 1}],
        "sheet": {"Objetivo": "Sobreviver"},
    }
    original = copy.deepcopy(state)
    updated = ui_runtime.apply(PROFILE, state, {
        "hud": [{"label": "Local", "value": "Sala dos Girinos", "tone": "info"}],
        "actions": [{"label": "Perguntar", "type": "say", "text": "Pergunto."}],
        "stats": [{"name": "HP", "value": 99}, {"name": "Mana", "value": -4},
                  {"name": "Inexistente", "value": 1}],
        "inventory": [
            {"op": "remove", "name": "Corda", "qty": 1},
            {"op": "add", "name": "Tocha", "qty": 2},
        ],
        "fields": [{"name": "Objetivo", "value": "Escapar"}],
    }, seq=7)
    assert state == original  # nunca muta o estado recebido
    assert updated["stats"] == {"HP": 20, "Mana": 0}
    assert updated["bag"] == [{"name": "Tocha", "qty": 2}]
    assert updated["sheet"] == {"Objetivo": "Escapar"}
    assert updated[ui_runtime.HUD_KEY][0]["value"] == "Sala dos Girinos"
    assert updated[ui_runtime.ACTIONS_KEY][0]["label"] == "Perguntar"
    assert updated[ui_runtime.SEQ_KEY] == 7


def test_prompt_context_and_render_player_state_hide_reserved_keys():
    state = {
        "stats": {"HP": 7},
        "bag": [{"name": "Tocha", "qty": 2}],
        ui_runtime.HUD_KEY: [{"label": "Local", "value": "Caverna"}],
        ui_runtime.ACTIONS_KEY: [{"label": "Sair", "type": "do", "text": "Saio."}],
        ui_runtime.SEQ_KEY: 3,
    }
    payload = json.loads(ui_runtime.prompt_context(PROFILE, state))
    assert payload["hud"][0]["label"] == "Local"
    assert payload["actions"][0]["type"] == "do"
    assert payload["widgets"][0]["state"] == {"HP": 7}

    rendered = ui_runtime.prompt_context(PROFILE, state, limit=10)
    assert len(rendered) <= 10

    from app import context_builder as cb
    player = cb.render_player_state(state)
    assert "stats: HP 7" in player
    assert "_hud" not in player
    assert "_actions" not in player
    assert "_seq" not in player


def test_chronicle_applies_ui_and_undo_restores_it(tmp_db):
    adv = make_adventure(plot_essentials="[Name: Caio]")
    action = add_action(adv["id"], 1, "continue", "Caio abre a porta.")
    with db.session() as conn:
        db.update_adventure_ui_profile(conn, adv["id"], PROFILE)
        before = {
            "stats": {"HP": 20, "Mana": 5},
            "bag": [{"name": "Corda", "qty": 1}],
            "sheet": {"Objetivo": "Sobreviver"},
            ui_runtime.HUD_KEY: [{"label": "Local", "value": "Corredor", "tone": "neutral"}],
            ui_runtime.ACTIONS_KEY: [{"label": "Abrir", "type": "do", "text": "Abro a porta."}],
        }
        db.update_adventure_ui_state(conn, adv["id"], before)
        result = npc_chronicle.apply_chronicle(
            conn, adv["id"], {
                "ui": {
                    "hud": [{"label": "Local", "value": "Sala dos Girinos", "tone": "info"}],
                    "actions": [{"label": "Perguntar", "type": "say",
                                 "text": "Pergunto sobre a marca."}],
                    "stats": [{"name": "HP", "value": 7}],
                    "inventory": [{"op": "add", "name": "Tocha", "qty": 2}],
                },
            },
            adv["plot_essentials"],
            source_action_id=action["id"], source_seq=1,
        )
        assert result == {"created": 0, "updated": 0, "bodies": 0}
        after = db.get_adventure(conn, adv["id"])["ui_state"]
        rules = db.get_action_rules(conn, action["id"])
        assert after["stats"]["HP"] == 7
        assert after[ui_runtime.HUD_KEY][0]["value"] == "Sala dos Girinos"
        assert after[ui_runtime.ACTIONS_KEY][0]["type"] == "say"
        assert {"name": "Tocha", "qty": 2} in after["bag"]
        assert rules["ui_before"] == before

        game_engine._restore_body_from_action(conn, adv["id"], action)
        restored = db.get_adventure(conn, adv["id"])["ui_state"]
        assert restored == before
