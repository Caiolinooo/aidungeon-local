"""RNG de gameplay reproduzível. A seed nasce uma vez; o contador anda com o estado."""

from __future__ import annotations

import random
import secrets

_COUNTER = "counter"
_SEED = "seed"


def _as_int(value) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, str) and value.strip().lstrip("-").isdigit():
        return int(value.strip())
    return None


def ensure(state: dict) -> dict:
    """Garante `state["rng"] = {seed, counter}`. Seed existente não é trocada."""
    raw = state.get("rng") if isinstance(state.get("rng"), dict) else None
    seed = _as_int((raw or {}).get(_SEED)) if raw else None
    counter = _as_int((raw or {}).get(_COUNTER)) if raw else None
    if seed is None or seed < 0:
        record = {_SEED: secrets.randbits(63), _COUNTER: 0}
    else:
        record = {_SEED: seed, _COUNTER: 0 if counter is None or counter < 0 else counter}
    state["rng"] = record
    return record


class Stream:
    """Cada sorteio consome um inteiro do contador. O mesmo par (seed, n) repete."""

    def __init__(self, record: dict):
        self._record = record

    def _next(self) -> random.Random:
        seed = int(self._record[_SEED])
        counter = int(self._record[_COUNTER])
        self._record[_COUNTER] = counter + 1
        return random.Random(f"{seed}:{counter}")

    def randint(self, a: int, b: int) -> int:
        return self._next().randint(a, b)

    def random(self) -> float:
        return self._next().random()

    def choice(self, seq):
        return self._next().choice(seq)


def gameplay_rng(state: dict) -> Stream:
    return Stream(ensure(state))
