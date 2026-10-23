"""Runtime do painel dinâmico.

O manifesto (adventures.ui_profile) diz quais widgets existem. O estado
(adventures.ui_state) guarda os valores. Este módulo aplica o delta que o
passe do chronicle devolve a cada turno e mantém o HUD/ações contextuais no
mesmo objeto, com chaves reservadas que nunca entram no contexto do narrador
como se fossem atributos do jogador.

Formato do delta (tudo opcional):

    {
      "hud": [{"label": "Local", "value": "Sala dos Girinos", "tone": "info"}],
      "actions": [{"label": "Perguntar sobre a Marca", "type": "say",
                   "text": "Pergunto sobre a Marca do Pântano."}],
      "stats": [{"widget": "stats", "name": "HP", "value": 12}],
      "inventory": [{"widget": "bag", "op": "add", "name": "Corda", "qty": 1}],
      "fields": [{"widget": "sheet", "name": "Objetivo", "value": "..."}]
    }

O código só valida, limita e aplica. Quem decide o que mudou é o LLM.
"""

from __future__ import annotations

import copy
import json
from typing import Any

HUD_KEY = "_hud"
ACTIONS_KEY = "_actions"
SEQ_KEY = "_seq"
RESERVED_KEYS = frozenset((HUD_KEY, ACTIONS_KEY, SEQ_KEY))

MAX_HUD = 6
MAX_ACTIONS = 6
MAX_LABEL = 40
MAX_VALUE = 120
MAX_ACTION_TEXT = 320
_TONES = ("neutral", "info", "good", "warn", "danger")
_ACTION_TYPES = ("do", "say", "story")


def _text(value: Any, limit: int) -> str:
    return " ".join(str(value or "").split())[:limit]


def _int(value: Any, default: int = 0) -> int:
    if isinstance(value, bool) or value is None:
        return default
    try:
        return int(round(float(value)))
    except (TypeError, ValueError):
        return default


def _tone(value: Any) -> str:
    tone = _text(value, 16).casefold()
    return tone if tone in _TONES else "neutral"


def normalize_hud(raw: Any) -> list[dict]:
    if not isinstance(raw, list):
        return []
    out: list[dict] = []
    seen: set[str] = set()
    for item in raw[:MAX_HUD * 2]:
        if not isinstance(item, dict):
            continue
        label = _text(item.get("label") or item.get("name"), MAX_LABEL)
        value = _text(item.get("value") or item.get("text"), MAX_VALUE)
        if not label or not value:
            continue
        key = label.casefold()
        if key in seen:
            continue
        seen.add(key)
        out.append({"label": label, "value": value, "tone": _tone(item.get("tone"))})
        if len(out) >= MAX_HUD:
            break
    return out


def normalize_actions(raw: Any) -> list[dict]:
    if not isinstance(raw, list):
        return []
    out: list[dict] = []
    seen: set[str] = set()
    for item in raw[:MAX_ACTIONS * 2]:
        if not isinstance(item, dict):
            continue
        text = _text(item.get("text") or item.get("action"), MAX_ACTION_TEXT)
        if not text:
            continue
        type_ = _text(item.get("type"), 12).casefold()
        if type_ not in _ACTION_TYPES:
            type_ = "do"
        label = _text(item.get("label"), MAX_LABEL) or text[:MAX_LABEL]
        key = text.casefold()
        if key in seen:
            continue
        seen.add(key)
        out.append({"label": label, "type": type_, "text": text})
        if len(out) >= MAX_ACTIONS:
            break
    return out


def seed(profile: dict | None) -> dict:
    """Estado inicial opcional a partir do manifesto (HUD/ações da abertura)."""
    profile = profile if isinstance(profile, dict) else {}
    out: dict[str, Any] = {}
    hud = normalize_hud(profile.get("hud"))
    actions = normalize_actions(profile.get("actions"))
    if hud:
        out[HUD_KEY] = hud
    if actions:
        out[ACTIONS_KEY] = actions
    return out


def _widgets(profile: dict | None) -> list[dict]:
    widgets = (profile or {}).get("widgets") if isinstance(profile, dict) else []
    return [widget for widget in widgets or [] if isinstance(widget, dict)]


def _find_widget(profile: dict | None, raw: dict, wanted: str) -> dict | None:
    widget_id = _text(raw.get("widget") or raw.get("widget_id"), 24)
    widgets = _widgets(profile)
    if widget_id:
        return next((w for w in widgets if w.get("id") == widget_id), None)
    matches = [w for w in widgets if w.get("type") == wanted]
    return matches[0] if len(matches) == 1 else None


def _stat_item(raw: Any) -> dict | None:
    if isinstance(raw, dict):
        return raw
    return None


def _iter_stats(update: dict) -> list[dict]:
    raw = update.get("stats")
    rows: list[dict] = []
    if isinstance(raw, dict):
        for name, value in raw.items():
            rows.append({"name": name, "value": value})
    elif isinstance(raw, list):
        rows = [item for item in raw if _stat_item(item) is not None]
    return rows


def _iter_fields(update: dict) -> list[dict]:
    raw = update.get("fields")
    rows: list[dict] = []
    if isinstance(raw, dict):
        for name, value in raw.items():
            rows.append({"name": name, "value": value})
    elif isinstance(raw, list):
        rows = [item for item in raw if isinstance(item, dict)]
    return rows


def _iter_inventory(update: dict) -> list[dict]:
    raw = update.get("inventory")
    rows: list[dict] = []
    if isinstance(raw, dict):
        for name, qty in raw.items():
            rows.append({"op": "add", "name": name, "qty": qty})
    elif isinstance(raw, list):
        rows = [item for item in raw if isinstance(item, dict)]
    return rows


