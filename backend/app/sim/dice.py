"""Rolagem autoritativa. O cliente pede o resultado; quem sorteia é o servidor."""

from __future__ import annotations

import random
import re
from dataclasses import dataclass

_EXPR = re.compile(
    r"^\s*(\d{1,2})?d(\d{1,3}|F)\s*([+-]\s*\d{1,3})?\s*$",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class Roll:
    expr: str
    rolls: list[int]
    modifier: int
    total: int


def parse(expr: str) -> tuple[int, str, int]:
    """(quantidade, faces ou 'F', modificador). ValueError se a expressão não serve."""
    match = _EXPR.match(expr or "")
    if not match:
        raise ValueError("expressão de dado inválida")
    count = int(match.group(1) or 1)
    faces = match.group(2)
    raw_mod = match.group(3)
    modifier = int(raw_mod.replace(" ", "")) if raw_mod else 0
    if not 1 <= count <= 20:
        raise ValueError("expressão de dado inválida")
    if faces.upper() == "F":
        return count, "F", modifier
    sides = int(faces)
    if not 2 <= sides <= 1000:
        raise ValueError("expressão de dado inválida")
    return count, str(sides), modifier


def roll(expr: str, rng: random.Random | None = None) -> Roll:
    count, faces, modifier = parse(expr)
    if rng is None:
        raise ValueError("rng obrigatório")
    if faces == "F":
        rolls = [rng.choice((-1, 0, 1)) for _ in range(count)]
    else:
        sides = int(faces)
        rolls = [rng.randint(1, sides) for _ in range(count)]
    return Roll(
        expr=(expr or "").strip(),
        rolls=rolls,
        modifier=modifier,
        total=sum(rolls) + modifier,
    )


def roll_d20(rng: random.Random, mode: str = "") -> tuple[int, list[int]]:
    """Um d20, ou dois com vantagem (`adv`) / desvantagem (`dis`). Devolve (escolhido, dados)."""
    first = rng.randint(1, 20)
    if mode in ("adv", "dis"):
        second = rng.randint(1, 20)
        rolls = [first, second]
        chosen = max(rolls) if mode == "adv" else min(rolls)
        return chosen, rolls
    return first, [first]


def roll_d100(rng: random.Random) -> int:
    return rng.randint(1, 100)


def roll_max(expr: str) -> int:
    """Máximo possível da expressão (cada dF vale +1)."""
    count, faces, modifier = parse(expr)
    if faces == "F":
        return count + modifier
    return count * int(faces) + modifier
