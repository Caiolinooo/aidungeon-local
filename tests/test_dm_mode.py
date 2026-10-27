"""Modo DM: fila de intenção, turno na aprovação e segredos fora do share.

Continue, retry e cancel ficam com o dono. O share toma 403 sem LLM.
"""

from __future__ import annotations

import json
import threading

from app import db, game_engine, users, world_map
from app.routes import generation as generation_routes
from test_v2 import make_v2_transport, override_transport


def _login(client, username, password):
    res = client.post("/api/auth/login", json={"username": username, "password": password})
    assert res.status_code == 200, res.text
    client.headers["Authorization"] = f"Bearer {res.json()['token']}"


def _user(conn, username, password):
    users.create_user(conn, username, password)


def _character(client, adventure_id, title):
    res = client.put(
        f"/api/adventures/{adventure_id}/character",
        json={"title": title, "sheet": {"appearance": title, "status": "alive"}},
    )
    assert res.status_code == 200, res.text


def _act(client, adventure_id, text="look"):
    return client.post(
        f"/api/adventures/{adventure_id}/action",
        json={"type": "do", "text": text},
    )


def _texts(adventure_id) -> list[str]:
    with db.session() as conn:
        return [row["text"] or "" for row in db.list_actions(conn, adventure_id)]


def _turn(client, adventure_id) -> str:
    return client.get(f"/api/adventures/{adventure_id}").json()["turn_user"]


def _sse_done(res):
    assert res.status_code == 200, res.text
    assert "event: done" in res.text
    player = None
    for block in res.text.split("\n\n"):
        if "event: done" not in block:
            continue
        for line in block.splitlines():
            if line.startswith("data:"):
                data = json.loads(line.split(":", 1)[1].strip())
                player = data.get("player_action") or player
    return player


def test_dm_off_still_streams(client):
    with db.session() as conn:
        _user(conn, "Mira", "mira-pass")
    override_transport(client, make_v2_transport())
    adv = client.post("/api/adventures", json={"title": "Sem DM"}).json()
    aid = adv["id"]
    assert adv["dm_mode"] is False
    assert client.post(f"/api/adventures/{aid}/sharing",
                       json={"username": "Mira"}).status_code == 201
    _character(client, aid, "Tester")
    owner = client.headers["Authorization"]
    played = _act(client, aid, "open the gate")
    assert played.status_code == 200, played.text
    assert "event: done" in played.text
    _login(client, "Mira", "mira-pass")
    _character(client, aid, "Mira")
    hers = _act(client, aid, "mira walks")
    assert hers.status_code == 200, hers.text
    assert hers.status_code != 202
    client.headers["Authorization"] = owner
    assert client.get(f"/api/adventures/{aid}").json()["dm_mode"] is False


