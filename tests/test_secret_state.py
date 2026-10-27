"""Visão de share, linhagem do cenário e guards logo após init_db."""

from __future__ import annotations

import sqlite3

import pytest

from app import db, users, world_map
from conftest import TEST_AUTH_USER

_GUARDS = {
    "publication_versions_immutable",
    "publication_versions_no_delete",
    "mod_versions_no_update",
    "mod_versions_no_delete",
}
_SEED = 918273645


class _Row:
    def __init__(self, data: dict):
        self._data = data

    def keys(self):
        return self._data.keys()

    def __getitem__(self, key):
        return self._data[key]


def _scenario_fields(**extra) -> dict:
    data = {
        "id": "s1",
        "title": "Sem linhagem",
        "description": "",
        "ai_instructions": "",
        "plot_essentials": "",
        "authors_note": "",
        "opening_text": "",
        "tags": "[]",
        "story_cards": "[]",
        "third_person": 0,
        "created_at": "t",
        "updated_at": "t",
    }
    data.update(extra)
    return data


def _triggers(conn) -> set[str]:
    return {
        row[0]
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='trigger'")
    }


def test_scenario_to_dict_lineage_follows_columns():
    bare = db.scenario_to_dict(_Row(_scenario_fields()))
    assert "parent_publication_id" not in bare
    assert "parent_version" not in bare

    linked = db.scenario_to_dict(_Row(_scenario_fields(
        parent_publication_id="pub-line",
        parent_version=4,
    )))
    assert linked["parent_publication_id"] == "pub-line"
    assert linked["parent_version"] == 4

    empty = db.scenario_to_dict(_Row(_scenario_fields(
        parent_publication_id=None,
        parent_version=None,
    )))
    assert empty["parent_publication_id"] is None
    assert empty["parent_version"] is None


def test_get_scenario_returns_lineage(tmp_db):
    with db.session() as conn:
        scenario = db.insert_scenario(conn, title="Filho", owner=TEST_AUTH_USER)
        now = db.utcnow()
        conn.execute(
            "INSERT INTO publications("
            "id, kind, source_id, owner, title, created_at, updated_at"
            ") VALUES(?,?,?,?,?,?,?)",
            ("pub-line", "scenario", scenario["id"], TEST_AUTH_USER, "Pai", now, now),
        )
        conn.execute(
            "UPDATE scenarios SET parent_publication_id=?, parent_version=? WHERE id=?",
            ("pub-line", 2, scenario["id"]),
        )
        loaded = db.get_scenario(conn, scenario["id"])
    assert loaded["parent_publication_id"] == "pub-line"
    assert loaded["parent_version"] == 2


def test_adventure_state_owner_share_and_internal(tmp_db):
    with db.session() as conn:
        adv = db.insert_adventure(conn, title="Segredo", owner=TEST_AUTH_USER)
        users.create_user(conn, "Mira", "mira-pass")
        db.share_adventure(conn, adv["id"], "Mira")
        beach = db.insert_card(conn, adv["id"], type="location", title="Praia", entry="Areia.")
        cave = db.insert_card(
            conn, adv["id"], type="location", title="Caverna Secreta", entry="Ouro escondido.",
        )
        hero = db.insert_card(conn, adv["id"], type="character", title="Heroi", entry="Aliada.")
        world_map.set_current_location(conn, adv["id"], beach["id"])
        world_map.ensure_link(
            conn, adv["id"], beach["id"], cave["id"], discovered=False,
        )
        db.set_sim_state(conn, adv["id"], {
            "v": 1,
            "pack": "d20",
            "actors": {"tester": {"name": "Tester", "hp": 9}},
            "foes": {},
            "life": None,
            "rng": {"seed": _SEED, "counter": 7},
            "quests": [
                {
                    "id": "q-hid", "title": "Plano oculto", "description": "SPOILER-OCULTO",
                    "status": "hidden", "stages": [], "progress": {"value": 0, "target": 1},
                },
                {
                    "id": "q-plan", "title": "Ainda planejada", "description": "",
                    "status": "planned", "stages": [], "progress": {"value": 0, "target": 1},
                },
                {
                    "id": "q-ok", "title": "Visivel", "description": "pode ver",
                    "status": "available", "stages": [], "progress": {"value": 0, "target": 1},
                },
                {
                    "id": "q-acc", "title": "Aceita", "description": "",
                    "status": "accepted", "stages": [], "progress": {"value": 0, "target": 1},
                },
                {
                    "id": "q-done", "title": "Feita", "description": "",
                    "status": "completed", "stages": [], "progress": {"value": 1, "target": 1},
                },
                {
                    "id": "q-fail", "title": "Falhou", "description": "",
                    "status": "failed", "stages": [], "progress": {"value": 0, "target": 1},
                },
            ],
            "triggers": [{
                "id": "t-secret",
                "when": [{"fact": "hp", "op": "lte", "value": 1}],
                "apply": [{"quest_id": "q-hid", "status": "available"}],
                "applied": False,
            }],
        })
        owner = db.adventure_state(conn, adv["id"], viewer=TEST_AUTH_USER)
        admin = db.adventure_state(conn, adv["id"], viewer="Caio")
        share = db.adventure_state(conn, adv["id"], viewer="Mira")
        internal = db.adventure_state(conn, adv["id"], viewer=None)
        beach_id, cave_id, hero_id = beach["id"], cave["id"], hero["id"]

    for full in (owner, admin, internal):
        assert full["sim_state"]["rng"]["seed"] == _SEED
        assert {quest["id"] for quest in full["sim_state"]["quests"]} == {
            "q-hid", "q-plan", "q-ok", "q-acc", "q-done", "q-fail",
        }
        assert full["sim_state"]["triggers"][0]["id"] == "t-secret"
        assert {card["id"] for card in full["story_cards"]} >= {beach_id, cave_id, hero_id}

    assert "rng" not in share["sim_state"]
    assert share["sim_state"]["pack"] == "d20"
    assert share["sim_state"]["actors"]["tester"]["hp"] == 9
    assert {quest["status"] for quest in share["sim_state"]["quests"]} == {
        "available", "accepted", "completed", "failed",
    }
    assert {quest["id"] for quest in share["sim_state"]["quests"]} == {
        "q-ok", "q-acc", "q-done", "q-fail",
    }
    assert share["sim_state"]["triggers"] == []
    visible = {card["id"] for card in share["story_cards"]}
    assert beach_id in visible
    assert hero_id in visible
    assert cave_id not in visible
    dumped = str(share["sim_state"]) + str(share["story_cards"])
    assert "SPOILER-OCULTO" not in dumped
    assert "t-secret" not in dumped
    assert "Ouro escondido" not in dumped
    assert str(_SEED) not in dumped


