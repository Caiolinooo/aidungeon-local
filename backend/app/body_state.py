"""Estado corporal canônico da ficha (ferimentos, dor e visibilidade).

O LLM decide o que aconteceu; este módulo só normaliza, aplica deltas e
descreve o que cada superfície visual deve mostrar. Não há decay nem cura
automática: uma condição só sai quando o chronicle manda curar/remover.

`portrait_visible` é julgado pela IA a partir do enquadramento do retrato
(busto/upper body) e da roupa. `scene_visible` vale para o fundo/See. Um corte
no rosto pode aparecer no retrato; uma ferida sob a armadura, não.
"""

from __future__ import annotations

import copy
import json
import re
import uuid
from typing import Any

VERSION = 1
MAX_CONDITIONS = 24
CONDITION_KINDS = (
    "wound", "cut", "bruise", "burn", "fracture", "scar", "amputation",
    "tattoo", "mark", "illness", "other",
)
VISIBILITIES = ("portrait", "scene", "hidden")


def _text(value: Any, limit: int = 300) -> str:
    return " ".join(str(value or "").split())[:limit]


def _bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        folded = value.strip().casefold()
        if folded in ("1", "true", "yes", "sim", "on"):
            return True
        if folded in ("0", "false", "no", "não", "nao", "off"):
            return False
    return default


def _int(value: Any, default: int, low: int, high: int) -> int:
    if isinstance(value, bool) or value is None:
        return default
    try:
        return max(low, min(high, int(float(value))))
    except (TypeError, ValueError):
        return default


def _new_id() -> str:
    return "c_" + uuid.uuid4().hex[:12]


def _kind(value: Any) -> str:
    kind = _text(value, 32).casefold().replace(" ", "_")
    return kind if kind in CONDITION_KINDS else "other"


def _visibility(raw: dict, active: bool) -> str:
    value = _text(raw.get("visibility"), 16).casefold()
    if value in VISIBILITIES:
        return value
    return "scene" if active else "hidden"


def normalize_condition(raw: Any, *, source_action_id: int | None = None,
                       source_seq: int | None = None) -> dict | None:
    if not isinstance(raw, dict):
        return None
    active = _bool(raw.get("active"), True)
    visibility = _visibility(raw, active)
    portrait_visible = _bool(
        raw.get("portrait_visible"),
        visibility == "portrait",
    ) and active and visibility != "hidden"
    scene_visible = _bool(
        raw.get("scene_visible"),
        visibility in ("portrait", "scene"),
    ) and active and visibility != "hidden"
    condition = {
        "id": _text(raw.get("id"), 48) or _new_id(),
        "kind": _kind(raw.get("kind")),
        "location": _text(raw.get("location"), 80),
        "severity": _int(raw.get("severity"), 3, 1, 10),
        "description": _text(raw.get("description"), 300),
        "visual_en": _text(raw.get("visual_en"), 240),
        "permanent": _bool(raw.get("permanent"), False),
        "active": active,
        "visibility": visibility,
        "portrait_visible": portrait_visible,
        "scene_visible": scene_visible,
        "source_action_id": source_action_id if source_action_id is not None
        else raw.get("source_action_id"),
        "source_seq": source_seq if source_seq is not None else raw.get("source_seq"),
        "created_seq": raw.get("created_seq"),
        "healed_seq": raw.get("healed_seq"),
    }
    return condition


def normalize_body_state(raw: Any) -> dict:
    raw = raw if isinstance(raw, dict) else {}
    conditions = []
    for item in raw.get("conditions") or []:
        condition = normalize_condition(item)
        if condition is not None:
            conditions.append(condition)
    conditions = _trim(conditions)
    return {
        "v": VERSION,
        "pain": _int(raw.get("pain"), 0, 0, 10),
        "conditions": conditions,
    }


def body_state_of(sheet: dict | None) -> dict:
    sheet = sheet if isinstance(sheet, dict) else {}
    return normalize_body_state(sheet.get("body_state"))


def _trim(conditions: list[dict]) -> list[dict]:
    if len(conditions) <= MAX_CONDITIONS:
        return conditions
    ordered = sorted(
        conditions,
        key=lambda item: (
            0 if item.get("active") else 1,
            -int(item.get("severity") or 0),
            -int(item.get("created_seq") or 0),
        ),
    )
    return ordered[:MAX_CONDITIONS]


def _find(conditions: list[dict], delta: dict) -> dict | None:
    cid = _text(delta.get("id"), 48)
    if cid:
        for condition in conditions:
            if condition.get("id") == cid:
                return condition
    location = _text(delta.get("location"), 80).casefold()
    kind = _kind(delta.get("kind"))
    for condition in reversed(conditions):
        if location and condition.get("location", "").casefold() == location \
                and condition.get("kind") == kind:
            return condition
    description = _text(delta.get("description"), 300).casefold()
    if description:
        for condition in reversed(conditions):
            text = (condition.get("description") or "").casefold()
            if text and (description in text or text in description):
                return condition
    return None


