"""Um turno do motor: classifica, resolve, grava. Único módulo que fala com o db."""

from __future__ import annotations

import json
import logging
import random
import re

from app import db, npc_assets
from app.sim import intent, life, packs, rules, state

log = logging.getLogger("aidungeon.sim.engine")


def _sim_settings(cfg: dict | None) -> dict:
    raw = None
    if isinstance(cfg, dict) and isinstance(cfg.get("sim"), dict):
        raw = cfg["sim"]
    if raw is None:
        try:
            from app import settings_store
            loaded = settings_store.load_file_settings().get("sim") or {}
            raw = loaded if isinstance(loaded, dict) else {}
        except Exception:
            log.warning("sim: configuração indisponível", exc_info=True)
            raw = {}
    return {
        "enabled": raw.get("enabled", True) is not False,
        "max_tokens": raw.get("max_tokens", 220),
        "timeout_seconds": raw.get("timeout_seconds", 20),
        "min_words": int(raw.get("min_words", 3) or 3),
        "npc_colocation_chance": float(raw.get("npc_colocation_chance", 0.25) or 0),
    }


def _plain(action: dict) -> str:
    text = (action.get("text") or "").strip()
    say = re.match(r'^>\s*You\s+say\s+"(.*)"\s*$', text, re.IGNORECASE | re.DOTALL)
    if say:
        return say.group(1).strip()
    do = re.match(r"^>\s*You\s+(.*)$", text, re.IGNORECASE | re.DOTALL)
    if do:
        return do.group(1).strip()
    return text


def _actor_view(actor: dict) -> dict:
    inventory = actor.get("inventory") if isinstance(actor.get("inventory"), list) else []
    return {
        "name": actor.get("name"),
        "hp": actor.get("hp"),
        "hp_max": actor.get("hp_max"),
        "ac": actor.get("ac"),
        "abilities": actor.get("abilities") or {},
        "skills": actor.get("skills") or {},
        "inventory": [row.get("name") for row in inventory if isinstance(row, dict)],
        "down": bool(actor.get("down")),
    }


def run_turn(adventure_id: str, adventure: dict, player_action: dict,
             actions: list[dict], user: str, llm_cfg: dict, cfg: dict,
             transport) -> dict | None:
    settings = _sim_settings(cfg)
    if not settings["enabled"]:
        return None
    if player_action is None or player_action.get("type") not in ("do", "say"):
        return None
    loaded = state.load(adventure)
    life_on = isinstance(loaded.get("life"), dict) and loaded["life"].get("enabled")
    pack_id = loaded.get("pack") or ""
    if not pack_id and not life_on:
        return None
    pack = packs.load_pack(pack_id) if pack_id else None
    before = state.snapshot(loaded)
    with db.session() as conn:
        cards = db.list_cards(conn, adventure_id)
        character = db.player_character(conn, adventure_id, user) if user else None
    sheet = npc_assets.parse_sheet((character or {}).get("sheet") or "")
    name = ((character or {}).get("title") or "").strip() or user or "Jogador"
    actor = rules.ensure_actor(
        loaded, pack, user, name, sheet.get("stats") if isinstance(sheet.get("stats"), dict) else None,
    )
    key = state.actor_key(user)
    text = _plain(player_action)
    if player_action.get("type") == "say":
        classified = intent.normalize({"kind": "none", "activity": "social", "minutes": 5})
    elif len(text.split()) >= settings["min_words"]:
        foes = [
            foe.get("name") for foe in (loaded.get("foes") or {}).values()
            if isinstance(foe, dict) and not foe.get("dead") and foe.get("name")
        ]
        classified = intent.classify(
            text, _actor_view(actor), foes, pack, llm_cfg, transport, settings,
        )
        if classified is None:
            classified = intent.normalize({})
    else:
        classified = intent.normalize({})
    penalty = life.penalty(actor, pack) if life_on else 0
    rng = random.SystemRandom()
    resolution = rules.resolve(loaded, pack, classified, key, cards, penalty, rng)
    if resolution.refusal:
        return {"refusal": resolution.refusal}
    time_info = None
    if life_on:
        time_info = life.advance(
            loaded, int(classified.get("minutes") or 5), classified.get("activity") or "other",
            key, cards, rng, settings["npc_colocation_chance"],
        )
    block = rules.build_block(resolution.steps, resolution.changes, time_info, None)
    payload = {
        "pack": pack_id,
        "intent": classified,
        "steps": resolution.steps,
        "changes": resolution.changes,
        "time": time_info,
        "block": block,
    }
    from app import game_engine
    with game_engine.appending(adventure_id) as conn:
        cards_before: dict[str, str | None] = {}
        for card_id, status in resolution.card_status.items():
            card = db.get_card(conn, adventure_id, card_id)
            if not card:
                continue
            sheet_now = npc_assets.parse_sheet(card.get("sheet") or "")
            previous = sheet_now.get("status")
            cards_before[str(card_id)] = previous if isinstance(previous, str) else None
            sheet_now["status"] = status
            db.update_card(conn, adventure_id, card_id, {
                "sheet": json.dumps(sheet_now, ensure_ascii=False),
            })
        if time_info and time_info.get("crossed_midnight"):
            from app import jobs
            jobs.enqueue_day_tick(conn, adventure_id, reason="clock")
        db.set_sim_state(conn, adventure_id, loaded)
        db.set_action_rules(conn, int(player_action["id"]), {
            "before": before,
            "cards": cards_before,
            "resolution": payload,
        })
    return {"resolution": payload}


def revert(conn, adventure_id: str, action_id: int) -> bool:
    payload = db.get_action_rules(conn, action_id)
    if not isinstance(payload, dict) or "before" not in payload:
        return False
    before = payload.get("before")
    if isinstance(before, dict):
        db.set_sim_state(conn, adventure_id, before)
    for card_id, previous in (payload.get("cards") or {}).items():
        card = db.get_card(conn, adventure_id, str(card_id))
        if not card:
            continue
        sheet = npc_assets.parse_sheet(card.get("sheet") or "")
        if previous is None:
            sheet.pop("status", None)
        else:
            sheet["status"] = previous
        db.update_card(conn, adventure_id, str(card_id), {
            "sheet": json.dumps(sheet, ensure_ascii=False),
        })
    db.clear_action_rules(conn, action_id)
    return True


def reuse_block(actions: list[dict], retry_of: dict) -> str | None:
    limit = int(retry_of.get("seq") or 0)
    prior = [
        action for action in actions
        if action.get("type") in db.PLAYER_TYPES and int(action.get("seq") or 0) < limit
    ]
    if not prior:
        return None
    last = max(prior, key=lambda action: int(action.get("seq") or 0))
    rules_payload = last.get("rules") if isinstance(last.get("rules"), dict) else None
    if not rules_payload:
        return None
    block = rules_payload.get("block")
    return block if isinstance(block, str) and block else None