def test_init_db_installs_immutability_guards(tmp_db):
    with db.session() as conn:
        assert _GUARDS <= _triggers(conn)
        scenario = db.insert_scenario(conn, title="Keep", owner=TEST_AUTH_USER)
        for name in _GUARDS:
            conn.execute(f"DROP TRIGGER IF EXISTS {name}")
        assert not (_GUARDS & _triggers(conn))
        scenario_id = scenario["id"]

    db.init_db()
    with db.session() as conn:
        assert db.get_scenario(conn, scenario_id)["title"] == "Keep"
        assert _GUARDS <= _triggers(conn)
        cols = [row["name"] for row in conn.execute("PRAGMA table_info(scenarios)")]
        assert cols.count("parent_publication_id") == 1
        assert cols.count("parent_version") == 1
        now = db.utcnow()
        conn.execute(
            "INSERT INTO publications("
            "id, kind, source_id, owner, title, created_at, updated_at"
            ") VALUES(?,?,?,?,?,?,?)",
            ("pub-g", "scenario", scenario_id, TEST_AUTH_USER, "G", now, now),
        )
        conn.execute(
            "INSERT INTO publication_versions("
            "publication_id, version, snapshot, content_hash, created_at"
            ") VALUES(?,?,?,?,?)",
            ("pub-g", 1, "{}", "hash", now),
        )
        conn.execute(
            "INSERT INTO mods("
            "id, owner, title, description, visibility, created_at, updated_at"
            ") VALUES(?,?,?,?,?,?,?)",
            ("mod-g", TEST_AUTH_USER, "M", "", "public", now, now),
        )
        conn.execute(
            "INSERT INTO mod_versions("
            "id, mod_id, version, content_hash, package_json, created_at"
            ") VALUES(?,?,?,?,?,?)",
            ("mv-g", "mod-g", 1, "hash", "{}", now),
        )

    db.init_db()
    with db.session() as conn:
        assert db.get_scenario(conn, scenario_id)["title"] == "Keep"
        assert conn.execute("SELECT COUNT(*) FROM scenarios").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM publication_versions").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM mod_versions").fetchone()[0] == 1
        guard = conn.execute(
            "SELECT id FROM publication_version_delete_guard WHERE id=1"
        ).fetchone()
        assert guard is not None
        assert _GUARDS <= _triggers(conn)

    with pytest.raises(sqlite3.IntegrityError, match="publication_versions are immutable"):
        with db.session() as conn:
            conn.execute(
                "UPDATE publication_versions SET snapshot=? WHERE publication_id=?",
                ("[]", "pub-g"),
            )
    with pytest.raises(sqlite3.IntegrityError, match="publication_versions are immutable"):
        with db.session() as conn:
            conn.execute(
                "DELETE FROM publication_versions WHERE publication_id=?",
                ("pub-g",),
            )
    with pytest.raises(sqlite3.IntegrityError, match="mod_versions are immutable"):
        with db.session() as conn:
            conn.execute(
                "UPDATE mod_versions SET package_json=? WHERE id=?",
                ("[]", "mv-g"),
            )
    with pytest.raises(sqlite3.IntegrityError, match="mod_versions are immutable"):
        with db.session() as conn:
            conn.execute("DELETE FROM mod_versions WHERE id=?", ("mv-g",))