def test_dm_queue_approve_reject_and_turns(client):
    with db.session() as conn:
        _user(conn, "Mira", "mira-pass")
        _user(conn, "Nia", "nia-pass")
        _user(conn, "Bo", "bo-password")
    calls: list = []
    override_transport(client, make_v2_transport(capture=calls))
    adv = client.post("/api/adventures", json={"title": "Mesa do mestre"}).json()
    aid = adv["id"]
    owner = client.headers["Authorization"]
    assert client.post(f"/api/adventures/{aid}/sharing",
                       json={"username": "Mira"}).status_code == 201
    assert client.post(f"/api/adventures/{aid}/sharing",
                       json={"username": "Nia"}).status_code == 201
    _character(client, aid, "Tester")
    _login(client, "Mira", "mira-pass")
    _character(client, aid, "Mira")
    _login(client, "Nia", "nia-pass")
    _character(client, aid, "Nia")
    client.headers["Authorization"] = owner

    assert client.patch(f"/api/adventures/{aid}", json={"turn_mode": True}).status_code == 200
    assert _turn(client, aid) == "tester"
    opened = _act(client, aid, "open the gate")
    assert opened.status_code == 200, opened.text
    assert _turn(client, aid) == "Mira"
    blocked = client.post(f"/api/adventures/{aid}/continue")
    assert blocked.status_code == 403
    assert blocked.json()["error"]["code"] == "not_your_turn"

    enabled = client.patch(f"/api/adventures/{aid}", json={"dm_mode": True})
    assert enabled.status_code == 200, enabled.text
    assert enabled.json()["dm_mode"] is True
    continued = client.post(f"/api/adventures/{aid}/continue")
    assert continued.status_code == 200, continued.text
    assert "event: done" in continued.text
    assert _turn(client, aid) == "Mira"

    before_calls = len(calls)
    before_texts = _texts(aid)
    _login(client, "Mira", "mira-pass")
    queued = _act(client, aid, "mira-key")
    assert queued.status_code == 202, queued.text
    mira_intent = queued.json()["intent"]
    assert mira_intent["status"] == "pending"
    assert mira_intent["username"] == "Mira"
    assert mira_intent["text"] == "mira-key"
    with db.session() as conn:
        stored = db.get_intent(conn, mira_intent["id"])
    assert stored["status"] == "pending"
    assert _texts(aid) == before_texts
    assert len(calls) == before_calls

    _login(client, "Nia", "nia-pass")
    nia_queued = _act(client, aid, "nia-sneak")
    assert nia_queued.status_code == 202, nia_queued.text
    nia_intent = nia_queued.json()["intent"]
    assert len(calls) == before_calls
    assert _texts(aid) == before_texts

    _login(client, "Mira", "mira-pass")
    mira_list = client.get(f"/api/adventures/{aid}/intents")
    assert mira_list.status_code == 200
    mira_ids = {item["id"] for item in mira_list.json()["intents"]}
    assert mira_ids == {mira_intent["id"]}
    assert "nia-sneak" not in mira_list.text
    history = client.get(f"/api/adventures/{aid}/events/history")
    assert history.status_code == 200, history.text
    assert "nia-sneak" not in history.text
    assert "mira-key" in history.text

    _login(client, "Nia", "nia-pass")
    nia_list = client.get(f"/api/adventures/{aid}/intents").json()["intents"]
    assert {item["id"] for item in nia_list} == {nia_intent["id"]}

    client.headers["Authorization"] = owner
    owner_ids = {item["id"] for item in client.get(f"/api/adventures/{aid}/intents").json()["intents"]}
    assert owner_ids == {mira_intent["id"], nia_intent["id"]}
    owner_history = client.get(f"/api/adventures/{aid}/events/history").text
    assert "nia-sneak" in owner_history
    assert "mira-key" in owner_history

    early = client.post(f"/api/adventures/{aid}/intents/{nia_intent['id']}/approve")
    assert early.status_code == 409
    assert early.json()["error"]["code"] == "not_your_turn"
    assert _turn(client, aid) == "Mira"
    assert len(calls) == before_calls
    with db.session() as conn:
        assert db.get_intent(conn, nia_intent["id"])["status"] == "pending"

    approved = client.post(f"/api/adventures/{aid}/intents/{mira_intent['id']}/approve")
    player = _sse_done(approved)
    assert player["actor"] == "Mira"
    assert "mira-key" in (player.get("text") or "")
    assert _turn(client, aid) == "Nia"
    with db.session() as conn:
        assert db.get_intent(conn, mira_intent["id"])["status"] == "approved"
        assert db.get_intent(conn, mira_intent["id"])["action_id"] == player["id"]

    after_approve = len(calls)
    rejected = client.post(f"/api/adventures/{aid}/intents/{nia_intent['id']}/reject")
    assert rejected.status_code == 200, rejected.text
    assert rejected.json()["intent"]["status"] == "rejected"
    assert _turn(client, aid) == "Nia"
    assert len(calls) == after_approve
    assert not any("nia-sneak" in text for text in _texts(aid))

    assert client.post(f"/api/adventures/{aid}/sharing",
                       json={"username": "Bo"}).status_code == 201
    _login(client, "Bo", "bo-password")
    missing = _act(client, aid, "bo-sem-ficha")
    assert missing.status_code == 428
    assert missing.json()["error"]["code"] == "character_required"
    assert client.get(f"/api/adventures/{aid}/intents").json()["intents"] == []
    assert not any("bo-sem-ficha" in text for text in _texts(aid))


