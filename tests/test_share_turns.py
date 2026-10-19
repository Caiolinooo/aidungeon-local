"""Compartilhar aventura e jogar por turno."""

from __future__ import annotations

from app import db, events, users
from test_v2 import make_v2_transport, override_transport


def _login(client, username, password):
    res = client.post("/api/auth/login", json={"username": username, "password": password})
    assert res.status_code == 200, res.text
    client.headers["Authorization"] = f"Bearer {res.json()['token']}"


def _mira(conn) -> None:
    users.create_user(conn, "Mira", "mira-pass")


def _pull(sub) -> list:
    found = []
    while True:
        event = sub.get(timeout=0)
        if event is None:
            break
        found.append(event)
    return found


def _act(client, adventure_id, text="look"):
    return client.post(
        f"/api/adventures/{adventure_id}/action",
        json={"type": "do", "text": text},
    )


def test_share_crud(client):
    with db.session() as conn:
        _mira(conn)
    adv = client.post("/api/adventures", json={"title": "Mesa"}).json()
    aid = adv["id"]

    missing = client.post(f"/api/adventures/{aid}/sharing", json={"username": "Nobody"})
    assert missing.status_code == 404
    assert missing.json()["error"]["code"] == "user_not_found"

    owner = client.post(f"/api/adventures/{aid}/sharing", json={"username": "tester"})
    assert owner.status_code == 400
    assert owner.json()["error"]["code"] == "cannot_share_owner"

    created = client.post(f"/api/adventures/{aid}/sharing", json={"username": "Mira"})
    assert created.status_code == 201, created.text
    body = created.json()
    assert body["owner"] == "tester"
    assert body["shares"] == ["Mira"]
    assert body["participants"] == ["tester", "Mira"]

    again = client.post(f"/api/adventures/{aid}/sharing", json={"username": "Mira"})
    assert again.status_code == 409
    assert again.json()["error"]["code"] == "already_shared"

    listed = client.get(f"/api/adventures/{aid}/sharing")
    assert listed.status_code == 200
    assert listed.json()["shares"] == ["Mira"]

    removed = client.delete(f"/api/adventures/{aid}/sharing/Mira")
    assert removed.status_code == 200, removed.text
    assert removed.json()["shares"] == []
    assert removed.json()["participants"] == ["tester"]
    assert client.get(f"/api/adventures/{aid}/sharing").json()["shares"] == []


def test_access_list_and_owner_only_writes(client):
    with db.session() as conn:
        _mira(conn)
    adv = client.post("/api/adventures", json={"title": "Somente minha"}).json()
    aid = adv["id"]
    owner_header = client.headers["Authorization"]

    _login(client, "Mira", "mira-pass")
    hidden = client.get(f"/api/adventures/{aid}")
    assert hidden.status_code == 404
    assert all(row["id"] != aid for row in client.get("/api/adventures").json())

    client.headers["Authorization"] = owner_header
    assert client.post(f"/api/adventures/{aid}/sharing",
                       json={"username": "Mira"}).status_code == 201

    _login(client, "Mira", "mira-pass")
    opened = client.get(f"/api/adventures/{aid}")
    assert opened.status_code == 200, opened.text
    row = next(item for item in client.get("/api/adventures").json() if item["id"] == aid)
    assert row["owner"] == "tester"
    assert row["shared_by"] == "tester"
    assert row["turn_mode"] is False
    assert "turn_user" in row

    denied = client.patch(f"/api/adventures/{aid}", json={"title": "Roubada"})
    assert denied.status_code == 403
    assert denied.json()["error"]["code"] == "forbidden"
    turn = client.patch(f"/api/adventures/{aid}", json={"turn_mode": True})
    assert turn.status_code == 403
    assert client.delete(f"/api/adventures/{aid}").status_code == 403
    assert client.get(f"/api/adventures/{aid}").json()["title"] == "Somente minha"


