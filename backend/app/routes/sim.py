"""Motor de regras e Modo Vida: pack, relógio, actor e rolagem no servidor."""

from __future__ import annotations

import json
from typing import Literal

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from app import auth, db, events, npc_assets
from app.errors import GameError
from app.routes.adventures import _managed_adventure, _visible_adventure
from app.schemas import QuestCreate, QuestPatch, QuestTransition, QuestTriggerCreate
from app.sim import dice, intent, life, packs, quests, rng as sim_rng, rules, runtime, state, triggers

router = APIRouter()


class PackBody(BaseModel):
    pack: str = ""


class LifeBody(BaseModel):
    enabled: bool
    wage_per_hour: int = 5


class AdvanceBody(BaseModel):
    minutes: int = Field(ge=1, le=720)
    activity: str

class PlaybackBody(BaseModel):
    running: bool
    speed: Literal[1, 4, 20] | None = None


class StepBody(BaseModel):
    minutes: int = Field(ge=1, le=60)


class CatchupBody(BaseModel):
    minutes: int = Field(default=24 * 60, ge=1, le=24 * 60)
    key: str = "catchup"


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


def _clock_view(sim: dict, user: str, playback: dict | None = None) -> dict | None:
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
            "reason": npc.get("reason") or "",
            "agenda_next": npc.get("agenda_next") if isinstance(npc.get("agenda_next"), dict) else None,
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
    reactions = []
    raw_rx = life_state.get("reactions") if isinstance(life_state.get("reactions"), list) else []
    for item in raw_rx[-10:]:
        if not isinstance(item, dict):
            continue
        text = str(item.get("text") or "").strip()
        if text:
            reactions.append({
                "day": int(item.get("day") or day),
                "clock": str(item.get("clock") or ""),
                "text": text,
            })
    playback = playback or {"running": False, "speed": 1}
    return {
        "enabled": bool(life_state.get("enabled")),
        "running": bool(playback.get("running")),
        "speed": int(playback.get("speed") or 1),
        "day": day,
        "minute": minute,
        "clock": life.clock(minute),
        "weekday": week,
        "season": season,
        "npcs": npcs,
        "relations": relations,
        "reactions": reactions,
    }


def _quest_lists(adventure: dict, sim: dict, user: str) -> dict:
    owner = db.is_adventure_owner(adventure, user)
    return {
        "quests": quests.public_quests(sim, owner=owner),
        "triggers": quests.public_triggers(sim, owner=owner),
    }


