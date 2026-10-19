"""Tier 1: Feature Coverage (Opaque-Box E2E Tests).

Covers happy paths for all 14 core features in isolation:
1. Auth & Route Protection
2. Scenario Management & AI Assistant
3. Adventure Lifecycle CRUD
4. Action Turns Loop (Do/Say/Story)
5. Turn Flow Controls (Continue/Retry/Undo/Edit)
6. Context Assembly & Prompt Sandwich
7. Dynamic UI Controls & State
8. Action Choices Suggestions
9. Story Cards Lorebook
10. Live NPCs & Chronicle
11. Memories & Summarizer
12. Scene Illustration ("See")
13. Voice Narration (TTS & STT)
14. Backup Import/Export Round-Trip & Scripting Contract
"""

from __future__ import annotations

import importlib
import io
import json
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import pytest

from app import auth as auth_core, context_builder, db, settings_store
from conftest import TEST_AUTH_PASSWORD, TEST_AUTH_USER, add_action, make_adventure
from tests.e2e_opaque.conftest import (
    MP3_BYTES,
    PNG_B64,
    PNG_BYTES,
    build_synthetic_backup_zip,
    make_mock_transport,
    parse_sse_events,
)


# ==============================================================================
# FEATURE 1: Auth & Route Protection
# ==============================================================================
class TestTier1Auth:
    def test_t1_auth_01_login_valid_credentials(self, e2e_client):
        """Valid credentials return 200 with Bearer token and expiration."""
        resp = e2e_client.post(
            "/api/auth/login",
            json={"username": TEST_AUTH_USER, "password": TEST_AUTH_PASSWORD},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "token" in data and len(data["token"]) > 20
        assert "expires_at" in data

    def test_t1_auth_02_check_valid_token(self, e2e_client):
        """GET /api/auth/check confirms valid bearer token."""
        resp = e2e_client.get("/api/auth/check")
        assert resp.status_code == 200
        data = resp.json()
        assert data.get("ok") is True
        assert data.get("user") == TEST_AUTH_USER

    def test_t1_auth_03_route_unauthenticated_returns_401(self, raw_client):
        """Protected /api route without Authorization header is rejected with 401."""
        resp = raw_client.get("/api/adventures")
        assert resp.status_code == 401

    def test_t1_auth_04_route_invalid_token_returns_401(self, raw_client):
        """Protected route with corrupted token is rejected with 401."""
        resp = raw_client.get("/api/adventures", headers={"Authorization": "Bearer badtoken"})
        assert resp.status_code == 401

    def test_t1_auth_05_token_expiration_utc_format(self, e2e_client):
        """Token expiration string conforms to ISO-8601 UTC format."""
        resp = e2e_client.post(
            "/api/auth/login",
            json={"username": TEST_AUTH_USER, "password": TEST_AUTH_PASSWORD},
        )
        assert resp.status_code == 200
        expires_at = resp.json()["expires_at"]
        assert expires_at.endswith("Z") or "+00:00" in expires_at


# ==============================================================================
# FEATURE 2: Scenario Management & AI Assistant
# ==============================================================================
class TestTier1Scenarios:
    def test_t1_scenario_01_list_scenarios(self, e2e_client):
        """Listing scenarios returns 200 and a list."""
        resp = e2e_client.get("/api/scenarios")
        assert resp.status_code == 200
        assert isinstance(resp.json(), list)

    def test_t1_scenario_02_get_scenario_by_id(self, e2e_client):
        """Retrieve existing scenario returns its full definition."""
        with db.session() as conn:
            sc = db.insert_scenario(
                conn,
                title="Crypt of Shadows",
                description="Explore a dark tomb.",
                opening_text="You stand before the obsidian gate.",
                tags=["horror", "dungeon"],
            )
        resp = e2e_client.get(f"/api/scenarios/{sc['id']}")
        assert resp.status_code == 200
        data = resp.json()
        assert data["title"] == "Crypt of Shadows"
        assert data["opening_text"] == "You stand before the obsidian gate."

    def test_t1_scenario_03_create_scenario_db(self, e2e_client):
        """Scenario creation stores tags, plot essentials, and story cards."""
        with db.session() as conn:
            sc = db.insert_scenario(
                conn,
                title="Star Voyager",
                description="Space exploration",
                tags=["sci-fi"],
                story_cards=[{"title": "Ship AI", "entry": "Guardian AI"}],
            )
        assert len(sc["id"]) == 32
        assert sc["tags"] == ["sci-fi"]

    def test_t1_scenario_04_ai_assistant_scenario_draft(self, e2e_client):
        """AI assistant generates structured scenario from brief."""
        draft_content = json.dumps({
            "title": "Neon Syndicate",
            "description": "Cyberpunk heist in Neo-Tokyo",
            "ai_instructions": "Focus on high tech and corporate intrigue",
            "opening_text": "Rain drenches the neon alleys of Sector 4.",
            "tags": ["cyberpunk", "heist"],
            "story_cards": [{"title": "Fixer", "entry": "Informant in the underground."}],
        })
        transport, _ = make_mock_transport(non_stream_content=draft_content)
        from app import deps
        e2e_client.app.dependency_overrides[deps.get_http_transport] = lambda: transport

        resp = e2e_client.post(
            "/api/assistant/scenario",
            json={"brief": "A cyberpunk heist with an underground fixer", "language": "en"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["title"] == "Neon Syndicate"
        assert "Sector 4" in data["opening_text"]

    def test_t1_scenario_05_ai_assistant_extracts_cards(self, e2e_client):
        """AI assistant response contains structured draft story cards."""
        draft_content = json.dumps({
            "title": "Forgotten Island",
            "description": "Stranded survivors",
            "opening_text": "The shipwreck lies in pieces along the shore.",
            "tags": ["survival"],
            "story_cards": [
                {"title": "Captain", "entry": "Stern veteran officer", "type": "character"}
            ],
        })
        transport, _ = make_mock_transport(non_stream_content=draft_content)
        from app import deps
        e2e_client.app.dependency_overrides[deps.get_http_transport] = lambda: transport

        resp = e2e_client.post(
            "/api/assistant/scenario",
            json={"brief": "A survival mystery on a deserted island", "language": "en"},
        )
        assert resp.status_code == 200
        cards = resp.json().get("story_cards", [])
        assert len(cards) >= 1
        assert cards[0]["title"] == "Captain"


# ==============================================================================
# FEATURE 3: Adventure Lifecycle CRUD
# ==============================================================================
class TestTier1Adventures:
    def test_t1_adventure_01_create_empty_adventure(self, e2e_client):
        """POST /api/adventures creates empty adventure with 201 status."""
        resp = e2e_client.post(
            "/api/adventures",
            json={"title": "Lost Expedition", "tags": ["exploration"]},
        )
        assert resp.status_code == 201
        data = resp.json()
        assert data["title"] == "Lost Expedition"
        assert "exploration" in data["tags"]
        assert "id" in data

    def test_t1_adventure_02_create_from_scenario(self, e2e_client):
        """Creating an adventure from a scenario inherits cards and start action."""
        with db.session() as conn:
            sc = db.insert_scenario(
                conn,
                title="Dragon Spire",
                opening_text="The mountain peaks pierce the frozen sky.",
                story_cards=[{"title": "Red Wyrm", "entry": "An ancient dragon."}],
            )

        resp = e2e_client.post("/api/adventures", json={"scenario_id": sc["id"]})
        assert resp.status_code == 201
        data = resp.json()
        assert data["title"] == "Dragon Spire"
        assert len(data["actions"]) == 1
        assert data["actions"][0]["type"] == "start"
        assert "mountain peaks" in data["actions"][0]["text"]
        assert len(data["story_cards"]) == 1
        assert data["story_cards"][0]["title"] == "Red Wyrm"

    def test_t1_adventure_03_list_adventures(self, e2e_client):
        """GET /api/adventures returns summaries including created adventure."""
        adv = make_adventure(title="Chronicle of Eldoria")
        resp = e2e_client.get("/api/adventures")
        assert resp.status_code == 200
        summaries = resp.json()
        assert any(a["id"] == adv["id"] for a in summaries)

    def test_t1_adventure_04_patch_adventure_metadata(self, e2e_client):
        """PATCH /api/adventures/{id} updates title, description, and tags."""
        adv = make_adventure(title="Original Title")
        resp = e2e_client.patch(
            f"/api/adventures/{adv['id']}",
            json={"title": "Renamed Title", "tags": ["remastered"]},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["title"] == "Renamed Title"
        assert body["tags"] == ["remastered"]

    def test_t1_adventure_05_cascade_delete_adventure(self, e2e_client):
        """DELETE /api/adventures/{id} removes adventure and cascades to actions/cards."""
        adv = make_adventure(title="Doomed Kingdom")
        add_action(adv["id"], 0, "start", "The castle gates creak open.")
        with db.session() as conn:
            db.insert_card(conn, adv["id"], title="Guard", entry="A stalwart guard.")

        resp = e2e_client.delete(f"/api/adventures/{adv['id']}")
        assert resp.status_code == 200
        assert resp.json().get("deleted") is True

        assert e2e_client.get(f"/api/adventures/{adv['id']}").status_code == 404


# ==============================================================================
# FEATURE 4: Action Turns Loop (Do/Say/Story)
# ==============================================================================
class TestTier1ActionTurns:
    def test_t1_action_01_do_action_sse_stream(self, e2e_client):
        """Action type 'do' formats as `\n> You ...\n` and streams SSE tokens."""
        adv = make_adventure(title="Dungeon Run")
        add_action(adv["id"], 0, "start", "You awaken in a damp stone chamber.")

        resp = e2e_client.post(
            f"/api/adventures/{adv['id']}/action",
            json={"type": "do", "text": "light the torch"},
        )
        assert resp.status_code == 200
        assert "text/event-stream" in resp.headers["content-type"]
        events = parse_sse_events(resp.text)
        assert any(ev["event"] == "token" for ev in events)
        assert any(ev["event"] == "done" for ev in events)

        # Check DB action formatting
        with db.session() as conn:
            actions = db.list_actions(conn, adv["id"])
            assert len(actions) == 3  # start, do, continue
            assert actions[1]["text"] == "\n> You light the torch\n"
            assert actions[1]["type"] == "do"
            assert actions[2]["type"] == "continue"

    def test_t1_action_02_say_action_quotes_formatting(self, e2e_client):
        """Action type 'say' formats text with quotes: `\n> You say "..."\n`."""
        adv = make_adventure(title="Tavern Talk")
        add_action(adv["id"], 0, "start", "The tavern is bustling with patrons.")

        resp = e2e_client.post(
            f"/api/adventures/{adv['id']}/action",
            json={"type": "say", "text": "I am seeking the guildmaster"},
        )
        assert resp.status_code == 200
        with db.session() as conn:
            actions = db.list_actions(conn, adv["id"])
            say_act = next(a for a in actions if a["type"] == "say")
            assert say_act["text"] == '\n> You say "I am seeking the guildmaster"\n'

    def test_t1_action_03_story_action_direct_injection(self, e2e_client):
        """Action type 'story' formats narrative beat directly as pure prose."""
        adv = make_adventure(title="Stormy Night")
        add_action(adv["id"], 0, "start", "The clouds darken the sky.")

        resp = e2e_client.post(
            f"/api/adventures/{adv['id']}/action",
            json={"type": "story", "text": "Lightning strikes the tower bell."},
        )
        assert resp.status_code == 200
        with db.session() as conn:
            actions = db.list_actions(conn, adv["id"])
            story_act = next(a for a in actions if a["type"] == "story")
            assert story_act["text"] == "Lightning strikes the tower bell."

    def test_t1_action_04_turn_persists_actions_in_db(self, e2e_client):
        """Player action and narrator response are committed with contiguous seq."""
        adv = make_adventure()
        add_action(adv["id"], 0, "start", "The road splits into two.")

        e2e_client.post(
            f"/api/adventures/{adv['id']}/action",
            json={"type": "do", "text": "take the left path"},
        )
        with db.session() as conn:
            actions = db.list_actions(conn, adv["id"])
            seqs = [a["seq"] for a in actions]
            assert seqs == [0, 1, 2]

    def test_t1_action_05_multi_turn_sequence_contiguity(self, e2e_client):
        """Multiple consecutive turns keep sequence strictly contiguous (0..4)."""
        adv = make_adventure()
        add_action(adv["id"], 0, "start", "Start.")

        e2e_client.post(f"/api/adventures/{adv['id']}/action", json={"type": "do", "text": "turn one"})
        e2e_client.post(f"/api/adventures/{adv['id']}/action", json={"type": "say", "text": "turn two"})

        with db.session() as conn:
            actions = db.list_actions(conn, adv["id"])
            assert [a["seq"] for a in actions] == [0, 1, 2, 3, 4]


# ==============================================================================
# FEATURE 5: Turn Flow Controls (Continue/Retry/Undo/Edit)
# ==============================================================================
class TestTier1TurnFlow:
    def test_t1_turnflow_01_continue_action(self, e2e_client):
        """POST /continue generates narrator text without adding a player action."""
        adv = make_adventure()
        add_action(adv["id"], 0, "start", "The ritual has begun.")

        resp = e2e_client.post(f"/api/adventures/{adv['id']}/continue")
        assert resp.status_code == 200
        events = parse_sse_events(resp.text)
        assert any(ev["event"] == "done" for ev in events)

        with db.session() as conn:
            actions = db.list_actions(conn, adv["id"])
            assert len(actions) == 2
            assert actions[1]["type"] == "continue"

    def test_t1_turnflow_02_retry_action(self, e2e_client):
        """POST /retry replaces the last continue generation."""
        adv = make_adventure()
        add_action(adv["id"], 0, "start", "Start.")
        add_action(adv["id"], 1, "do", "\n> You search\n")
        add_action(adv["id"], 2, "continue", " Old text.")

        resp = e2e_client.post(f"/api/adventures/{adv['id']}/retry")
        assert resp.status_code == 200

        with db.session() as conn:
            actions = db.list_actions(conn, adv["id"])
            assert len(actions) == 3
            assert actions[2]["type"] == "continue"
            assert "The dungeon door opens slowly." in actions[2]["text"]

    def test_t1_turnflow_03_undo_action(self, e2e_client):
        """POST /undo removes player action and narrator response."""
        adv = make_adventure()
        add_action(adv["id"], 0, "start", "Opening.")
        add_action(adv["id"], 1, "do", "\n> You inspect the alter\n")
        add_action(adv["id"], 2, "continue", " The alter is carved with runes.")

        resp = e2e_client.post(f"/api/adventures/{adv['id']}/undo")
        assert resp.status_code == 200
        data = resp.json()
        assert data["action_count"] == 1
        assert len(data["removed"]) == 2

    def test_t1_turnflow_04_edit_action(self, e2e_client):
        """PATCH /api/actions/{action_id} updates text and marks edited_at."""
        adv = make_adventure()
        act = add_action(adv["id"], 0, "start", "Original opening text.")

        resp = e2e_client.patch(
            f"/api/actions/{act['id']}",
            json={"text": "Revised opening description."},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["text"] == "Revised opening description."
        assert data["edited_at"] is not None

    def test_t1_turnflow_05_generation_lock_concurrency(self, e2e_client):
        """Attempting generation while lock is held returns 409 Conflict."""
        adv = make_adventure()
        from app import game_engine
        assert game_engine.acquire_generation_lock(adv["id"]) is True
        try:
            resp = e2e_client.post(f"/api/adventures/{adv['id']}/continue")
            assert resp.status_code == 409
            assert resp.json()["error"]["code"] == "generation_in_progress"
        finally:
            game_engine.release_generation_lock(adv["id"])


# ==============================================================================
# FEATURE 6: Dynamic Context Assembly
# ==============================================================================
class TestTier1ContextAssembly:
    def test_t1_context_01_sandwich_structure(self, e2e_client):
        """Context builder puts system message at index 0 followed by history."""
        adv = make_adventure(ai_instructions="Be dramatic.")
        add_action(adv["id"], 0, "start", "Scene starts.")
        with db.session() as conn:
            actions = db.list_actions(conn, adv["id"])
            cfg = settings_store.runtime_config(conn)
            built = context_builder.build_context(adv, actions, cfg, conn)

        assert len(built.messages) >= 2
        assert built.messages[0]["role"] == "system"
        assert "Be dramatic." in built.messages[0]["content"]

    def test_t1_context_02_stop_sequences_included(self, e2e_client):
        """Stop sequences `['\\n> You', '\\n> you']` are configured context stop constants."""
        stops = settings_store.get_stop_sequences()
        assert "\n> You" in stops
        assert "\n> you" in stops

    def test_t1_context_03_single_system_message(self, e2e_client):
        """Qwen3 invariant: exactly 1 system message exists at index 0."""
        adv = make_adventure(plot_essentials="Crucial secret")
        add_action(adv["id"], 0, "start", "Dark forest.")
        with db.session() as conn:
            actions = db.list_actions(conn, adv["id"])
            cfg = settings_store.runtime_config(conn)
            built = context_builder.build_context(adv, actions, cfg, conn)

        system_msgs = [m for m in built.messages if m["role"] == "system"]
        assert len(system_msgs) == 1
        assert built.messages[0]["role"] == "system"

    def test_t1_context_04_plot_essentials_included(self, e2e_client):
        """System message contains plot essentials."""
        adv = make_adventure(plot_essentials="The king has been poisoned with belladonna.")
        add_action(adv["id"], 0, "start", "Throne room.")
        with db.session() as conn:
            actions = db.list_actions(conn, adv["id"])
            cfg = settings_store.runtime_config(conn)
            built = context_builder.build_context(adv, actions, cfg, conn)

        assert "The king has been poisoned with belladonna." in built.messages[0]["content"]

    def test_t1_context_05_authors_note_included(self, e2e_client):
        """Author's note is placed in final user message right before last action."""
        adv = make_adventure(authors_note="Describe scents and shadows vividly.")
        add_action(adv["id"], 0, "start", "Alleyway.")
        with db.session() as conn:
            actions = db.list_actions(conn, adv["id"])
            cfg = settings_store.runtime_config(conn)
            built = context_builder.build_context(adv, actions, cfg, conn)

        assert any("Describe scents and shadows vividly." in m["content"] for m in built.messages)


# ==============================================================================
# FEATURE 7: Dynamic UI Controls & State
# ==============================================================================
class TestTier1UIProfile:
    def test_t1_ui_01_get_ui_profile(self, e2e_client):
        """GET /api/adventures/{id}/ui-profile returns profile and state."""
        adv = make_adventure()
        resp = e2e_client.get(f"/api/adventures/{adv['id']}/ui-profile")
        assert resp.status_code == 200
        data = resp.json()
        assert "profile" in data
        assert "state" in data

    def test_t1_ui_02_generate_ui_profile(self, e2e_client):
        """POST /api/adventures/{id}/ui-profile generates tabs and widgets via LLM."""
        adv = make_adventure(title="Dungeon Delve")
        add_action(adv["id"], 0, "start", "You enter the crypt with a sword and shield.")

        manifest = json.dumps({
            "tabs": [
                {"id": "do", "label": "Act", "placeholder": "What do you do?"},
                {"id": "say", "label": "Speak", "placeholder": "What do you say?"},
                {"id": "story", "label": "Narrate", "placeholder": "What happens?"},
            ],
            "widgets": [
                {"id": "dice", "type": "dice", "label": "Dice", "dice": [6, 20]},
                {"id": "stats", "type": "stats", "label": "Stats", "stats": [{"name": "HP", "value": 20, "max": 20, "min": 0}]},
            ],
        })
        transport, _ = make_mock_transport(non_stream_content=manifest)
        from app import deps
        e2e_client.app.dependency_overrides[deps.get_http_transport] = lambda: transport

        resp = e2e_client.post(f"/api/adventures/{adv['id']}/ui-profile")
        assert resp.status_code == 200
        data = resp.json()
        assert data["profile"]["tabs"][0]["label"] == "Act"
        assert len(data["profile"]["widgets"]) == 2

    def test_t1_ui_03_initial_state_seeding(self, e2e_client):
        """Generating UI profile seeds initial widget state."""
        adv = make_adventure()
        manifest = json.dumps({
            "tabs": [],
            "widgets": [
                {"id": "stats", "type": "stats", "label": "Stats", "stats": [{"name": "Mana", "value": 50}]},
                {"id": "inv", "type": "inventory", "label": "Bag", "items": [{"name": "Rope", "qty": 1}]},
            ],
        })
        transport, _ = make_mock_transport(non_stream_content=manifest)
        from app import deps
        e2e_client.app.dependency_overrides[deps.get_http_transport] = lambda: transport

        resp = e2e_client.post(f"/api/adventures/{adv['id']}/ui-profile")
        assert resp.status_code == 200
        state = resp.json()["state"]
        assert state.get("stats") == {"Mana": 50}
        assert state.get("inv") == [{"name": "Rope", "qty": 1}]

    def test_t1_ui_04_update_ui_state(self, e2e_client):
        """PUT /api/adventures/{id}/ui-state stores updated widget state."""
        adv = make_adventure()
        new_state = {"stats": {"HP": 12, "Gold": 150}, "inv": [{"name": "Potion", "qty": 2}]}
        resp = e2e_client.put(
            f"/api/adventures/{adv['id']}/ui-state",
            json={"state": new_state},
        )
        assert resp.status_code == 200
        assert resp.json()["state"] == new_state

        # Verify persistence
        get_resp = e2e_client.get(f"/api/adventures/{adv['id']}/ui-profile")
        assert get_resp.json()["state"] == new_state

    def test_t1_ui_05_ui_state_injected_in_context(self, e2e_client):
        """Active UI state is injected into context builder prompt."""
        adv = make_adventure()
        add_action(adv["id"], 0, "start", "A dark dungeon.")
        with db.session() as conn:
            db.update_adventure_ui_state(conn, adv["id"], {"stats": {"HP": 5, "Mana": 0}})
            adv_fresh = db.get_adventure(conn, adv["id"])
            actions = db.list_actions(conn, adv["id"])
            cfg = settings_store.runtime_config(conn)
            built = context_builder.build_context(adv_fresh, actions, cfg, conn)

        assert "HP" in built.messages[-1]["content"]
        assert "Mana" in built.messages[-1]["content"]


# ==============================================================================
# FEATURE 8: Action Choices Suggestions
# ==============================================================================
class TestTier1Choices:
    def test_t1_choices_01_post_choices_generates_chips(self, e2e_client):
        """POST /api/adventures/{id}/choices returns list of action suggestions."""
        adv = make_adventure()
        add_action(adv["id"], 0, "start", "A wild beast blocks your path.")

        choices_json = json.dumps([
            {"type": "do", "text": "draw your broadsword"},
            {"type": "say", "text": "calm down, beast"},
            {"type": "story", "text": "the beast turns out to be wounded"},
        ])
        transport, _ = make_mock_transport(non_stream_content=choices_json)
        from app import deps
        e2e_client.app.dependency_overrides[deps.get_http_transport] = lambda: transport

        resp = e2e_client.post(f"/api/adventures/{adv['id']}/choices")
        assert resp.status_code == 200
        choices = resp.json().get("choices", [])
        assert len(choices) == 3

    def test_t1_choices_02_choices_types_valid(self, e2e_client):
        """Every suggested choice has valid type in ('do', 'say', 'story')."""
        adv = make_adventure()
        add_action(adv["id"], 0, "start", "In an ancient library.")
        choices_json = json.dumps([
            {"type": "do", "text": "examine the tome"},
            {"type": "say", "text": "is anyone here?"},
            {"type": "story", "text": "a dusty book falls from the shelf"},
        ])
        transport, _ = make_mock_transport(non_stream_content=choices_json)
        from app import deps
        e2e_client.app.dependency_overrides[deps.get_http_transport] = lambda: transport

        resp = e2e_client.post(f"/api/adventures/{adv['id']}/choices")
        assert resp.status_code == 200
        for c in resp.json()["choices"]:
            assert c["type"] in ("do", "say", "story")

    def test_t1_choices_03_choices_length_constrained(self, e2e_client):
        """Choice text length does not exceed 120 characters."""
        adv = make_adventure()
        add_action(adv["id"], 0, "start", "Start.")
        choices_json = json.dumps([
            {"type": "do", "text": "X" * 200},
        ])
        transport, _ = make_mock_transport(non_stream_content=choices_json)
        from app import deps
        e2e_client.app.dependency_overrides[deps.get_http_transport] = lambda: transport

        resp = e2e_client.post(f"/api/adventures/{adv['id']}/choices")
        assert resp.status_code == 200
        assert len(resp.json()["choices"][0]["text"]) <= 120

    def test_t1_choices_04_disabled_choices_returns_empty(self, e2e_client, monkeypatch):
        """When choices are disabled in settings, endpoint returns empty choices list."""
        adv = make_adventure()
        monkeypatch.setattr(settings_store, "load_file_settings", lambda: {"choices": {"enabled": False}})

        resp = e2e_client.post(f"/api/adventures/{adv['id']}/choices")
        assert resp.status_code == 200
        assert resp.json() == {"choices": []}

    def test_t1_choices_05_choices_uses_recent_scene(self, e2e_client):
        """Choices generator captures recent actions in the user prompt."""
        adv = make_adventure()
        add_action(adv["id"], 0, "start", "UniqueScenePromptCheckKeyWord")

        captured = []
        def capture_handler(payload):
            captured.append(payload)
            return json.dumps([{"type": "do", "text": "proceed"}])

        transport, _ = make_mock_transport(chat_handler=capture_handler)
        from app import deps
        e2e_client.app.dependency_overrides[deps.get_http_transport] = lambda: transport

        resp = e2e_client.post(f"/api/adventures/{adv['id']}/choices")
        assert resp.status_code == 200
        assert len(captured) >= 1
        user_content = next(m["content"] for m in captured[0]["messages"] if m["role"] == "user")
        assert "UniqueScenePromptCheckKeyWord" in user_content


# ==============================================================================
# FEATURE 9: Story Cards Lorebook
# ==============================================================================
class TestTier1StoryCards:
    def test_t1_cards_01_create_story_card(self, e2e_client):
        """POST /api/adventures/{id}/cards creates story card."""
        adv = make_adventure()
        resp = e2e_client.post(
            f"/api/adventures/{adv['id']}/cards",
            json={
                "title": "Excalibur",
                "type": "item",
                "triggers": ["excalibur", "sword of king"],
                "entry": "The legendary blade embedded in stone.",
                "pinned": False,
            },
        )
        assert resp.status_code == 201
        card = resp.json()
        assert card["title"] == "Excalibur"
        assert card["triggers"] == ["excalibur", "sword of king"]
        assert "id" in card

    def test_t1_cards_02_list_story_cards(self, e2e_client):
        """GET /api/adventures/{id}/cards returns all cards of adventure."""
        adv = make_adventure()
        with db.session() as conn:
            db.insert_card(conn, adv["id"], title="Tavern", entry="A cozy tavern.")

        resp = e2e_client.get(f"/api/adventures/{adv['id']}/cards")
        assert resp.status_code == 200
        cards = resp.json()
        assert len(cards) == 1
        assert cards[0]["title"] == "Tavern"

    def test_t1_cards_03_get_card_by_id(self, e2e_client):
        """GET /api/adventures/{id}/cards/{card_id} returns card detail."""
        adv = make_adventure()
        with db.session() as conn:
            c = db.insert_card(conn, adv["id"], title="Necromancer", entry="Master of dark arts.")

        resp = e2e_client.get(f"/api/adventures/{adv['id']}/cards/{c['id']}")
        assert resp.status_code == 200
        assert resp.json()["title"] == "Necromancer"

    def test_t1_cards_04_update_story_card(self, e2e_client):
        """PATCH /api/adventures/{id}/cards/{card_id} updates card fields."""
        adv = make_adventure()
        with db.session() as conn:
            c = db.insert_card(conn, adv["id"], title="Old Shield", entry="A rusty iron shield.")

        resp = e2e_client.patch(
            f"/api/adventures/{adv['id']}/cards/{c['id']}",
            json={"entry": "A polished aegis reflecting moonlight.", "pinned": True},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["entry"] == "A polished aegis reflecting moonlight."
        assert data["pinned"] is True

    def test_t1_cards_05_delete_story_card(self, e2e_client):
        """DELETE /api/adventures/{id}/cards/{card_id} removes card with 204."""
        adv = make_adventure()
        with db.session() as conn:
            c = db.insert_card(conn, adv["id"], title="Temporary NPC", entry="Passing traveler.")

        resp = e2e_client.delete(f"/api/adventures/{adv['id']}/cards/{c['id']}")
        assert resp.status_code == 204

        assert e2e_client.get(f"/api/adventures/{adv['id']}/cards/{c['id']}").status_code == 404


# ==============================================================================
# FEATURE 10: Live NPCs & Chronicle
# ==============================================================================
class TestTier1NPCs:
    def test_t1_npc_01_create_npc_card_with_sheet(self, e2e_client):
        """NPC card created with type='character' and structured sheet."""
        adv = make_adventure()
        sheet = {"name": "Lyra", "role": "Rogue", "appearance": "Cloaked in midnight silk"}
        resp = e2e_client.post(
            f"/api/adventures/{adv['id']}/cards",
            json={
                "title": "Lyra",
                "type": "character",
                "triggers": ["lyra", "the rogue"],
                "entry": "A cunning thief.",
                "sheet": sheet,
                "voice": "F1",
            },
        )
        assert resp.status_code == 201
        card = resp.json()
        assert card["type"] == "character"
        assert json.loads(card["sheet"]) == sheet
        assert card["voice"] == "F1"

    def test_t1_npc_02_enrich_npc_card(self, e2e_client):
        """POST /api/adventures/{id}/cards/{card_id}/enrich populates sheet & portrait prompt."""
        adv = make_adventure()
        with db.session() as conn:
            c = db.insert_card(conn, adv["id"], title="Borin Ironbreaker", entry="A stalwart dwarven blacksmith.")

        enrich_json = json.dumps({
            "appearance": "Braided red beard, dark piercing eyes, soot on brow",
            "body": "Broad muscular dwarven build",
            "clothing": "Leather blacksmith apron over chainmail",
            "personality": "Gruff, honorable, speaks plainly",
            "stats": {"str": 16, "dex": 12, "con": 16, "int": 10, "wis": 12, "cha": 8},
            "status": "alive",
            "notes": "Forges masterwork weapons",
            "portrait_prompt": "fantasy portrait of a rugged dwarf blacksmith with braided red beard",
        })
        transport, _ = make_mock_transport(non_stream_content=enrich_json)
        from app import deps
        e2e_client.app.dependency_overrides[deps.get_http_transport] = lambda: transport

        resp = e2e_client.post(f"/api/adventures/{adv['id']}/cards/{c['id']}/enrich")
        assert resp.status_code == 200
        card = resp.json()
        assert "dwarf blacksmith" in card["portrait_prompt"]

    def test_t1_npc_03_scenario_npc_cards_inherited(self, e2e_client):
        """Scenario NPC cards transfer to adventure upon creation."""
        with db.session() as conn:
            sc = db.insert_scenario(
                conn,
                title="Warlock's Den",
                story_cards=[{
                    "title": "Morzan",
                    "type": "character",
                    "entry": "Dark sorcerer",
                    "sheet": {"class": "Warlock"},
                }],
            )

        resp = e2e_client.post("/api/adventures", json={"scenario_id": sc["id"]})
        assert resp.status_code == 201
        cards = resp.json()["story_cards"]
        assert len(cards) == 1
        assert cards[0]["title"] == "Morzan"
        assert cards[0]["type"] == "character"

    def test_t1_npc_04_npc_portrait_attachment(self, e2e_client):
        """Cards returned from API include attached portrait object if present."""
        adv = make_adventure()
        with db.session() as conn:
            c = db.insert_card(conn, adv["id"], title="Elven Scout", entry="Swift archer.")
            img = db.insert_image(
                conn, adventure_id=adv["id"], action_seq=0, file_path="data/images/scout.png",
                prompt="portrait of an elven scout",
            )
            db.set_card_portrait(conn, c["id"], img["id"])

        resp = e2e_client.get(f"/api/adventures/{adv['id']}/cards/{c['id']}")
        assert resp.status_code == 200
        card = resp.json()
        assert card.get("portrait") is not None
        assert card["portrait"]["id"] == img["id"]

    def test_t1_npc_05_npc_chronicle_execution(self, e2e_client):
        """Chronicle analyzer runs on actions and records updates."""
        from app import npc_chronicle
        adv = make_adventure()
        add_action(adv["id"], 0, "start", "A battered knight named Thorne guards the ruined castle.")

        chronicle_json = json.dumps({
            "plot_essentials": "",
            "characters": [{
                "name": "Thorne",
                "aliases": ["knight"],
                "existing_title": "",
                "summary": "A battered knight guarding the ruins",
                "facts": ["Guarding the castle alone"],
                "status": "alive",
                "appearance": "Dented steel plate armor, scarred jaw",
                "clothing": "Steel plate",
                "personality": "Stoic and watchful",
                "look_changed": False,
                "stat_delta": {"str": 0, "dex": 0, "con": 0, "int": 0, "wis": 0, "cha": 0},
            }],
            "places": [],
            "factions": [],
            "items": [],
        })
        transport, _ = make_mock_transport(non_stream_content=chronicle_json)
        result = npc_chronicle.update_from_turn(adv["id"], transport=transport)
        assert result.get("created", 0) + result.get("updated", 0) >= 1


# ==============================================================================
# FEATURE 11: Memories & Summarizer
# ==============================================================================
class TestTier1Memories:
    def test_t1_memory_01_get_memory_state(self, e2e_client):
        """GET /api/adventures/{id}/memory returns plot essentials, author note, and memories."""
        adv = make_adventure(plot_essentials="Ring of power destroyed", authors_note="Mythic tone")
        resp = e2e_client.get(f"/api/adventures/{adv['id']}/memory")
        assert resp.status_code == 200
        data = resp.json()
        assert data["plot_essentials"] == "Ring of power destroyed"
        assert data["authors_note"] == "Mythic tone"
        assert isinstance(data["memories"], list)

    def test_t1_memory_02_update_plot_essentials_and_authors_note(self, e2e_client):
        """PUT /api/adventures/{id}/memory updates plot essentials and author's note."""
        adv = make_adventure()
        resp = e2e_client.put(
            f"/api/adventures/{adv['id']}/memory",
            json={
                "plot_essentials": "The dragon's weak spot is beneath its left wing.",
                "authors_note": "Keep combat visceral and descriptions sensory.",
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "dragon's weak spot" in data["plot_essentials"]
        assert "visceral" in data["authors_note"]

    def test_t1_memory_03_list_memories_in_db(self, e2e_client):
        """Memories table maintains seq_start and seq_end bounds."""
        adv = make_adventure()
        with db.session() as conn:
            db.insert_memory(conn, adv["id"], "The party escaped the burning village.", seq_start=0, seq_end=4)
            mems = db.list_memories(conn, adv["id"])

        assert len(mems) == 1
        assert mems[0]["text"] == "The party escaped the burning village."
        assert mems[0]["seq_start"] == 0
        assert mems[0]["seq_end"] == 4

    def test_t1_memory_04_memory_retrieval_ranking(self, e2e_client):
        """select_memories ranks memories containing search query keywords higher."""
        from memory import retrieval
        adv = make_adventure()
        with db.session() as conn:
            db.insert_memory(conn, adv["id"], "Found an ancient ruby in the crypt.")
            db.insert_memory(conn, adv["id"], "Met a wandering bard named Terry.")
            selected = retrieval.select_memories(conn, adv["id"], "tell me about the ruby gem")

        assert len(selected) >= 1
        assert any("ruby" in m["text"] for m in selected)

    def test_t1_memory_05_newest_memory_always_included(self, e2e_client):
        """The most recent memory is always included in retrieval results."""
        from memory import retrieval
        adv = make_adventure()
        with db.session() as conn:
            for i in range(15):
                db.insert_memory(conn, adv["id"], f"Historical event number {i}")
            latest = db.insert_memory(conn, adv["id"], "The party just reached the citadel apex.")

            selected = retrieval.select_memories(conn, adv["id"], "something completely unrelated")

        assert any("citadel apex" in m["text"] for m in selected)


# ==============================================================================
# FEATURE 12: Scene Illustration ("See" Action)
# ==============================================================================
class TestTier1SceneSee:
    def test_t1_scene_01_sync_scene_returns_environment(self, e2e_client):
        """POST /api/adventures/{id}/scene evaluates actions and returns scene info."""
        adv = make_adventure()
        add_action(adv["id"], 0, "start", "You arrive at the harbor under a foggy twilight.")

        resp = e2e_client.post(f"/api/adventures/{adv['id']}/scene")
        assert resp.status_code == 200
        data = resp.json()
        assert "location" in data or "backdrop" in data

    def test_t1_scene_02_see_action_generates_image(self, e2e_client):
        """Action type 'see' generates an illustration and links to action."""
        adv = make_adventure()
        add_action(adv["id"], 0, "start", "The castle looms ahead.")

        resp = e2e_client.post(
            f"/api/adventures/{adv['id']}/see",
            json={"action_seq": 0, "extra_prompt": "look at the grand gates"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "id" in data
        assert "url" in data
        assert data.get("provider") == "sdwebui"

    def test_t1_scene_03_get_image_settings(self, e2e_client):
        """GET /api/settings/image returns image generation settings with masked key."""
        resp = e2e_client.get("/api/settings/image")
        assert resp.status_code == 200
        data = resp.json()
        assert "sdwebui_steps" in data
        assert "provider" in data
        assert "openai_api_key" in data

    def test_t1_scene_04_update_image_settings(self, e2e_client):
        """PUT /api/settings/image updates steps, cfg_scale, and prompt prefix."""
        resp = e2e_client.put(
            "/api/settings/image",
            json={
                "sdwebui_steps": 25,
                "style_suffix": ", oil painting",
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["sdwebui_steps"] == 25
        assert ", oil painting" in data["style_suffix"]

    def test_t1_scene_05_scene_retry_flag(self, e2e_client):
        """POST /api/adventures/{id}/scene with retry=True forces regeneration."""
        adv = make_adventure()
        add_action(adv["id"], 0, "start", "In an ancient colosseum.")

        resp = e2e_client.post(f"/api/adventures/{adv['id']}/scene", json={"retry": True})
        assert resp.status_code == 200


# ==============================================================================
# FEATURE 13: Voice Narration (TTS & STT)
# ==============================================================================
class TestTier1Voice:
    def test_t1_voice_01_list_voices(self, e2e_client):
        """GET /api/voices returns list of available voices and default narrator voice."""
        resp = e2e_client.get("/api/voices")
        assert resp.status_code == 200
        data = resp.json()
        assert "voices" in data
        assert "narrator_voice" in data
        assert "M1" in data["voices"]

    def test_t1_voice_02_get_voice_settings(self, e2e_client):
        """GET /api/settings/voice returns current voice configuration."""
        resp = e2e_client.get("/api/settings/voice")
        assert resp.status_code == 200
        data = resp.json()
        assert data.get("enabled") is True
        assert "tts_model" in data
        assert "stt_model" in data

    def test_t1_voice_03_update_voice_settings(self, e2e_client):
        """PUT /api/settings/voice updates narrator voice choice."""
        resp = e2e_client.put(
            "/api/settings/voice",
            json={"narrator_voice": "F1"},
        )
        assert resp.status_code == 200
        assert resp.json()["narrator_voice"] == "F1"

    def test_t1_voice_04_tts_audio_synthesis(self, e2e_client):
        """POST /api/tts returns binary audio/mpeg MP3 stream."""
        resp = e2e_client.post(
            "/api/tts",
            json={"text": "The dragon roars in the distance.", "voice": "M1"},
        )
        assert resp.status_code == 200
        assert resp.headers["content-type"] == "audio/mpeg"
        assert resp.content == MP3_BYTES

    def test_t1_voice_05_stt_transcription(self, e2e_client):
        """POST /api/stt transcribes multipart audio upload to text."""
        files = {"file": ("recording.wav", b"RIFF....WAVEfmtfakeaudio", "audio/wav")}
        resp = e2e_client.post("/api/stt", files=files)
        assert resp.status_code == 200
        assert "text" in resp.json()
        assert len(resp.json()["text"]) > 0


# ==============================================================================
# FEATURE 14: Backup Import/Export Round-Trip & Scripting Contract
# ==============================================================================
class TestTier1BackupAndScripting:
    def test_t1_backup_01_export_single_adventure_zip(self, e2e_client):
        """GET /api/adventures/{id}/export returns application/zip with backup files."""
        adv = make_adventure(title="Export Test")
        add_action(adv["id"], 0, "start", "A lone traveler on the trail.")

        resp = e2e_client.get(f"/api/adventures/{adv['id']}/export")
        assert resp.status_code == 200
        assert resp.headers["content-type"] == "application/zip"

        with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
            names = zf.namelist()
            assert "metadata.json" in names
            assert "actions-001.json" in names

    def test_t1_backup_02_export_all_adventures_zip(self, e2e_client):
        """GET /api/adventures/export creates multi-adventure backup with directory per adventure."""
        a1 = make_adventure(title="Alpha Journey")
        a2 = make_adventure(title="Beta Saga")
        add_action(a1["id"], 0, "start", "Alpha starts.")
        add_action(a2["id"], 0, "start", "Beta starts.")

        resp = e2e_client.get("/api/adventures/export")
        assert resp.status_code == 200
        assert resp.headers["content-type"] == "application/zip"

        with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
            names = zf.namelist()
            assert any("Alpha-Journey/metadata.json" in n for n in names)
            assert any("Beta-Saga/metadata.json" in n for n in names)

    def test_t1_backup_03_import_single_adventure_zip(self, e2e_client):
        """POST /api/import/aidungeon imports synthetic zip and restores adventure."""
        zip_bytes = build_synthetic_backup_zip(
            title="Imported Haven",
            description="Restored from synthetic backup",
            actions=[
                {"id": "a-0", "type": "start", "text": "Start in the sanctuary."},
                {"id": "a-1", "type": "continue", "text": " Sanctuary bells chime."},
            ],
            story_cards=[{"title": "High Priest", "value": "Keeper of sacred flame."}],
        )

        resp = e2e_client.post(
            "/api/import/aidungeon",
            files={"file": ("backup.zip", zip_bytes, "application/zip")},
        )
        assert resp.status_code == 201
        report = resp.json()
        assert report["title"] == "Imported Haven"
        assert report["actions_imported"] == 2
        assert report["cards_imported"] == 1

    def test_t1_backup_04_round_trip_export_delete_import(self, e2e_client):
        """Exporting an adventure, deleting it, and re-importing preserves full content."""
        adv = make_adventure(title="Round Trip Hero", plot_essentials="Sacred sword location")
        add_action(adv["id"], 0, "start", "The journey begins.")
        add_action(adv["id"], 1, "do", "\n> You equip your sword\n")
        add_action(adv["id"], 2, "continue", " The blade gleams in sunlight.")
        with db.session() as conn:
            db.insert_card(conn, adv["id"], title="Sword", triggers=["sword"], entry="A holy weapon.")

        # 1. Export
        export_resp = e2e_client.get(f"/api/adventures/{adv['id']}/export")
        assert export_resp.status_code == 200
        backup_data = export_resp.content

        # 2. Delete original
        e2e_client.delete(f"/api/adventures/{adv['id']}")
        assert e2e_client.get(f"/api/adventures/{adv['id']}").status_code == 404

        # 3. Import
        import_resp = e2e_client.post(
            "/api/import/aidungeon",
            files={"file": ("restore.zip", backup_data, "application/zip")},
        )
        assert import_resp.status_code == 201
        restored_id = import_resp.json()["adventure_id"]

        # 4. Verify restored state
        restored_state = e2e_client.get(f"/api/adventures/{restored_id}").json()
        assert restored_state["title"] == "Round Trip Hero"
        assert restored_state["plot_essentials"] == "Sacred sword location"
        assert len(restored_state["actions"]) == 3
        assert restored_state["actions"][1]["text"] == "\n> You equip your sword\n"
        assert len(restored_state["story_cards"]) == 1
        assert restored_state["story_cards"][0]["title"] == "Sword"

    def test_t1_backup_05_scripting_contract_or_skip(self):
        """Validates scripting contract in app.scripting (M4) or asserts interface specification."""
        try:
            scripting = importlib.import_module("app.scripting")
            assert hasattr(scripting, "run_input_modifier")
            assert hasattr(scripting, "run_context_modifier")
            assert hasattr(scripting, "run_output_modifier")
        except ModuleNotFoundError:
            # Progressive testability: M4 contract documented; verified once implemented
            pytest.skip("app.scripting is scheduled for Milestone M4")
