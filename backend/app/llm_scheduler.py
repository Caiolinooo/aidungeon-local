"""Scheduler das chamadas ao llama-server (G3, contrato C1).

Toda chamada passa por `llm_client.chat_completion` ou
`stream_chat_completion`, que pedem a vez aqui antes de abrir a conexão.

- Prioridade por role, menor primeiro e FIFO dentro da mesma: narrative 0
  (nunca espera), choices e os papéis do turno (sim_intent, turn_check,
  turn_gate) 1, image_prompt e meta 2, assistant 3, chronicle 4, summary 5,
  assets 6.
- Uma auxiliar por slot fixado: a segunda ficaria na fila do llama-server com
  o prazo correndo. Sem slot (servidor com 1 slot ou `/props` ilegível) vale
  só o teto global.
- Teto global de auxiliares em voo: 3, ou 2 com narrativa em curso. A
  admissão é gulosa por (prioridade, chegada): quem espera o próprio slot não
  segura quem vai para outro.
- A espera na fila tem teto (padrão: o prazo do role) e respeita `cancel`.
  Estourar a fila não conta para o breaker; o prazo do role começa na admissão.
- Circuit breaker: 3 timeouts seguidos do servidor (prazo do role ou narrativa
  parada) abrem por 60 s. Aberto, auxiliar levanta `Degraded` na hora; a
  narrativa nunca é barrada. Passados os 60 s, um timeout reabre e um ok fecha.
  `add_listener` recebe `("llm_degraded", {reason, until})` e
  `("llm_recovered", {})`.
- `preempt(slot)`: o fallback da narrativa toma o slot. A auxiliar em voo nele
  recebe `ticket.preempt` (o llm_client derruba o socket e a repete) e nenhuma
  outra entra no slot até o fim do fallback.

Os erros daqui (`Degraded`, `Cancelled`, `QueueTimeout`) viram `LlmDegraded`,
`LlmCancelled` e `LlmQueueTimeout` no llm_client.
"""

from __future__ import annotations

import itertools
import logging
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Iterator

log = logging.getLogger("aidungeon.llm.scheduler")

PRIORITIES = {"narrative": 0, "choices": 1, "sim_intent": 1, "turn_check": 1,
              "turn_gate": 1, "image_prompt": 2, "meta": 2,
              "assistant": 3, "chronicle": 4, "summary": 5, "assets": 6}

# Outcomes do llm_client que contam como falha do servidor.
SERVER_FAILURES = ("timeout", "stalled")

_POLL_S = 0.05

Listener = Callable[[str, dict], None]


class SchedulerError(Exception):
    pass


class Degraded(SchedulerError):
    pass


class Cancelled(SchedulerError):
    pass


class QueueTimeout(SchedulerError):
    pass


@dataclass(frozen=True)
class Limits:
    aux_cap: int = 3
    aux_cap_during_narrative: int = 2
    breaker_threshold: int = 3
    breaker_open_s: float = 60.0

    @classmethod
    def from_settings(cls, raw: dict | None) -> "Limits":
        """Bloco `scheduler` do settings (chaves ausentes ou ilegíveis: padrão)."""
        raw = raw if isinstance(raw, dict) else {}
        breaker = raw.get("breaker") if isinstance(raw.get("breaker"), dict) else {}
        base = cls()

        def number(source: dict, key: str, default: float, cast: type) -> Any:
            try:
                return cast(source.get(key, default))
            except (TypeError, ValueError):
                return default

        return cls(
            aux_cap=max(1, number(raw, "aux_cap", base.aux_cap, int)),
            aux_cap_during_narrative=max(
                1, number(raw, "aux_cap_during_narrative", base.aux_cap_during_narrative, int)),
            breaker_threshold=max(1, number(breaker, "threshold", base.breaker_threshold, int)),
            breaker_open_s=max(1.0, number(breaker, "open_s", base.breaker_open_s, float)),
        )