def test_turn_rotation_blocks_the_other_player(client):
    with db.session() as conn:
        _mira(conn)
    override_transport(client, make_v2_transport())
    adv = client.post("/api/adventures", json={"title": "Turnos"}).json()
    aid = adv["id"]
    owner_header = client.headers["Authorization"]
    assert client.post(f"/api/adventures/{aid}/sharing",
                       json={"username": "Mira"}).status_code == 201
    enabled = client.patch(f"/api/adventures/{aid}", json={"turn_mode": True})
    assert enabled.status_code == 200, enabled.text
    assert enabled.json()["turn_mode"] is True
    assert enabled.json()["turn_user"] == "tester"

    with events.subscribe(aid) as sub:
        first = _act(client, aid, "open the gate")
        assert first.status_code == 200, first.text
        assert "event: done" in first.text
        changed = [event for event in _pull(sub) if event.type == "turn_changed"]
    assert changed[-1].data == {"turn_user": "Mira"}
    assert client.get(f"/api/adventures/{aid}").json()["turn_user"] == "Mira"

    blocked = _act(client, aid, "again")
    assert blocked.status_code == 403
    assert blocked.json()["error"]["code"] == "not_your_turn"
    assert blocked.json()["error"]["message"] == "vez de Mira"
    assert client.post(f"/api/adventures/{aid}/continue").status_code == 403
    assert client.post(f"/api/adventures/{aid}/retry").json()["error"]["code"] == "not_your_turn"
    see = client.post(f"/api/adventures/{aid}/see", json={"action_seq": 1})
    if see.status_code == 403:
        assert see.json()["error"]["code"] != "not_your_turn"
    assert client.get(f"/api/adventures/{aid}").json()["turn_user"] == "Mira"

    _login(client, "Mira", "mira-pass")
    followed = client.post(f"/api/adventures/{aid}/continue")
    assert followed.status_code == 200, followed.text
    assert "event: done" in followed.text
    retried = client.post(f"/api/adventures/{aid}/retry")
    assert retried.status_code == 200, retried.text
    assert client.get(f"/api/adventures/{aid}").json()["turn_user"] == "Mira"

    with events.subscribe(aid) as sub:
        hers = _act(client, aid, "take the key")
        assert hers.status_code == 200, hers.text
        changed = [event for event in _pull(sub) if event.type == "turn_changed"]
    assert changed[-1].data["turn_user"] == "tester"
    assert client.get(f"/api/adventures/{aid}").json()["turn_user"] == "tester"
    assert _act(client, aid, "too soon").status_code == 403

    client.headers["Authorization"] = owner_header
    assert _act(client, aid, "my turn again").status_code == 200
    assert client.get(f"/api/adventures/{aid}").json()["turn_user"] == "Mira"
    assert _act(client, aid, "not yet").status_code == 403


def test_turn_mode_off_lets_both_act(client):
    with db.session() as conn:
        _mira(conn)
    override_transport(client, make_v2_transport())
    adv = client.post("/api/adventures", json={"title": "Livre"}).json()
    aid = adv["id"]
    owner_header = client.headers["Authorization"]
    client.post(f"/api/adventures/{aid}/sharing", json={"username": "Mira"})
    assert client.patch(f"/api/adventures/{aid}", json={"turn_mode": True}).status_code == 200
    assert client.patch(f"/api/adventures/{aid}", json={"turn_mode": False}).status_code == 200
    state = client.get(f"/api/adventures/{aid}").json()
    assert state["turn_mode"] is False
    held = state["turn_user"]

    assert _act(client, aid, "owner moves").status_code == 200
    _login(client, "Mira", "mira-pass")
    assert _act(client, aid, "mira moves").status_code == 200
    assert client.get(f"/api/adventures/{aid}").json()["turn_user"] == held
    client.headers["Authorization"] = owner_header
    assert client.get(f"/api/adventures/{aid}").json()["turn_mode"] is False
