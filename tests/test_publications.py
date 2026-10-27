"""Catálogo: snapshot imutável, hash, links/scripts, like, remix e autorização.

O DDL pode ainda não estar em schema.sql. ensure_schema aplica
037_publications.sql e os triggers (o runner de migrations parte em ';').
"""

from __future__ import annotations

import sqlite3

import pytest

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from app import auth as auth_core
from app import db, publications
from app.errors import GameError
from app.routes.publications import router
from conftest import TEST_AUTH_SECRET, TEST_AUTH_USER


def _app() -> FastAPI:
    """App mínimo: main.py está fora desta fatia e pode estar inconsistente."""
    app = FastAPI()

    @app.exception_handler(GameError)
    def game_error_handler(request, exc: GameError):
        error = {"code": exc.code, "message": exc.message}
        if exc.hint:
            error["hint"] = exc.hint
        return JSONResponse(status_code=exc.status, content={"error": error})

    @app.exception_handler(RequestValidationError)
    def validation_error_handler(request, exc: RequestValidationError):
        messages = "; ".join(
            f"{'.'.join(str(part) for part in err.get('loc', []))}: {err.get('msg', '')}"
            for err in exc.errors()
        )
        return JSONResponse(
            status_code=422,
            content={"error": {"code": "validation_error", "message": messages or "invalid payload"}},
        )

    app.include_router(router, prefix="/api/publications")
    return app


@pytest.fixture(autouse=True)
def _publications_schema(tmp_db):
    with db.session() as conn:
        publications.ensure_schema(conn)
    return tmp_db


@pytest.fixture
def client(auth_token):
    with TestClient(_app()) as test_client:
        test_client.headers["Authorization"] = f"Bearer {auth_token}"
        yield test_client


def _publish(client, kind: str, source_id: str, **extra):
    body = {"kind": kind, "source_id": source_id, **extra}
    return client.post("/api/publications", json=body)


def _keys(value, found: set | None = None) -> set:
    found = found if found is not None else set()
    if isinstance(value, dict):
        found.update(value)
        for child in value.values():
            _keys(child, found)
    elif isinstance(value, list):
        for child in value:
            _keys(child, found)
    return found


def test_publications_require_auth():
    with TestClient(_app()) as bare:
        res = bare.get("/api/publications")
    assert res.status_code == 401


def test_scenario_version_stays_frozen_and_remix_keeps_lineage(client):
    with db.session() as conn:
        scenario = db.insert_scenario(
            conn,
            title="Porto",
            description="neblina",
            ai_instructions="inst",
            plot_essentials="plot",
            authors_note="note",
            opening_text="The sun rises.",
            story_starts=[{"id": "dawn", "title": "Dawn", "text": "The sun rises."}],
            image_style="ink",
            tags=["mist", "mist"],
            third_person=True,
            owner=TEST_AUTH_USER,
            story_cards=[
                {
                    "title": "Inn",
                    "type": "location",
                    "entry": "warm fire",
                    "triggers": ["inn"],
                    "pinned": True,
                    "portrait_image_id": "PORTRAIT-SECRET-ID",
                    "voice": "VOICE-SECRET",
                },
                {
                    "title": "You",
                    "type": "character",
                    "entry": "the hero",
                    "player_username": "tester",
                },
            ],
        )
    first = _publish(client, "scenario", scenario["id"], cover_image="https://cdn.example/cover.png")
    assert first.status_code == 201, first.text
    body = first.json()
    assert body["created"] is True
    assert body["latest_version"] == 1
    assert body["content_hash"] == publications.content_hash(body["snapshot"])
    snap = body["snapshot"]
    assert snap["title"] == "Porto"
    assert snap["tags"] == ["mist"]
    assert snap["third_person"] is True
    assert snap["opening_text"] == "The sun rises."
    assert snap["links"] == []
    assert snap["scripts"]["input_modifier"] == ""
    assert [card["title"] for card in snap["story_cards"]] == ["Inn"]
    assert "portrait_image_id" not in _keys(snap)
    assert "PORTRAIT-SECRET-ID" not in publications.canonical_json(snap)
    assert "You" not in publications.canonical_json(snap)
    with db.session() as conn:
        stored = conn.execute(
            "SELECT snapshot, content_hash FROM publication_versions "
            "WHERE publication_id=? AND version=1",
            (body["id"],),
        ).fetchone()
    assert stored["content_hash"] == body["content_hash"]
    assert stored["snapshot"] == publications.canonical_json(snap)

    again = _publish(client, "scenario", scenario["id"])
    assert again.status_code == 200, again.text
    assert again.json()["created"] is False
    assert again.json()["latest_version"] == 1
    assert again.json()["content_hash"] == body["content_hash"]

    with db.session() as conn:
        db.update_scenario(conn, scenario["id"], title="Mudou", description="outra")
    third = _publish(client, "scenario", scenario["id"])
    assert third.status_code == 201, third.text
    assert third.json()["latest_version"] == 2
    assert third.json()["snapshot"]["title"] == "Mudou"
    frozen = client.get(f"/api/publications/{body['id']}/versions/1")
    assert frozen.status_code == 200, frozen.text
    assert frozen.json()["snapshot"]["title"] == "Porto"
    assert frozen.json()["content_hash"] == body["content_hash"]
    assert frozen.json()["snapshot"]["description"] == "neblina"

    remix = client.post(
        f"/api/publications/{body['id']}/remix",
        json={"version": 1},
    )
    assert remix.status_code == 201, remix.text
    created = remix.json()
    assert created["parent_publication_id"] == body["id"]
    assert created["parent_version"] == 1
    assert created["title"] == "Porto"
    assert [card["title"] for card in created["scenario"]["story_cards"]] == ["Inn"]
    with db.session() as conn:
        row = conn.execute(
            "SELECT parent_publication_id, parent_version, title FROM scenarios WHERE id=?",
            (created["scenario_id"],),
        ).fetchone()
    assert row["parent_publication_id"] == body["id"]
    assert row["parent_version"] == 1
    assert row["title"] == "Porto"