def _apply_stats(out: dict, profile: dict | None, update: dict) -> bool:
    changed = False
    for raw in _iter_stats(update):
        name = _text(raw.get("name"), MAX_LABEL)
        widget = _find_widget(profile, raw, "stats")
        if not name or widget is None:
            continue
        definition = next(
            (stat for stat in widget.get("stats") or []
             if _text(stat.get("name"), MAX_LABEL).casefold() == name.casefold()),
            None,
        )
        if definition is None:
            continue
        value = _int(raw.get("value"), _int(raw.get("set"), 0))
        if definition.get("min") is not None:
            value = max(int(definition["min"]), value)
        if definition.get("max") is not None:
            value = min(int(definition["max"]), value)
        widget_id = str(widget.get("id") or "stats")
        current = out.get(widget_id)
        if not isinstance(current, dict):
            current = {str(s.get("name")): s.get("value", 0)
                       for s in widget.get("stats") or [] if s.get("name")}
        name = str(definition.get("name"))
        if current.get(name) == value:
            continue
        current[name] = value
        out[widget_id] = current
        changed = True
    return changed


def _apply_fields(out: dict, profile: dict | None, update: dict) -> bool:
    changed = False
    for raw in _iter_fields(update):
        name = _text(raw.get("name"), MAX_LABEL)
        widget = _find_widget(profile, raw, "character")
        if not name or widget is None:
            continue
        definition = next(
            (field for field in widget.get("fields") or []
             if _text(field.get("name"), MAX_LABEL).casefold() == name.casefold()),
            None,
        )
        if definition is None:
            continue
        value = _text(raw.get("value"), MAX_VALUE)
        widget_id = str(widget.get("id") or "character")
        current = out.get(widget_id)
        if not isinstance(current, dict):
            current = {str(f.get("name")): f.get("value", "")
                       for f in widget.get("fields") or [] if f.get("name")}
        name = str(definition.get("name"))
        if current.get(name) == value:
            continue
        current[name] = value
        out[widget_id] = current
        changed = True
    return changed


def _apply_inventory(out: dict, profile: dict | None, update: dict) -> bool:
    changed = False
    for raw in _iter_inventory(update):
        name = _text(raw.get("name"), MAX_LABEL)
        widget = _find_widget(profile, raw, "inventory")
        if not name or widget is None:
            continue
        op = _text(raw.get("op"), 12).casefold() or "add"
        qty = max(1, _int(raw.get("qty"), 1))
        widget_id = str(widget.get("id") or "inventory")
        current = out.get(widget_id)
        if not isinstance(current, list):
            current = [dict(item) for item in widget.get("items") or []
                       if isinstance(item, dict)]
        current = [dict(item) for item in current if isinstance(item, dict)]
        key = name.casefold()
        found = next((item for item in current
                      if _text(item.get("name"), MAX_LABEL).casefold() == key), None)
        touched = False
        if op in ("remove", "take", "lose"):
            if found is None:
                continue
            left = _int(found.get("qty"), 1) - qty
            if left > 0:
                found["qty"] = left
            else:
                current = [item for item in current if item is not found]
            touched = True
        elif op in ("add", "gain", "give", "set"):
            if found is not None:
                found["qty"] = qty if op == "set" else \
                    max(1, _int(found.get("qty"), 1) + qty)
            else:
                current.append({"name": name, "qty": qty})
            touched = True
        if touched:
            out[widget_id] = current
            changed = True
    return changed


def apply(profile: dict | None, state: dict | None, update: Any, *,
          seq: int | None = None) -> dict:
    """Aplica o delta do turno e devolve um estado novo (nunca muta o original)."""
    out = copy.deepcopy(state) if isinstance(state, dict) else {}
    if not isinstance(update, dict):
        return out
    if "hud" in update:
        out[HUD_KEY] = normalize_hud(update.get("hud"))
    if "actions" in update:
        out[ACTIONS_KEY] = normalize_actions(update.get("actions"))
    _apply_stats(out, profile, update)
    _apply_fields(out, profile, update)
    _apply_inventory(out, profile, update)
    if seq is not None:
        out[SEQ_KEY] = int(seq)
    return out


def _widget_context(profile: dict | None, state: dict) -> list[dict]:
    rows = []
    for widget in _widgets(profile):
        widget_id = str(widget.get("id") or widget.get("type") or "")
        row: dict[str, Any] = {
            "id": widget_id,
            "type": widget.get("type") or "",
            "label": widget.get("label") or "",
            "state": state.get(widget_id),
        }
        if widget.get("type") == "stats":
            row["stats"] = [
                {"name": stat.get("name"), "min": stat.get("min"), "max": stat.get("max")}
                for stat in widget.get("stats") or [] if isinstance(stat, dict)
            ]
        elif widget.get("type") == "character":
            row["fields"] = [
                {"name": field.get("name")}
                for field in widget.get("fields") or [] if isinstance(field, dict)
            ]
        rows.append(row)
    return rows


def prompt_context(profile: dict | None, state: dict | None, *,
                   limit: int = 2400) -> str:
    """Bloco compacto para o chronicle decidir HUD/ações e valores."""
    state = state if isinstance(state, dict) else {}
    payload = {
        "hud": state.get(HUD_KEY) or [],
        "actions": state.get(ACTIONS_KEY) or [],
        "widgets": _widget_context(profile, state),
    }
    return json.dumps(payload, ensure_ascii=False)[:limit]
