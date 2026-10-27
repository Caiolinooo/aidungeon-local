"""Missões locais em `sim_state`. Sem LLM. Transições explícitas e idempotentes."""

from __future__ import annotations

import uuid

from app.sim import state as sim_state

STATUSES = ("hidden", "planned", "available", "accepted", "completed", "failed")
_PLAYER_STATUSES = frozenset({"available", "accepted", "completed", "failed"})
TRANSITIONS = {
    "hidden": {"planned", "available"},
    "planned": {"hidden", "available"},
    "available": {"hidden", "accepted", "failed"},
    "accepted": {"completed", "failed"},
    "completed": set(),
    "failed": {"planned"},
}
FACTS = ("hp", "gold", "san", "resource", "item", "location", "relation")
OPS = ("eq", "ne", "lt", "lte", "gt", "gte")
_RELATION_STATS = (
    "trust", "fear", "affinity", "relationship", "distrust", "desire", "boldness", "value",
)


class QuestError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def _new_id(prefix: str, taken: set[str]) -> str:
    for _ in range(6):
        candidate = prefix + uuid.uuid4().hex[:10]
        if candidate not in taken:
            return candidate
    return prefix + uuid.uuid4().hex


def _text(value, limit: int, field: str) -> str:
    if not isinstance(value, str):
        raise QuestError("invalid_quest", f"{field} inválido")
    text = " ".join(value.split()).strip()
    if len(text) > limit:
        raise QuestError("invalid_quest", f"{field} longo demais")
    return text


def _optional_int(value) -> int | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise QuestError("invalid_trigger", "número inválido")
    return value


def quests_of(sim: dict) -> list:
    rows = sim.get("quests")
    if not isinstance(rows, list):
        rows = []
        sim["quests"] = rows
    return rows


def triggers_of(sim: dict) -> list:
    rows = sim.get("triggers")
    if not isinstance(rows, list):
        rows = []
        sim["triggers"] = rows
    return rows


def _ids(rows: list) -> set[str]:
    return {row.get("id") for row in rows if isinstance(row, dict) and row.get("id")}


def find_quest(sim: dict, quest_id: str) -> dict:
    for quest in quests_of(sim):
        if isinstance(quest, dict) and quest.get("id") == quest_id:
            return quest
    raise QuestError("quest_not_found", "quest not found")


def _progress(quest: dict) -> dict:
    raw = quest.get("progress") if isinstance(quest.get("progress"), dict) else {}
    target = raw.get("target")
    value = raw.get("value")
    target = int(target) if isinstance(target, int) and target >= 1 else 1
    value = int(value) if isinstance(value, int) and value >= 0 else 0
    progress = {"value": min(value, target), "target": target}
    quest["progress"] = progress
    return progress


def _sync_progress_from_stages(quest: dict) -> None:
    stages = quest.get("stages") if isinstance(quest.get("stages"), list) else []
    if not stages:
        return
    progress = _progress(quest)
    progress["target"] = len(stages)
    progress["value"] = min(sum(1 for stage in stages if isinstance(stage, dict) and stage.get("done")), len(stages))


def transition_status(quest: dict, status: str) -> None:
    if status not in STATUSES:
        raise QuestError("invalid_transition", "invalid quest status")
    current = quest.get("status")
    if current == status:
        return
    allowed = TRANSITIONS.get(current, set())
    if status not in allowed:
        raise QuestError("invalid_transition", f"invalid quest transition: {current} -> {status}")
    quest["status"] = status
    if status == "completed":
        for stage in quest.get("stages") or []:
            if isinstance(stage, dict):
                stage["done"] = True
        progress = _progress(quest)
        progress["value"] = progress["target"]


def mark_stage(quest: dict, stage_id: str, done: bool) -> None:
    for stage in quest.get("stages") or []:
        if isinstance(stage, dict) and stage.get("id") == stage_id:
            stage["done"] = bool(done)
            _sync_progress_from_stages(quest)
            return
    raise QuestError("invalid_quest", "stage not found")


def set_progress(quest: dict, value: int) -> None:
    progress = _progress(quest)
    progress["value"] = max(0, min(progress["target"], int(value)))


def add_progress(quest: dict, delta: int) -> None:
    progress = _progress(quest)
    set_progress(quest, progress["value"] + int(delta))


def apply_update(quest: dict, *, status: str | None = None, stage_id: str | None = None,
                 done: bool | None = None, progress: int | None = None,
                 progress_delta: int | None = None) -> None:
    if status:
        transition_status(quest, status)
    if stage_id:
        mark_stage(quest, stage_id, True if done is None else bool(done))
    if progress is not None:
        set_progress(quest, progress)
    if progress_delta is not None:
        add_progress(quest, progress_delta)


