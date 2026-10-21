"""Motor de regras e Modo Vida: pack, relógio, actor e rolagem no servidor."""

from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from app import auth, db, events, npc_assets
from app.errors import GameError
from app.routes.adventures import _managed_adventure, _visible_adventure
from app.sim import dice, intent, life, packs, rules, state

router = APIRouter()


class PackBody(BaseModel):
    pack: str = ""


class LifeBody(BaseModel):
    enabled: bool
    wage_per_hour: int = 5


class AdvanceBody(BaseModel):
    minutes: int = Field(ge=1, le=720)
    activity: str


class InventoryRow(BaseModel):
    name: str
    qty: int = 1


class ActorBody(BaseModel):
    resync: bool = False
    abilities: dict[str, int] | None = None
    skills: dict[str, int] | None = None
    proficient: list[str] | None = None
    inventory: list[InventoryRow] | None = None
    hp: int | None = None
    hp_max: int | None = None
    gold: int | None = None
    level: int | None = None
    xp: int | None = None


class RollBody(BaseModel):
    expr: str
    mode: str = ""


_PACKS = ("", "d20", "d100")


def _publish(adventure_id: str) -> None:
    events.publish(adventure_id, "sim_changed", {})


def _legacy_inventory(ui_state: dict) -> list | None:
    if not isinstance(ui_state, dict):
        return None
    named = ui_state.get("inventory")
    if isinstance(named, list):
        return named
    for value in ui_state.values():
        if isinstance(value, list) and value and all(
            isinstance(row, dict) and "name" in row for row in value
        ):
            return value
    return None


def _clock_view(sim: dict, user: str) -> dict | None:
    life_state = sim.get("life") if isinstance(sim.get("life"), dict) else None
    if not life_state:
        return None
    day = max(1, int(life_state.get("day") or 1))
    minute = int(life_state.get("minute") or 0)
    week, season = life.weekday_season(day)
    npcs = []
    bucket = life_state.get("npcs") if isinstance(life_state.get("npcs"), dict) else {}
    for card_id, npc in bucket.items():
        if not isinstance(npc, dict):
            continue
        needs = npc.get("needs") if isinstance(npc.get("needs"), dict) else {}
        npcs.append({
            "card_id": card_id,
            "name": npc.get("name") or "",
            "activity": npc.get("activity") or "",
            "place": npc.get("place") or "",
            "needs": needs,
            "mood": life.mood(needs),
        })
    relations = []
    raw = life_state.get("relations") if isinstance(life_state.get("relations"), dict) else {}
    for key, value in raw.items():
        if not isinstance(key, str) or "|" not in key:
            continue
        left, right = key.split("|", 1)
        relations.append({
            "a": left, "b": right,
            "label": life.relation_label(int(value or 0)),
            "value": int(value or 0),
        })
    return {
        "enabled": bool(life_state.get("enabled")),
        "day": day,
        "minute": minute,
        "clock": life.clock(minute),
        "weekday": week,
        "season": season,
        "npcs": npcs,
        "relations": relations,
    }


def _view(adventure: dict, user: str) -> dict:
    sim = state.load(adventure)
    key = state.actor_key(user)
    actor = (sim.get("actors") or {}).get(key)
    foes = [
        foe for foe in (sim.get("foes") or {}).values()
        if isinstance(foe, dict) and not foe.get("dead")
    ]
    return {
        "pack": sim.get("pack") or "",
        "packs": packs.list_packs(),
        "actor": actor if isinstance(actor, dict) else None,
        "foes": foes,
        "life": _clock_view(sim, user),
    }


def _sheet_stats(conn, adventure_id: str, user: str) -> tuple[dict | None, str]:
    card = db.player_character(conn, adventure_id, user) if user else None
    sheet = npc_assets.parse_sheet((card or {}).get("sheet") or "")
    stats_raw = sheet.get("stats") if isinstance(sheet.get("stats"), dict) else None
    title = ((card or {}).get("title") or "").strip() or user or "Jogador"
    return stats_raw, title


@router.get("/{adventure_id}/sim")
def get_sim(adventure_id: str, info: auth.AuthInfo = Depends(auth.require_auth)) -> dict:
    with db.session() as conn:
        adventure = _visible_adventure(conn, adventure_id, info.user)
        return _view(adventure, info.user)


@router.put("/{adventure_id}/sim/pack")
def put_pack(adventure_id: str, body: PackBody,
             info: auth.AuthInfo = Depends(auth.require_auth)) -> dict:
    pack_id = body.pack.strip()
    if pack_id not in _PACKS:
        raise GameError(422, "invalid_pack", "pack must be '', 'd20' or 'd100'")
    from app import game_engine
    with game_engine.appending(adventure_id) as conn:
        adventure = _managed_adventure(conn, adventure_id, info.user)
        sim = state.load(adventure)
        sim["pack"] = pack_id
        if pack_id:
            stats_raw, title = _sheet_stats(conn, adventure_id, info.user)
            rules.ensure_actor(
                sim, packs.load_pack(pack_id), info.user, title, stats_raw,
                _legacy_inventory(adventure.get("ui_state") or {}),
            )
        db.set_sim_state(conn, adventure_id, sim)
        saved = db.get_adventure(conn, adventure_id) or adventure
        payload = _view(saved, info.user)
    _publish(adventure_id)
    return payload