def test_private_facts_and_dm_journal_follow_the_owner(client):
    with db.session() as conn:
        _user(conn, "Mira", "mira-pass")
    adv = client.post("/api/adventures", json={"title": "Segredos"}).json()
    aid = adv["id"]
    owner = client.headers["Authorization"]
    assert client.post(f"/api/adventures/{aid}/sharing",
                       json={"username": "Mira"}).status_code == 201
    secret = "fato-so-do-dono"
    with db.session() as conn:
        db.update_adventure_ui_state(conn, aid, {
            "hp": 4,
            "private_facts": [{"npc": "Alok", "fact": secret}],
        })
        actions = db.list_actions(conn, aid)
        seq = (actions[-1]["seq"] + 1) if actions else 0
        db.insert_action(conn, aid, seq, "story", "Alok smiles, happy to see you.")
        db.insert_card(
            conn, aid, type="character", title="Alok", entry="guarda da porta",
            sheet=json.dumps({"status": "alive"}),
        )
        beach = db.insert_card(conn, aid, type="location", title="Praia", entry="Areia.")
        cave = db.insert_card(conn, aid, type="location", title="Caverna", entry="Escura.")
        world_map.set_current_location(conn, aid, beach["id"])
        world_map.ensure_link(conn, aid, beach["id"], cave["id"], discovered=False)
        world_map.add_journal(
            conn, aid, kind="note", title="Praia", text="mar calmo",
            entity_type="location", entity_id=beach["id"],
        )
        world_map.add_journal(
            conn, aid, kind="note", title="Mestre", text="nota-do-mestre",
            entity_type="location", entity_id=beach["id"], visibility="dm",
        )
        world_map.add_journal(
            conn, aid, kind="discovery", title="Caverna", text="gruta-secreta",
            entity_type="location", entity_id=cave["id"],
        )

    owner_state = client.get(f"/api/adventures/{aid}")
    assert owner_state.status_code == 200
    assert secret in owner_state.text
    owner_ui = client.get(f"/api/adventures/{aid}/ui-profile")
    assert secret in owner_ui.text
    closed = client.get(f"/api/adventures/{aid}/journal").text
    assert "mar calmo" in closed
    assert "nota-do-mestre" in closed
    assert "gruta-secreta" not in closed

    owner_scene = client.post(f"/api/adventures/{aid}/scene", json={})
    assert owner_scene.status_code == 200, owner_scene.text
    cast = owner_scene.json()["cast"]
    alok = next(member for member in cast if member["title"] == "Alok")
    assert secret in alok["private_facts"]

    assert client.patch(f"/api/adventures/{aid}", json={"dm_mode": True}).status_code == 200
    opened = client.get(f"/api/adventures/{aid}/journal").text
    assert "gruta-secreta" in opened
    assert "nota-do-mestre" in opened

    _login(client, "Mira", "mira-pass")
    shared = client.get(f"/api/adventures/{aid}")
    assert shared.status_code == 200, shared.text
    assert secret not in shared.text
    assert shared.json()["ui_state"]["hp"] == 4
    assert "private_facts" not in shared.json()["ui_state"]
    assert secret not in client.get(f"/api/adventures/{aid}/ui-profile").text
    journal = client.get(f"/api/adventures/{aid}/journal").text
    assert "mar calmo" in journal
    assert "nota-do-mestre" not in journal
    assert "gruta-secreta" not in journal
    scene = client.post(f"/api/adventures/{aid}/scene", json={})
    assert scene.status_code == 200, scene.text
    assert secret not in scene.text
    shared_alok = next(member for member in scene.json()["cast"] if member["title"] == "Alok")
    assert "private_facts" not in shared_alok
    client.headers["Authorization"] = owner


def _dm_table(client, calls: list) -> tuple[str, str]:
    """Mesa com dm_mode, vez da Mira e uma resposta `continue` do dono."""
    with db.session() as conn:
        _user(conn, "Mira", "mira-pass")
    override_transport(client, make_v2_transport(capture=calls))
    adv = client.post("/api/adventures", json={"title": "Mesa fechada"}).json()
    aid = adv["id"]
    owner = client.headers["Authorization"]
    assert client.post(f"/api/adventures/{aid}/sharing",
                       json={"username": "Mira"}).status_code == 201
    _character(client, aid, "Tester")
    _login(client, "Mira", "mira-pass")
    _character(client, aid, "Mira")
    client.headers["Authorization"] = owner
    assert client.patch(f"/api/adventures/{aid}", json={"turn_mode": True}).status_code == 200
    opened = _act(client, aid, "open the gate")
    assert opened.status_code == 200, opened.text
    assert _turn(client, aid) == "Mira"
    assert client.patch(f"/api/adventures/{aid}", json={"dm_mode": True}).status_code == 200
    continued = client.post(f"/api/adventures/{aid}/continue")
    assert continued.status_code == 200, continued.text
    assert "event: done" in continued.text
    assert _turn(client, aid) == "Mira"
    return aid, owner


