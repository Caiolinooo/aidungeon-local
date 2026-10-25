"""Autonomia do Modo Vida: worker, idempotência, retry e NPCs inativos."""

from __future__ import annotations

import json

import pytest

from app import db, npc_life
from app.sim import life, runtime, state
from conftest import TEST_AUTH_USER, make_adventure


def _card(adventure_id: str, title: str, status: str = "alive") -> dict:
    with db.session() as conn:
        return db.insert_card(
            conn, adventure_id, type="character", title=title,
            sheet=json.dumps({"status": status}),
        )


def _life_adventure() -> dict:
    adventure = make_adventure(owner=TEST_AUTH_USER)
    sim = state.empty()
    life.enable(sim)
    sim["life"]["minute"] = 7 * 60
    with db.session() as conn:
        db.set_sim_state(conn, adventure["id"], sim)
    return adventure


def _sim(adventure_id: str) -> dict:
    with db.session() as conn:
        return db.get_adventure(conn, adventure_id)["sim_state"]


def _tick_events(adventure_id: str) -> list[dict]:
    with db.session() as conn:
        return [dict(row) for row in conn.execute(
            "SELECT * FROM world_events WHERE adventure_id=? AND type='life_tick_completed'",
            (adventure_id,),
        )]


def test_three_idle_cycles_change_world_and_record_events_without_llm(
    tmp_db, monkeypatch,
):
    adventure = _life_adventure()
    alive = _card(adventure["id"], "Ada")
    _card(adventure["id"], "Morto", "dead")
    _card(adventure["id"], "Sumido", "missing")
    monkeypatch.setattr(
        "app.llm_client.chat_completion",
        lambda *_args, **_kwargs: pytest.fail("autonomous tick called the LLM"),
    )
    with db.session() as conn:
        runtime.set_playback(conn, adventure["id"], True, 1)

    before = _sim(adventure["id"])
    assert runtime.run_pending() == 1
    assert runtime.run_pending() == 1
    assert runtime.run_pending() == 1
    after = _sim(adventure["id"])

    assert after["life"]["minute"] == before["life"]["minute"] + 3
    npc = after["life"]["npcs"][alive["id"]]
    assert npc["needs"] != before["life"]["npcs"].get(alive["id"], {}).get("needs")
    assert npc["activity"] == "eat"
    assert set(after["life"]["npcs"]) == {alive["id"]}
    assert len(_tick_events(adventure["id"])) == 3


def test_tick_replay_does_not_duplicate_state_or_events(tmp_db):
    adventure = _life_adventure()
    _card(adventure["id"], "Ada")
    first = runtime.run_tick(adventure["id"], 60, tick_id="tick-replay")
    committed = _sim(adventure["id"])
    events_before = _tick_events(adventure["id"])

    replay = runtime.run_tick(adventure["id"], 60, tick_id="tick-replay")

    assert first["replayed"] is False
    assert replay["replayed"] is True
    assert replay["tick_id"] == first["tick_id"]
    assert _sim(adventure["id"]) == committed
    assert _tick_events(adventure["id"]) == events_before


def test_failed_tick_retry_does_not_advance_time_twice(tmp_db, monkeypatch):
    adventure = _life_adventure()
    _card(adventure["id"], "Ada")
    before = _sim(adventure["id"])

    original_record = runtime.events.record

    def fail_record(*_args, **_kwargs):
        raise RuntimeError("event insert failed")

    monkeypatch.setattr(runtime.events, "record", fail_record)
    with pytest.raises(RuntimeError, match="event insert failed"):
        runtime.run_tick(adventure["id"], 60, tick_id="tick-retry")
    assert _sim(adventure["id"]) == before
    assert _tick_events(adventure["id"]) == []

    monkeypatch.setattr(runtime.events, "record", original_record)
    result = runtime.run_tick(adventure["id"], 60, tick_id="tick-retry")
    assert result["replayed"] is False
    assert _sim(adventure["id"])["life"]["minute"] == before["life"]["minute"] + 60
    assert len(_tick_events(adventure["id"])) == 1


def test_playback_get_and_manual_step_api(client):
    adventure = _life_adventure()
    base = f"/api/adventures/{adventure['id']}/sim"

    response = client.put(f"{base}/life/playback", json={"running": True, "speed": 4})
    assert response.status_code == 200, response.text
    assert response.json()["life"]["running"] is True
    assert response.json()["life"]["speed"] == 4

    response = client.get(base)
    assert response.status_code == 200
    assert response.json()["life"]["running"] is True
    assert response.json()["life"]["speed"] == 4

    response = client.post(f"{base}/life/step", json={"minutes": 60})
    assert response.status_code == 200, response.text
    assert response.json()["life"]["minute"] == 8 * 60

    response = client.put(f"{base}/life/playback", json={"running": False})
    assert response.status_code == 200
    assert response.json()["life"]["speed"] == 4
    assert client.post(f"{base}/life/step", json={"minutes": 61}).status_code == 422
    assert client.put(
        f"{base}/life/playback", json={"running": True, "speed": 2}
    ).status_code == 422


def test_dead_and_missing_npcs_have_no_minds_or_reflections(tmp_db, monkeypatch):
    adventure = make_adventure()
    dead = _card(adventure["id"], "Morto", "dead")
    missing = _card(adventure["id"], "Sumido", "missing")
    calls = []
    monkeypatch.setattr(
        "app.llm_client.chat_completion",
        lambda *_args, **_kwargs: calls.append(1) or "{}",
    )

    assert npc_life.run_mind(adventure["id"]) == {"updated": 0}
    assert npc_life.tick_day(adventure["id"])["updated"] == 0
    assert calls == []
    with db.session() as conn:
        assert db.list_npc_memories(conn, adventure["id"], dead["id"]) == []
        assert db.list_npc_memories(conn, adventure["id"], missing["id"]) == []


def test_boredom_label_is_portuguese():
    assert life.NEED_BANDS["diversao"][2] == "entediado"
