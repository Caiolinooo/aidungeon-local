"""Dados do motor: parse, faixa e vantagem."""

from __future__ import annotations

import random

import pytest

from app.sim import dice


def test_seeded_3d6_plus_2_matches_the_same_rng():
    probe = random.Random(0)
    expected = [probe.randint(1, 6) for _ in range(3)]
    rolled = dice.roll("3d6+2", random.Random(0))
    assert rolled.rolls == expected
    assert rolled.modifier == 2
    assert rolled.total == sum(expected) + 2


def test_4df_stays_in_range():
    rng = random.Random(1)
    for _ in range(40):
        rolled = dice.roll("4dF", rng)
        assert len(rolled.rolls) == 4
        assert all(value in (-1, 0, 1) for value in rolled.rolls)
        assert -4 <= rolled.total <= 4


@pytest.mark.parametrize("expr", ["0d6", "21d6", "d1", "abc", "2d1001", ""])
def test_invalid_expressions(expr):
    with pytest.raises(ValueError, match="expressão de dado inválida"):
        dice.parse(expr)


def test_roll_d20_advantage_keeps_the_higher():
    probe = random.Random(2)
    first = probe.randint(1, 20)
    second = probe.randint(1, 20)
    chosen, rolls = dice.roll_d20(random.Random(2), "adv")
    assert rolls == [first, second]
    assert chosen == max(first, second)


def test_roll_max_is_the_ceiling():
    assert dice.roll_max("2d6+1") == 13
    assert dice.roll_max("4dF") == 4
