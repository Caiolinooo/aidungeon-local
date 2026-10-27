"""GET/PUT /api/adventures/{id}/scripts — auth, dono, sandbox, sem executar."""

from __future__ import annotations

from unittest import mock

from fastapi import Depends

from app import db, scripting, users
from app.auth import require_auth
from conftest import TEST_AUTH_USER, make_adventure

_SCRIPTS = "/api/adventures/{adventure_id}/scripts"


def _mount() -> None:
    from app.main import app
    from app.routes.adventure_scripts import router

    found: set[str] = set()
    for route in app.routes:
        if getattr(route, "path", None) == _SCRIPTS:
            found |= set(getattr(route, "methods", set()) or set())
    if {"GET", "PUT"} <= found:
        return
    app.include_router(
        router,
        prefix="/api/adventures",
        tags=["scripts"],
        dependencies=[Depends(require_auth)],
    )


_mount()


def _login(client, username: str, password: str) -> None:
    res = client.post("/api/auth/login", json={"username": username, "password": password})
    assert res.status_code == 200, res.text
    client.headers["Authorization"] = f"Bearer {res.json()['token']}"


def _as_caio(client) -> None:
    with db.session() as conn:
        conn.execute(
            "INSERT INTO users(username, password_hash, role, created_at) VALUES(?,?,?,?)",
            (users.ADMIN_USERNAME, users.password_hash("caio-pass"), "admin", db.utcnow()),
        )
    _login(client, users.ADMIN_USERNAME, "caio-pass")


def _url(adventure_id: str) -> str:
    return f"/api/adventures/{adventure_id}/scripts"


def test_scripts_require_auth(tmp_db):
    from fastapi.testclient import TestClient

    from app.main import app

    adv = make_adventure(title="Auth", owner=TEST_AUTH_USER)
    with TestClient(app) as anon:
        assert anon.get(_url(adv["id"])).status_code == 401
        denied = anon.put(_url(adv["id"]), json={"output_modifier": "text = 'x'"})
        assert denied.status_code == 401
        assert denied.json()["error"]["code"] == "unauthorized"


def test_missing_adventure_is_404(client):
    missing = "0" * 32
    res = client.get(_url(missing))
    assert res.status_code == 404
    assert res.json()["error"]["code"] == "not_found"
    put = client.put(_url(missing), json={"output_modifier": "text = 'x'"})
    assert put.status_code == 404


def test_stranger_cannot_read_or_write_another_adventure(client):
    adv = make_adventure(title="Alheia", owner="Outro")
    scripting.set_adventure_script(adv["id"], "input_modifier", "action['text'] = 'secret'")

    read = client.get(_url(adv["id"]))
    assert read.status_code == 404
    assert read.json()["error"]["code"] == "not_found"

    write = client.put(_url(adv["id"]), json={"input_modifier": "action['text'] = 'nope'"})
    assert write.status_code == 404
    assert scripting.get_adventure_script(adv["id"], "input_modifier") == "action['text'] = 'secret'"


def test_shared_player_cannot_change_scripts(client):
    adv = make_adventure(title="Mesa", owner=TEST_AUTH_USER)
    scripting.set_adventure_script(adv["id"], "output_modifier", "text = 'keep'")
    with db.session() as conn:
        users.create_user(conn, "Mira", "mira-pass")
        db.share_adventure(conn, adv["id"], "Mira")
    _login(client, "Mira", "mira-pass")

    read = client.get(_url(adv["id"]))
    assert read.status_code == 403
    assert read.json()["error"]["code"] == "forbidden"
    write = client.put(_url(adv["id"]), json={"output_modifier": "text = 'stolen'"})
    assert write.status_code == 403
    assert scripting.get_adventure_script(adv["id"], "output_modifier") == "text = 'keep'"


def test_owner_get_shows_global_fallback_without_copying_it(client):
    adv = make_adventure(title="Mine", owner=TEST_AUTH_USER)
    other = make_adventure(title="Other", owner=TEST_AUTH_USER)
    with db.session() as conn:
        db.set_setting(conn, "script:global:input_modifier", "action['text'] = 'G-IN'")
        db.set_setting(conn, "script:global:output_modifier", "text = text + ' G'")

    res = client.get(_url(adv["id"]))
    assert res.status_code == 200
    body = res.json()
    assert body["input_modifier"] == ""
    assert body["context_modifier"] == ""
    assert body["output_modifier"] == ""
    assert body["sources"] == {
        "input_modifier": "global",
        "context_modifier": "empty",
        "output_modifier": "global",
    }
    assert body["global"]["input_modifier"] == "action['text'] = 'G-IN'"
    assert body["global"]["output_modifier"] == "text = text + ' G'"
    assert client.get(_url(other["id"])).json()["input_modifier"] == ""


