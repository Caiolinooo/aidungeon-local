"""Undo restaura a foto lógica do turno e descarta crônica obsoleta."""

from __future__ import annotations

import json

from app import canon, db, game_engine, npc_chronicle
from app.users import create_user
from conftest import add_action, make_adventure, make_llm_transport


def _fingerprint(adventure_id: str) -> str:
    with db.session() as conn:
        return canon.fingerprint(conn, adventure_id)


def _turn_user(adventure_id: str) -> str:
    with db.session() as conn:
        return db.get_adventure(conn, adventure_id)["turn_user"]


def test_mixed_turns_undo_back_to_the_same_logical_hash(tmp_db):
    adventure = make_adventure(title="Canon", plot_essentials="Caio começa aqui.")
    add_action(adventure["id"], 0, "start", "You stand before the gate.")
    with db.session() as conn:
        before = canon.fingerprint(conn, adventure["id"])
    transport = make_llm_transport(tokens=("The gate answers.",))

    for index in range(7):
        events = list(game_engine.handle_action(
            adventure["id"], "do", f"wait {index}", transport=transport,
        ))
        assert any(event["event"] == "done" for event in events)
        with db.session() as conn:
            db.update_adventure(conn, adventure["id"], plot_essentials=f"virou {index}")
            db.insert_card(conn, adventure["id"], type="character", title=f"NPC {index}",
                           entry="alguém novo")
            db.insert_memory(conn, adventure["id"], f"memória {index}", index, index)

    retry_transport = make_llm_transport(tokens=("The gate answers again.",))
    assert any(event["event"] == "done" for event in game_engine.handle_retry(
        adventure["id"], transport=retry_transport,
    ))
    continued = list(game_engine.handle_continue(
        adventure["id"], transport=make_llm_transport(tokens=("Morning comes.",)),
    ))
    assert any(event["event"] == "done" for event in continued)
    with db.session() as conn:
        last = db.list_actions(conn, adventure["id"])[-1]
    game_engine.handle_edit(last["id"], "Morning comes, edited.")
    with db.session() as conn:
        db.update_adventure(conn, adventure["id"], story_summary="resumo que o undo apaga")
        db.set_sim_state(conn, adventure["id"], {"life": {"enabled": False, "day": 9}})

    assert _fingerprint(adventure["id"]) != before
    for _ in range(8):
        game_engine.handle_undo(adventure["id"])
    assert _fingerprint(adventure["id"]) == before


def test_undo_restores_turn_user(tmp_db):
    with db.session() as conn:
        create_user(conn, "alice", "senha-longa-1")
        create_user(conn, "bob", "senha-longa-2")
        adventure = db.insert_adventure(conn, title="Turnos", owner="alice")
        db.share_adventure(conn, adventure["id"], "bob")
        db.set_turn_mode(conn, adventure["id"], True, "alice")
        db.insert_action(conn, adventure["id"], 0, "start", "A mesa está posta.")
    transport = make_llm_transport(tokens=("Bob escuta.",))
    list(game_engine.handle_action(
        adventure["id"], "say", "sua vez", transport=transport, user="alice",
    ))
    assert _turn_user(adventure["id"]) == "bob"
    game_engine.handle_undo(adventure["id"])
    assert _turn_user(adventure["id"]) == "alice"


def test_chronicle_is_idempotent_and_drops_after_undo(tmp_db, monkeypatch):
    adventure = make_adventure()
    add_action(adventure["id"], 0, "start", "You wait.")
    continued = add_action(adventure["id"], 1, "continue", "Ada entra na sala.")
    payload = {
        "characters": [{
            "name": "Ada",
            "summary": "Ada observa a sala.",
            "appearance": "cabelos curtos",
        }],
    }
    with db.session() as conn:
        first = npc_chronicle.apply_chronicle(
            conn, adventure["id"], payload, "",
            source_action_id=continued["id"], source_seq=continued["seq"],
        )
        second = npc_chronicle.apply_chronicle(
            conn, adventure["id"], payload, "",
            source_action_id=continued["id"], source_seq=continued["seq"],
        )
        cards = db.list_cards(conn, adventure["id"])
    assert first["created"] == 1
    assert second["idempotent"] is True
    assert second["created"] == 0
    assert len(cards) == 1

    fresh = make_adventure()
    add_action(fresh["id"], 0, "start", "You wait.")
    target = add_action(fresh["id"], 1, "continue", "Ada entra de novo.")

    def drop_during_llm(*_args, **_kwargs):
        with db.session() as conn:
            db.delete_action(conn, target["id"])
        return json.dumps({
            "plot_essentials": "",
            "characters": [{
                "name": "Ada",
                "summary": "Ada não devia ser gravada.",
                "appearance": "cabelos curtos",
            }],
            "bodies": [],
            "ui": {},
            "places": [],
            "factions": [],
            "items": [],
        })

    monkeypatch.setattr(npc_chronicle.llm_client, "chat_completion", drop_during_llm)
    result = npc_chronicle.update_from_turn(fresh["id"])
    assert result["stale"] is True
    with db.session() as conn:
        assert db.list_cards(conn, fresh["id"]) == []
        kinds = [row["type"] for row in conn.execute(
            "SELECT type FROM world_events WHERE adventure_id=?", (fresh["id"],),
        )]
    assert kinds.count("stale_work_dropped") == 1
