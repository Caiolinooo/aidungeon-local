"""Mods: versões imutáveis, apply não-destrutivo, append e scripts por enabled."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from fastapi import Depends, FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.testclient import TestClient
from starlette.requests import Request
from starlette.responses import JSONResponse

from app import auth as auth_core
from app import context_builder as cb
from app import db, scripting, settings_store, users
from app.auth import require_auth
from app.errors import GameError
from conftest import TEST_AUTH_SECRET, TEST_AUTH_USER, add_action, make_adventure

_MIGRATION = Path(__file__).resolve().parents[1] / "backend" / "migrations" / "038_mods.sql"


def _app() -> FastAPI:
    """App mínimo. O owner monta o mesmo router em app.main."""
    from app.routes.mods import router

    app = FastAPI()

    @app.exception_handler(GameError)
    def game_error_handler(request: Request, exc: GameError) -> JSONResponse:
        error = {"code": exc.code, "message": exc.message}
        if exc.hint:
            error["hint"] = exc.hint
        return JSONResponse(status_code=exc.status, content={"error": error})

    @app.exception_handler(RequestValidationError)
    def validation_error_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
        messages = "; ".join(
            f"{'.'.join(str(part) for part in err.get('loc', []))}: {err.get('msg', '')}"
            for err in exc.errors()
        )
        return JSONResponse(
            status_code=422,
            content={"error": {"code": "validation_error", "message": messages or "invalid payload"}},
        )

    app.include_router(
        router, prefix="/api", tags=["mods"], dependencies=[Depends(require_auth)],
    )
    return app


@pytest.fixture()
def client(mods_schema):
    with TestClient(_app()) as api:
        api.headers["Authorization"] = f"Bearer {_token(TEST_AUTH_USER)}"
        yield api


@pytest.fixture()
def mods_schema(tmp_db):
    with db.session() as conn:
        db._apply_migration(conn, _MIGRATION)
        from app import mods
        mods.install_version_guards(conn)
    return tmp_db


def _token(username: str) -> str:
    return auth_core.make_token(username, TEST_AUTH_SECRET, ttl_h=1)


def _as(client, username: str) -> None:
    client.headers["Authorization"] = f"Bearer {_token(username)}"


def _input(suffix: str) -> str:
    return f"action['text'] = action['text'] + '{suffix}'"


def _context(suffix: str) -> str:
    return (
        "def modifier(ctx):\n"
        "    ctx = list(ctx)\n"
        f"    ctx.append({{'role': 'system', 'content': 'CTX_{suffix}'}})\n"
        "    return ctx\n"
    )


def _output(suffix: str) -> str:
    return (
        "def modifier(t):\n"
        f"    return t + '{suffix}'\n"
    )


def _package(suffix: str = "M") -> dict:
    return {
        "actions": [{"text": "nope"}],
        "ai_instructions": "SHOULD_NOT_WRITE",
        "sim_state": {"hp": 1},
        "map_state": {"current_card_id": "nope"},
        "cards": [
            {
                "key": f"inn-{suffix}",
                "title": f"Mod Inn {suffix}",
                "type": "location",
                "triggers": ["inn"],
                "entry": f"entry {suffix}",
                "pinned": False,
            },
            {
                "key": f"road-{suffix}",
                "title": f"Mod Road {suffix}",
                "type": "location",
                "triggers": ["road"],
                "entry": "road",
                "pinned": False,
            },
        ],
        "links": [{
            "from_key": f"inn-{suffix}",
            "to_key": f"road-{suffix}",
            "travel_text": f"walk {suffix}",
            "travel_cost": "1",
            "bidirectional": True,
        }],
        "append": {
            "authors_note": f"MODAPPEND_NOTE_{suffix}",
            "ai_instructions": f"MODAPPEND_AI_{suffix}",
            "plot_essentials": f"MODAPPEND_PLOT_{suffix}",
        },
        "scripts": {
            "input_modifier": _input(suffix),
            "context_modifier": _context(suffix),
            "output_modifier": _output(suffix),
        },
    }


def _publish(client, suffix: str = "M", **fields) -> dict:
    body = {
        "title": fields.get("title", f"Pacote {suffix}"),
        "description": fields.get("description", f"desc {suffix}"),
        "visibility": fields.get("visibility", "public"),
        "package": fields.get("package", _package(suffix)),
    }
    res = client.post("/api/mods", json=body)
    assert res.status_code == 200, res.text
    return res.json()


def _snapshot(adventure_id: str) -> dict:
    with db.session() as conn:
        adv = conn.execute(
            "SELECT ai_instructions, plot_essentials, authors_note, story_summary, "
            "sim_state, character_rules FROM adventures WHERE id=?",
            (adventure_id,),
        ).fetchone()
        actions = conn.execute(
            "SELECT seq, type, text FROM actions WHERE adventure_id=? ORDER BY seq",
            (adventure_id,),
        ).fetchall()
        cards = conn.execute(
            "SELECT id, title, entry, origin_mod_version_id, updated_at "
            "FROM story_cards WHERE adventure_id=? ORDER BY id",
            (adventure_id,),
        ).fetchall()
        links = conn.execute(
            "SELECT id, from_card_id, to_card_id, travel_text, origin_mod_version_id "
            "FROM location_links WHERE adventure_id=? ORDER BY id",
            (adventure_id,),
        ).fetchall()
        map_row = conn.execute(
            "SELECT current_card_id FROM map_state WHERE adventure_id=?",
            (adventure_id,),
        ).fetchone()
    return {
        "adv": tuple(adv),
        "actions": [tuple(row) for row in actions],
        "cards": [tuple(row) for row in cards],
        "links": [tuple(row) for row in links],
        "map": None if map_row is None else map_row["current_card_id"],
    }


def _prepare_save(owner: str = TEST_AUTH_USER) -> dict:
    adv = make_adventure(
        title="Save",
        owner=owner,
        ai_instructions="BASE_AI",
        plot_essentials="BASE_PLOT",
        authors_note="BASE_NOTE",
        story_summary="BASE_SUMMARY",
    )
    add_action(adv["id"], 0, "start", "The gate stands shut.")
    with db.session() as conn:
        keep = db.insert_card(conn, adv["id"], title="User Keep", type="location", entry="keep")
        gate = db.insert_card(conn, adv["id"], title="User Gate", type="location", entry="gate")
        link_id = db.new_id()
        conn.execute(
            "INSERT INTO location_links("
            "id, adventure_id, from_card_id, to_card_id, travel_text, travel_cost, "
            "bidirectional, discovered, created_at) VALUES(?,?,?,?,?,?,?,?,?)",
            (link_id, adv["id"], keep["id"], gate["id"], "user path", "0", 1, 1, db.utcnow()),
        )
        conn.execute(
            "INSERT INTO map_state(adventure_id, current_card_id, updated_at) VALUES(?,?,?)",
            (adv["id"], keep["id"], db.utcnow()),
        )
        db.set_sim_state(conn, adv["id"], {"hp": 7, "gold": 3})
        db.set_setting(conn, f"script:{adv['id']}:input_modifier", _input("A"))
    adv["keep_id"] = keep["id"]
    adv["user_link_id"] = link_id
    return adv


def _build(adv: dict) -> cb.BuiltContext:
    with db.session() as conn:
        actions = db.list_actions(conn, adv["id"])
        fresh = db.get_adventure(conn, adv["id"])
        return cb.build_context(fresh, actions, settings_store.runtime_config(conn), conn)


def test_schema_columns_and_runner_statements(mods_schema):
    raw = _MIGRATION.read_text(encoding="utf-8")
    executable = "\n".join(
        line for line in raw.splitlines() if not line.strip().startswith("--")
    )
    assert "CREATE TRIGGER" not in executable
    with db.session() as conn:
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name IN ('mods', 'mod_versions', 'adventure_mods')"
            )
        }
        cards = {row[1] for row in conn.execute("PRAGMA table_info(story_cards)")}
        links = {row[1] for row in conn.execute("PRAGMA table_info(location_links)")}
    assert tables == {"mods", "mod_versions", "adventure_mods"}
    assert "origin_mod_version_id" in cards
    assert "origin_mod_version_id" in links


def test_publish_versions_are_immutable_and_addressable(client, mods_schema):
    from app import mods

    first = _publish(client, "M")
    version = first["version"]
    assert version["version"] == 1
    normalized = mods.normalize_package(_package("M"))
    assert version["package"] == normalized
    assert version["content_hash"] == mods.content_hash(normalized)
    assert "actions" not in version["package"]
    assert "SHOULD_NOT_WRITE" not in mods.canonical_json(version["package"])

    second = client.post(f"/api/mods/{first['mod']['id']}/versions", json={"package": _package("N")})
    assert second.status_code == 200, second.text
    assert second.json()["version"]["version"] == 2
    assert second.json()["version"]["content_hash"] != version["content_hash"]

    again = client.get(f"/api/mods/{first['mod']['id']}/versions/{version['id']}")
    assert again.status_code == 200
    assert again.json()["package"]["append"]["ai_instructions"] == "MODAPPEND_AI_M"

    listed = client.get("/api/mods", params={"q": "Pacote M"})
    assert listed.status_code == 200
    assert any(item["id"] == first["mod"]["id"] for item in listed.json()["mods"])

    with pytest.raises(sqlite3.IntegrityError):
        with db.session() as conn:
            conn.execute(
                "UPDATE mod_versions SET package_json=? WHERE id=?",
                ("{}", version["id"]),
            )
    with pytest.raises(sqlite3.IntegrityError):
        with db.session() as conn:
            conn.execute("DELETE FROM mod_versions WHERE id=?", (version["id"],))


def test_invalid_package_and_script_are_rejected(client, mods_schema):
    bad_script = client.post("/api/mods", json={
        "title": "Ruim",
        "package": {
            "cards": [],
            "links": [],
            "append": {},
            "scripts": {"input_modifier": "import os\nos.system('echo hacked')"},
        },
    })
    assert bad_script.status_code == 422
    assert bad_script.json()["error"]["code"] == "script_rejected"

    duplicate = client.post("/api/mods", json={
        "title": "Dup",
        "package": _package("M") | {"cards": [
            {"key": "inn-M", "title": "A", "entry": "a"},
            {"key": "inn-M", "title": "B", "entry": "b"},
        ]},
    })
    assert duplicate.status_code == 400
    assert duplicate.json()["error"]["code"] == "invalid_package"

    with db.session() as conn:
        count = conn.execute("SELECT COUNT(*) FROM mods").fetchone()[0]
    assert count == 0


def test_apply_does_not_touch_the_save_and_reapply_is_stable(client, mods_schema):
    adv = _prepare_save()
    before = _snapshot(adv["id"])
    published = _publish(client, "M")
    version_id = published["version"]["id"]
    applied = client.post(f"/api/adventures/{adv['id']}/mods", json={"mod_version_id": version_id})
    assert applied.status_code == 200, applied.text
    body = applied.json()
    assert body["inserted_cards"] == 2
    assert body["inserted_links"] == 1
    assert body["already_applied"] is False
    assert body["enabled"] is True

    after = _snapshot(adv["id"])
    assert after["adv"] == before["adv"]
    assert after["actions"] == before["actions"]
    assert after["map"] == before["map"] == adv["keep_id"]
    assert before["cards"][0] in after["cards"]
    assert before["links"][0] in after["links"]
    origins = [card for card in after["cards"] if card[3] == version_id]
    assert len(origins) == 2
    assert all(card[1].startswith("Mod ") for card in origins)
    origin_links = [link for link in after["links"] if link[4] == version_id]
    assert len(origin_links) == 1
    assert origin_links[0][3] == "walk M"

    with db.session() as conn:
        conn.execute(
            "UPDATE story_cards SET title=?, entry=? WHERE origin_mod_version_id=?",
            ("hacked", "changed", version_id),
        )
    again = client.post(f"/api/adventures/{adv['id']}/mods", json={"mod_version_id": version_id})
    assert again.status_code == 200, again.text
    assert again.json()["inserted_cards"] == 0
    assert again.json()["inserted_links"] == 0
    assert again.json()["already_applied"] is True
    with db.session() as conn:
        titles = [
            row[0] for row in conn.execute(
                "SELECT title FROM story_cards WHERE origin_mod_version_id=?",
                (version_id,),
            )
        ]
    assert sorted(titles) == ["hacked", "hacked"]
    assert _snapshot(adv["id"])["adv"] == before["adv"]
    assert _snapshot(adv["id"])["actions"] == before["actions"]
    assert _snapshot(adv["id"])["map"] == adv["keep_id"]


def test_append_follows_enabled_without_writing_columns(client, mods_schema):
    adv = _prepare_save()
    first = _publish(client, "M")
    second = _publish(client, "Z", title="Pacote Z")
    for published in (first, second):
        res = client.post(
            f"/api/adventures/{adv['id']}/mods",
            json={"mod_version_id": published["version"]["id"]},
        )
        assert res.status_code == 200, res.text

    built = _build(adv)
    system = built.messages[0]["content"]
    final = built.messages[-1]["content"]
    assert system.index("BASE_AI") < system.index("MODAPPEND_AI_M") < system.index("MODAPPEND_AI_Z")
    assert system.index("BASE_PLOT") < system.index("MODAPPEND_PLOT_M") < system.index("MODAPPEND_PLOT_Z")
    assert "MODAPPEND_NOTE_M" in final and "MODAPPEND_NOTE_Z" in final
    assert final.index("BASE_NOTE") < final.index("MODAPPEND_NOTE_M") < final.index("MODAPPEND_NOTE_Z")

    off = client.post(f"/api/adventures/{adv['id']}/mods/{first['version']['id']}/disable")
    assert off.status_code == 200, off.text
    assert off.json()["enabled"] is False
    disabled = _build(adv)
    disabled_system = disabled.messages[0]["content"]
    assert "BASE_AI" in disabled_system
    assert "MODAPPEND_AI_M" not in disabled_system
    assert "MODAPPEND_AI_Z" in disabled_system
    with db.session() as conn:
        still = conn.execute(
            "SELECT COUNT(*) FROM story_cards WHERE origin_mod_version_id=?",
            (first["version"]["id"],),
        ).fetchone()[0]
        column = conn.execute(
            "SELECT ai_instructions FROM adventures WHERE id=?",
            (adv["id"],),
        ).fetchone()[0]
    assert still == 2
    assert column == "BASE_AI"

    back = client.post(f"/api/adventures/{adv['id']}/mods/{first['version']['id']}/enable")
    assert back.status_code == 200
    assert "MODAPPEND_AI_M" in _build(adv).messages[0]["content"]


def test_scripts_keep_precedence_and_respect_enabled(client, mods_schema):
    adv = _prepare_save()
    published = _publish(client, "M")
    other = _publish(client, "Z", title="Pacote Z")
    assert client.post(
        f"/api/adventures/{adv['id']}/mods",
        json={"mod_version_id": published["version"]["id"]},
    ).status_code == 200
    assert client.post(
        f"/api/adventures/{adv['id']}/mods",
        json={"mod_version_id": other["version"]["id"]},
    ).status_code == 200

    with db.session() as conn:
        db.set_setting(conn, "script:global:input_modifier", _input("G"))
        own = db.get_setting(conn, f"script:{adv['id']}:input_modifier")
    assert own == _input("A")

    composed = scripting.get_adventure_script(adv["id"], "input_modifier")
    parts = scripting.chain_parts(composed)
    assert parts == [_input("A"), _input("M"), _input("Z")]
    assert scripting.run_input_modifier(composed, {"type": "do", "text": "x"})["text"] == "xAMZ"

    client.post(f"/api/adventures/{adv['id']}/mods/{published['version']['id']}/disable")
    client.post(f"/api/adventures/{adv['id']}/mods/{other['version']['id']}/disable")
    assert scripting.get_adventure_script(adv["id"], "input_modifier") == _input("A")

    client.post(f"/api/adventures/{adv['id']}/mods/{published['version']['id']}/enable")
    with db.session() as conn:
        db.set_setting(conn, f"script:{adv['id']}:input_modifier", "")
    blocked = scripting.get_adventure_script(adv["id"], "input_modifier")
    assert blocked == _input("M")
    assert "G" not in (blocked or "")
    assert scripting.run_input_modifier(blocked, {"type": "do", "text": "x"})["text"] == "xM"

    with db.session() as conn:
        conn.execute("DELETE FROM settings WHERE key=?", (f"script:{adv['id']}:input_modifier",))
    global_chain = scripting.chain_parts(scripting.get_adventure_script(adv["id"], "input_modifier"))
    assert global_chain[0] == _input("G")
    assert global_chain[1] == _input("M")


def test_remove_deletes_only_that_origin(client, mods_schema):
    adv = _prepare_save()
    before = _snapshot(adv["id"])
    first = _publish(client, "M")
    second = _publish(client, "Z", title="Pacote Z")
    for published in (first, second):
        assert client.post(
            f"/api/adventures/{adv['id']}/mods",
            json={"mod_version_id": published["version"]["id"]},
        ).status_code == 200

    removed = client.delete(f"/api/adventures/{adv['id']}/mods/{first['version']['id']}")
    assert removed.status_code == 200, removed.text
    assert removed.json()["deleted_cards"] == 2
    assert removed.json()["deleted_links"] == 1

    after = _snapshot(adv["id"])
    assert after["adv"] == before["adv"]
    assert after["actions"] == before["actions"]
    assert after["map"] == adv["keep_id"]
    assert before["cards"][0] in after["cards"]
    assert before["links"][0] in after["links"]
    assert all(card[3] != first["version"]["id"] for card in after["cards"])
    assert any(card[3] == second["version"]["id"] for card in after["cards"])
    assert all(link[4] != first["version"]["id"] for link in after["links"])
    assert "MODAPPEND_AI_M" not in _build(adv).messages[0]["content"]
    assert "MODAPPEND_AI_Z" in _build(adv).messages[0]["content"]
    with db.session() as conn:
        still = conn.execute(
            "SELECT package_json FROM mod_versions WHERE id=?",
            (first["version"]["id"],),
        ).fetchone()
    assert still is not None
    assert "MODAPPEND_AI_M" in still[0]
    listed = client.get(f"/api/adventures/{adv['id']}/mods")
    assert [item["mod_version_id"] for item in listed.json()["mods"]] == [second["version"]["id"]]


def test_authorization(client, mods_schema):
    adv = _prepare_save()
    public = _publish(client, "P", title="Publico", visibility="public")

    with TestClient(_app()) as anon:
        assert anon.get("/api/mods").status_code == 401

    with db.session() as conn:
        users.create_user(conn, "Mira", "mira-pass-1")
        db.share_adventure(conn, adv["id"], "Mira")
    _as(client, "Mira")
    shared = client.post(
        f"/api/adventures/{adv['id']}/mods",
        json={"mod_version_id": public["version"]["id"]},
    )
    assert shared.status_code == 403
    assert shared.json()["error"]["code"] == "forbidden"

    _as(client, "Outro")
    published = _publish(client, "M", visibility="private", title="Segredo")
    version_id = published["version"]["id"]

    _as(client, TEST_AUTH_USER)
    hidden = client.get(f"/api/mods/{published['mod']['id']}")
    assert hidden.status_code == 404
    assert client.get("/api/mods", params={"q": "Segredo"}).json()["mods"] == []
    denied_apply = client.post(
        f"/api/adventures/{adv['id']}/mods",
        json={"mod_version_id": version_id},
    )
    assert denied_apply.status_code == 404

    stranger_adv = make_adventure(title="Alheia", owner="Outro")
    missing = client.post(
        f"/api/adventures/{stranger_adv['id']}/mods",
        json={"mod_version_id": public["version"]["id"]},
    )
    assert missing.status_code == 404

    _as(client, "Caio")
    assert client.get(f"/api/mods/{published['mod']['id']}").status_code == 200
    admin_apply = client.post(
        f"/api/adventures/{adv['id']}/mods",
        json={"mod_version_id": version_id},
    )
    assert admin_apply.status_code == 200, admin_apply.text

    other = client.post("/api/mods", json={
        "title": "Do outro",
        "visibility": "public",
        "package": _package("Q"),
    })
    assert other.status_code == 200, other.text
    _as(client, TEST_AUTH_USER)
    forbidden = client.post(
        f"/api/mods/{other.json()['mod']['id']}/versions",
        json={"package": _package("R")},
    )
    assert forbidden.status_code == 403


def test_context_and_script_without_mods_tables(tmp_db):
    adv = make_adventure(title="Plain", ai_instructions="BASE_AI", owner=TEST_AUTH_USER)
    scripting.set_adventure_script(adv["id"], "input_modifier", "action['text'] = 'A'")
    assert scripting.get_adventure_script(adv["id"], "input_modifier") == "action['text'] = 'A'"
    add_action(adv["id"], 0, "start", "Hello.")
    with db.session() as conn:
        actions = db.list_actions(conn, adv["id"])
        built = cb.build_context(adv, actions, settings_store.runtime_config(conn), conn)
    assert "BASE_AI" in built.messages[0]["content"]


def test_sandbox_chains_hooks_and_skips_invalid_parts():
    assert scripting.run_input_modifier(
        "action['text'] = 'Z'",
        {"type": "do", "text": "a"},
    )["text"] == "Z"

    input_chain = scripting.compose_hook_scripts(
        "def modifier(act):\n    act = dict(act)\n    act['text'] = act['text'] + 'A'\n    return act\n",
        ["def modifier(act):\n    act = dict(act)\n    act['text'] = act['text'] + 'M'\n    return act\n"],
    )
    assert scripting.run_input_modifier(input_chain, {"type": "do", "text": "x"})["text"] == "xAM"

    skipped = scripting.compose_hook_scripts(
        "action['text'] = action['text'] + 'A'",
        ["import os\nos.system('echo hacked')", "action['text'] = action['text'] + 'B'"],
    )
    assert scripting.chain_parts(skipped) == [
        "action['text'] = action['text'] + 'A'",
        "action['text'] = action['text'] + 'B'",
    ]
    assert scripting.run_input_modifier(skipped, {"type": "do", "text": "x"})["text"] == "xAB"

    output_chain = scripting.compose_hook_scripts(_output("A"), [_output("M")])
    assert scripting.run_output_modifier(output_chain, "hello") == "helloAM"
    toplevel = scripting.compose_hook_scripts("text = text + 'A'", ["text = text + 'M'"])
    assert scripting.run_output_modifier(toplevel, "hello") == "helloAM"

    context_chain = scripting.compose_hook_scripts(_context("A"), [_context("M")])
    ctx = scripting.run_context_modifier(context_chain, [{"role": "user", "content": "hi"}])
    assert [item["content"] for item in ctx] == ["hi", "CTX_A", "CTX_M"]

    assert scripting.compose_hook_scripts("action['text'] = 'A'", []) == "action['text'] = 'A'"
    assert scripting.compose_hook_scripts(None, []) is None


def test_origin_link_honors_discovered_and_defaults_false(client, mods_schema):
    from app import mods

    raw = _package("Z")
    raw["cards"].append({
        "key": "cellar-Z",
        "title": "Cellar Z",
        "type": "location",
        "triggers": [],
        "entry": "dark",
        "pinned": False,
    })
    raw["links"] = [
        {
            "from_key": "inn-Z",
            "to_key": "road-Z",
            "travel_text": "open road",
            "travel_cost": "",
            "bidirectional": True,
            "discovered": True,
        },
        {
            "from_key": "inn-Z",
            "to_key": "cellar-Z",
            "travel_text": "SECRET-CELLAR-PATH",
            "travel_cost": "",
            "bidirectional": False,
        },
    ]
    normalized = mods.normalize_package(raw)
    by_to = {link["to_key"]: link["discovered"] for link in normalized["links"]}
    assert by_to == {"road-Z": True, "cellar-Z": False}

    published = _publish(client, "Z", package=raw)
    version_id = published["version"]["id"]
    stored = {
        link["to_key"]: link["discovered"]
        for link in published["version"]["package"]["links"]
    }
    assert stored == by_to

    adv = _prepare_save()
    applied = client.post(
        f"/api/adventures/{adv['id']}/mods",
        json={"mod_version_id": version_id},
    )
    assert applied.status_code == 200, applied.text
    with db.session() as conn:
        rows = conn.execute(
            "SELECT travel_text, discovered FROM location_links "
            "WHERE adventure_id=? AND origin_mod_version_id=?",
            (adv["id"], version_id),
        ).fetchall()
    flags = {row["travel_text"]: int(row["discovered"]) for row in rows}
    assert flags == {"open road": 1, "SECRET-CELLAR-PATH": 0}