def _normalize_stage(raw: dict, taken: set[str]) -> dict:
    if not isinstance(raw, dict):
        raise QuestError("invalid_quest", "stage inválido")
    title = _text(raw.get("title") or "", 120, "stage")
    if not title:
        raise QuestError("invalid_quest", "stage sem título")
    description = _text(raw.get("description") or "", 500, "stage")
    stage_id = raw.get("id") if isinstance(raw.get("id"), str) else ""
    stage_id = stage_id.strip()
    if not stage_id or stage_id in taken:
        stage_id = _new_id("s", taken)
    taken.add(stage_id)
    return {
        "id": stage_id,
        "title": title,
        "description": description,
        "done": bool(raw.get("done")),
    }


def _normalize_clause(raw: dict) -> dict:
    if not isinstance(raw, dict):
        raise QuestError("invalid_trigger", "cláusula inválida")
    fact = raw.get("fact")
    if fact not in FACTS:
        raise QuestError("invalid_trigger", "fato de gatilho inválido")
    op = raw.get("op") or None
    if op is not None and op not in OPS:
        raise QuestError("invalid_trigger", "operador de gatilho inválido")
    clause: dict = {"fact": fact}
    if op:
        clause["op"] = op
    value = _optional_int(raw.get("value"))
    if fact in ("hp", "gold", "san", "resource", "relation"):
        if value is None:
            raise QuestError("invalid_trigger", "gatilho sem valor")
        clause["value"] = value
    elif value is not None:
        clause["value"] = value
    if fact == "resource":
        name = _text(raw.get("name") or "", 40, "resource")
        if not name:
            raise QuestError("invalid_trigger", "recurso sem nome")
        clause["name"] = name
    if fact == "item":
        name = _text(raw.get("name") or "", 80, "item")
        if not name:
            raise QuestError("invalid_trigger", "item sem nome")
        clause["name"] = name
        qty = _optional_int(raw.get("qty"))
        clause["qty"] = 1 if qty is None else max(0, min(999, qty))
        clause.setdefault("op", "gte")
    if fact == "location":
        location_id = raw.get("location_id") or raw.get("id") or ""
        title = raw.get("title") or ""
        place = raw.get("place") or ""
        card_id = raw.get("card_id") or ""
        if isinstance(location_id, str) and location_id.strip():
            clause["location_id"] = location_id.strip()[:80]
        if isinstance(title, str) and title.strip():
            clause["title"] = _text(title, 120, "local")
        if isinstance(place, str) and place.strip():
            clause["place"] = _text(place, 80, "lugar")
        if isinstance(card_id, str) and card_id.strip():
            clause["card_id"] = card_id.strip()[:80]
        if not any(clause.get(key) for key in ("location_id", "title", "place")):
            raise QuestError("invalid_trigger", "local sem referência")
    if fact == "relation":
        for key, limit in (("a", 80), ("b", 80), ("card_id", 80), ("other_id", 80)):
            raw_value = raw.get(key) or ""
            if isinstance(raw_value, str) and raw_value.strip():
                clause[key] = _text(raw_value, limit, key)
        stat = raw.get("stat") or ""
        if isinstance(stat, str) and stat.strip():
            stat = stat.strip()
            if stat not in _RELATION_STATS:
                raise QuestError("invalid_trigger", "stat de relação inválido")
            clause["stat"] = stat
        life = clause.get("a") and clause.get("b")
        card = clause.get("card_id")
        if not life and not card:
            raise QuestError("invalid_trigger", "relação sem alvo")
        clause.setdefault("op", "gte")
        if not life:
            clause.setdefault("stat", "trust")
    return clause


def _normalize_effect(raw: dict, quest_id: str) -> dict:
    if not isinstance(raw, dict):
        raise QuestError("invalid_trigger", "efeito inválido")
    effect: dict = {}
    target = raw.get("quest_id") if isinstance(raw.get("quest_id"), str) else ""
    effect["quest_id"] = target.strip() or quest_id
    if not effect["quest_id"]:
        raise QuestError("invalid_trigger", "efeito sem missão")
    status = raw.get("status") or None
    if status is not None:
        if status not in STATUSES:
            raise QuestError("invalid_trigger", "status de efeito inválido")
        effect["status"] = status
    stage_id = raw.get("stage_id") if isinstance(raw.get("stage_id"), str) else ""
    if stage_id.strip():
        effect["stage_id"] = stage_id.strip()[:80]
        if raw.get("done") is not None:
            effect["done"] = bool(raw.get("done"))
    progress = _optional_int(raw.get("progress"))
    if progress is not None:
        effect["progress"] = max(0, min(999, progress))
    delta = _optional_int(raw.get("progress_delta"))
    if delta is not None:
        effect["progress_delta"] = max(-999, min(999, delta))
    if not any(key in effect for key in ("status", "stage_id", "progress", "progress_delta")):
        raise QuestError("invalid_trigger", "efeito vazio")
    return effect