@router.put("/{adventure_id}/sim/life")
def put_life(adventure_id: str, body: LifeBody,
             info: auth.AuthInfo = Depends(auth.require_auth)) -> dict:
    from app import game_engine
    wage = max(0, min(100000, int(body.wage_per_hour)))
    with game_engine.appending(adventure_id) as conn:
        adventure = _managed_adventure(conn, adventure_id, info.user)
        sim = state.load(adventure)
        if body.enabled:
            if not isinstance(sim.get("life"), dict):
                life.enable(sim, wage)
            else:
                sim["life"]["enabled"] = True
                sim["life"]["wage_per_hour"] = wage
        else:
            life.disable(sim)
        db.set_sim_state(conn, adventure_id, sim)
        saved = db.get_adventure(conn, adventure_id) or adventure
        payload = _view(saved, info.user)
    _publish(adventure_id)
    return payload


@router.post("/{adventure_id}/sim/life/advance")
def post_advance(adventure_id: str, body: AdvanceBody,
                 info: auth.AuthInfo = Depends(auth.require_auth)) -> dict:
    if body.activity not in intent.ACTIVITIES:
        raise GameError(422, "invalid_activity", "unknown activity")
    from app import game_engine
    with game_engine.appending(adventure_id) as conn:
        adventure = _visible_adventure(conn, adventure_id, info.user)
        sim = state.load(adventure)
        life_state = sim.get("life") if isinstance(sim.get("life"), dict) else None
        if not life_state or not life_state.get("enabled"):
            raise GameError(409, "life_disabled", "life mode is off")
        cards = db.list_cards(conn, adventure_id)
        stats_raw, title = _sheet_stats(conn, adventure_id, info.user)
        rules.ensure_actor(sim, packs.load_pack(sim.get("pack") or ""), info.user, title, stats_raw)
        import random
        jumped = life.advance(
            sim, body.minutes, body.activity, state.actor_key(info.user),
            cards, random.SystemRandom(), 0.25,
        )
        if jumped.get("crossed_midnight"):
            from app import jobs
            jobs.enqueue_day_tick(conn, adventure_id, reason="clock")
        db.set_sim_state(conn, adventure_id, sim)
        saved = db.get_adventure(conn, adventure_id) or adventure
        payload = _view(saved, info.user)
    _publish(adventure_id)
    return payload


@router.put("/{adventure_id}/sim/actor")
def put_actor(adventure_id: str, body: ActorBody,
              info: auth.AuthInfo = Depends(auth.require_auth)) -> dict:
    from app import game_engine
    with game_engine.appending(adventure_id) as conn:
        adventure = _visible_adventure(conn, adventure_id, info.user)
        sim = state.load(adventure)
        pack = packs.load_pack(sim.get("pack") or "")
        stats_raw, title = _sheet_stats(conn, adventure_id, info.user)
        actor = rules.ensure_actor(sim, pack, info.user, title, stats_raw)
        if body.resync:
            fresh: dict = {"actors": {}}
            rules.ensure_actor(fresh, pack, info.user, title, stats_raw)
            actor["abilities"] = fresh["actors"][state.actor_key(info.user)]["abilities"]
        if body.abilities is not None:
            actor["abilities"] = {
                str(key)[:40]: max(1, min(100, int(value)))
                for key, value in body.abilities.items()
            }
        if body.skills is not None:
            actor["skills"] = {
                str(key)[:80]: max(0, min(200, int(value)))
                for key, value in body.skills.items()
            }
        if body.proficient is not None:
            actor["proficient"] = [str(item)[:80] for item in body.proficient[:40]]
        if body.inventory is not None:
            actor["inventory"] = rules._clean_inventory(
                [row.model_dump() for row in body.inventory[:40]]
            )
        if body.hp_max is not None:
            actor["hp_max"] = max(1, min(9999, int(body.hp_max)))
        if body.hp is not None:
            cap = actor.get("hp_max")
            hp = max(0, int(body.hp))
            if isinstance(cap, int):
                hp = min(hp, cap)
            actor["hp"] = hp
        if body.gold is not None:
            resources = actor.setdefault("resources", {"gold": 0, "san": None})
            resources["gold"] = max(0, min(1000000, int(body.gold)))
        if body.level is not None:
            actor["level"] = max(1, min(30, int(body.level)))
        if body.xp is not None:
            actor["xp"] = max(0, min(1000000, int(body.xp)))
        db.set_sim_state(conn, adventure_id, sim)
        saved = db.get_adventure(conn, adventure_id) or adventure
        payload = _view(saved, info.user)
    _publish(adventure_id)
    return payload


@router.post("/{adventure_id}/sim/roll")
def post_roll(adventure_id: str, body: RollBody,
              info: auth.AuthInfo = Depends(auth.require_auth)) -> dict:
    with db.session() as conn:
        _visible_adventure(conn, adventure_id, info.user)
    mode = body.mode if body.mode in ("", "adv", "dis") else ""
    try:
        if body.expr.strip().lower() == "d20" and mode in ("adv", "dis"):
            import random
            chosen, rolls = dice.roll_d20(random.SystemRandom(), mode)
            return {"expr": "d20", "rolls": rolls, "modifier": 0, "total": chosen}
        rolled = dice.roll(body.expr)
    except ValueError as exc:
        raise GameError(422, "invalid_dice", str(exc)) from exc
    return {
        "expr": rolled.expr,
        "rolls": rolled.rolls,
        "modifier": rolled.modifier,
        "total": rolled.total,
    }
