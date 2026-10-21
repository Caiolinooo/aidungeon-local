"""Relógio, necessidades e teto de relação."""

from __future__ import annotations

from app.sim import life, state


class Always:
    def random(self) -> float:
        return 0.0

    def randint(self, a: int, _b: int) -> int:
        return a


def _needs(energia: int = 80) -> dict:
    return {key: (energia if key == "energia" else 80) for key in life.NEEDS}


def _actor(energia: int = 80, gold: int = 0) -> dict:
    return {
        "name": "Aldric",
        "resources": {"gold": gold, "san": None},
        "needs": _needs(energia),
        "inventory": [],
    }


def test_eight_hours_of_sleep_fills_energy_and_crosses_midnight():
    sim = state.empty()
    life.enable(sim, 5)
    sim["life"]["minute"] = 20 * 60
    sim["actors"]["_solo"] = _actor(30)
    jumped = life.advance(sim, 480, "sleep", "_solo", [], Always(), 0)
    assert sim["actors"]["_solo"]["needs"]["energia"] == 100
    assert jumped["crossed_midnight"] is True
    assert sim["life"]["day"] == 2
    assert sim["life"]["minute"] == 4 * 60


def test_full_day_keeps_the_clock():
    sim = state.empty()
    life.enable(sim, 5)
    sim["life"]["minute"] = 100
    sim["actors"]["_solo"] = _actor()
    jumped = life.advance(sim, 24 * 60, "other", "_solo", [], Always(), 0)
    assert sim["life"]["minute"] == 100
    assert sim["life"]["day"] == 2
    assert jumped["crossed_midnight"] is True
    assert jumped["minutes"] == 1440


def test_penalty_caps_at_two():
    actor = _actor()
    actor["needs"]["fome"] = 10
    actor["needs"]["energia"] = 14
    actor["needs"]["higiene"] = 0
    assert life.penalty(actor, None) == 2
    actor["needs"] = _needs()
    assert life.penalty(actor, None) == 0
    assert life.penalty({"needs": None}, None) == 0


def test_work_two_hours_pays_ten_coins():
    sim = state.empty()
    life.enable(sim, 5)
    sim["actors"]["_solo"] = _actor(gold=0)
    life.advance(sim, 120, "work", "_solo", [], Always(), 0)
    assert sim["actors"]["_solo"]["resources"]["gold"] == 10


def test_colocation_respects_the_daily_cap():
    sim = state.empty()
    life.enable(sim, 5)
    sim["actors"]["_solo"] = _actor()
    cards = [
        {"id": "clara", "type": "character", "title": "Clara"},
        {"id": "tomas", "type": "character", "title": "Tomás"},
    ]
    saved = dict(life.DEFAULT_ROUTINE)
    try:
        for hour in range(24):
            life.DEFAULT_ROUTINE[hour] = ("social", "praca")
        life.advance(sim, 5 * 60, "other", "_solo", cards, Always(), 1.0)
    finally:
        life.DEFAULT_ROUTINE.clear()
        life.DEFAULT_ROUTINE.update(saved)
    assert sim["life"]["relations"]["clara|tomas"] == 6
    assert sim["life"]["relations_day"]["clara|tomas"] == 6