def normalize_trigger(raw: dict, quest_id: str, taken: set[str]) -> dict:
    if not isinstance(raw, dict):
        raise QuestError("invalid_trigger", "gatilho inválido")
    when_raw = raw.get("when")
    apply_raw = raw.get("apply")
    if not isinstance(when_raw, list) or not when_raw or len(when_raw) > 12:
        raise QuestError("invalid_trigger", "gatilho sem condições")
    if not isinstance(apply_raw, list) or not apply_raw or len(apply_raw) > 8:
        raise QuestError("invalid_trigger", "gatilho sem efeitos")
    trigger_id = raw.get("id") if isinstance(raw.get("id"), str) else ""
    trigger_id = trigger_id.strip()
    if not trigger_id or trigger_id in taken:
        trigger_id = _new_id("t", taken)
    taken.add(trigger_id)
    return {
        "id": trigger_id,
        "when": [_normalize_clause(clause) for clause in when_raw],
        "apply": [_normalize_effect(effect, quest_id) for effect in apply_raw],
        "applied": bool(raw.get("applied")),
    }


def _blank_progress(stages: list[dict], target: int | None, status: str) -> dict:
    if stages:
        progress_target = len(stages)
        value = sum(1 for stage in stages if stage.get("done"))
    else:
        progress_target = target or 1
        value = 0
    if status == "completed":
        value = progress_target
        for stage in stages:
            stage["done"] = True
    return {"value": min(value, progress_target), "target": progress_target}


def create(sim: dict, payload: dict) -> dict:
    title = _text((payload or {}).get("title") or "", 120, "título")
    if not title:
        raise QuestError("invalid_quest", "título obrigatório")
    description = _text((payload or {}).get("description") or "", 2000, "descrição")
    status = (payload or {}).get("status") or "planned"
    if status not in STATUSES:
        raise QuestError("invalid_quest", "status inicial inválido")
    stage_rows = (payload or {}).get("stages") or []
    if not isinstance(stage_rows, list) or len(stage_rows) > 20:
        raise QuestError("invalid_quest", "estágios inválidos")
    taken_stages: set[str] = set()
    stages = [_normalize_stage(row, taken_stages) for row in stage_rows]
    target = _optional_int((payload or {}).get("progress_target"))
    if target is not None and not 1 <= target <= 999:
        raise QuestError("invalid_quest", "progresso alvo inválido")
    quest_rows = quests_of(sim)
    quest_id = _new_id("q", _ids(quest_rows))
    quest = {
        "id": quest_id,
        "title": title,
        "description": description,
        "status": status,
        "stages": stages,
        "progress": _blank_progress(stages, target, status),
    }
    trigger_rows = (payload or {}).get("triggers") or []
    normalized_triggers: list[dict] = []
    if trigger_rows:
        if not isinstance(trigger_rows, list) or len(trigger_rows) > 12:
            raise QuestError("invalid_trigger", "gatilhos inválidos")
        taken = _ids(triggers_of(sim))
        for row in trigger_rows:
            normalized_triggers.append(normalize_trigger(row, quest_id, taken))
    quest_rows.append(quest)
    if normalized_triggers:
        triggers_of(sim).extend(normalized_triggers)
    return quest


def update(sim: dict, quest_id: str, payload: dict) -> dict:
    quest = find_quest(sim, quest_id)
    if "title" in payload and payload.get("title") is not None:
        title = _text(payload.get("title") or "", 120, "título")
        if not title:
            raise QuestError("invalid_quest", "título obrigatório")
        quest["title"] = title
    if "description" in payload and payload.get("description") is not None:
        quest["description"] = _text(payload.get("description") or "", 2000, "descrição")
    if payload.get("stages") is not None:
        stage_rows = payload.get("stages")
        if not isinstance(stage_rows, list) or len(stage_rows) > 20:
            raise QuestError("invalid_quest", "estágios inválidos")
        taken: set[str] = set()
        quest["stages"] = [_normalize_stage(row, taken) for row in stage_rows]
        _sync_progress_from_stages(quest)
        if not quest["stages"]:
            progress = _progress(quest)
            progress["target"] = max(1, progress["target"])
            progress["value"] = min(progress["value"], progress["target"])
    if payload.get("progress_target") is not None and not quest.get("stages"):
        target = _optional_int(payload.get("progress_target"))
        if target is None or not 1 <= target <= 999:
            raise QuestError("invalid_quest", "progresso alvo inválido")
        progress = _progress(quest)
        progress["target"] = target
        progress["value"] = min(progress["value"], target)
    if payload.get("status"):
        transition_status(quest, payload["status"])
    return quest


