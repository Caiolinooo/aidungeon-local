"""Eventos por aventura (contrato C2): pub/sub em memória.

- `publish(adventure_id, type, data)` numera o evento e entrega a quem está
  inscrito na aventura; os últimos `BUFFER_SIZE` ficam para reconexão.
- `subscribe(adventure_id, last_event_id)` devolve uma `Subscription` que
  primeiro reentrega o buffer com id maior que `last_event_id` (sem
  `last_event_id`, só o que vier depois). Consumo síncrono (`get`) ou
  assíncrono (`aget`, usado pela rota SSE).
- `broadcast(type, data)` vai para todas as aventuras com canal aberto
  (`llm_degraded`/`llm_recovered`, vindos dos listeners do llm_scheduler).

Os ids crescem dentro do processo e começam no relógio em milissegundos, para
um `Last-Event-ID` de antes de um reinício não esconder os eventos novos.
Publicar acontece em threads de geração e de jobs; a entrega em ordem é
garantida porque numerar, guardar e entregar acontecem sob o mesmo lock.
"""

from __future__ import annotations

import asyncio
import collections
import itertools
import json
import threading
import time
from dataclasses import dataclass

BUFFER_SIZE = 100
SUBSCRIPTION_MAX = 500
MAX_CHANNELS = 256


@dataclass(frozen=True)
class Event:
    id: int
    type: str
    data: dict
    adventure_id: str

    def sse(self) -> str:
        payload = json.dumps(self.data, ensure_ascii=False, separators=(",", ":"))
        return f"id: {self.id}\nevent: {self.type}\ndata: {payload}\n\n"


class Subscription:
    """Fila de um inscrito. Inscrito lento perde os mais antigos (`dropped`).

    `since` é o id a partir do qual a inscrição entrega: o `last_event_id`
    pedido ou o último id publicado no momento da inscrição."""

    def __init__(self, adventure_id: str, backlog: list[Event], since: int) -> None:
        self.adventure_id = adventure_id
        self.since = since
        self.dropped = 0
        self.closed = False
        self._items: collections.deque[Event] = collections.deque(backlog,
                                                                  maxlen=SUBSCRIPTION_MAX)
        self._cond = threading.Condition()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._wakeup: asyncio.Event | None = None

    def _push(self, event: Event) -> None:
        with self._cond:
            if self.closed:
                return
            if len(self._items) == self._items.maxlen:
                self.dropped += 1
            self._items.append(event)
            self._cond.notify_all()
            loop, wakeup = self._loop, self._wakeup
        if loop is not None and wakeup is not None:
            try:
                loop.call_soon_threadsafe(wakeup.set)
            except RuntimeError:
                pass

    def get(self, timeout: float | None = None) -> Event | None:
        """Próximo evento, ou None se `timeout` passar (ou a inscrição fechar)."""
        with self._cond:
            if not self._items and not self.closed:
                self._cond.wait(timeout)
            return self._items.popleft() if self._items else None

    async def aget(self, timeout: float) -> Event | None:
        with self._cond:
            if self._loop is None:
                self._loop = asyncio.get_running_loop()
                self._wakeup = asyncio.Event()
            wakeup = self._wakeup
        while True:
            with self._cond:
                if self._items:
                    return self._items.popleft()
                if self.closed:
                    return None
                wakeup.clear()
            try:
                await asyncio.wait_for(wakeup.wait(), timeout)
            except asyncio.TimeoutError:
                return None

    def close(self) -> None:
        _unsubscribe(self)
        with self._cond:
            self.closed = True
            self._cond.notify_all()
            loop, wakeup = self._loop, self._wakeup
        if loop is not None and wakeup is not None:
            try:
                loop.call_soon_threadsafe(wakeup.set)
            except RuntimeError:
                pass

    def __enter__(self) -> "Subscription":
        return self

    def __exit__(self, *exc) -> None:
        self.close()


class _Channel:
    __slots__ = ("buffer", "subscribers", "touched")

    def __init__(self) -> None:
        self.buffer: collections.deque[Event] = collections.deque(maxlen=BUFFER_SIZE)
        self.subscribers: set[Subscription] = set()
        self.touched = time.monotonic()


_lock = threading.Lock()
_channels: dict[str, _Channel] = {}
_ids = itertools.count(int(time.time() * 1000))
_last_id = 0


def _channel_locked(adventure_id: str) -> _Channel:
    channel = _channels.get(adventure_id)
    if channel is None:
        if len(_channels) >= MAX_CHANNELS:
            idle = sorted((c.touched, key) for key, c in _channels.items() if not c.subscribers)
            for _, key in idle[:max(1, len(idle) // 2)]:
                del _channels[key]
        channel = _channels[adventure_id] = _Channel()
    channel.touched = time.monotonic()
    return channel


def _publish_locked(channel: _Channel, adventure_id: str, type_: str, data: dict) -> int:
    global _last_id
    event = Event(next(_ids), type_, data, adventure_id)
    _last_id = event.id
    channel.buffer.append(event)
    for subscription in list(channel.subscribers):
        subscription._push(event)
    return event.id


def publish(adventure_id: str, type: str, data: dict | None = None) -> int:
    with _lock:
        return _publish_locked(_channel_locked(adventure_id), adventure_id, type,
                               dict(data or {}))


def broadcast(type: str, data: dict | None = None) -> list[int]:
    with _lock:
        return [_publish_locked(channel, adventure_id, type, dict(data or {}))
                for adventure_id, channel in list(_channels.items())]


def subscribe(adventure_id: str, last_event_id: int | None = None) -> Subscription:
    with _lock:
        channel = _channel_locked(adventure_id)
        if last_event_id is None:
            backlog, since = [], _last_id
        else:
            backlog = [event for event in channel.buffer if event.id > last_event_id]
            since = last_event_id
        subscription = Subscription(adventure_id, backlog, since)
        channel.subscribers.add(subscription)
    return subscription


def _unsubscribe(subscription: Subscription) -> None:
    with _lock:
        channel = _channels.get(subscription.adventure_id)
        if channel is not None:
            channel.subscribers.discard(subscription)


def forget(adventure_id: str) -> None:
    """Aventura apagada: fecha os inscritos e descarta o buffer."""
    with _lock:
        channel = _channels.pop(adventure_id, None)
    if channel is not None:
        for subscription in list(channel.subscribers):
            subscription.close()


def reset() -> None:
    """Testes: descarta canais e inscritos."""
    with _lock:
        channels = list(_channels.values())
        _channels.clear()
    for channel in channels:
        for subscription in list(channel.subscribers):
            subscription.close()


# Host sem llm_scheduler ainda publica os eventos dos jobs. O listener do
# breaker só entra quando esse módulo está instalado.
try:
    from app import llm_scheduler
except ImportError:
    pass
else:
    llm_scheduler.add_listener(broadcast)
