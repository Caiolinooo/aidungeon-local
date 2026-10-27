"""Consequências persistentes de magia, percepção e ações sociais.

Um turno pode alterar a realidade física, a memória ou a percepção de uma
pessoa. Este módulo mantém esses efeitos em `adventure.ui_state` para que
narrativa, adjudicação, memória e imagens usem o mesmo estado.
"""

from __future__ import annotations

import json
import re
from typing import Any

from app import db

EFFECT_KINDS = (
    "reality", "perception", "memory", "emotion", "social", "physical",
)

_SYSTEM = """You evaluate lasting consequences of a magical or social story event.
Return STRICT JSON only:
{
  "effects": [
    {
      "kind": "reality|perception|memory|emotion|social|physical",
      "target": "world|player|exact NPC name|everyone_present",
      "summary": "what permanently changed, one concrete sentence",
      "visual_en": "short English phrase for images, or empty",
      "duration": "permanent|scene|temporary",
      "certainty": "established|attempted"
    }
  ],
  "public_facts": ["what witnesses now know or believe"],
  "private_facts": [{"npc": "exact name", "fact": "what only this person knows or believes"}]
}
Rules:
- Record only changes actually established by the canon passage, never a player wish.
- A spell that alters reality creates a reality effect; one that alters only what
  people perceive creates a perception effect. Mind control creates memory or
  perception effects, not a new physical fact.
- Include who witnessed the event and what they now believe.
- Do not invent consequences not present in the passage.
- If nothing lasting changed, return empty arrays."""


def _ui_state(adventure: dict) -> dict:
    raw = adventure.get("ui_state")
    if isinstance(raw, dict):
        return dict(raw)
    if isinstance(raw, str) and raw.strip():
        try:
            parsed = json.loads(raw)
            return dict(parsed) if isinstance(parsed, dict) else {}
        except ValueError:
            return {}
    return {}


def active_effects(adventure: dict, *, target: str = "") -> list[dict]:
    state = _ui_state(adventure)
    effects = state.get("world_effects")
    if not isinstance(effects, list):
        return []
    wanted = " ".join((target or "").split()).casefold()
    found = []
    for item in effects:
        if not isinstance(item, dict) or not item.get("summary"):
            continue
        if wanted:
            candidate = " ".join(str(item.get("target") or "").split()).casefold()
            if candidate not in (wanted, "world", "everyone_present"):
                continue
        found.append(item)
    return found


def effect_context(adventure: dict, *, target: str = "") -> str:
    """Texto curto para narrativa/adjudicação/imagens."""
    effects = active_effects(adventure, target=target)
    if not effects:
        return ""
    lines = ["[Persistent consequences]"]
    for item in effects[-8:]:
        kind = item.get("kind") or "effect"
        who = item.get("target") or "world"
        lines.append(f"- {kind} / {who}: {item['summary']}")
    public = _ui_state(adventure).get("public_facts") or []
    for fact in public[-4:]:
        lines.append(f"- Witnesses know: {fact}")
    return "\n".join(lines)


def visual_context(adventure: dict, *, target: str = "") -> str:
    """Somente efeitos com representação visual explícita."""
    bits = []
    for item in active_effects(adventure, target=target):
        visual = " ".join(str(item.get("visual_en") or "").split())
        if visual:
            bits.append(visual)
    return ", ".join(dict.fromkeys(bits))


def _extract_json(text: str) -> dict:
    cleaned = re.sub(r"```(?:json)?", "", text or "")
    start, end = cleaned.find("{"), cleaned.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("resposta sem JSON")
    data = json.loads(cleaned[start:end + 1])
    return data if isinstance(data, dict) else {}


def evaluate_passage(llm_cfg: dict, passage: str, cards: list[dict],
                     adventure: dict, transport: Any = None) -> dict:
    """Avalia efeitos duradouros do trecho canônico; não altera o banco."""
    from app import llm_client
    roster = [card.get("title") for card in cards if card.get("type") == "character"]
    payload = {
        "passage": passage[-3500:],
        "people": roster,
        "existing_effects": active_effects(adventure)[-6:],
        "plot": adventure.get("plot_essentials") or "",
    }
    raw = llm_client.chat_completion(
        llm_cfg,
        [
            {"role": "system", "content": _SYSTEM},
            {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
        ],
        role="chronicle", temperature=0.1, max_tokens=500, transport=transport,
    )
    return _extract_json(raw)


def apply_evaluation(conn, adventure_id: str, evaluation: dict) -> dict:
    """Persiste efeitos e fatos sociais sem duplicar os já registrados."""
    adventure = db.get_adventure(conn, adventure_id)
    if not adventure:
        return {"effects": 0, "public_facts": 0}
    state = _ui_state(adventure)
    effects = list(state.get("world_effects") or [])
    added = 0
    for item in evaluation.get("effects") or []:
        if not isinstance(item, dict):
            continue
        kind = str(item.get("kind") or "")
        summary = " ".join(str(item.get("summary") or "").split())
        if kind not in EFFECT_KINDS or not summary:
            continue
        target = " ".join(str(item.get("target") or "world").split())
        if any(
            str(old.get("kind")) == kind
            and str(old.get("target") or "").casefold() == target.casefold()
            and str(old.get("summary") or "").casefold() == summary.casefold()
            for old in effects
        ):
            continue
        effects.append({
            "kind": kind,
            "target": target,
            "summary": summary[:500],
            "visual_en": " ".join(str(item.get("visual_en") or "").split())[:220],
            "duration": str(item.get("duration") or "scene"),
            "certainty": str(item.get("certainty") or "established"),
        })
        added += 1
    public = list(state.get("public_facts") or [])
    public_added = 0
    for fact in evaluation.get("public_facts") or []:
        text = " ".join(str(fact).split())
        if text and text.casefold() not in [old.casefold() for old in public]:
            public.append(text[:400])
            public_added += 1
    private = list(state.get("private_facts") or [])
    for item in evaluation.get("private_facts") or []:
        if not isinstance(item, dict):
            continue
        npc = " ".join(str(item.get("npc") or "").split())
        fact = " ".join(str(item.get("fact") or "").split())
        if not npc or not fact:
            continue
        if not any(
            str(old.get("npc") or "").casefold() == npc.casefold()
            and str(old.get("fact") or "").casefold() == fact.casefold()
            for old in private
        ):
            private.append({"npc": npc, "fact": fact[:400]})
            card = next(
                (c for c in db.list_cards(conn, adventure_id)
                 if str(c.get("title") or "").casefold() == npc.casefold()),
                None,
            )
            if card:
                db.insert_npc_memory(
                    conn, adventure_id=adventure_id, card_id=card["id"],
                    kind="event", text=fact[:400], mood="",
                )
    state["world_effects"] = effects[-40:]
    state["public_facts"] = public[-30:]
    state["private_facts"] = private[-40:]
    db.update_adventure_ui_state(conn, adventure_id, state)
    return {"effects": added, "public_facts": public_added}


def without_private_facts(value: Any) -> Any:
    """Cópia da resposta sem `private_facts`. O objeto original e o banco ficam."""
    if isinstance(value, dict):
        return {
            key: without_private_facts(item)
            for key, item in value.items()
            if key != "private_facts"
        }
    if isinstance(value, list):
        return [without_private_facts(item) for item in value]
    return value


def private_facts_for(adventure: dict, npc_name: str) -> list[str]:
    wanted = " ".join((npc_name or "").split()).casefold()
    return [
        str(item.get("fact") or "")
        for item in (_ui_state(adventure).get("private_facts") or [])
        if str(item.get("npc") or "").casefold() == wanted and item.get("fact")
    ]