def test_adventure_snapshot_excludes_runtime_and_keeps_links_and_scripts(client):
    with db.session() as conn:
        adventure = db.insert_adventure(
            conn,
            title="Harbor Tale",
            description="docks",
            ai_instructions="inst-a",
            plot_essentials="plot-a",
            authors_note="note-a",
            story_summary="SUMMARY-SECRET",
            image_style="oil",
            tags=["sea", "trade"],
            third_person=True,
            character_rules="No mercy",
            owner=TEST_AUTH_USER,
        )
        aid = adventure["id"]
        db.insert_action(conn, aid, 1, "start", "OPENING-HARBOR-TEXT")
        db.insert_action(conn, aid, 2, "do", "ACTION-SECRET-TEXT")
        harbor = db.insert_card(
            conn, aid, title="Harbor", type="location", entry="salt",
            triggers=["dock"], pinned=True, sheet='{"mood":"calm"}',
        )
        market = db.insert_card(
            conn, aid, title="Market", type="location", entry="spice",
        )
        db.insert_card(
            conn, aid, title="Player Hero", type="character",
            entry="the player", player_username="tester",
        )
        conn.execute(
            "UPDATE story_cards SET portrait_image_id=? WHERE id=?",
            ("PORTRAIT-SECRET-ID", harbor["id"]),
        )
        conn.execute(
            "INSERT INTO location_links("
            "id, adventure_id, from_card_id, to_card_id, travel_text, travel_cost, "
            "bidirectional, discovered, created_at) VALUES(?,?,?,?,?,?,?,?,?)",
            (
                db.new_id(), aid, harbor["id"], market["id"],
                "walk the docks", "1 hour", 1, 1, db.utcnow(),
            ),
        )
        conn.execute(
            "INSERT INTO location_links("
            "id, adventure_id, from_card_id, to_card_id, travel_text, travel_cost, "
            "bidirectional, discovered, created_at) VALUES(?,?,?,?,?,?,?,?,?)",
            (
                db.new_id(), aid, market["id"], harbor["id"],
                "HIDDEN-DOCK-SECRET", "secret toll", 0, 0, db.utcnow(),
            ),
        )
        db.insert_memory(conn, aid, "MEMORY-SECRET-TEXT")
        db.set_setting(conn, f"script:{aid}:input_modifier", "IN-SCRIPT")
        db.set_setting(conn, f"script:{aid}:context_modifier", "CTX-SCRIPT")
        db.set_setting(conn, "script:global:output_modifier", "GLOBAL-SCRIPT-SECRET")

    res = _publish(client, "adventure", aid, cover_image="https://cdn.example/harbor.png")
    assert res.status_code == 201, res.text
    body = res.json()
    snap = body["snapshot"]
    blob = publications.canonical_json(snap)
    assert snap["opening_text"] == "OPENING-HARBOR-TEXT"
    assert snap["story_starts"] == [{
        "id": "opening", "title": "Opening", "text": "OPENING-HARBOR-TEXT",
    }]
    assert snap["character_rules"] == "No mercy"
    assert snap["image_style"] == "oil"
    assert snap["tags"] == ["sea", "trade"]
    assert [card["title"] for card in snap["story_cards"]] == ["Harbor", "Market"]
    assert snap["story_cards"][0]["key"] == "harbor"
    assert snap["story_cards"][0]["pinned"] is True
    assert snap["links"] == [{
        "from_key": "harbor",
        "to_key": "market",
        "travel_text": "walk the docks",
        "travel_cost": "1 hour",
        "bidirectional": True,
        "discovered": True,
    }]
    assert snap["scripts"] == {
        "input_modifier": "IN-SCRIPT",
        "context_modifier": "CTX-SCRIPT",
        "output_modifier": "",
    }
    for secret in (
        "ACTION-SECRET-TEXT",
        "MEMORY-SECRET-TEXT",
        "SUMMARY-SECRET",
        "PORTRAIT-SECRET-ID",
        "GLOBAL-SCRIPT-SECRET",
        "Player Hero",
        "HIDDEN-DOCK-SECRET",
        "secret toll",
    ):
        assert secret not in blob
    assert "actions" not in snap
    assert "story_summary" not in snap
    forbidden = {"portrait_image_id", "portrait_anchor_id", "player_username", "voice", "vrm_id"}
    assert forbidden.isdisjoint(_keys(snap))
    with db.session() as conn:
        stamped = conn.execute(
            "SELECT publication_id, publication_version FROM adventures WHERE id=?",
            (aid,),
        ).fetchone()
    assert stamped["publication_id"] == body["id"]
    assert stamped["publication_version"] == 1

    with db.session() as conn:
        db.update_adventure(conn, aid, title="Renamed Harbor")
        conn.execute(
            "UPDATE actions SET text=? WHERE adventure_id=? AND type='start'",
            ("NEW-OPENING", aid),
        )
    revised = _publish(client, "adventure", aid)
    assert revised.status_code == 201, revised.text
    assert revised.json()["latest_version"] == 2
    assert revised.json()["snapshot"]["title"] == "Renamed Harbor"
    assert revised.json()["snapshot"]["opening_text"] == "NEW-OPENING"
    frozen = client.get(f"/api/publications/{body['id']}/versions/1")
    assert frozen.json()["snapshot"]["title"] == "Harbor Tale"
    assert frozen.json()["snapshot"]["opening_text"] == "OPENING-HARBOR-TEXT"
    assert frozen.json()["content_hash"] == body["content_hash"]
    with db.session() as conn:
        stamped = conn.execute(
            "SELECT publication_version FROM adventures WHERE id=?",
            (aid,),
        ).fetchone()
    assert stamped["publication_version"] == 2


