"""Gerenciamento de aventuras: PATCH (rename), DELETE, backup completo
(export-all) e import multi-aventura com round-trip."""

from __future__ import annotations

import io
import json
import zipfile

from conftest import add_action, make_adventure

from app import db


# ------------------------------------------------------------------ PATCH
def test_patch_adventure_renames(client):
    adv = make_adventure(title="Old Title", description="d", tags=["a"])
    res = client.patch(f"/api/adventures/{adv['id']}", json={"title": "New Title"})
    assert res.status_code == 200
    assert res.json()["title"] == "New Title"
    # campos ausentes não mudam
    assert res.json()["description"] == "d"
    assert res.json()["tags"] == ["a"]


def test_patch_adventure_tags_and_description(client):
    adv = make_adventure()
    res = client.patch(f"/api/adventures/{adv['id']}",
                       json={"description": "nova", "tags": ["x", "y"]})
    assert res.status_code == 200
    body = res.json()
    assert body["description"] == "nova"
    assert body["tags"] == ["x", "y"]


def test_patch_adventure_not_found(client):
    res = client.patch("/api/adventures/nope", json={"title": "x"})
    assert res.status_code == 404


def test_patch_adventure_empty_title_rejected(client):
    adv = make_adventure()
    res = client.patch(f"/api/adventures/{adv['id']}", json={"title": ""})
    assert res.status_code == 422


# ----------------------------------------------------------------- DELETE
def test_delete_adventure_removes_dependents(client):
    adv = make_adventure()
    add_action(adv["id"], 0, "start", "Once upon a time.")
    with db.session() as conn:
        db.insert_card(conn, adv["id"], title="NPC", entry="e")
        db.insert_memory(conn, adv["id"], "mem")

    res = client.delete(f"/api/adventures/{adv['id']}")
    assert res.status_code == 200
    assert res.json() == {"deleted": True, "id": adv["id"]}

    assert client.get(f"/api/adventures/{adv['id']}").status_code == 404
    with db.session() as conn:
        for table in ("actions", "story_cards", "memories", "images", "jobs"):
            count = conn.execute(
                f"SELECT COUNT(*) AS c FROM {table} WHERE adventure_id=?",
                (adv["id"],)).fetchone()["c"]
            assert count == 0, f"{table} órfãos após delete"
    # e some da listagem
    assert all(a["id"] != adv["id"] for a in client.get("/api/adventures").json())


def test_delete_adventure_not_found(client):
    assert client.delete("/api/adventures/nope").status_code == 404


# ------------------------------------------------------ backup completo
def _zip_members(blob: bytes) -> list[str]:
    with zipfile.ZipFile(io.BytesIO(blob)) as zf:
        return zf.namelist()


def test_export_all_zip_one_folder_per_adventure(client):
    a1 = make_adventure(title="Alpha Quest")
    a2 = make_adventure(title="Beta Tale")
    add_action(a1["id"], 0, "start", "Alpha begins.")
    add_action(a2["id"], 0, "start", "Beta begins.")
    add_action(a2["id"], 1, "continue", "Beta continues.")

    res = client.get("/api/adventures/export")
    assert res.status_code == 200
    assert res.headers["content-type"] == "application/zip"
    members = _zip_members(res.content)
    for slug in ("Alpha-Quest", "Beta-Tale"):
        assert any(m.endswith(f"-{slug}/metadata.json") for m in members), slug
        assert any(m.endswith(f"-{slug}/actions-001.json") for m in members), slug


def test_export_all_route_not_shadowed_by_adventure_id(client):
    """GET /adventures/export não pode cair no handler /{adventure_id}."""
    make_adventure()
    res = client.get("/api/adventures/export")
    assert res.status_code == 200
    assert res.headers["content-type"] == "application/zip"


def test_backup_all_round_trip_restores_everything(client):
    a1 = make_adventure(title="Keep Me", plot_essentials="plot alpha")
    a2 = make_adventure(title="And Me", plot_essentials="plot beta")
    add_action(a1["id"], 0, "start", "Alpha opening.")
    add_action(a1["id"], 1, "continue", "Alpha continues.")
    add_action(a2["id"], 0, "start", "Beta opening.")
    with db.session() as conn:
        db.insert_card(conn, a1["id"], title="Hero", entry="the chosen one",
                       triggers=["hero"])

    blob = client.get("/api/adventures/export").content
    for aid in (a1["id"], a2["id"]):
        client.delete(f"/api/adventures/{aid}")
    assert client.get("/api/adventures").json() == []

    res = client.post("/api/import/aidungeon",
                      files={"file": ("backup.zip", blob, "application/zip")})
    assert res.status_code == 201
    report = res.json()
    assert report["multi"] is True
    assert report["imported"] == 2

    titles = {a["title"] for a in client.get("/api/adventures").json()}
    assert titles == {"Keep Me", "And Me"}
    restored = {a["title"]: a["id"] for a in client.get("/api/adventures").json()}
    state = client.get(f"/api/adventures/{restored['Keep Me']}").json()
    assert [a["text"] for a in state["actions"]] == ["Alpha opening.", "Alpha continues."]
    assert state["plot_essentials"] == "plot alpha"
    assert state["story_cards"][0]["title"] == "Hero"
    assert state["story_cards"][0]["triggers"] == ["hero"]


def test_import_multi_partial_failure_still_imports_valid(client):
    """Uma pasta corrompida não derruba as aventuras válidas do backup."""
    adv = make_adventure(title="Good One")
    add_action(adv["id"], 0, "start", "Opening.")
    good = client.get(f"/api/adventures/{adv['id']}/export").content

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        with zipfile.ZipFile(io.BytesIO(good)) as src:
            for name in src.namelist():
                zf.writestr(f"001-good/{name}", src.read(name))
        zf.writestr("002-broken/metadata.json", "{not json")
    res = client.post("/api/import/aidungeon",
                      files={"file": ("b.zip", buf.getvalue(), "application/zip")})
    assert res.status_code == 201
    report = res.json()
    assert report["imported"] == 1
    assert any("error" in r for r in report["results"])
    assert any(a["title"] == "Good One" and a["id"] != adv["id"]
               for a in client.get("/api/adventures").json())
