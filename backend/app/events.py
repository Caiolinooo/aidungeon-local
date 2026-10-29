"""Eventos duráveis por aventura e pub/sub SSE em memória.

`record` grava o fato na transação do chamador. O `db.session` entrega os
registros pendentes somente depois do commit; quem usa uma conexão manual deve
chamar `publish(record)` depois do commit. `publish(adventure_id, type, data)`
mantém a API antiga: fatos são persistidos quando a aventura existe e tokens
continuam efêmeros, fora do histórico.
"""

from __future__ import annotations

import asyncio
import collections
import itertools
import json
import sqlite3
import threading
import time
from dataclasses import dataclass
from typing import Any

BUFFER_SIZE = 100
SUBSCRIPTION_MAX = 500
MAX_CHANNELS = 256


@dataclass(frozen=True)
class Event:
    id: int
    type: str
    data: dict
    adventure_id: str
    state_revision: int | None = None
    actor: str = ""
    entity_id: str = ""
    turn_seq: int | None = None
    generation_id: str = ""
    correlation_id: str = ""
    causation_id: int | None = None
    sim_day: int | None = None
    sim_minute: int | None = None
    created_at: str | None = None
    durable: bool = True

    @property
    def event_id(self) -> int:
        return self.id

    def sse(self) -> str:
        payload = json.dumps(self.data, ensure_ascii=False, separators=(",", ":"))
        cursor = f"id: {self.id}\n" if self.durable else ""
        return f"{cursor}event: {self.type}\ndata: {payload}\n\n"


class Subscription:
    """Fila de um inscrito. Backlog durável não é limitado; eventos ao vivo
    descartam os mais antigos quando um cliente lento excede SUBSCRIPTION_MAX."""

    def __init__(self, adventure_id: str, backlog: list[Event], since: int, *,
                 filter_durable: bool = True) -> None:
        self.adventure_id = adventure_id
        self.since = since
        self.dropped = 0
        self.closed = False
        self._items: collections.deque[Event] = collections.deque(backlog)
        self.filter_durable = filter_durable
        self._cond = threading.Condition()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._wakeup: asyncio.Event | None = None

    def _push(self, event: Event) -> None:
        with self._cond:
            if self.closed or (self.filter_durable and event.durable and event.id <= self.since):
                return
            if len(self._items) >= SUBSCRIPTION_MAX:
                self._items.popleft()
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
    __slots__ = ("buffer", "subscribers", "touched", "latest")

    def __init__(self) -> None:
        self.buffer: collections.deque[Event] = collections.deque(maxlen=BUFFER_SIZE)
        self.subscribers: set[Subscription] = set()
        self.touched = time.monotonic()
        self.latest = 0


_lock = threading.Lock()
_channels: dict[str, _Channel] = {}
_ids = itertools.count(int(time.time() * 1000))
_pending_lock = threading.Lock()
_pending: dict[int, tuple[sqlite3.Connection, list[dict]]] = {}


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


def _deliver_locked(channel: _Channel, event: Event) -> int:
    if event.durable and any(item.id == event.id for item in channel.buffer):
        return event.id
    channel.buffer.append(event)
    if event.durable:
        channel.latest = max(channel.latest, event.id)
    for subscription in list(channel.subscribers):
        subscription._push(event)
    return event.id


def _publish_locked(channel: _Channel, adventure_id: str, type_: str, data: dict,
                    *, durable: bool) -> int:
    return _deliver_locked(channel, Event(
        next(_ids), type_, data, adventure_id, durable=durable,
    ))


def _event_from_record(record: dict) -> Event:
    return Event(
        id=int(record["event_id"]), type=record["type"],
        data=dict(record.get("payload") or {}), adventure_id=record["adventure_id"],
        state_revision=record.get("state_revision"), actor=record.get("actor") or "",
        entity_id=record.get("entity_id") or "", turn_seq=record.get("turn_seq"),
        generation_id=record.get("generation_id") or "",
        correlation_id=record.get("correlation_id") or "",
        causation_id=record.get("causation_id"), sim_day=record.get("sim_day"),
        sim_minute=record.get("sim_minute"), created_at=record.get("created_at"),
        durable=True,
    )


def publish_record(record: dict) -> int:
    """Entrega um registro já committed. Repetir o mesmo registro é inócuo."""
    event = _event_from_record(record)
    with _lock:
        return _deliver_locked(_channel_locked(event.adventure_id), event)


