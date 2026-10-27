"""Context cap: llm.context_size above the per-slot max clamps on read.

Stored overrides are not rewritten. context_size_max comes from
config/settings.json llm.context_size_limits.max (106496).
"""

from __future__ import annotations

import json

from app import db, settings_store


def test_oversized_context_size_clamps_on_read(client):
    with db.session() as conn:
        db.set_setting(conn, "llm.context_size", json.dumps(262144))
    resp = client.get("/api/settings/llm")
    assert resp.status_code == 200
    data = resp.json()
    assert data["context_size"] == 106496
    assert data["context_size_max"] == 106496
    with db.session() as conn:
        stored = json.loads(db.get_setting(conn, "llm.context_size"))
    assert stored == 262144


def test_context_size_within_cap_passes_through(client):
    with db.session() as conn:
        db.set_setting(conn, "llm.context_size", json.dumps(65536))
    data = client.get("/api/settings/llm").json()
    assert data["context_size"] == 65536
    assert data["context_size_max"] == 106496


def test_file_defaults_are_32k_with_dry_window(client):
    data = client.get("/api/settings/llm").json()
    assert data["context_size"] == 32768
    assert data["max_tokens"] == 300
    assert data["dry_penalty_last_n"] == 512
    file_cfg = settings_store.load_file_settings()
    assert file_cfg["context"]["context_size"] == 32768
    assert file_cfg["llm"]["context_size"] == 32768
    assert file_cfg["sampler"]["max_tokens"] == 300
    assert file_cfg["sampler"]["dry_penalty_last_n"] == 512
    scaling = file_cfg["context"]["dynamic_scaling"]
    assert scaling["enabled"] is True
    assert scaling["max_scale_factor"] == 1.0
    budgets = file_cfg["context"]["budgets"]
    fixed = sum(budgets[key] for key in (
        "ai_instructions", "plot_essentials", "character_rules", "story_cards",
        "memory_bank", "story_summary", "authors_note", "last_action",
        "player_state", "npc_minds",
    ))
    assert budgets["response_reserve"] == 1024
    assert budgets["recent_window"] == 32768 - fixed - budgets["response_reserve"]