def test_like_is_idempotent_and_catalog_filters(client, admin_token):
    with db.session() as conn:
        alpha = db.insert_scenario(
            conn, title="Alpha Harbor", description="old docks",
            tags=["sea"], owner=TEST_AUTH_USER,
        )
        beta = db.insert_scenario(
            conn, title="Beta Town", description="market",
            tags=["town"], owner=TEST_AUTH_USER,
        )
    alpha_pub = _publish(client, "scenario", alpha["id"])
    beta_pub = _publish(client, "scenario", beta["id"])
    assert alpha_pub.status_code == 201
    assert beta_pub.status_code == 201
    alpha_id = alpha_pub.json()["id"]
    beta_id = beta_pub.json()["id"]
    with db.session() as conn:
        conn.execute(
            "UPDATE publications SET updated_at=? WHERE id=?",
            ("2020-01-01T00:00:00.000Z", alpha_id),
        )
        conn.execute(
            "UPDATE publications SET updated_at=? WHERE id=?",
            ("2021-01-01T00:00:00.000Z", beta_id),
        )
    first = client.post(f"/api/publications/{alpha_id}/like")
    second = client.post(f"/api/publications/{alpha_id}/like")
    assert first.status_code == 200, first.text
    assert second.status_code == 200, second.text
    assert first.json()["likes"] == 1
    assert second.json()["likes"] == 1
    assert second.json()["liked"] is True
    tester = client.headers["Authorization"]
    client.headers["Authorization"] = f"Bearer {admin_token}"
    admin_like = client.post(f"/api/publications/{alpha_id}/like")
    client.headers["Authorization"] = tester
    assert admin_like.status_code == 200, admin_like.text
    assert admin_like.json()["likes"] == 2

    newest = client.get("/api/publications?sort=new")
    assert [item["id"] for item in newest.json()] == [beta_id, alpha_id]
    liked = client.get("/api/publications?sort=likes")
    assert [item["id"] for item in liked.json()] == [alpha_id, beta_id]
    by_name = client.get("/api/publications?q=BETA")
    assert [item["id"] for item in by_name.json()] == [beta_id]
    by_tag = client.get("/api/publications?tag=sea")
    assert [item["id"] for item in by_tag.json()] == [alpha_id]
    assert client.get("/api/publications?q=zzzz").json() == []

    removed = client.delete(f"/api/publications/{alpha_id}/like")
    assert removed.status_code == 200, removed.text
    assert removed.json()["liked"] is False
    assert removed.json()["likes"] == 1