def _view(adventure: dict, user: str, conn=None) -> dict:
    sim = state.load(adventure)
    key = state.actor_key(user)
    actor = (sim.get("actors") or {}).get(key)
    foes = [
        foe for foe in (sim.get("foes") or {}).values()
        if isinstance(foe, dict) and not foe.get("dead")
    ]
    listed = _quest_lists(adventure, sim, user)
    return {
        "pack": sim.get("pack") or "",
        "packs": packs.list_packs(),
        "actor": actor if isinstance(actor, dict) else None,
        "foes": foes,
        "life": _clock_view(
            sim, user, runtime.get_playback(conn, adventure["id"]) if conn is not None else None
        ),
        "quests": listed["quests"],
        "triggers": listed["triggers"],
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
        return _view(adventure, info.user, conn)


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
        payload = _view(saved, info.user, conn)
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
            runtime.set_playback(conn, adventure_id, False)
        db.set_sim_state(conn, adventure_id, sim)
        saved = db.get_adventure(conn, adventure_id) or adventure
        payload = _view(saved, info.user, conn)
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
        rng = sim_rng.gameplay_rng(sim)
        jumped = life.advance(
            sim, body.minutes, body.activity, state.actor_key(info.user),
            cards, rng, 0.25,
        )
        triggers.refresh(conn, adventure_id, sim, user=info.user, cards=cards)
        if jumped.get("crossed_midnight"):
            from app import jobs
            jobs.enqueue_day_tick(conn, adventure_id, reason="clock")
        db.set_sim_state(conn, adventure_id, sim)
        saved = db.get_adventure(conn, adventure_id) or adventure
        payload = _view(saved, info.user, conn)
    _publish(adventure_id)
    return payload


@router.put("/{adventure_id}/sim/life/playback")
def put_playback(adventure_id: str, body: PlaybackBody,
                 info: auth.AuthInfo = Depends(auth.require_auth)) -> dict:
    from app import game_engine
    with game_engine.appending(adventure_id) as conn:
        adventure = _managed_adventure(conn, adventure_id, info.user)
        sim = state.load(adventure)
        life_state = sim.get("life") if isinstance(sim.get("life"), dict) else None
        if not life_state or not life_state.get("enabled"):
            raise GameError(409, "life_disabled", "life mode is off")
        runtime.set_playback(conn, adventure_id, body.running, body.speed)
        saved = db.get_adventure(conn, adventure_id) or adventure
        payload = _view(saved, info.user, conn)
        playback = payload["life"]
        created = events.record(
            conn, adventure_id, "life_playback_changed",
            {"running": playback["running"], "speed": playback["speed"]},
            sim_day=playback["day"], sim_minute=playback["minute"],
        )
    events.publish(created)
    _publish(adventure_id)
    return payload


@router.post("/{adventure_id}/sim/life/catchup")
def post_catchup(adventure_id: str, body: CatchupBody,
                 info: auth.AuthInfo = Depends(auth.require_auth)) -> dict:
    with db.session() as conn:
        _visible_adventure(conn, adventure_id, info.user)
    runtime.catch_up(adventure_id, body.minutes, key=body.key or "catchup")
    with db.session() as conn:
        saved = _visible_adventure(conn, adventure_id, info.user)
        return _view(saved, info.user, conn)


@router.post("/{adventure_id}/sim/life/step")
def post_step(adventure_id: str, body: StepBody,
              info: auth.AuthInfo = Depends(auth.require_auth)) -> dict:
    with db.session() as conn:
        _visible_adventure(conn, adventure_id, info.user)
    runtime.run_tick(adventure_id, body.minutes, source="step")
    with db.session() as conn:
        saved = _visible_adventure(conn, adventure_id, info.user)
        return _view(saved, info.user, conn)


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
        payload = _view(saved, info.user, conn)
    _publish(adventure_id)
    return payload


@router.post("/{adventure_id}/sim/roll")
def post_roll(adventure_id: str, body: RollBody,
              info: auth.AuthInfo = Depends(auth.require_auth)) -> dict:
    with db.session() as conn:
        _visible_adventure(conn, adventure_id, info.user)
    mode = body.mode if body.mode in ("", "adv", "dis") else ""
    expr = body.expr or ""
    try:
        if not (expr.strip().lower() == "d20" and mode in ("adv", "dis")):
            dice.parse(expr)
    except ValueError as exc:
        raise GameError(422, "invalid_dice", str(exc)) from exc
    from app import game_engine
    with game_engine.appending(adventure_id) as conn:
        adventure = _visible_adventure(conn, adventure_id, info.user)
        sim = state.load(adventure)
        rng = sim_rng.gameplay_rng(sim)
        try:
            if expr.strip().lower() == "d20" and mode in ("adv", "dis"):
                chosen, rolls = dice.roll_d20(rng, mode)
                result = {"expr": "d20", "rolls": rolls, "modifier": 0, "total": chosen}
            else:
                rolled = dice.roll(expr, rng)
                result = {
                    "expr": rolled.expr,
                    "rolls": rolled.rolls,
                    "modifier": rolled.modifier,
                    "total": rolled.total,
                }
        except ValueError as exc:
            raise GameError(422, "invalid_dice", str(exc)) from exc
        db.set_sim_state(conn, adventure_id, sim)
    return result


class ActionBody(BaseModel):
    """Ação mecânica direta (sem LLM): o jogador clicou, o servidor rola."""
    kind: str
    target: str = ""
    toughness: str = "normal"
    hostile: bool = False
    weapon: str = ""
    item: str = ""
    skill: str = ""
    ability: str = ""
    gold: int = 0
    difficulty: str = "medium"
    advantage: str = ""
    minutes: int = 5
    heal: bool = False


_ACTION_KINDS = ("attack", "skill", "save", "use_item", "buy", "take", "rest")


@router.post("/{adventure_id}/sim/action")
def post_action(adventure_id: str, body: ActionBody,
                info: auth.AuthInfo = Depends(auth.require_auth)) -> dict:
    if body.kind not in _ACTION_KINDS:
        raise GameError(422, "invalid_kind", "unknown action kind")
    from app import game_engine
    minutes = max(1, min(720, int(body.minutes or 5)))
    activity = "rest"
    if body.kind == "rest" and minutes >= 360:
        activity = "sleep"
    elif body.kind != "rest":
        activity = "other"
    raw = intent.normalize({
        "kind": body.kind, "ability": body.ability, "skill": body.skill,
        "target": body.target, "toughness": body.toughness, "hostile": body.hostile,
        "weapon": body.weapon, "item": body.item, "gold": body.gold,
        "difficulty": body.difficulty, "advantage": body.advantage,
        "minutes": minutes, "activity": activity, "shock": "none", "heal": body.heal,
    })
    with game_engine.appending(adventure_id) as conn:
        adventure = _visible_adventure(conn, adventure_id, info.user)
        sim = state.load(adventure)
        pack_id = sim.get("pack") or ""
        life_state = sim.get("life") if isinstance(sim.get("life"), dict) else None
        life_on = bool(life_state and life_state.get("enabled"))
        if not pack_id and not life_on:
            raise GameError(409, "sim_off", "rules and life mode are off")
        pack = packs.load_pack(pack_id)
        cards = db.list_cards(conn, adventure_id)
        stats_raw, title = _sheet_stats(conn, adventure_id, info.user)
        actor = rules.ensure_actor(sim, pack, info.user, title, stats_raw)
        key = state.actor_key(info.user)
        penalty = life.penalty(actor, pack) if life_on else 0
        rng = sim_rng.gameplay_rng(sim)
        resolution = rules.resolve(sim, pack, raw, key, cards, penalty, rng)
        if resolution.refusal:
            raise GameError(409, "rules_refused", resolution.refusal)
        triggers.refresh(conn, adventure_id, sim, user=info.user, cards=cards)
        time_info = None
        if life_on:
            time_info = life.advance(
                sim, int(raw.get("minutes") or 5), raw.get("activity") or "other",
                key, cards, rng, 0.25,
            )
            triggers.refresh(conn, adventure_id, sim, user=info.user, cards=cards)
            if time_info.get("crossed_midnight"):
                from app import jobs
                jobs.enqueue_day_tick(conn, adventure_id, reason="clock")
        for card_id, status in resolution.card_status.items():
            card = db.get_card(conn, adventure_id, card_id)
            if not card:
                continue
            sheet = npc_assets.parse_sheet(card.get("sheet") or "")
            sheet["status"] = status
            db.update_card(conn, adventure_id, card_id, {
                "sheet": json.dumps(sheet, ensure_ascii=False),
            })
        db.set_sim_state(conn, adventure_id, sim)
        saved = db.get_adventure(conn, adventure_id) or adventure
        payload = _view(saved, info.user, conn)
        payload["resolution"] = {
            "steps": resolution.steps,
            "changes": resolution.changes,
            "time": time_info,
        }
    _publish(adventure_id)
    return payload


def _raise_quest(exc: quests.QuestError) -> None:
    status = {"quest_not_found": 404, "trigger_not_found": 404, "invalid_transition": 409}.get(exc.code, 422)
    raise GameError(status, exc.code, exc.message) from exc


def _mutate_quests(adventure_id: str, user: str, fn) -> dict:
    from app import game_engine
    with game_engine.appending(adventure_id) as conn:
        adventure = _managed_adventure(conn, adventure_id, user)
        sim = state.load(adventure)
        try:
            fn(sim)
        except quests.QuestError as exc:
            _raise_quest(exc)
        db.set_sim_state(conn, adventure_id, sim)
        saved = db.get_adventure(conn, adventure_id) or adventure
        payload = _view(saved, user, conn)
    _publish(adventure_id)
    return payload


@router.get("/{adventure_id}/sim/quests")
def get_quests(adventure_id: str, info: auth.AuthInfo = Depends(auth.require_auth)) -> dict:
    with db.session() as conn:
        adventure = _visible_adventure(conn, adventure_id, info.user)
        sim = state.load(adventure)
        return _quest_lists(adventure, sim, info.user)


@router.post("/{adventure_id}/sim/quests")
def post_quest(adventure_id: str, body: QuestCreate,
               info: auth.AuthInfo = Depends(auth.require_auth)) -> dict:
    return _mutate_quests(adventure_id, info.user, lambda sim: quests.create(sim, body.model_dump()))


@router.patch("/{adventure_id}/sim/quests/{quest_id}")
def patch_quest(adventure_id: str, quest_id: str, body: QuestPatch,
                info: auth.AuthInfo = Depends(auth.require_auth)) -> dict:
    data = body.model_dump(exclude_unset=True)
    return _mutate_quests(
        adventure_id, info.user, lambda sim: quests.update(sim, quest_id, data),
    )


@router.post("/{adventure_id}/sim/quests/{quest_id}/transition")
def post_quest_transition(adventure_id: str, quest_id: str, body: QuestTransition,
                          info: auth.AuthInfo = Depends(auth.require_auth)) -> dict:
    data = body.model_dump(exclude_unset=True)
    return _mutate_quests(
        adventure_id, info.user, lambda sim: quests.transition(sim, quest_id, data),
    )


@router.delete("/{adventure_id}/sim/quests/{quest_id}")
def delete_quest(adventure_id: str, quest_id: str,
                 info: auth.AuthInfo = Depends(auth.require_auth)) -> dict:
    return _mutate_quests(adventure_id, info.user, lambda sim: quests.delete(sim, quest_id))


@router.post("/{adventure_id}/sim/triggers")
def post_trigger(adventure_id: str, body: QuestTriggerCreate,
                 info: auth.AuthInfo = Depends(auth.require_auth)) -> dict:
    return _mutate_quests(adventure_id, info.user, lambda sim: quests.add_trigger(sim, body.model_dump()))


@router.delete("/{adventure_id}/sim/triggers/{trigger_id}")
def delete_trigger(adventure_id: str, trigger_id: str,
                   info: auth.AuthInfo = Depends(auth.require_auth)) -> dict:
    return _mutate_quests(adventure_id, info.user, lambda sim: quests.delete_trigger(sim, trigger_id))


triggers.install_hooks()
