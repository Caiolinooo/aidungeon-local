"""Context cap: llm.context_size above the per-slot max clamps on read.

Stored overrides are not rewritten. context_size_max comes from
config/settings.json llm.context_size_limits.max (106496).
"""

from __future__ import annotations

import json

from app import db


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