def _hold_generation(adventure_id: str) -> threading.Event:
    """Geração ativa sem LLM, para o cancel ser observável."""
    cancel = threading.Event()
    assert game_engine.acquire_generation_lock(adventure_id)
    with generation_routes._active_guard:
        assert adventure_id not in generation_routes._active
        generation_routes._active[adventure_id] = cancel
    return cancel


def _release_generation(adventure_id: str, cancel: threading.Event) -> None:
    with generation_routes._active_guard:
        if generation_routes._active.get(adventure_id) is cancel:
            generation_routes._active.pop(adventure_id, None)
    game_engine.release_generation_lock(adventure_id)


def _assert_share_blocked(client, aid: str, calls: list) -> None:
    before_calls = len(calls)
    before_texts = _texts(aid)
    turn = _turn(client, aid)
    queued = _act(client, aid, "mira-ainda-na-fila")
    assert queued.status_code == 202, queued.text
    assert queued.json()["intent"]["status"] == "pending"
    assert "event:" not in queued.text
    for path in ("continue", "retry"):
        blocked = client.post(f"/api/adventures/{aid}/{path}")
        assert blocked.status_code == 403, blocked.text
        assert blocked.json()["error"]["code"] == "forbidden"
        assert "event:" not in blocked.text
    cancel = _hold_generation(aid)
    try:
        stopped = client.post(f"/api/adventures/{aid}/generation/cancel")
        assert stopped.status_code == 403, stopped.text
        assert stopped.json()["error"]["code"] == "forbidden"
        assert not cancel.is_set()
        assert game_engine.is_generation_active(aid)
    finally:
        _release_generation(aid, cancel)
    assert len(calls) == before_calls
    assert _texts(aid) == before_texts
    assert _turn(client, aid) == turn


def _assert_owner_narrates(client, aid: str, calls: list) -> None:
    before = len(calls)
    continued = client.post(f"/api/adventures/{aid}/continue")
    assert continued.status_code == 200, continued.text
    assert "event: done" in continued.text
    assert len(calls) > before
    mid = len(calls)
    retried = client.post(f"/api/adventures/{aid}/retry")
    assert retried.status_code == 200, retried.text
    assert "event: done" in retried.text
    assert len(calls) > mid
    cancel = _hold_generation(aid)

    def _watch() -> None:
        if cancel.wait(2.0):
            game_engine.release_generation_lock(aid)

    threading.Thread(target=_watch, daemon=True).start()
    try:
        stopped = client.post(f"/api/adventures/{aid}/generation/cancel")
        assert stopped.status_code == 200, stopped.text
        assert cancel.is_set()
        assert stopped.json()["status"] == "cancelled"
        assert not game_engine.is_generation_active(aid)
    finally:
        _release_generation(aid, cancel)


def test_dm_share_cannot_narrate_on_own_turn(client):
    calls: list = []
    aid, owner = _dm_table(client, calls)
    assert _turn(client, aid) == "Mira"
    _login(client, "Mira", "mira-pass")
    _assert_share_blocked(client, aid, calls)
    client.headers["Authorization"] = owner
    assert _turn(client, aid) == "Mira"
    _assert_owner_narrates(client, aid, calls)
    assert _turn(client, aid) == "Mira"


def test_dm_share_cannot_narrate_with_turn_mode_off(client):
    calls: list = []
    aid, owner = _dm_table(client, calls)
    disabled = client.patch(f"/api/adventures/{aid}", json={"turn_mode": False})
    assert disabled.status_code == 200, disabled.text
    assert disabled.json()["turn_mode"] is False
    assert disabled.json()["dm_mode"] is True
    _login(client, "Mira", "mira-pass")
    _assert_share_blocked(client, aid, calls)
    client.headers["Authorization"] = owner
    _assert_owner_narrates(client, aid, calls)