def transition(sim: dict, quest_id: str, payload: dict) -> dict:
    quest = find_quest(sim, quest_id)
    status = payload.get("status") or None
    stage_id = payload.get("stage_id") or None
    if isinstance(stage_id, str):
        stage_id = stage_id.strip() or None
    progress = payload.get("progress")
    if status is None and stage_id is None and progress is None:
        raise QuestError("invalid_quest", "transição vazia")
    apply_update(
        quest,
        status=status,
        stage_id=stage_id,
        done=payload.get("done"),
        progress=progress if progress is not None else None,
    )
    return quest


def delete(sim: dict, quest_id: str) -> None:
    rows = quests_of(sim)
    kept = [quest for quest in rows if not (isinstance(quest, dict) and quest.get("id") == quest_id)]
    if len(kept) == len(rows):
        raise QuestError("quest_not_found", "quest not found")
    sim["quests"] = kept
    kept_triggers = []
    for trigger in triggers_of(sim):
        if not isinstance(trigger, dict):
            continue
        effects = [
            effect for effect in (trigger.get("apply") or [])
            if not (isinstance(effect, dict) and effect.get("quest_id") == quest_id)
        ]
        if len(effects) != len(trigger.get("apply") or []):
            if not effects:
                continue
            trigger = dict(trigger)
            trigger["apply"] = effects
        kept_triggers.append(trigger)
    sim["triggers"] = kept_triggers


def add_trigger(sim: dict, payload: dict) -> dict:
    quest_id = payload.get("quest_id") if isinstance(payload.get("quest_id"), str) else ""
    effects = payload.get("apply") if isinstance(payload.get("apply"), list) else []
    if not quest_id:
        for effect in effects:
            if isinstance(effect, dict) and effect.get("quest_id"):
                quest_id = effect["quest_id"]
                break
    if quest_id:
        find_quest(sim, quest_id)
    stored = triggers_of(sim)
    trigger = normalize_trigger(payload, quest_id, _ids(stored))
    for effect in trigger["apply"]:
        find_quest(sim, effect["quest_id"])
    stored.append(trigger)
    return trigger


def delete_trigger(sim: dict, trigger_id: str) -> None:
    rows = triggers_of(sim)
    kept = [row for row in rows if not (isinstance(row, dict) and row.get("id") == trigger_id)]
    if len(kept) == len(rows):
        raise QuestError("trigger_not_found", "trigger not found")
    sim["triggers"] = kept


def public_quest(quest: dict) -> dict:
    progress = _progress(quest)
    stages = []
    for stage in quest.get("stages") or []:
        if not isinstance(stage, dict):
            continue
        stages.append({
            "id": stage.get("id") or "",
            "title": stage.get("title") or "",
            "description": stage.get("description") or "",
            "done": bool(stage.get("done")),
        })
    return {
        "id": quest.get("id") or "",
        "title": quest.get("title") or "",
        "description": quest.get("description") or "",
        "status": quest.get("status") if quest.get("status") in STATUSES else "planned",
        "stages": stages,
        "progress": {"value": progress["value"], "target": progress["target"]},
    }


def public_quests(sim: dict, *, owner: bool = False) -> list[dict]:
    visible = []
    for quest in quests_of(sim):
        if not isinstance(quest, dict) or not quest.get("id"):
            continue
        status = quest.get("status") if quest.get("status") in STATUSES else "planned"
        if not owner and status not in _PLAYER_STATUSES:
            continue
        visible.append(public_quest(quest))
    return visible


def public_trigger(trigger: dict) -> dict:
    return {
        "id": trigger.get("id") or "",
        "when": list(trigger.get("when") or []),
        "apply": list(trigger.get("apply") or []),
        "applied": bool(trigger.get("applied")),
    }


def public_triggers(sim: dict, *, owner: bool = False) -> list[dict]:
    if not owner:
        return []
    return [
        public_trigger(trigger)
        for trigger in triggers_of(sim)
        if isinstance(trigger, dict) and trigger.get("id")
    ]


def public_sim(sim: dict) -> dict:
    """Estado de jogo para quem não é dono: sem RNG e sem missão ou gatilho oculto."""
    loaded = sim_state.load({"sim_state": sim if isinstance(sim, dict) else {}})
    loaded.pop("rng", None)
    loaded["quests"] = public_quests(loaded, owner=False)
    loaded["triggers"] = public_triggers(loaded, owner=False)
    return loaded


def folded_items(actor: dict | None) -> dict[str, int]:
    totals: dict[str, int] = {}
    inventory = (actor or {}).get("inventory") if isinstance((actor or {}).get("inventory"), list) else []
    for row in inventory:
        if not isinstance(row, dict) or not row.get("name"):
            continue
        key = sim_state.fold(str(row.get("name")))
        try:
            qty = int(row.get("qty") or 0)
        except (TypeError, ValueError):
            qty = 0
        totals[key] = totals.get(key, 0) + max(0, qty)
    return totals