def publish(adventure_id: str | dict, type: str | None = None,
            data: dict | None = None, **metadata) -> int:
    """Publica um registro (`publish(record)`) ou grava e publica um fato.

    A forma antiga continua útil para chamadas fora de uma transação. Tokens,
    fases do turno e eventos globais do breaker são efêmeros e não avançam o
    cursor SSE.
    """
    if isinstance(adventure_id, dict):
        return publish_record(adventure_id)
    if type is None:
        raise TypeError("type is required")
    if type in ("token", "turn_phase"):
        with _lock:
            return _publish_locked(_channel_locked(adventure_id), adventure_id, type,
                                   dict(data or {}), durable=False)

    from app import db
    try:
        with db.session() as conn:
            if db.get_adventure(conn, adventure_id) is None:
                created = None
            else:
                created = record(conn, adventure_id, type, data, **metadata)
    except sqlite3.OperationalError as exc:
        # O bus também é usado isoladamente, sem inicializar o banco da app.
        if "no such table: adventures" not in str(exc):
            raise
        created = None
    if created is not None:
        return publish_record(created)
    # Compatibilidade com o bus isolado usado por testes e ferramentas sem uma
    # aventura persistida. Esses eventos ainda têm replay no buffer em memória.
    with _lock:
        return _publish_locked(_channel_locked(adventure_id), adventure_id, type,
                               dict(data or {}), durable=True)


def broadcast(type: str, data: dict | None = None) -> list[int]:
    with _lock:
        return [_publish_locked(channel, adventure_id, type, dict(data or {}),
                                durable=False)
                for adventure_id, channel in list(_channels.items())]


def _queue_after_commit(conn: sqlite3.Connection, created: dict) -> None:
    with _pending_lock:
        entry = _pending.get(id(conn))
        if entry is None or entry[0] is not conn:
            entry = (conn, [])
            _pending[id(conn)] = entry
        entry[1].append(created)


def committed(conn: sqlite3.Connection) -> None:
    """Chamado exclusivamente por `db.session`, depois de um commit bem-sucedido."""
    with _pending_lock:
        entry = _pending.pop(id(conn), None)
    if entry is None or entry[0] is not conn:
        return
    for created in entry[1]:
        publish_record(created)


def rolled_back(conn: sqlite3.Connection) -> None:
    with _pending_lock:
        entry = _pending.get(id(conn))
        if entry is not None and entry[0] is conn:
            del _pending[id(conn)]


def _row_to_record(row: sqlite3.Row | dict) -> dict:
    keys = row.keys() if hasattr(row, "keys") else ()
    raw = row["payload_json"] if "payload_json" in keys else row["payload"]
    payload = json.loads(raw) if isinstance(raw, str) else dict(raw or {})
    event_id = int(row["event_id"])
    return {
        "id": event_id,
        "event_id": event_id,
        "adventure_id": row["adventure_id"],
        "state_revision": int(row["state_revision"]),
        "type": row["type"],
        "payload": payload,
        "actor": row["actor"] or "",
        "entity_id": row["entity_id"] or "",
        "turn_seq": row["turn_seq"],
        "generation_id": row["generation_id"] or "",
        "correlation_id": row["correlation_id"] or "",
        "causation_id": row["causation_id"],
        "sim_day": row["sim_day"],
        "sim_minute": row["sim_minute"],
        "created_at": row["created_at"],
        "idempotency_key": row["idempotency_key"],
    }


def _metadata(metadata: dict, *names: str, default: Any = None) -> Any:
    for name in names:
        if name in metadata:
            return metadata[name]
    return default


