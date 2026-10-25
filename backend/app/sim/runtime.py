"""Tempo autônomo do Modo Vida: lease, ticks idempotentes e worker sem LLM."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import random
import threading
import time

from app import db, events
from app.errors import GameError
from app.sim import life, state

log = logging.getLogger("aidungeon.sim.runtime")

DEFAULT_SPEED = 1
SPEEDS = (1, 4, 20)
BASE_TICK_MINUTES = 1
LEASE_SECONDS = 30.0
WORKER_INTERVAL = 1.0
_WAKEUP = threading.Event()


def get_playback(conn, adventure_id: str) -> dict:
    row = conn.execute(
        "SELECT running, speed FROM sim_runtime WHERE adventure_id=?", (adventure_id,)
    ).fetchone()
    if row is None:
        return {"running": False, "speed": DEFAULT_SPEED}
    speed = int(row["speed"] or DEFAULT_SPEED)
    return {
        "running": bool(row["running"]),
        "speed": speed if speed in SPEEDS else DEFAULT_SPEED,
    }


def set_playback(conn, adventure_id: str, running: bool,
                 speed: int | None = None) -> dict:
    current = get_playback(conn, adventure_id)
    selected = current["speed"] if speed is None else int(speed)
    if selected not in SPEEDS:
        raise GameError(422, "invalid_speed", "speed must be 1, 4 or 20")
    conn.execute(
        """INSERT INTO sim_runtime(adventure_id, running, speed, updated_at)
           VALUES(?,?,?,?)
           ON CONFLICT(adventure_id) DO UPDATE SET
             running=excluded.running, speed=excluded.speed, updated_at=excluded.updated_at""",
        (adventure_id, int(bool(running)), selected, db.utcnow()),
    )
    _WAKEUP.set()
    return {"running": bool(running), "speed": selected}


def _stored_tick(row: dict | None) -> dict | None:
    if row is None:
        return None
    payload = json.loads(row["payload"] or "{}")
    return {**payload, "replayed": True}


def _tick_row(conn, adventure_id: str, tick_id: str):
    return conn.execute(
        "SELECT payload FROM sim_ticks WHERE adventure_id=? AND tick_id=?",
        (adventure_id, tick_id),
    ).fetchone()


def _release_lease(adventure_id: str, tick_id: str, owner: str) -> None:
    try:
        with db.session() as conn:
            conn.execute(
                """UPDATE sim_runtime SET lease_owner='', lease_until=0, updated_at=?
                   WHERE adventure_id=? AND active_tick_id=? AND lease_owner=?""",
                (db.utcnow(), adventure_id, tick_id, owner),
            )
    except Exception:
        log.exception("sim runtime: falha ao liberar lease (%s)", adventure_id)


def run_tick(adventure_id: str, minutes: int | None = None, *,
             tick_id: str | None = None, require_running: bool = False,
             source: str = "auto", lease_seconds: float = LEASE_SECONDS) -> dict:
    """Executa um tick sob lease. Reusar `tick_id` nunca repete seus efeitos."""
    from app import game_engine

    owner = db.new_id()
    now = time.time()
    with game_engine.appending(adventure_id) as conn:
        adventure = db.get_adventure(conn, adventure_id)
        if adventure is None:
            raise GameError(404, "not_found", "adventure not found")
        sim = state.load(adventure)
        life_state = sim.get("life") if isinstance(sim.get("life"), dict) else None
        if not life_state or not life_state.get("enabled"):
            raise GameError(409, "life_disabled", "life mode is off")
        playback = get_playback(conn, adventure_id)
        if require_running and not playback["running"]:
            return {"skipped": "paused", "replayed": False}
        if tick_id:
            replay = _stored_tick(_tick_row(conn, adventure_id, tick_id))
            if replay is not None:
                return replay
        conn.execute(
            "INSERT OR IGNORE INTO sim_runtime(adventure_id, updated_at) VALUES(?,?)",
            (adventure_id, db.utcnow()),
        )
        runtime = conn.execute(
            "SELECT * FROM sim_runtime WHERE adventure_id=?", (adventure_id,)
        ).fetchone()
        active = str(runtime["active_tick_id"] or "")
        leased_until = float(runtime["lease_until"] or 0)
        if active and leased_until > now:
            return {"skipped": "leased", "tick_id": active, "replayed": False}
        if tick_id is None:
            if active:
                tick_id = active
            else:
                sequence = int(runtime["tick_seq"] or 0) + 1
                tick_id = f"{adventure_id}:{sequence}"
        elif active and active != tick_id:
            tick_id = active
        selected_minutes = (
            BASE_TICK_MINUTES * playback["speed"] if minutes is None else int(minutes)
        )
        if selected_minutes < 1:
            raise ValueError("minutes must be positive")
        cur = conn.execute(
            """UPDATE sim_runtime SET
                 tick_seq=CASE WHEN active_tick_id='' THEN tick_seq+1 ELSE tick_seq END,
                 active_tick_id=?, lease_owner=?, lease_until=?, updated_at=?
               WHERE adventure_id=? AND
                 (active_tick_id=? OR active_tick_id='' OR lease_until<=?)""",
            (tick_id, owner, now + max(1.0, float(lease_seconds)), db.utcnow(),
             adventure_id, active, now),
        )
        if cur.rowcount != 1:
            return {"skipped": "leased", "replayed": False}

    created = None
    try:
        with game_engine.appending(adventure_id) as conn:
            replay = _stored_tick(_tick_row(conn, adventure_id, tick_id))
            if replay is not None:
                return replay
            adventure = db.get_adventure(conn, adventure_id)
            sim = state.load(adventure)
            life_state = sim.get("life") if isinstance(sim.get("life"), dict) else None
            if not life_state or not life_state.get("enabled"):
                raise GameError(409, "life_disabled", "life mode is off")
            day = max(1, int(life_state.get("day") or 1))
            minute = int(life_state.get("minute") or 0)
            claimed = conn.execute(
                """INSERT OR IGNORE INTO sim_ticks(
                     adventure_id, tick_id, day, minute, payload, created_at)
                   VALUES(?,?,?,?,?,?)""",
                (adventure_id, tick_id, day, minute, "{}", db.utcnow()),
            )
            if claimed.rowcount != 1:
                return _stored_tick(_tick_row(conn, adventure_id, tick_id))
            cards = db.list_cards(conn, adventure_id)
            actor_key = state.actor_key(adventure.get("owner") or "")
            digest = hashlib.sha256(f"{adventure_id}:{tick_id}".encode()).digest()
            jumped = life.advance(
                sim, selected_minutes, "other", actor_key, cards,
                random.Random(int.from_bytes(digest[:8], "big")), 0.25,
            )
            db.set_sim_state(conn, adventure_id, sim)
            saved_life = sim["life"]
            activities = [
                {
                    "card_id": card_id,
                    "name": npc.get("name") or "NPC",
                    "activity": npc.get("activity") or "",
                    "place": npc.get("place") or "",
                    "reason": npc.get("reason") or "",
                    "agenda_next": npc.get("agenda_next") if isinstance(npc.get("agenda_next"), dict) else None,
                }
                for card_id, npc in (saved_life.get("npcs") or {}).items()
                if isinstance(npc, dict)
            ]
            payload = {
                **jumped,
                "tick_id": tick_id,
                "minute": int(saved_life["minute"]),
                "clock": life.clock(saved_life["minute"]),
                "source": source,
                "activities": activities,
            }
            conn.execute(
                "UPDATE sim_ticks SET day=?, minute=?, payload=? WHERE adventure_id=? AND tick_id=?",
                (int(saved_life["day"]), int(saved_life["minute"]),
                 json.dumps(payload, ensure_ascii=False), adventure_id, tick_id),
            )
            created = events.record(
                conn, adventure_id, "life_tick_completed", payload,
                idempotency_key=tick_id,
                sim_day=int(saved_life["day"]), sim_minute=int(saved_life["minute"]),
            )
            conn.execute(
                """UPDATE sim_runtime SET active_tick_id='', lease_owner='',
                     lease_until=0, last_tick_real=?, updated_at=?
                   WHERE adventure_id=? AND active_tick_id=? AND lease_owner=?""",
                (time.time(), db.utcnow(), adventure_id, tick_id, owner),
            )
    except Exception:
        _release_lease(adventure_id, tick_id, owner)
        raise

    events.publish(created)
    events.publish(adventure_id, "sim_changed", {"tick_id": tick_id})
    return {**payload, "replayed": False}


def catch_up(adventure_id: str, minutes: int = 24 * 60, *, key: str = "catchup") -> dict:
    """Recupera minutos lógicos em fatias de uma hora. A mesma chave não aplica duas vezes.

    Não chama o LLM. O teto é 24 horas lógicas. O worker usa isto depois de
    um buraco no relógio real, com chave presa ao instante do último tick.
    """
    minutes = max(0, min(24 * 60, int(minutes)))
    safe = "".join(ch for ch in str(key) if ch.isalnum() or ch in ":-_")[:80] or "catchup"
    offset = 0
    applied = 0
    replayed = 0
    while offset < minutes:
        slice_min = min(60, minutes - offset)
        result = run_tick(
            adventure_id, slice_min, tick_id=f"{safe}:{offset}", source="catchup",
        )
        if result.get("replayed"):
            replayed += 1
        elif not result.get("skipped"):
            applied += 1
        offset += slice_min
    return {"minutes": minutes, "applied": applied, "replayed": replayed, "key": safe}


def _catch_up_downtime(adventure_id: str) -> None:
    with db.session() as conn:
        row = conn.execute(
            "SELECT running, speed, last_tick_real FROM sim_runtime WHERE adventure_id=?",
            (adventure_id,),
        ).fetchone()
    if row is None or not row["running"]:
        return
    last = float(row["last_tick_real"] or 0)
    if last <= 0:
        return
    gap = time.time() - last
    if gap < 5:
        return
    logical = min(24 * 60, int(gap * int(row["speed"] or 1)))
    if logical < 1:
        return
    catch_up(adventure_id, logical, key=f"downtime:{int(last)}")


def run_pending() -> int:
    """Um lote do worker: no máximo um tick por aventura ativa."""
    with db.session() as conn:
        ids = [row["id"] for row in conn.execute(
            "SELECT a.id FROM adventures a JOIN sim_runtime r ON r.adventure_id=a.id "
            "WHERE r.running=1 ORDER BY a.id"
        )]
    completed = 0
    for adventure_id in ids:
        try:
            _catch_up_downtime(adventure_id)
            result = run_tick(adventure_id, require_running=True)
            completed += int(not result.get("skipped") and not result.get("replayed"))
        except GameError as exc:
            if exc.code != "life_disabled":
                log.warning("sim runtime: tick recusado (%s): %s", adventure_id, exc.message)
        except Exception:
            log.exception("sim runtime: tick falhou (%s)", adventure_id)
    return completed


async def worker_loop(interval: float = WORKER_INTERVAL) -> None:
    log.info("sim runtime worker iniciado")
    while True:
        try:
            await asyncio.to_thread(run_pending)
        except Exception:
            log.exception("sim runtime worker: erro no ciclo")
        await asyncio.to_thread(_WAKEUP.wait, interval)
        _WAKEUP.clear()


def start_worker(app) -> None:
    from app import jobs

    if not jobs.worker_enabled():
        log.info("sim runtime worker desligado (AIDUNGEON_JOBS_WORKER=0)")
        return
    _WAKEUP.clear()
    app.state.sim_worker = asyncio.create_task(worker_loop())


def stop_worker(app) -> None:
    task = getattr(app.state, "sim_worker", None)
    if task:
        task.cancel()
        _WAKEUP.set()