def test_authorization_owner_admin_and_stranger(client, admin_token):
    with db.session() as conn:
        foreign = db.insert_scenario(conn, title="Foreign", owner="someone")
        own = db.insert_scenario(conn, title="Mine", owner=TEST_AUTH_USER)
    denied = _publish(client, "scenario", foreign["id"])
    assert denied.status_code == 403
    assert denied.json()["error"]["code"] == "forbidden"
    missing = _publish(client, "scenario", "does-not-exist")
    assert missing.status_code == 404
    created = _publish(client, "scenario", own["id"])
    assert created.status_code == 201, created.text
    publication_id = created.json()["id"]

    stranger = auth_core.make_token("other", TEST_AUTH_SECRET, ttl_h=1)
    tester = client.headers["Authorization"]
    client.headers["Authorization"] = f"Bearer {stranger}"
    forbidden = client.delete(f"/api/publications/{publication_id}")
    assert forbidden.status_code == 403
    client.headers["Authorization"] = f"Bearer {admin_token}"
    removed = client.delete(f"/api/publications/{publication_id}")
    assert removed.status_code == 200, removed.text
    assert removed.json()["deleted"] is True
    client.headers["Authorization"] = tester
    assert client.get(f"/api/publications/{publication_id}").status_code == 404


def test_version_rows_reject_update_and_delete(client):
    with db.session() as conn:
        scenario = db.insert_scenario(conn, title="Locked", owner=TEST_AUTH_USER)
    created = _publish(client, "scenario", scenario["id"])
    assert created.status_code == 201, created.text
    publication_id = created.json()["id"]
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        with db.session() as conn:
            conn.execute(
                "UPDATE publication_versions SET snapshot=? WHERE publication_id=?",
                ("{}", publication_id),
            )
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        with db.session() as conn:
            conn.execute(
                "DELETE FROM publication_versions WHERE publication_id=?",
                (publication_id,),
            )
    with db.session() as conn:
        row = conn.execute(
            "SELECT snapshot FROM publication_versions WHERE publication_id=? AND version=1",
            (publication_id,),
        ).fetchone()
    assert row is not None
    assert "Locked" in row["snapshot"]
    removed = client.delete(f"/api/publications/{publication_id}")
    assert removed.status_code == 200, removed.text
    with db.session() as conn:
        left = conn.execute(
            "SELECT COUNT(*) AS c FROM publication_versions WHERE publication_id=?",
            (publication_id,),
        ).fetchone()["c"]
        guard = conn.execute(
            "SELECT id FROM publication_version_delete_guard WHERE id=1"
        ).fetchone()
    assert left == 0
    assert guard is not None


def test_invalid_sort_and_missing_version(client):
    bad = client.get("/api/publications?sort=hot")
    assert bad.status_code == 400
    assert bad.json()["error"]["code"] == "invalid_sort"
    assert client.get("/api/publications/missing").status_code == 404
    with db.session() as conn:
        scenario = db.insert_scenario(conn, title="One", owner=TEST_AUTH_USER)
    created = _publish(client, "scenario", scenario["id"])
    publication_id = created.json()["id"]
    missing = client.get(f"/api/publications/{publication_id}/versions/9")
    assert missing.status_code == 404
    remix = client.post(
        f"/api/publications/{publication_id}/remix",
        json={"version": 9},
    )
    assert remix.status_code == 404
