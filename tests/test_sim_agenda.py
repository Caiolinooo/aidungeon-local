"""Agendas por NPC, utilidade e catch-up idempotente. Sem LLM."""

from __future__ import annotations

import json

import pytest

from app import db
from app.sim import life, runtime, state
from conftest import make_adventure


def _enable(adventure_id: str, minute: int = 9 * 60, day: int = 1) -> None:
    sim = state.empty()
    life.enable(sim)
    sim["life"]["minute"] = minute
    sim["life"]["day"] = day
    with db.session() as conn:
        db.set_sim_state(conn, adventure_id, sim)


def _card(adventure_id: str, title: str, sheet: dict) -> dict:
    with db.session() as conn:
        return db.insert_card(
            conn, adventure_id, type="character", title=title,
            sheet=json.dumps(sheet),
        )


def _sim(adventure_id: str) -> dict:
    with db.session() as conn:
        return db.get_adventure(conn, adventure_id)["sim_state"]


def test_professions_weekdays_and_winter_change_the_agenda():
    guard = {"sheet": json.dumps({"profession": "guarda"})}
    keeper = {"sheet": json.dumps({"profession": "taverneiro"})}
    assert life.agenda_slot(guard, 1, 9)[:2] == ("work", "quartel")
    assert life.agenda_slot(keeper, 1, 12)[:2] == ("work", "taverna")
    assert life.agenda_slot(guard, 6, 9)[:2] == ("fun", "praça")
    assert life.agenda_slot(keeper, 6, 12)[2] == "profissão"
    assert life.agenda_slot(guard, 91, 21)[:2] == ("sleep", "casa")
    assert life.agenda_slot(None, 1, 7)[:2] == ("eat", "casa")


def test_critical_need_recovers_and_prison_records_a_block(tmp_db):
    adventure = make_adventure()
    _enable(adventure["id"], minute=10 * 60)
    hungry = []
    for index in range(20):
        card = _card(adventure["id"], f"NPC {index}", {"profession": "ferreiro"})
        hungry.append(card["id"])
    locked = _card(adventure["id"], "Preso", {"status": "preso", "profession": "ferreiro"})
    dead = _card(adventure["id"], "Morto", {"status": "dead"})
    down = _card(adventure["id"], "Caido", {"status": "down"})
    with db.session() as conn:
        sim = db.get_adventure(conn, adventure["id"])["sim_state"]
        blank = {key: 80 for key in life.NEEDS}
        for card_id in hungry:
            sim["life"]["npcs"][card_id] = {
                "name": card_id, "needs": {**blank, "fome": 10},
                "activity": "", "place": "",
            }
        sim["life"]["npcs"][locked["id"]] = {
            "name": "Preso", "needs": {**blank, "fome": 0},
            "activity": "", "place": "",
        }
        db.set_sim_state(conn, adventure["id"], sim)

    runtime.run_tick(adventure["id"], 1, tick_id="need-1")
    saved = _sim(adventure["id"])["life"]["npcs"]
    recovered = [saved[card_id] for card_id in hungry]
    assert sum(1 for npc in recovered if npc["activity"] == "eat") >= 19
    assert saved[locked["id"]]["reason"].startswith("bloqueado")
    assert dead["id"] not in saved
    assert down["id"] not in saved


def test_catchup_of_one_day_is_idempotent_and_skips_the_llm(tmp_db, monkeypatch):
    adventure = make_adventure()
    _enable(adventure["id"], minute=8 * 60)
    _card(adventure["id"], "Ada", {"profession": "guarda"})
    monkeypatch.setattr(
        "app.llm_client.chat_completion",
        lambda *_args, **_kwargs: pytest.fail("catch-up called the LLM"),
    )
    first = runtime.catch_up(adventure["id"], 24 * 60, key="dia")
    committed = _sim(adventure["id"])
    with db.session() as conn:
        events_before = conn.execute(
            "SELECT COUNT(*) FROM world_events WHERE adventure_id=?",
            (adventure["id"],),
        ).fetchone()[0]
    second = runtime.catch_up(adventure["id"], 24 * 60, key="dia")
    with db.session() as conn:
        events_after = conn.execute(
            "SELECT COUNT(*) FROM world_events WHERE adventure_id=?",
            (adventure["id"],),
        ).fetchone()[0]
    assert first["applied"] == 24
    assert second["replayed"] == 24
    assert second["applied"] == 0
    assert _sim(adventure["id"]) == committed
    assert events_after == events_before
    assert committed["life"]["day"] == 2
    assert committed["life"]["minute"] == 8 * 60
