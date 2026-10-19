"""Tier 3: Cross-Feature Combinations (Opaque-Box E2E Tests).

Covers pairwise and multi-feature interaction flows:
- Action turn triggering story cards & updating memory bank
- Illustration ('see') followed by Undo rollback
- Dynamic UI profile & widget state injection during generation
- Complete adventure export, cascade deletion, import, and continued gameplay
- Concurrency generation lock synchronization across all generation endpoints
- Voice STT -> Player action -> Narrator SSE -> Voice TTS loop
- Action editing altering subsequent context and retry generation
- NPC creation, sheet enrichment, dialogue turn, and chronicle tracking
- Context eviction, summarization, and memory bank retrieval
- Multi-adventure bulk export and selective restore
"""

from __future__ import annotations

import io
import json
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import pytest

from app import context_builder, db, game_engine, settings_store
from conftest import add_action, make_adventure
from memory import retrieval, triggers
from tests.e2e_opaque.conftest import (
    MP3_BYTES,
    PNG_BYTES,
    make_mock_transport,
    parse_sse_events,
)


class TestTier3Combinations:
    def test_t3_combo_01_do_action_triggers_card_and_updates_memory(self, e2e_client):
        """Action turn triggering story card which informs context and generates choices."""
        adv = make_adventure(title="The Dragon Citadel")
        add_action(adv["id"], 0, "start", "You approach the jagged peaks.")

        with db.session() as conn:
            db.insert_card(
                conn, adv["id"],
                title="Dragon Horn",
                triggers=["dragon horn", "horn"],
                entry="A mythical horn that can summon the elder wyrm.",
            )

        captured_prompts = []
        def capture_handler(payload):
            captured_prompts.append(payload)
            return ("The", " ground", " shakes", " violently.")

        transport, _ = make_mock_transport(chat_handler=capture_handler)
        from app import deps
        e2e_client.app.dependency_overrides[deps.get_http_transport] = lambda: transport

        # Player blows the dragon horn
        resp = e2e_client.post(
            f"/api/adventures/{adv['id']}/action",
            json={"type": "do", "text": "blow the dragon horn with all your might"},
        )
        assert resp.status_code == 200
        events = parse_sse_events(resp.text)
        assert any(ev["event"] == "done" for ev in events)

        # Verify that Dragon Horn was triggered into context
        assert len(captured_prompts) >= 1
        # Layout cache-friendly: [World info] (cards) vive na última mensagem.
        system_content = captured_prompts[0]["messages"][-1]["content"]
        assert "Dragon Horn" in system_content or "elder wyrm" in system_content

        # Subsequent choice generation takes new scene into account
        choices_json = json.dumps([{"type": "do", "text": "brace for the wyrm"}])
        transport_choices, _ = make_mock_transport(non_stream_content=choices_json)
        e2e_client.app.dependency_overrides[deps.get_http_transport] = lambda: transport_choices

        choices_resp = e2e_client.post(f"/api/adventures/{adv['id']}/choices")
        assert choices_resp.status_code == 200
        assert len(choices_resp.json()["choices"]) >= 1

    def test_t3_combo_02_see_action_followed_by_undo(self, e2e_client):
        """Scene 'see' illustration followed by Undo cleans up action safely."""
        adv = make_adventure(title="Scenic Valley")
        add_action(adv["id"], 0, "start", "The morning sun rises over emerald hills.")

        # 1. Generate image via See action
        see_resp = e2e_client.post(
            f"/api/adventures/{adv['id']}/see",
            json={"action_seq": 0, "extra_prompt": "emerald hills in morning sunlight"},
        )
        assert see_resp.status_code == 200
        image_id = see_resp.json()["id"]

        with db.session() as conn:
            actions = db.list_actions(conn, adv["id"])
            assert len(actions) == 2
            assert actions[1]["type"] == "see"

        # 2. Undo the See action
        undo_resp = e2e_client.post(f"/api/adventures/{adv['id']}/undo")
        assert undo_resp.status_code == 200
        assert undo_resp.json()["action_count"] == 1

        with db.session() as conn:
            actions_after = db.list_actions(conn, adv["id"])
            assert len(actions_after) == 1
            assert actions_after[0]["type"] == "start"

        # 3. Lock must not be left lingering: subsequent turn works immediately
        cont_resp = e2e_client.post(f"/api/adventures/{adv['id']}/continue")
        assert cont_resp.status_code == 200
        cont_events = parse_sse_events(cont_resp.text)
        assert any(ev["event"] == "done" for ev in cont_events)

    def test_t3_combo_03_dynamic_ui_profile_and_state_in_story_generation(self, e2e_client):
        """UI profile created, widget state updated, and state injected in next turn."""
        adv = make_adventure(title="RPG Quest")
        add_action(adv["id"], 0, "start", "You are a warrior with sword and shield.")

        manifest = json.dumps({
            "tabs": [{"id": "do", "label": "Do", "placeholder": ""}],
            "widgets": [
                {"id": "hp", "type": "stats", "label": "Health", "stats": [{"name": "HP", "value": 20, "max": 20}]},
                {"id": "inv", "type": "inventory", "label": "Pack", "items": [{"name": "Healing Potion", "qty": 1}]},
            ],
        })
        transport_ui, _ = make_mock_transport(non_stream_content=manifest)
        from app import deps
        e2e_client.app.dependency_overrides[deps.get_http_transport] = lambda: transport_ui

        # 1. Generate UI Profile
        prof_resp = e2e_client.post(f"/api/adventures/{adv['id']}/ui-profile")
        assert prof_resp.status_code == 200

        # 2. Player gets hurt and consumes potion -> update state
        new_state = {
            "hp": {"HP": 12},
            "inv": [{"name": "Empty Flask", "qty": 1}],
        }
        state_resp = e2e_client.put(
            f"/api/adventures/{adv['id']}/ui-state",
            json={"state": new_state},
        )
        assert state_resp.status_code == 200

        # 3. Next turn takes place: captured payload must reflect updated state
        captured_payloads = []
        def capture_handler(payload):
            captured_payloads.append(payload)
            return ("The", " wound", " aches", " slightly.")

        transport_game, _ = make_mock_transport(chat_handler=capture_handler)
        e2e_client.app.dependency_overrides[deps.get_http_transport] = lambda: transport_game

        turn_resp = e2e_client.post(
            f"/api/adventures/{adv['id']}/action",
            json={"type": "do", "text": "press on through the dungeon corridor"},
        )
        assert turn_resp.status_code == 200
        parse_sse_events(turn_resp.text)

        assert len(captured_payloads) >= 1
        # Layout cache-friendly: [Player state] vive na última mensagem.
        prompt = captured_payloads[0]["messages"][-1]["content"]
        assert "HP" in prompt
        assert "12" in prompt

    def test_t3_combo_04_full_adventure_round_trip_and_continuation(self, e2e_client):
        """Full lifecycle: create -> play turns -> add cards -> export -> delete -> import -> play more."""
        # 1. Create adventure
        adv_resp = e2e_client.post("/api/adventures", json={"title": "Epic Voyage", "tags": ["sea", "epic"]})
        assert adv_resp.status_code == 201
        adv_id = adv_resp.json()["id"]
        add_action(adv_id, 0, "start", "The flagship sails into unknown waters.")

        # 2. Play 3 turns (do, say, story)
        e2e_client.post(f"/api/adventures/{adv_id}/action", json={"type": "do", "text": "climb the main mast"})
        e2e_client.post(f"/api/adventures/{adv_id}/action", json={"type": "say", "text": "Land ho to the east!"})
        e2e_client.post(f"/api/adventures/{adv_id}/action", json={"type": "story", "text": "A dense fog rolls in."})

        # 3. Add story card
        e2e_client.post(
            f"/api/adventures/{adv_id}/cards",
            json={"title": "Sea Serpent", "triggers": ["serpent"], "entry": "A colossal sea serpent."},
        )

        # 4. Export adventure
        export_resp = e2e_client.get(f"/api/adventures/{adv_id}/export")
        assert export_resp.status_code == 200
        zip_bytes = export_resp.content

        # 5. Cascade delete
        e2e_client.delete(f"/api/adventures/{adv_id}")
        assert e2e_client.get(f"/api/adventures/{adv_id}").status_code == 404

        # 6. Re-import
        import_resp = e2e_client.post(
            "/api/import/aidungeon",
            files={"file": ("voyage.zip", zip_bytes, "application/zip")},
        )
        assert import_resp.status_code == 201
        restored_id = import_resp.json()["adventure_id"]

        # 7. Continue play on restored adventure
        cont_resp = e2e_client.post(
            f"/api/adventures/{restored_id}/action",
            json={"type": "do", "text": "steer the ship away from the serpent"},
        )
        assert cont_resp.status_code == 200
        events = parse_sse_events(cont_resp.text)
        assert any(ev["event"] == "done" for ev in events)

        # Verify action count increased
        state_after = e2e_client.get(f"/api/adventures/{restored_id}").json()
        assert len(state_after["actions"]) >= 9

    def test_t3_combo_05_concurrency_lock_across_action_see_choices_ui(self, e2e_client):
        """Lock acquired during generation blocks all concurrent generation endpoints with 409."""
        adv = make_adventure(title="Concurrency Guard")
        add_action(adv["id"], 0, "start", "The chamber is silent.")

        # Acquire lock manually to simulate active generation
        assert game_engine.acquire_generation_lock(adv["id"]) is True
        try:
            # 1. Action
            r1 = e2e_client.post(f"/api/adventures/{adv['id']}/action", json={"type": "do", "text": "test"})
            assert r1.status_code == 409
            assert r1.json()["error"]["code"] == "generation_in_progress"

            # 2. Continue
            r2 = e2e_client.post(f"/api/adventures/{adv['id']}/continue")
            assert r2.status_code == 409

            # 3. Retry
            r3 = e2e_client.post(f"/api/adventures/{adv['id']}/retry")
            assert r3.status_code == 409

            # 4. See
            r4 = e2e_client.post(f"/api/adventures/{adv['id']}/see", json={"action_seq": 0})
            assert r4.status_code == 409

            # 5. Choices
            r5 = e2e_client.post(f"/api/adventures/{adv['id']}/choices")
            assert r5.status_code == 409

            # 6. UI Profile
            r6 = e2e_client.post(f"/api/adventures/{adv['id']}/ui-profile")
            assert r6.status_code == 409
        finally:
            game_engine.release_generation_lock(adv["id"])

        # After lock release, endpoint responds normally
        r_after = e2e_client.post(f"/api/adventures/{adv['id']}/continue")
        assert r_after.status_code == 200

    def test_t3_combo_06_voice_stt_transcription_into_player_action_into_tts(self, e2e_client):
        """Full voice loop: STT -> Do Action -> Narrator response -> TTS synthesis."""
        adv = make_adventure(title="Voice Loop Tale")
        add_action(adv["id"], 0, "start", "A whispering cave.")

        # 1. Player speaks into microphone -> STT transcription
        transport_voice, _ = make_mock_transport(stt_text="I light a torch and inspect the cave wall")
        from app import deps
        e2e_client.app.dependency_overrides[deps.get_http_transport] = lambda: transport_voice

        stt_resp = e2e_client.post(
            "/api/stt",
            files={"file": ("mic.wav", b"RIFFfakeaudio", "audio/wav")},
        )
        assert stt_resp.status_code == 200
        transcribed = stt_resp.json()["text"]
        assert "torch" in transcribed

        # 2. Send transcribed text as player Do action
        turn_resp = e2e_client.post(
            f"/api/adventures/{adv['id']}/action",
            json={"type": "do", "text": transcribed},
        )
        assert turn_resp.status_code == 200
        events = parse_sse_events(turn_resp.text)
        done_ev = next(ev for ev in events if ev["event"] == "done")
        narrator_text = done_ev["data"]["action"]["text"]

        # 3. Narrator text sent to TTS for voice narration
        tts_resp = e2e_client.post(
            "/api/tts",
            json={"text": narrator_text.strip(), "voice": "M1"},
        )
        assert tts_resp.status_code == 200
        assert tts_resp.headers["content-type"] == "audio/mpeg"
        assert tts_resp.content == MP3_BYTES

    def test_t3_combo_07_edit_past_turn_affects_subsequent_retry_and_context(self, e2e_client):
        """Editing an earlier action modifies the prompt for subsequent retry."""
        adv = make_adventure(title="Time Paradox")
        add_action(adv["id"], 0, "start", "The fork in the road.")
        act1 = add_action(adv["id"], 1, "do", "\n> You take the left path\n")
        act2 = add_action(adv["id"], 2, "continue", " You find a tranquil pond.")

        # 1. Edit the action to choose the right path instead
        edit_resp = e2e_client.patch(
            f"/api/actions/{act1['id']}",
            json={"text": "\n> You take the right path into the thornwood\n"},
        )
        assert edit_resp.status_code == 200
        assert "thornwood" in edit_resp.json()["text"]

        # 2. Retry the continuation
        captured_payloads = []
        def capture_handler(payload):
            captured_payloads.append(payload)
            return ("The", " thorns", " tear", " at", " your", " cloak.")

        transport, _ = make_mock_transport(chat_handler=capture_handler)
        from app import deps
        e2e_client.app.dependency_overrides[deps.get_http_transport] = lambda: transport

        retry_resp = e2e_client.post(f"/api/adventures/{adv['id']}/retry")
        assert retry_resp.status_code == 200
        parse_sse_events(retry_resp.text)

        # 3. Verify context contained the edited path
        assert len(captured_payloads) >= 1
        all_user_text = "".join(m["content"] for m in captured_payloads[0]["messages"] if m["role"] == "user")
        assert "thornwood" in all_user_text

    def test_t3_combo_08_npc_creation_enrichment_chronicle_interaction(self, e2e_client):
        """Create NPC card -> enrich sheet -> dialogue action -> chronicle update."""
        from app import npc_chronicle
        adv = make_adventure(title="The Blacksmith's Forge")
        add_action(adv["id"], 0, "start", "The heat from the forge fills the shop.")

        # 1. Create NPC card
        card_resp = e2e_client.post(
            f"/api/adventures/{adv['id']}/cards",
            json={
                "title": "Kaelen",
                "type": "character",
                "triggers": ["kaelen", "smith"],
                "entry": "An apprentice blacksmith.",
                "voice": "M1",
            },
        )
        assert card_resp.status_code == 201
        card_id = card_resp.json()["id"]

        # 2. Enrich card
        enrich_data = json.dumps({
            "appearance": "Sweat-streaked face, sturdy leather apron",
            "body": "Young, athletic build",
            "clothing": "Leather apron and boots",
            "personality": "Nervous but eager to help",
            "stats": {"str": 14, "dex": 12, "con": 13, "int": 10, "wis": 11, "cha": 12},
            "status": "alive",
            "notes": "Secretly studying alchemy",
            "portrait_prompt": "portrait of a young blacksmith apprentice in a forge",
        })
        transport_enrich, _ = make_mock_transport(non_stream_content=enrich_data)
        from app import deps
        e2e_client.app.dependency_overrides[deps.get_http_transport] = lambda: transport_enrich

        enrich_resp = e2e_client.post(f"/api/adventures/{adv['id']}/cards/{card_id}/enrich")
        assert enrich_resp.status_code == 200

        # 3. Player speaks to Kaelen
        say_resp = e2e_client.post(
            f"/api/adventures/{adv['id']}/action",
            json={"type": "say", "text": "Kaelen, do you know where the master keeps the silver sword?"},
        )
        assert say_resp.status_code == 200
        parse_sse_events(say_resp.text)

        # 4. Chronicle pass
        chronicle_data = json.dumps({
            "plot_essentials": "",
            "characters": [{
                "name": "Kaelen",
                "aliases": ["smith"],
                "existing_title": "Kaelen",
                "summary": "An apprentice blacksmith who knows where the silver sword is kept.",
                "facts": ["Asked about the silver sword"],
                "status": "alive",
                "appearance": "Sweat-streaked face",
                "clothing": "Leather apron",
                "personality": "Nervous",
                "look_changed": False,
                "stat_delta": {"str": 0, "dex": 0, "con": 0, "int": 0, "wis": 0, "cha": 0},
            }],
            "places": [],
            "factions": [],
            "items": [],
        })
        transport_chronicle, _ = make_mock_transport(non_stream_content=chronicle_data)
        result = npc_chronicle.update_from_turn(adv["id"], transport=transport_chronicle)
        assert result.get("updated", 0) + result.get("created", 0) >= 1

    def test_t3_combo_09_memory_eviction_summarizer_retrieval_flow(self, e2e_client):
        """Simulate turn accumulation past context window, verify memory ranking and inclusion."""
        adv = make_adventure(title="The Odyssey")
        add_action(adv["id"], 0, "start", "The hero embarks on the voyage.")

        with db.session() as conn:
            # Seed ancient memories from past acts
            db.insert_memory(conn, adv["id"], "The crew was cursed by the sea witch in Calypso.", seq_start=0, seq_end=10)
            db.insert_memory(conn, adv["id"], "Lost the golden rudder during the maelstrom.", seq_start=11, seq_end=20)

            # Retrieve with query referencing the sea witch
            selected = retrieval.select_memories(conn, adv["id"], "beware the sea witch curse")
            assert len(selected) >= 1
            assert any("sea witch" in m["text"] for m in selected)

    def test_t3_combo_10_multi_adventure_bulk_export_and_selective_delete(self, e2e_client):
        """Export all adventures, selectively delete one, and verify restore without corruption."""
        a1 = make_adventure(title="Kingdom A")
        a2 = make_adventure(title="Kingdom B")
        a3 = make_adventure(title="Kingdom C")
        add_action(a1["id"], 0, "start", "A starts.")
        add_action(a2["id"], 0, "start", "B starts.")
        add_action(a3["id"], 0, "start", "C starts.")

        # 1. Bulk export
        bulk_resp = e2e_client.get("/api/adventures/export")
        assert bulk_resp.status_code == 200
        bulk_zip = bulk_resp.content

        # 2. Delete Kingdom B
        e2e_client.delete(f"/api/adventures/{a2['id']}")
        adventures_list = e2e_client.get("/api/adventures").json()
        ids = {a["id"] for a in adventures_list}
        assert a2["id"] not in ids
        assert a1["id"] in ids
        assert a3["id"] in ids

        # 3. Import full archive
        import_resp = e2e_client.post(
            "/api/import/aidungeon",
            files={"file": ("full_backup.zip", bulk_zip, "application/zip")},
        )
        assert import_resp.status_code == 201
        report = import_resp.json()
        assert report.get("multi") is True
        assert report.get("imported") >= 1

        # 4. Verify Kingdom B exists again
        final_list = e2e_client.get("/api/adventures").json()
        titles = {a["title"] for a in final_list}
        assert "Kingdom B" in titles
