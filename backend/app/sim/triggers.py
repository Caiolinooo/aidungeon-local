"""Gatilhos declarativos. Avaliam o estado depois de resolve, viagem e relação."""

from __future__ import annotations

import copy

from app.sim import quests, state as sim_state

_OPS = {
    "eq": lambda left, right: left == right,
    "ne": lambda left, right: left != right,
    "lt": lambda left, right: left < right,
    "lte": lambda left, right: left <= right,
    "gt": lambda left, right: left > right,
    "gte": lambda left, right: left >= right,
}


def _cmp(op: str | None, left, right) -> bool:
    if left is None or right is None:
        return False
    fn = _OPS.get(op or "eq")
    if fn is None:
        return False
    try:
        return bool(fn(left, right))
    except TypeError:
        return False


def _actor(sim: dict, user: str) -> dict | None:
    actors = sim.get("actors") if isinstance(sim.get("actors"), dict) else {}
    if user:
        actor = actors.get(sim_state.actor_key(user))
        if isinstance(actor, dict):
            return actor
    for actor in actors.values():
        if isinstance(actor, dict):
            return actor
    return None


def _sheet(card: dict) -> dict:
    raw = card.get("sheet") if isinstance(card, dict) else None
    if isinstance(raw, dict):
        return raw
    from app import npc_assets
    return npc_assets.parse_sheet(raw or "")


def _int_map(raw: dict) -> dict[str, int]:
    out: dict[str, int] = {}
    for key, value in raw.items():
        if isinstance(value, bool):
            continue
        try:
            out[str(key)] = int(value)
        except (TypeError, ValueError):
            continue
    return out


def build_facts(sim: dict, *, user: str = "", cards: list | None = None,
                location: dict | None = None) -> dict:
    actor = _actor(sim, user)
    resources = actor.get("resources") if isinstance((actor or {}).get("resources"), dict) else {}
    life = sim.get("life") if isinstance(sim.get("life"), dict) else {}
    npcs = life.get("npcs") if isinstance(life.get("npcs"), dict) else {}
    places = []
    for card_id, npc in npcs.items():
        if isinstance(npc, dict):
            places.append({"card_id": str(card_id), "place": npc.get("place") or ""})
    relations = life.get("relations") if isinstance(life.get("relations"), dict) else {}
    card_facts: dict[str, dict] = {}
    for card in cards or []:
        if not isinstance(card, dict) or not card.get("id"):
            continue
        sheet = _sheet(card)
        bonds = sheet.get("bonds") if isinstance(sheet.get("bonds"), dict) else {}
        raw_relations = sheet.get("relations") if isinstance(sheet.get("relations"), dict) else {}
        cleaned_relations: dict[str, dict] = {}
        for other_id, rel in raw_relations.items():
            if isinstance(rel, dict):
                cleaned_relations[str(other_id)] = _int_map(rel)
            elif isinstance(rel, int) and not isinstance(rel, bool):
                cleaned_relations[str(other_id)] = {"value": rel}
        card_facts[str(card["id"])] = {
            "id": str(card["id"]),
            "title": card.get("title") or "",
            "bonds": _int_map(bonds),
            "relations": cleaned_relations,
        }
    loc = None
    if isinstance(location, dict) and location.get("id"):
        loc = {"id": str(location["id"]), "title": location.get("title") or ""}
    hp = actor.get("hp") if isinstance((actor or {}).get("hp"), int) else None
    gold = resources.get("gold")
    san = resources.get("san")
    return {
        "hp": hp if isinstance(hp, int) else None,
        "gold": int(gold) if isinstance(gold, int) and not isinstance(gold, bool) else None,
        "san": int(san) if isinstance(san, int) and not isinstance(san, bool) else None,
        "resources": {
            key: int(value)
            for key, value in resources.items()
            if isinstance(value, int) and not isinstance(value, bool)
        },
        "items": quests.folded_items(actor),
        "location": loc,
        "places": places,
        "life_relations": relations,
        "cards": card_facts,
    }


def _card(facts: dict, ident: str) -> dict | None:
    cards = facts.get("cards") or {}
    if ident in cards:
        return cards[ident]
    folded = sim_state.fold(ident)
    hits = [card for card in cards.values() if sim_state.fold(card.get("title") or "") == folded]
    if len(hits) == 1:
        return hits[0]
    return None


def _relation_value(facts: dict, clause: dict):
    if clause.get("a") and clause.get("b"):
        left, right = clause["a"], clause["b"]
        raw = facts.get("life_relations") or {}
        for key in (f"{left}|{right}", f"{right}|{left}"):
            if key in raw:
                try:
                    return int(raw[key])
                except (TypeError, ValueError):
                    return None
        return None
    card = _card(facts, clause.get("card_id") or "")
    if not card:
        return None
    other = clause.get("other_id") or ""
    stat = clause.get("stat") or "trust"
    if other:
        rel = (card.get("relations") or {}).get(other) or {}
        if stat not in rel:
            return None
        return rel[stat]
    bonds = card.get("bonds") or {}
    if stat not in bonds:
        return None
    return bonds[stat]