def test_put_saves_clears_and_does_not_execute(client):
    adv = make_adventure(title="Save", owner=TEST_AUTH_USER)
    with db.session() as conn:
        db.set_setting(conn, "script:global:input_modifier", "action['text'] = 'G'")
        db.set_setting(conn, "script:global:output_modifier", "text = text + ' G'")

    with mock.patch.object(scripting, "run_input_modifier") as run_in, \
         mock.patch.object(scripting, "run_context_modifier") as run_ctx, \
         mock.patch.object(scripting, "run_output_modifier") as run_out:
        saved = client.put(_url(adv["id"]), json={
            "input_modifier": "action['text'] = 'A'",
            "output_modifier": "text = 'A'",
        })
        run_in.assert_not_called()
        run_ctx.assert_not_called()
        run_out.assert_not_called()

    assert saved.status_code == 200, saved.text
    body = saved.json()
    assert body["input_modifier"] == "action['text'] = 'A'"
    assert body["output_modifier"] == "text = 'A'"
    assert body["context_modifier"] == ""
    assert body["sources"]["input_modifier"] == "adventure"
    assert body["sources"]["output_modifier"] == "adventure"
    assert body["sources"]["context_modifier"] == "empty"
    assert scripting.get_adventure_script(adv["id"], "input_modifier") == "action['text'] = 'A'"

    only_ctx = client.put(_url(adv["id"]), json={
        "context_modifier": "context.append({'role': 'system', 'content': 'ctx'})",
    })
    assert only_ctx.status_code == 200
    assert only_ctx.json()["input_modifier"] == "action['text'] = 'A'"
    assert only_ctx.json()["sources"]["context_modifier"] == "adventure"

    stored_divzero = client.put(_url(adv["id"]), json={"output_modifier": "1 / 0"})
    assert stored_divzero.status_code == 200
    assert scripting.get_adventure_script(adv["id"], "output_modifier") == "1 / 0"

    cleared = client.put(_url(adv["id"]), json={"input_modifier": "  ", "output_modifier": ""})
    assert cleared.status_code == 200
    cleared_body = cleared.json()
    assert cleared_body["input_modifier"] == ""
    assert cleared_body["output_modifier"] == ""
    assert cleared_body["sources"]["input_modifier"] == "global"
    assert cleared_body["sources"]["output_modifier"] == "global"
    assert cleared_body["sources"]["context_modifier"] == "adventure"
    assert scripting.get_adventure_script(adv["id"], "input_modifier") == "action['text'] = 'G'"
    assert scripting.get_adventure_script(adv["id"], "output_modifier") == "text = text + ' G'"
    with db.session() as conn:
        assert db.get_setting(conn, f"script:{adv['id']}:input_modifier") is None
        assert db.get_setting(conn, f"script:{adv['id']}:output_modifier") is None


def test_rejected_script_does_not_overwrite(client):
    adv = make_adventure(title="Reject", owner=TEST_AUTH_USER)
    scripting.set_adventure_script(adv["id"], "output_modifier", "text = 'keep'")

    bad = client.put(_url(adv["id"]), json={
        "input_modifier": "import os\nos.system('echo hacked')",
        "output_modifier": "text = 'changed'",
    })
    assert bad.status_code == 422
    assert bad.json()["error"]["code"] == "script_rejected"
    with db.session() as conn:
        assert db.get_setting(conn, f"script:{adv['id']}:input_modifier") is None
        assert db.get_setting(conn, f"script:{adv['id']}:output_modifier") == "text = 'keep'"

    syntax = client.put(_url(adv["id"]), json={"context_modifier": "def invalid_syntax(:"})
    assert syntax.status_code == 422
    assert syntax.json()["error"]["code"] == "script_rejected"
    assert scripting.get_adventure_script(adv["id"], "output_modifier") == "text = 'keep'"


def test_admin_can_edit_another_users_scripts(client):
    adv = make_adventure(title="Alheia", owner="Outro")
    _as_caio(client)
    res = client.put(_url(adv["id"]), json={"output_modifier": "text = 'admin'"})
    assert res.status_code == 200
    assert res.json()["sources"]["output_modifier"] == "adventure"
    assert scripting.get_adventure_script(adv["id"], "output_modifier") == "text = 'admin'"


def test_scripts_stay_inside_one_adventure(client):
    first = make_adventure(title="A", owner=TEST_AUTH_USER)
    second = make_adventure(title="B", owner=TEST_AUTH_USER)
    saved = client.put(_url(first["id"]), json={"output_modifier": "text = 'only-a'"})
    assert saved.status_code == 200
    other = client.get(_url(second["id"]))
    assert other.status_code == 200
    assert other.json()["output_modifier"] == ""
    assert other.json()["sources"]["output_modifier"] == "empty"
    assert scripting.get_adventure_script(second["id"], "output_modifier") is None
