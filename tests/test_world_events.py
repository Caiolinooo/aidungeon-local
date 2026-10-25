"""Log durável world_events: transação, replay e paginação."""

from __future__ import annotations

import pytest

from app import db, events
from conftest import make_adventure


def test_record_is_durable_per_adventure_and_idempotent(tmp_db):
    first = make_adventure(title="Uma")
    second = make_adventure(title="Outra")
    with db.session() as conn:
        created = events.record(
            conn, first["id"], "life_tick_completed", {"ok": True},
            actor="world", entity_id="npc-1", turn_seq=7,
            generation_id="gen-1", correlation_id="corr-1", causation_id=None,
            sim_day=3, sim_minute=481, idempotency_key="tick-1",
        )
        repeated = events.record(
            conn, first["id"], "life_tick_completed", {"ok": False},
            idempotency_key="tick-1",
        )
        other = events.record(conn, second["id"], "turn_done", {"seq": 2})
    assert created["event_id"] == other["event_id"] == 1
    assert created["state_revision"] == 1
    assert repeated == created
    assert created["payload"] == {"ok": True}
    assert created["sim_day"] == 3 and created["sim_minute"] == 481

    events.reset()  # restart lógico do bus não toca o log
    db.init_db()
    with db.session() as conn:
        replayed, expired, latest = events.replay(conn, first["id"], 0)
    assert not expired and latest == 1
    assert replayed == [created]


def test_rollback_persists_nothing_and_publishes_nothing(tmp_db):
    adv = make_adventure()
    with events.subscribe(adv["id"], 0) as sub:
        with pytest.raises(RuntimeError):
            with db.session() as conn:
                events.record(conn, adv["id"], "turn_done", {"seq": 1})
                raise RuntimeError("abort")
        assert sub.get(0.05) is None
    with db.session() as conn:
        page, _ = events.list_history(conn, adv["id"], after=0, limit=None)
        created = events.record(conn, adv["id"], "turn_done", {"seq": 2})
    assert page == []
    assert created["event_id"] == 1


def test_commit_publishes_once_and_replay_has_no_duplicates(tmp_db):
    adv = make_adventure()
    with events.subscribe(adv["id"], 0) as sub:
        with db.session() as conn:
            created = events.record(conn, adv["id"], "turn_done", {"seq": 1})
        events.publish(created)  # chamada manual após commit é deduplicada
        live = sub.get(1.0)
        assert live is not None and live.id == created["event_id"]
        assert sub.get(0.05) is None
    events.reset()
    with db.session() as conn:
        backlog, expired, _ = events.replay(conn, adv["id"], 0)
    with events.subscribe(adv["id"], 0, backlog=backlog) as sub:
        assert [sub.get(1.0).id] == [created["event_id"]]
        assert sub.get(0.05) is None


def test_tokens_do_not_replace_durable_history(tmp_db):
    adv = make_adventure()
    with db.session() as conn:
        created = events.record(conn, adv["id"], "turn_done", {"seq": 1})
    for n in range(events.BUFFER_SIZE + 20):
        events.publish(adv["id"], "token", {"token": str(n)})
    with db.session() as conn:
        backlog, expired, _ = events.replay(conn, adv["id"], 0)
    assert not expired
    assert [item["event_id"] for item in backlog] == [created["event_id"]]
    assert all(item["type"] != "token" for item in backlog)


def test_new_subscriber_receives_durable_event_after_ephemeral_tokens(tmp_db):
    adv = make_adventure()
    with db.session() as conn:
        created = events.record(conn, adv["id"], "turn_changed", {"turn_user": "Mira"})
    events.reset()
    for n in range(5):
        events.publish(adv["id"], "token", {"token": str(n)})

    with events.subscribe(adv["id"]) as sub:
        events.publish(created)
        received = sub.get(1.0)
        assert received is not None
        assert received.data == {"turn_user": "Mira"}


def test_expired_cursor_requires_reset(tmp_db):
    adv = make_adventure()
    with db.session() as conn:
        events.record(conn, adv["id"], "turn_done", {"seq": 1})
    with db.session() as conn:
        backlog, expired, latest = events.replay(conn, adv["id"], 99)
    assert backlog == [] and expired and latest == 1


def test_history_endpoint_paginates_in_event_order(client):
    adv = make_adventure()
    with db.session() as conn:
        for seq in range(3):
            events.record(conn, adv["id"], "turn_done", {"seq": seq})
    base = f"/api/adventures/{adv['id']}/events/history"
    first = client.get(base, params={"limit": 2})
    assert first.status_code == 200
    body = first.json()
    assert [item["event_id"] for item in body["events"]] == [1, 2]
    assert body["has_more"] is True and body["next_cursor"] == 2
    second = client.get(base, params={"after": body["next_cursor"], "limit": 2})
    assert [item["event_id"] for item in second.json()["events"]] == [3]
    assert second.json()["has_more"] is False