class Ticket:
    """A vez de uma chamada. `preempt` é setado quando o fallback da narrativa
    toma o slot dela."""

    __slots__ = ("role", "slot", "priority", "order", "admitted", "released",
                 "preempt", "queued_at", "admitted_at")

    def __init__(self, role: str, slot: int | None, priority: int, order: int,
                 now: float) -> None:
        self.role = role
        self.slot = slot
        self.priority = priority
        self.order = order
        self.admitted = False
        self.released = False
        self.preempt = threading.Event()
        self.queued_at = now
        self.admitted_at: float | None = None

    @property
    def waited_s(self) -> float:
        if self.admitted_at is None:
            return 0.0
        return self.admitted_at - self.queued_at


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).isoformat(
        timespec="seconds").replace("+00:00", "Z")


class Scheduler:
    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._cond = threading.Condition()
        self._listeners: list[Listener] = []
        self._reset_locked()

    def _reset_locked(self) -> None:
        self._order = itertools.count()
        self._waiting: list[Ticket] = []
        self._busy: dict[int, Ticket] = {}
        self._reserved: dict[int, int] = {}
        self._aux = 0
        self._narratives = 0
        self._limits = Limits()
        self._failures = 0
        self._tripped = False
        self._open_until = 0.0
        self._open_until_wall = 0.0

    # ------------------------------------------------------------ estado
    def reset(self) -> None:
        """Zera fila, slots e breaker (testes). Os listeners ficam."""
        with self._cond:
            self._reset_locked()
            self._cond.notify_all()

    def configure(self, limits: Limits) -> None:
        with self._cond:
            if limits != self._limits:
                self._limits = limits
                self._admit_locked()

    def _cap_locked(self) -> int:
        if self._narratives:
            return self._limits.aux_cap_during_narrative
        return self._limits.aux_cap

    def _is_open_locked(self) -> bool:
        return self._tripped and self._clock() < self._open_until

    def _degraded_message(self) -> str:
        return f"llm degraded: circuit breaker open until {_iso(self._open_until_wall)}"

    # ---------------------------------------------------------- admissão
    def acquire(self, role: str, slot: int | None, *,
                cancel: threading.Event | None = None,
                max_wait: float | None = None) -> Ticket:
        priority = PRIORITIES.get(role, PRIORITIES["assistant"])
        with self._cond:
            ticket = Ticket(role, slot, priority, next(self._order), self._clock())
            if role == "narrative":
                self._narratives += 1
                ticket.admitted = True
                ticket.admitted_at = ticket.queued_at
                return ticket
            if self._is_open_locked():
                raise Degraded(self._degraded_message())
            self._waiting.append(ticket)
            self._waiting.sort(key=lambda t: (t.priority, t.order))
            self._admit_locked()
            deadline = None if max_wait is None else ticket.queued_at + max_wait
            while not ticket.admitted:
                if cancel is not None and cancel.is_set():
                    self._drop_locked(ticket)
                    raise Cancelled("llm call cancelled while queued")
                if self._is_open_locked():
                    self._drop_locked(ticket)
                    raise Degraded(self._degraded_message())
                wait = _POLL_S
                if deadline is not None:
                    remaining = deadline - self._clock()
                    if remaining <= 0:
                        self._drop_locked(ticket)
                        raise QueueTimeout(f"llm queue wait timed out after {max_wait:.0f}s")
                    wait = min(wait, remaining)
                self._cond.wait(wait)
            return ticket

    def _drop_locked(self, ticket: Ticket) -> None:
        if ticket in self._waiting:
            self._waiting.remove(ticket)
        self._admit_locked()

    def _admit_locked(self) -> None:
        cap = self._cap_locked()
        admitted = False
        for ticket in list(self._waiting):
            if self._aux >= cap:
                break
            if ticket.slot is not None and (ticket.slot in self._busy
                                            or ticket.slot in self._reserved):
                continue
            self._waiting.remove(ticket)
            ticket.admitted = True
            ticket.admitted_at = self._clock()
            self._aux += 1
            if ticket.slot is not None:
                self._busy[ticket.slot] = ticket
            admitted = True
        if admitted:
            self._cond.notify_all()

    def release(self, ticket: Ticket, outcome: str) -> None:
        with self._cond:
            if ticket.released:
                return
            ticket.released = True
            if ticket.role == "narrative":
                self._narratives = max(0, self._narratives - 1)
            elif ticket.admitted:
                self._aux = max(0, self._aux - 1)
                if ticket.slot is not None and self._busy.get(ticket.slot) is ticket:
                    del self._busy[ticket.slot]
            events = self._note_locked(outcome)
            self._admit_locked()
            self._cond.notify_all()
        self._emit(events)

    # ----------------------------------------------------------- breaker
    def note_outcome(self, outcome: str) -> None:
        """Resultado de uma tentativa que não solta a vez (narrativa parada
        antes do fallback)."""
        with self._cond:
            events = self._note_locked(outcome)
            self._cond.notify_all()
        self._emit(events)

    def _note_locked(self, outcome: str) -> list[tuple[str, dict]]:
        if outcome == "ok":
            recovered = self._tripped
            self._failures = 0
            self._tripped = False
            self._open_until = 0.0
            return [("llm_recovered", {})] if recovered else []
        if outcome not in SERVER_FAILURES:
            return []
        self._failures += 1
        now = self._clock()
        if self._tripped and now < self._open_until:
            return []
        if not self._tripped and self._failures < self._limits.breaker_threshold:
            return []
        self._tripped = True
        self._open_until = now + self._limits.breaker_open_s
        self._open_until_wall = time.time() + self._limits.breaker_open_s
        reason = f"{self._failures} consecutive llm timeouts"
        log.warning("llm breaker aberto por %.0fs: %s", self._limits.breaker_open_s, reason)
        return [("llm_degraded", {"reason": reason, "until": _iso(self._open_until_wall)})]

    # ---------------------------------------------------------- preempção
    @contextmanager
    def preempt(self, slot: int | None) -> Iterator[None]:
        if slot is None:
            yield
            return
        with self._cond:
            self._reserved[slot] = self._reserved.get(slot, 0) + 1
            victim = self._busy.get(slot)
            if victim is not None:
                victim.preempt.set()
        try:
            yield
        finally:
            with self._cond:
                left = self._reserved.get(slot, 1) - 1
                if left > 0:
                    self._reserved[slot] = left
                else:
                    self._reserved.pop(slot, None)
                self._admit_locked()
                self._cond.notify_all()

    # ---------------------------------------------------------- listeners
    def add_listener(self, listener: Listener) -> None:
        with self._cond:
            if listener not in self._listeners:
                self._listeners.append(listener)

    def remove_listener(self, listener: Listener) -> None:
        with self._cond:
            if listener in self._listeners:
                self._listeners.remove(listener)

    def _emit(self, events: list[tuple[str, dict]]) -> None:
        if not events:
            return
        with self._cond:
            listeners = list(self._listeners)
        for type_, data in events:
            for listener in listeners:
                try:
                    listener(type_, dict(data))
                except Exception:
                    log.exception("listener do scheduler falhou (%s)", type_)

    # ---------------------------------------------------------- snapshot
    def snapshot(self) -> dict:
        with self._cond:
            now = self._clock()
            state = "closed"
            if self._tripped:
                state = "open" if now < self._open_until else "half_open"
            return {
                "breaker": {
                    "state": state,
                    "failures": self._failures,
                    "until": _iso(self._open_until_wall) if state == "open" else None,
                },
                "narratives": self._narratives,
                "aux_in_flight": self._aux,
                "aux_cap": self._cap_locked(),
                "busy_slots": sorted(self._busy),
                "reserved_slots": sorted(self._reserved),
                "waiting": [{"role": t.role, "slot": t.slot,
                             "waited_s": round(now - t.queued_at, 1)}
                            for t in self._waiting],
            }


_scheduler = Scheduler()


def acquire(role: str, slot: int | None, *, cancel: threading.Event | None = None,
            max_wait: float | None = None, limits: Limits | None = None) -> Ticket:
    if limits is not None:
        _scheduler.configure(limits)
    return _scheduler.acquire(role, slot, cancel=cancel, max_wait=max_wait)


def release(ticket: Ticket, outcome: str) -> None:
    _scheduler.release(ticket, outcome)


def note_outcome(outcome: str) -> None:
    _scheduler.note_outcome(outcome)


def preempt(slot: int | None):
    return _scheduler.preempt(slot)


def add_listener(listener: Listener) -> None:
    _scheduler.add_listener(listener)


def remove_listener(listener: Listener) -> None:
    _scheduler.remove_listener(listener)


def snapshot() -> dict:
    return _scheduler.snapshot()


def reset() -> None:
    _scheduler.reset()
