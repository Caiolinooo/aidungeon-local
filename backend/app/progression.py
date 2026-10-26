"""Progressão de personagens, skills e relações entre NPCs.

Níveis mudam possibilidades, não apenas números: desbloqueiam ações,
melhoram capacidades e alteram como os NPCs reagem. Relações entre NPCs
ficam em `sheet["relations"]` como {other_card_id: {trust, fear, affinity,
history: [...]}} e são atualizadas por eventos canônicos.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from app import db, npc_assets

log = logging.getLogger("aidungeon.progression")

XP_PER_LEVEL = 100
MAX_LEVEL = 50

SKILL_TREES = {
    "combat": {
        "name": "Combate",
        "levels": {
            1: "basic_attack",
            3: "power_strike",
            5: "counterattack",
            8: "area_attack",
            12: "legendary_blow",
        },
    },
    "social": {
        "name": "Influência",
        "levels": {
            1: "persuade",
            3: "intimidate",
            5: "charm_group",
            8: "mass_suggestion",
            12: "absolute_command",
        },
    },
    "magic": {
        "name": "Poder sobrenatural",
        "levels": {
            1: "minor_spell",
            3: "enhanced_spell",
            5: "reality_touch",
            8: "mass_alteration",
            12: "reality_dominion",
        },
    },
    "survival": {
        "name": "Sobrevivência",
        "levels": {
            1: "track",
            3: "hide",
            5: "resist_pain",
            8: "rapid_heal",
            12: "immortal_body",
        },
    },
}


def progression_of(sheet: dict | None) -> dict:
    sheet = sheet if isinstance(sheet, dict) else {}
    raw = sheet.get("progression") if isinstance(sheet.get("progression"), dict) else {}
    level = _int(raw.get("level"), 1, 1, MAX_LEVEL)
    xp = _int(raw.get("xp"), 0, 0, 999999)
    skills = raw.get("skills") if isinstance(raw.get("skills"), dict) else {}
    unlocked = raw.get("unlocked") if isinstance(raw.get("unlocked"), list) else []
    return {
        "level": level,
        "xp": xp,
        "skills": skills,
        "unlocked": unlocked,
    }


def _int(value: Any, default: int, minimum: int, maximum: int) -> int:
    try:
        return max(minimum, min(maximum, int(value)))
    except (TypeError, ValueError):
        return default


def level_for_xp(xp: int) -> int:
    return min(MAX_LEVEL, 1 + xp // XP_PER_LEVEL)


def add_xp(sheet: dict, amount: int) -> dict:
    """Aplica XP e recalcula nível e habilidades desbloqueadas."""
    current = progression_of(sheet)
    current["xp"] = max(0, current["xp"] + int(amount))
    current["level"] = level_for_xp(current["xp"])
    current["unlocked"] = unlocked_skills(current)
    updated = dict(sheet or {})
    updated["progression"] = current
    return updated


def unlocked_skills(progression: dict) -> list[str]:
    level = progression["level"]
    unlocked = []
    for tree, definition in SKILL_TREES.items():
        tree_level = _int(
            (progression.get("skills") or {}).get(tree),
            level, 1, MAX_LEVEL,
        )
        for required, skill in definition["levels"].items():
            if tree_level >= int(required):
                unlocked.append(skill)
    return sorted(set(unlocked))


def skill_level(sheet: dict, skill: str) -> int:
    current = progression_of(sheet)
    if skill in current["unlocked"]:
        return current["level"]
    return 0


def can_use(sheet: dict, skill: str) -> bool:
    return skill_level(sheet, skill) > 0


def progression_context(sheet: dict) -> str:
    current = progression_of(sheet)
    skills = ", ".join(current["unlocked"][-8:]) or "none"
    return (
        f"Level {current['level']} (XP {current['xp']}). "
        f"Unlocked abilities: {skills}. "
        "Higher levels enable stronger actions, but do not override established facts."
    )


def relations_of(sheet: dict) -> dict:
    raw = sheet.get("relations") if isinstance(sheet, dict) else {}
    return raw if isinstance(raw, dict) else {}


def relation_with(sheet: dict, other_card_id: str) -> dict:
    relations = relations_of(sheet)
    raw = relations.get(other_card_id) if isinstance(relations.get(other_card_id), dict) else {}
    return {
        "trust": _int(raw.get("trust"), 40, 0, 100),
        "fear": _int(raw.get("fear"), 10, 0, 100),
        "affinity": _int(raw.get("affinity"), 30, 0, 100),
        "history": list(raw.get("history") or [])[-8:],
    }


def update_relation(sheet: dict, other_card_id: str, *,
                    trust: int = 0, fear: int = 0, affinity: int = 0,
                    event: str = "") -> dict:
    if isinstance(sheet, str):
        sheet = npc_assets.parse_sheet(sheet)
    updated = dict(sheet or {})
    relations = dict(relations_of(updated))
    current = relation_with(updated, other_card_id)
    current["trust"] = _int(current["trust"] + trust, current["trust"], 0, 100)
    current["fear"] = _int(current["fear"] + fear, current["fear"], 0, 100)
    current["affinity"] = _int(current["affinity"] + affinity, current["affinity"], 0, 100)
    if event:
        current["history"].append(event[:240])
        current["history"] = current["history"][-8:]
    relations[other_card_id] = current
    updated["relations"] = relations
    return updated


def relationship_context(cards: list[dict]) -> str:
    """Relações entre NPCs para adjudicação e narrativa."""
    by_id = {card.get("id"): card for card in cards}
    lines = []
    for card in cards:
        title = card.get("title") or "NPC"
        for other_id, relation in relations_of(npc_assets.parse_sheet(card.get("sheet") or "")).items():
            other = by_id.get(other_id)
            if not other:
                continue
            details = relation_with(npc_assets.parse_sheet(card.get("sheet") or ""), other_id)
            history = details["history"][-1] if details["history"] else ""
            lines.append(
                f"{title} → {other.get('title')}: trust {details['trust']}, "
                f"fear {details['fear']}, affinity {details['affinity']}"
                + (f" ({history})" if history else "")
            )
    return "\n".join(lines)


def apply_turn_progression(conn, adventure_id: str, player_card_id: str | None,
                           xp_amount: int = 5) -> None:
    """XP por turno canônico; não muda regras, só possibilidades futuras."""
    if not player_card_id or xp_amount <= 0:
        return
    card = db.get_card(conn, adventure_id, player_card_id)
    if not card:
        return
    sheet = npc_assets.parse_sheet(card.get("sheet") or "")
    updated = add_xp(sheet, xp_amount)
    db.update_card(conn, adventure_id, player_card_id, {
        "sheet": json.dumps(updated, ensure_ascii=False),
    })