def _clause_ok(clause: dict, facts: dict) -> bool:
    if not isinstance(clause, dict):
        return False
    fact = clause.get("fact")
    op = clause.get("op")
    if fact == "hp":
        return _cmp(op or "lte", facts.get("hp"), clause.get("value"))
    if fact == "gold":
        return _cmp(op or "gte", facts.get("gold"), clause.get("value"))
    if fact == "san":
        return _cmp(op or "lte", facts.get("san"), clause.get("value"))
    if fact == "resource":
        return _cmp(op or "gte", (facts.get("resources") or {}).get(clause.get("name")), clause.get("value"))
    if fact == "item":
        have = (facts.get("items") or {}).get(sim_state.fold(clause.get("name") or ""), 0)
        return _cmp(op or "gte", have, int(clause.get("qty") if clause.get("qty") is not None else 1))
    if fact == "location":
        loc = facts.get("location") or {}
        if clause.get("location_id") and loc.get("id") != clause["location_id"]:
            return False
        if clause.get("title") and sim_state.fold(loc.get("title") or "") != sim_state.fold(clause["title"]):
            return False
        if clause.get("place"):
            want = sim_state.fold(clause["place"])
            card_id = clause.get("card_id") or ""
            places = facts.get("places") or []
            if not any(
                sim_state.fold(place.get("place") or "") == want and (not card_id or place.get("card_id") == card_id)
                for place in places
            ):
                return False
        return bool(clause.get("location_id") or clause.get("title") or clause.get("place"))
    if fact == "relation":
        return _cmp(op or "gte", _relation_value(facts, clause), clause.get("value"))
    return False


def _when_ok(trigger: dict, facts: dict) -> bool:
    clauses = trigger.get("when")
    if not isinstance(clauses, list) or not clauses:
        return False
    return all(_clause_ok(clause, facts) for clause in clauses)


def _apply_effect(sim: dict, effect: dict) -> None:
    quest = quests.find_quest(sim, effect.get("quest_id") or "")
    quests.apply_update(
        quest,
        status=effect.get("status"),
        stage_id=effect.get("stage_id"),
        done=effect.get("done"),
        progress=effect.get("progress"),
        progress_delta=effect.get("progress_delta"),
    )


def evaluate(sim: dict, facts: dict) -> list[dict]:
    """Aplica gatilhos ainda não usados, em ordem de id. Segunda passagem não muda nada."""
    hits = []
    rows = [row for row in quests.triggers_of(sim) if isinstance(row, dict)]
    for trigger in sorted(rows, key=lambda row: row.get("id") or ""):
        if trigger.get("applied"):
            continue
        if not _when_ok(trigger, facts):
            continue
        saved = copy.deepcopy(sim.get("quests"))
        try:
            for effect in trigger.get("apply") or []:
                if isinstance(effect, dict):
                    _apply_effect(sim, effect)
        except quests.QuestError:
            sim["quests"] = saved
            continue
        trigger["applied"] = True
        hits.append({
            "id": trigger.get("id"),
            "apply": list(trigger.get("apply") or []),
        })
    return hits


def _location(conn, adventure_id: str):
    from app import world_map
    return world_map.current_location(conn, adventure_id)


def refresh(conn, adventure_id: str, sim: dict, *, user: str = "", cards: list | None = None) -> list[dict]:
    if cards is None and conn is not None:
        from app import db
        cards = db.list_cards(conn, adventure_id)
    location = _location(conn, adventure_id) if conn is not None else None
    facts = build_facts(sim, user=user, cards=cards or [], location=location)
    return evaluate(sim, facts)


def _refresh_saved(conn, adventure_id: str) -> list[dict]:
    from app import db
    adventure = db.get_adventure(conn, adventure_id)
    if not adventure:
        return []
    sim = sim_state.load(adventure)
    applied = refresh(conn, adventure_id, sim, user=(adventure.get("owner") or ""))
    if applied:
        db.set_sim_state(conn, adventure_id, sim)
    return applied


def install_hooks() -> None:
    """Viagem e mudança de vínculo passam pelo avaliador sem o chamador saber."""
    from app import npc_bonds, world_map

    if not getattr(world_map.travel, "_sim_quest_hook", False):
        original_travel = world_map.travel

        def travel(conn, adventure_id: str, to_card_id: str):
            result = original_travel(conn, adventure_id, to_card_id)
            if isinstance(result, dict) and result.get("travelled"):
                _refresh_saved(conn, adventure_id)
            return result

        travel._sim_quest_hook = True
        world_map.travel = travel

    if not getattr(npc_bonds.apply_adjudication, "_sim_quest_hook", False):
        original_apply = npc_bonds.apply_adjudication

        def apply_adjudication(session, adventure_id: str, adjudication: dict, seq: int,
                               player_text: str = "", actor_name: str = "") -> list[dict]:
            changed = original_apply(
                session, adventure_id, adjudication, seq,
                player_text=player_text, actor_name=actor_name,
            )
            if changed:
                _refresh_saved(session, adventure_id)
            return changed

        apply_adjudication._sim_quest_hook = True
        npc_bonds.apply_adjudication = apply_adjudication