def apply_body(state: Any, body: Any, *, source_action_id: int | None = None,
               source_seq: int | None = None) -> dict:
    """Aplica um delta de corpo e devolve o estado novo (sem mutar o original)."""
    current = normalize_body_state(state)
    body = body if isinstance(body, dict) else {}
    if body.get("pain") is not None:
        current["pain"] = _int(body.get("pain"), current["pain"], 0, 10)
    raw_conditions = body.get("conditions")
    if not isinstance(raw_conditions, list):
        raw_conditions = [body["condition"]] if isinstance(body.get("condition"), dict) else []
    conditions = [dict(item) for item in current["conditions"]]
    for raw in raw_conditions[:16]:
        if not isinstance(raw, dict):
            continue
        op = _text(raw.get("op"), 12).casefold() or "add"
        found = _find(conditions, raw)
        if op in ("heal", "remove", "cure"):
            if found is not None:
                found["active"] = False
                found["portrait_visible"] = False
                found["scene_visible"] = False
                found["healed_seq"] = source_seq
            continue
        if op == "update" and found is not None:
            merged = {**found, **raw}
            merged["id"] = found["id"]
            updated = normalize_condition(
                merged, source_action_id=source_action_id, source_seq=source_seq,
            )
            if updated is not None:
                conditions[conditions.index(found)] = updated
            continue
        if op not in ("add", "update"):
            continue
        if found is not None:
            merged = {**found, **raw}
            merged["id"] = found["id"]
            merged["active"] = True
            updated = normalize_condition(
                merged, source_action_id=source_action_id, source_seq=source_seq,
            )
            if updated is not None:
                conditions[conditions.index(found)] = updated
            continue
        created = normalize_condition(
            {**raw, "active": True, "created_seq": source_seq},
            source_action_id=source_action_id, source_seq=source_seq,
        )
        if created is not None:
            conditions.append(created)
    current["conditions"] = _trim(conditions)
    current["v"] = VERSION
    return current


def apply_to_sheet(sheet: dict | None, body: Any, *, source_action_id: int | None = None,
                   source_seq: int | None = None) -> dict:
    """Copia a ficha, aplica corpo e expressão do mesmo delta."""
    updated = dict(sheet or {})
    body = body if isinstance(body, dict) else {}
    updated["body_state"] = apply_body(
        updated.get("body_state"), body,
        source_action_id=source_action_id, source_seq=source_seq,
    )
    expression = _text(body.get("expression"), 80)
    if expression:
        updated["expression"] = expression
    outfit = _text(body.get("current_outfit") or body.get("outfit"), 240)
    if outfit:
        updated["current_outfit"] = outfit
    return updated


def active_conditions(state: Any, *, portrait: bool = False) -> list[dict]:
    conditions = normalize_body_state(state)["conditions"]
    if portrait:
        return [c for c in conditions if c.get("active") and c.get("portrait_visible")]
    return [c for c in conditions if c.get("active") and c.get("scene_visible")]


def visual_prompt(state: Any, *, portrait: bool = False) -> str:
    bits: list[str] = []
    for condition in active_conditions(state, portrait=portrait):
        visual = condition.get("visual_en") or condition.get("description") or ""
        visual = _text(visual, 180)
        if visual:
            bits.append(visual)
    return ", ".join(dict.fromkeys(bits))


def condition_context(state: Any, *, limit: int = 8) -> str:
    state = normalize_body_state(state)
    lines: list[str] = []
    for condition in state["conditions"]:
        if not condition.get("active"):
            continue
        bits = [
            condition.get("kind") or "condition",
            condition.get("location") or "",
            f"severity {condition.get('severity')}",
            condition.get("description") or "",
            "permanent" if condition.get("permanent") else "temporary",
            "portrait-visible" if condition.get("portrait_visible") else "not-portrait",
        ]
        line = ": ".join(
            part for part in (condition.get("id") or "", ", ".join(b for b in bits if b))
            if part
        )
        if line:
            lines.append(line)
        if len(lines) >= limit:
            break
    if state["pain"]:
        lines.append(f"pain {state['pain']}/10")
    return "\n".join(lines)


def snapshot(sheet: dict | None) -> dict:
    sheet = sheet if isinstance(sheet, dict) else {}
    return {
        "body_state": copy.deepcopy(body_state_of(sheet)),
        "expression": _text(sheet.get("expression"), 80),
        "portrait_image_id": sheet.get("portrait_image_id") or "",
        "portrait_anchor_id": sheet.get("portrait_anchor_id") or "",
        "portrait_state_key": sheet.get("portrait_state_key") or "",
        "portrait_job_id": sheet.get("portrait_job_id") or "",
        "portrait_prompt": sheet.get("portrait_prompt") or "",
        "current_outfit": sheet.get("current_outfit") or "",
    }


def restore_sheet(sheet: dict | None, snap: dict | None) -> dict:
    """Restaura os campos capturados por `snapshot`; preserva o resto da ficha."""
    updated = dict(sheet or {})
    snap = snap if isinstance(snap, dict) else {}
    if "body_state" in snap:
        updated["body_state"] = normalize_body_state(snap.get("body_state"))
    if "expression" in snap:
        updated["expression"] = _text(snap.get("expression"), 80)
    for key in ("portrait_state_key", "portrait_job_id", "portrait_prompt",
                "current_outfit"):
        if key in snap:
            updated[key] = snap.get(key) or ""
    return updated


def to_json(state: Any) -> str:
    return json.dumps(normalize_body_state(state), ensure_ascii=False)


def english_condition(raw: dict) -> str:
    """Frase curta em inglês para o retrato/cena (sem inventar if already EN)."""
    visual = _text(raw.get("visual_en"), 200)
    if visual:
        return visual
    kind = _kind(raw.get("kind"))
    location = _text(raw.get("location"), 80)
    severity = _int(raw.get("severity"), 3, 1, 10)
    size = "small" if severity <= 3 else "deep" if severity >= 7 else "visible"
    return " ".join(part for part in (size, kind.replace("_", " "), "on", location) if part)


def looks_like_condition(raw: Any) -> bool:
    return isinstance(raw, dict) and bool(re.search(
        r"(?i)wound|cut|bruise|burn|fracture|scar|injur|ferid|corte|queimad|fratur|"
        r"machuc|sangr|blood|pain|dor",
        " ".join(str(value) for value in raw.values() if isinstance(value, str)),
    ))