def record(conn: sqlite3.Connection, adventure_id: str, type: str,
           data: dict | None = None, **metadata) -> dict:
    """Insere um fato no mundo sem publicá-lo antes do commit.

    A sequência e a revisão são alocadas pela mesma transação SQLite. Uma
    idempotency_key repetida devolve o evento original sem criar outro.
    Metadados aceitos: state_revision, actor, entity_id/entity, turn_seq,
    generation_id, correlation_id/correlation, causation_id/causation,
    sim_day, sim_minute (ou sim_time={day, minute}), created_at e
    idempotency_key.
    """
    if not type or not isinstance(type, str):
        raise ValueError("event type is required")
    payload = dict(data or {})
    idempotency_key = metadata.get("idempotency_key")
    if idempotency_key is not None:
        idempotency_key = str(idempotency_key)
        previous = conn.execute(
            "SELECT * FROM world_events WHERE adventure_id=? AND idempotency_key=?",
            (adventure_id, idempotency_key),
        ).fetchone()
        if previous is not None:
            return _row_to_record(previous)

    # O UPDATE obtém o write lock do SQLite; dois escritores não podem alocar o
    # mesmo id. O contador e o evento fazem rollback juntos.
    conn.execute(
        "INSERT OR IGNORE INTO world_event_counters(adventure_id, last_event_id, state_revision)"
        " VALUES(?, COALESCE((SELECT MAX(event_id) FROM world_events WHERE adventure_id=?), 0),"
        " COALESCE((SELECT MAX(state_revision) FROM world_events WHERE adventure_id=?), 0))",
        (adventure_id, adventure_id, adventure_id),
    )
    conn.execute(
        "UPDATE world_event_counters SET last_event_id=last_event_id+1,"
        " state_revision=state_revision+1 WHERE adventure_id=?",
        (adventure_id,),
    )
    counter = conn.execute(
        "SELECT last_event_id, state_revision FROM world_event_counters WHERE adventure_id=?",
        (adventure_id,),
    ).fetchone()
    event_id = int(counter["last_event_id"])
    revision = int(counter["state_revision"])
    requested_revision = metadata.get("state_revision")
    if requested_revision is not None:
        revision = max(revision, int(requested_revision))
        conn.execute(
            "UPDATE world_event_counters SET state_revision=? WHERE adventure_id=?",
            (revision, adventure_id),
        )

    sim_time = metadata.get("sim_time") or {}
    if not isinstance(sim_time, dict):
        raise ValueError("sim_time must be an object with day and minute")
    created_at = metadata.get("created_at")
    if created_at is None:
        from app import db
        created_at = db.utcnow()
    values = (
        adventure_id, event_id, revision, type,
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
        str(_metadata(metadata, "actor", default="") or ""),
        str(_metadata(metadata, "entity_id", "entity", default="") or ""),
        metadata.get("turn_seq"),
        str(metadata.get("generation_id") or ""),
        str(_metadata(metadata, "correlation_id", "correlation", default="") or ""),
        _metadata(metadata, "causation_id", "causation"),
        _metadata(metadata, "sim_day", default=sim_time.get("day")),
        _metadata(metadata, "sim_minute", default=sim_time.get("minute")),
        created_at, idempotency_key,
    )
    cur = conn.execute(
        "INSERT INTO world_events(adventure_id, event_id, state_revision, type, payload_json,"
        " actor, entity_id, turn_seq, generation_id, correlation_id, causation_id,"
        " sim_day, sim_minute, created_at, idempotency_key)"
        " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT DO NOTHING",
        values,
    )
    if cur.rowcount != 1:
        previous = conn.execute(
            "SELECT * FROM world_events WHERE adventure_id=? AND idempotency_key=?",
            (adventure_id, idempotency_key),
        ).fetchone()
        if previous is None:
            raise sqlite3.IntegrityError("world event conflict without idempotency key")
        return _row_to_record(previous)
    created = _row_to_record(conn.execute(
        "SELECT * FROM world_events WHERE adventure_id=? AND event_id=?",
        (adventure_id, event_id),
    ).fetchone())
    _queue_after_commit(conn, created)
    return created


def list_history(conn: sqlite3.Connection, adventure_id: str, *, after: int = 0,
                 limit: int | None = 100) -> tuple[list[dict], bool]:
    sql = "SELECT * FROM world_events WHERE adventure_id=? AND event_id>? ORDER BY event_id"
    params: list[Any] = [adventure_id, int(after)]
    if limit is not None:
        sql += " LIMIT ?"
        params.append(int(limit) + 1)
    rows = conn.execute(sql, params).fetchall()
    has_more = limit is not None and len(rows) > limit
    if has_more:
        rows = rows[:limit]
    return [_row_to_record(row) for row in rows], has_more

def replay(conn: sqlite3.Connection, adventure_id: str,
           cursor: int) -> tuple[list[dict], bool, int]:
    bounds = conn.execute(
        "SELECT MIN(event_id) AS first_id, MAX(event_id) AS last_id"
        " FROM world_events WHERE adventure_id=?", (adventure_id,),
    ).fetchone()
    latest = int(bounds["last_id"] or 0)
    first = bounds["first_id"]
    expired = cursor < 0 or cursor > latest or (
        first is not None and cursor < int(first) - 1
    )
    if expired:
        return [], True, latest
    records, _ = list_history(conn, adventure_id, after=cursor, limit=None)
    return records, False, latest


def subscribe(adventure_id: str, last_event_id: int | None = None, *,
              backlog: list[dict] | None = None, since: int | None = None) -> Subscription:
    with _lock:
        channel = _channel_locked(adventure_id)
        if last_event_id is None:
            initial: list[Event] = []
            cursor = channel.latest if since is None else since
            filter_durable = since is not None
        else:
            filter_durable = True
            by_id = {int(item["event_id"]): _event_from_record(item)
                     for item in (backlog or []) if int(item["event_id"]) > last_event_id}
            for event in channel.buffer:
                if event.durable and event.id > last_event_id:
                    by_id.setdefault(event.id, event)
            initial = [by_id[key] for key in sorted(by_id)]
            cursor = last_event_id
        subscription = Subscription(adventure_id, initial, cursor,
                                    filter_durable=filter_durable)
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
    """Testes: descarta canais e inscritos, nunca o log durável."""
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
