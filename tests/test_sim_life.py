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


def test_crossing_25_warns_once_and_15_escalates():
    sim = state.empty()
    life.enable(sim, 5)
    actor = _actor()
    actor["needs"]["fome"] = 27
    sim["actors"]["_solo"] = actor
    jumped = life.advance(sim, 60, "other", "_solo", [], Always(), 0)
    # 1h de "other" tira 4 de fome: 27 -> 23, cruza o aviso de 25
    assert sim["actors"]["_solo"]["needs"]["fome"] == 23
    assert jumped["reactions"] == ["A fome aperta."]
    assert sim["life"]["reactions"][-1]["text"] == "A fome aperta."
    # mais 2h: 23 -> 15, nada novo (já avisou em 25); depois 15 -> 11, crítico
    jumped = life.advance(sim, 120, "other", "_solo", [], Always(), 0)
    assert jumped["reactions"] == []
    jumped = life.advance(sim, 120, "other", "_solo", [], Always(), 0)
    assert sim["actors"]["_solo"]["needs"]["fome"] == 7
    assert jumped["reactions"] == ["Fome crítica — forças se esvaindo."]


def test_bladder_at_zero_is_an_accident():
    sim = state.empty()
    life.enable(sim, 5)
    actor = _actor()
    actor["needs"]["bexiga"] = 4
    actor["needs"]["higiene"] = 80
    sim["actors"]["_solo"] = actor
    jumped = life.advance(sim, 60, "other", "_solo", [], Always(), 0)
    needs = sim["actors"]["_solo"]["needs"]
    assert needs["bexiga"] == 20
    assert needs["higiene"] == 38  # 80 - 2 da hora - 40 do acidente
    assert "acidente" in jumped["reactions"][-1]


def test_energy_at_zero_collapses_into_sleep():
    sim = state.empty()
    life.enable(sim, 5)
    actor = _actor(energia=2)
    sim["actors"]["_solo"] = actor
    jumped = life.advance(sim, 60, "other", "_solo", [], Always(), 0)
    assert sim["actors"]["_solo"]["needs"]["energia"] == 25
    assert "Desabou de exaustão" in jumped["reactions"][-1]


def test_reactions_feed_caps_at_ten():
    sim = state.empty()
    life.enable(sim, 5)
    for index in range(15):
        sim["life"].setdefault("reactions", []).append(
            {"day": 1, "clock": "08:00", "text": f"evento {index}"}
        )
    actor = _actor()
    actor["needs"]["fome"] = 26
    sim["actors"]["_solo"] = actor
    life.advance(sim, 60, "other", "_solo", [], Always(), 0)
    assert len(sim["life"]["reactions"]) == 10
    assert sim["life"]["reactions"][-1]["text"] == "A fome aperta."


def test_render_includes_recent_reactions():
    sim = state.empty()
    life.enable(sim, 5)
    sim["actors"]["_solo"] = _actor()
    sim["life"]["reactions"] = [
        {"day": 1, "clock": "09:00", "text": "A fome aperta."},
        {"day": 1, "clock": "10:00", "text": "A bexiga reclama."},
    ]
    block = life.render(sim, "_solo", [])
    assert "Recente: A fome aperta. A bexiga reclama." in block
