"""Tier 4: Real-World Application Scenarios (Opaque-Box E2E Tests).

Simulates complete, multi-turn interactive fiction play sessions:
- Scenario 1: Fantasy Dungeon Crawl (Crypt exploration, lorebook triggering, undead encounter, loot)
- Scenario 2: Sci-Fi Mystery (Derelict ship investigation, dialogue with AI, choices suggestion usage)
- Scenario 3: Social Intrigue & NPC Dialogue (Undercover spy interaction, say formatting, voice TTS)
- Scenario 4: Tabletop RPG with Dynamic Dice & Stats (HP damage tracking, potion usage, mana expenditure)
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import pytest

from app import db
from conftest import add_action, make_adventure
from tests.e2e_opaque.conftest import (
    MP3_BYTES,
    make_mock_transport,
    parse_sse_events,
)


class TestTier4RealWorldScenarios:
    def test_t4_scenario_01_fantasy_dungeon_crawl(self, e2e_client):
        """5-turn fantasy dungeon crawl session with story cards, combat, and loot."""
        # Setup adventure
        adv = make_adventure(
            title="The Tomb of King Aldor",
            description="A dark dungeon delve beneath the Whispering Crags.",
            ai_instructions="Dark fantasy tone, visceral combat, detailed architecture.",
            plot_essentials="Protagonist: Sir Gareth the Undaunted",
        )
        add_action(adv["id"], 0, "start", "You stand in the entrance hall of the ancient crypt, torch in hand.")

        # Story Cards for the crypt
        with db.session() as conn:
            db.insert_card(
                conn, adv["id"],
                title="Aldor's Sarcophagus",
                triggers=["sarcophagus", "tomb"],
                entry="The sealed burial vault of the mad king Aldor, carved with protective warding runes.",
            )
            db.insert_card(
                conn, adv["id"],
                title="Crown of Shadows",
                triggers=["crown", "artifact"],
                entry="A sinister silver circlet that bestows mastery over spectral beings.",
            )

        captured_turns = []
        def dm_responder(payload):
            captured_turns.append(payload)
            turn_idx = len(captured_turns)
            if turn_idx == 1:
                return ("The", " dust", " stirs", " as", " your", " boots", " echo", " on", " the", " stones.")
            elif turn_idx == 2:
                return ("Runes", " on", " the", " sarcophagus", " begin", " to", " flare", " with", " crimson", " light.")
            elif turn_idx == 3:
                return ("The", " skeletal", " champion", " raises", " its", " rusted", " halberd", " with", " a", " hollow", " rattle.")
            elif turn_idx == 4:
                return ("Radiant", " light", " engulfs", " the", " skeleton,", " reducing", " its", " bones", " to", " ash.")
            else:
                return ("You", " lift", " the", " Crown", " of", " Shadows,", " feeling", " ancient", " power", " surge.")

        transport, _ = make_mock_transport(chat_handler=dm_responder)
        from app import deps
        e2e_client.app.dependency_overrides[deps.get_http_transport] = lambda: transport

        # Turn 1 (Do): Advance into the crypt
        t1 = e2e_client.post(
            f"/api/adventures/{adv['id']}/action",
            json={"type": "do", "text": "raise your torch and advance carefully down the hall"},
        )
        assert t1.status_code == 200
        ev1 = parse_sse_events(t1.text)
        assert any(ev["event"] == "done" for ev in ev1)

        # Turn 2 (Do): Inspect sarcophagus (triggers card)
        t2 = e2e_client.post(
            f"/api/adventures/{adv['id']}/action",
            json={"type": "do", "text": "inspect the protective runes on the sarcophagus"},
        )
        assert t2.status_code == 200
        ev2 = parse_sse_events(t2.text)
        assert any(ev["event"] == "done" for ev in ev2)
        # Sarcophagus card must have been injected into prompt
        assert "protective warding runes" in captured_turns[1]["messages"][0]["content"]

        # Turn 3 (Story): Monster awakens
        t3 = e2e_client.post(
            f"/api/adventures/{adv['id']}/action",
            json={"type": "story", "text": "The crypt lid grinds open and an armored skeletal champion rises."},
        )
        assert t3.status_code == 200
        ev3 = parse_sse_events(t3.text)
        assert any(ev["event"] == "done" for ev in ev3)

        # Turn 4 (Do): Holy strike combat action
        t4 = e2e_client.post(
            f"/api/adventures/{adv['id']}/action",
            json={"type": "do", "text": "channel holy fire into your blade and smite the skeleton"},
        )
        assert t4.status_code == 200
        ev4 = parse_sse_events(t4.text)
        assert any(ev["event"] == "done" for ev in ev4)

        # Turn 5 (Do): Claim the artifact
        t5 = e2e_client.post(
            f"/api/adventures/{adv['id']}/action",
            json={"type": "do", "text": "reach into the sarcophagus and take the Crown of Shadows"},
        )
        assert t5.status_code == 200
        ev5 = parse_sse_events(t5.text)
        assert any(ev["event"] == "done" for ev in ev5)

        # Final state verification
        final_state = e2e_client.get(f"/api/adventures/{adv['id']}").json()
        actions = final_state["actions"]
        assert len(actions) == 11  # start + 5 * (player + continue)
        seqs = [a["seq"] for a in actions]
        assert seqs == list(range(11))

    def test_t4_scenario_02_scifi_mystery_investigation(self, e2e_client):
        """4-turn sci-fi derelict mystery session with choices integration."""
        adv = make_adventure(
            title="Derelict Station Daedalus",
            description="Deep space investigative thriller.",
            ai_instructions="Hard sci-fi tone, isolation, technical details.",
        )
        add_action(adv["id"], 0, "start", "You dock your shuttle with the silent research station USS Daedalus.")

        # Turn 1 (Do): Access airlock terminal
        t1 = e2e_client.post(
            f"/api/adventures/{adv['id']}/action",
            json={"type": "do", "text": "connect data-pad to the airlock bypass terminal"},
        )
        assert t1.status_code == 200
        parse_sse_events(t1.text)

        # Turn 2 (Say): Speak to ship AI
        t2 = e2e_client.post(
            f"/api/adventures/{adv['id']}/action",
            json={"type": "say", "text": "Computer, report life signs and status of sector 4"},
        )
        assert t2.status_code == 200
        parse_sse_events(t2.text)

        # Turn 3 (Choices suggestion): Query choices for next move
        choices_mock = json.dumps([
            {"type": "do", "text": "draw plasma cutter and breach the inner hatch"},
            {"type": "do", "text": "reroute auxiliary power to internal sensors"},
            {"type": "story", "text": "a sudden power surge locks the primary airlock"},
        ])
        transport_choices, _ = make_mock_transport(non_stream_content=choices_mock)
        from app import deps
        e2e_client.app.dependency_overrides[deps.get_http_transport] = lambda: transport_choices

        choices_resp = e2e_client.post(f"/api/adventures/{adv['id']}/choices")
        assert choices_resp.status_code == 200
        suggested = choices_resp.json()["choices"]
        assert len(suggested) == 3

        # Turn 4: Execute chosen suggestion
        chosen = suggested[0]
        t4 = e2e_client.post(
            f"/api/adventures/{adv['id']}/action",
            json={"type": chosen["type"], "text": chosen["text"]},
        )
        assert t4.status_code == 200
        ev4 = parse_sse_events(t4.text)
        assert any(ev["event"] == "done" for ev in ev4)

        # Confirm actions sequence and persistence
        with db.session() as conn:
            actions = db.list_actions(conn, adv["id"])
            assert len(actions) == 7
            assert actions[3]["type"] == "say"
            assert 'Computer, report life signs' in actions[3]["text"]

    def test_t4_scenario_03_social_intrigue_and_dialogue(self, e2e_client):
        """Social interaction session: whisper passphrase, bribe spy, and voice narration."""
        adv = make_adventure(
            title="The Shadows of Waterdeep",
            plot_essentials="Protagonist: Corvo, Agent of the Harpers",
        )
        add_action(adv["id"], 0, "start", "You sit in the dim corner booth of the Black Hound tavern.")

        # Create spy NPC card
        with db.session() as conn:
            db.insert_card(
                conn, adv["id"],
                title="Garrick the Whisperer",
                type="character",
                triggers=["garrick", "spy", "whisperer"],
                entry="A master broker of dangerous secrets in the city.",
                voice="M2",
            )

        # Turn 1 (Say): Whisper the secret password
        t1 = e2e_client.post(
            f"/api/adventures/{adv['id']}/action",
            json={"type": "say", "text": "The ravens nest in winter."},
        )
        assert t1.status_code == 200
        parse_sse_events(t1.text)

        # Turn 2 (Do): Bribe with pouch of gold
        t2 = e2e_client.post(
            f"/api/adventures/{adv['id']}/action",
            json={"type": "do", "text": "slide a heavy velvet pouch of coins under the table to Garrick"},
        )
        assert t2.status_code == 200
        parse_sse_events(t2.text)

        # Turn 3 (Say): Ask for the target's location
        t3 = e2e_client.post(
            f"/api/adventures/{adv['id']}/action",
            json={"type": "say", "text": "Where is Lord Cassalanter meeting his cultists tonight?"},
        )
        assert t3.status_code == 200
        ev3 = parse_sse_events(t3.text)
        done_ev = next(ev for ev in ev3 if ev["event"] == "done")
        reply_text = done_ev["data"]["action"]["text"]

        # Turn 4: Voice TTS narrating the dialogue response
        tts_resp = e2e_client.post(
            "/api/tts",
            json={"text": reply_text.strip(), "voice": "M2"},
        )
        assert tts_resp.status_code == 200
        assert tts_resp.content == MP3_BYTES

    def test_t4_scenario_04_tabletop_encounter_with_dice_and_stats(self, e2e_client):
        """Interactive session tracking player HP damage, potion recovery, and resource spend."""
        adv = make_adventure(title="The Goblin Warrens")
        add_action(adv["id"], 0, "start", "Goblin archers leap from behind barricades.")

        # 1. Initialize UI profile with HP, Mana, and Inventory
        manifest = json.dumps({
            "tabs": [{"id": "do", "label": "Do", "placeholder": ""}],
            "widgets": [
                {"id": "stats", "type": "stats", "label": "Stats", "stats": [
                    {"name": "HP", "value": 20, "max": 20, "min": 0},
                    {"name": "Mana", "value": 15, "max": 15, "min": 0},
                ]},
                {"id": "inv", "type": "inventory", "label": "Pouch", "items": [
                    {"name": "Greater Healing Potion", "qty": 1},
                ]},
            ],
        })
        transport_ui, _ = make_mock_transport(non_stream_content=manifest)
        from app import deps
        e2e_client.app.dependency_overrides[deps.get_http_transport] = lambda: transport_ui

        prof_resp = e2e_client.post(f"/api/adventures/{adv['id']}/ui-profile")
        assert prof_resp.status_code == 200

        # Turn 1: Player charges into combat
        t1 = e2e_client.post(
            f"/api/adventures/{adv['id']}/action",
            json={"type": "do", "text": "charge the barricade with shield raised"},
        )
        assert t1.status_code == 200
        parse_sse_events(t1.text)

        # Player takes 8 damage from an arrow -> update HP to 12
        e2e_client.put(
            f"/api/adventures/{adv['id']}/ui-state",
            json={"state": {"stats": {"HP": 12, "Mana": 15}, "inv": [{"name": "Greater Healing Potion", "qty": 1}]}},
        )

        # Turn 2: Player drinks healing potion
        t2 = e2e_client.post(
            f"/api/adventures/{adv['id']}/action",
            json={"type": "do", "text": "drink the Greater Healing Potion to mend wounds"},
        )
        assert t2.status_code == 200
        parse_sse_events(t2.text)

        # Heals back to 19 HP, inventory item consumed
        e2e_client.put(
            f"/api/adventures/{adv['id']}/ui-state",
            json={"state": {"stats": {"HP": 19, "Mana": 15}, "inv": []}},
        )

        # Turn 3: Player spends 5 mana to cast Lightning Bolt
        t3 = e2e_client.post(
            f"/api/adventures/{adv['id']}/action",
            json={"type": "do", "text": "unleash a blinding arc of Lightning Bolt across the barricade"},
        )
        assert t3.status_code == 200
        parse_sse_events(t3.text)

        # Mana drops to 10
        e2e_client.put(
            f"/api/adventures/{adv['id']}/ui-state",
            json={"state": {"stats": {"HP": 19, "Mana": 10}, "inv": []}},
        )

        # Verify final state reflects accurate stats
        final_ui = e2e_client.get(f"/api/adventures/{adv['id']}/ui-profile").json()
        assert final_ui["state"]["stats"]["HP"] == 19
        assert final_ui["state"]["stats"]["Mana"] == 10
        assert final_ui["state"]["inv"] == []
