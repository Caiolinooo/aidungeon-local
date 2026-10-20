"""Mente do NPC: memória do turno, humor, rotina e o dia fora de cena.

`run_mind` roda depois do chronicle, num passe só. `tick_day` inventa o que
aconteceu fora da cena e, com memória bastante, uma reflexão. Os dois são
best-effort: LLM ou JSON ruim não derruba o turno.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from app import db, llm_client, npc_assets, npc_bonds, settings_store

log = logging.getLogger("aidungeon.npc_life")

MEMORY_KINDS = ("event", "reflection")

_MIND_SYSTEM = """You update the inner life of characters after one story passage.
Output STRICT JSON (no markdown) with exactly this shape:
{"characters": [
  {"name": "canonical name",
   "memory": "what happened to them, 1-2 sentences, from the passage",
   "mood": "a word or short phrase",
   "activity": "what they are doing now",
   "sentiments": "how they feel about who is in the scene, starting with the player, at most 240 characters",
   "bonds": {"relationship": 0-100, "trust": 0-100, "fear": 0-100, "desire": 0-100, "boldness": 0-100}}
]}
Use the canonical name. Bonds are absolute values, not deltas. Omit a bond key you are not changing.
Do not change who the person is. Output ONLY the JSON object."""

_TICK_SYSTEM = """You invent one off-screen day for one character in an interactive story.
Output STRICT JSON (no markdown):
{"event": "one off-screen daily event, 1-2 sentences",
 "mood": "a word or short phrase",
 "activity": "what they are doing at the end of the day",
 "reflection": "at most 400 characters, only when they have enough memories; otherwise an empty string"}
Invent only off-screen daily events. Never change who the character is, their status, their appearance or their relationships.
Output ONLY the JSON object."""


def life_state(sheet: dict | None) -> dict:
    raw = sheet.get("life") if isinstance(sheet, dict) else None
    raw = raw if isinstance(raw, dict) else {}
    try:
        day = int(raw.get("day") or 0)
    except (TypeError, ValueError):
        day = 0
    return {
        "mood": str(raw.get("mood") or ""),
        "activity": str(raw.get("activity") or ""),
        "day": day,
        "last_tick": str(raw.get("last_tick") or ""),
    }


def write_life(sheet: dict, state: dict) -> dict:
    updated = dict(sheet or {})
    current = life_state(updated)
    try:
        day = int(state.get("day", current["day"]))
    except (TypeError, ValueError):
        day = current["day"]
    updated["life"] = {
        "mood": str(state.get("mood", current["mood"]) or ""),
        "activity": str(state.get("activity", current["activity"]) or ""),
        "day": day,
        "last_tick": str(state.get("last_tick", current["last_tick"]) or ""),
    }
    return updated


def add_memory(conn, adventure_id: str, card_id: str, kind: str, text: str,
               mood: str = "") -> dict:
    if kind not in MEMORY_KINDS:
        kind = "event"
    return db.insert_npc_memory(
        conn, adventure_id=adventure_id, card_id=card_id, kind=kind,
        text=text, mood=mood,
    )


def list_memories(conn, adventure_id: str, card_id: str, limit: int = 20) -> list[dict]:
    return db.list_npc_memories(conn, adventure_id, card_id, limit=limit)


def _pick_recent(rows: list[dict], per_card: int) -> list[dict]:
    reflections = [row for row in rows if row.get("kind") == "reflection"]
    events = [row for row in rows if row.get("kind") != "reflection"]
    return (reflections + events)[:per_card]


def recent_for_cards(conn, adventure_id: str, card_ids: list[str],
                     per_card: int = 3) -> list[dict]:
    found: list[dict] = []
    for card_id in card_ids:
        rows = db.list_npc_memories(conn, adventure_id, card_id, limit=20)
        found.extend(_pick_recent(rows, per_card))
    return found


def _memory_count(conn, adventure_id: str, card_id: str) -> int:
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM npc_memories WHERE adventure_id=? AND card_id=?",
        (adventure_id, card_id),
    ).fetchone()
    return int(row["n"] if row else 0)


def _passage(conn, adventure_id: str) -> str:
    actions = db.list_actions(conn, adventure_id)
    text = "".join(action.get("text") or "" for action in actions if action.get("type") != "see")
    return text[-2000:]


def mind_prompt(cards: list[dict], memories: list[dict], *, reason: str) -> list[dict]:
    by_card: dict[str, list[dict]] = {}
    for row in memories:
        by_card.setdefault(row.get("card_id") or "", []).append(row)
    blocks: list[str] = []
    for card in cards:
        sheet = npc_assets.parse_sheet(card.get("sheet") or "")
        bonds = npc_bonds.read_bonds(sheet)
        own = by_card.get(card.get("id") or "", [])
        lines = [npc_assets.card_canon_text(card)]
        lines.append("Bonds: " + " ".join(f"{key} {bonds[key]}" for key in npc_bonds.BOND_KEYS))
        if own:
            lines.append("Recent memories:")
            for row in own[:3]:
                lines.append(f"- ({row.get('kind') or 'event'}) {row.get('text') or ''}")
        blocks.append("\n".join(lines))
    user = f"Reason: {reason}\n\n" + "\n\n".join(blocks)
    return [
        {"role": "system", "content": _MIND_SYSTEM},
        {"role": "user", "content": user},
    ]


def _match_card(cards: list[dict], name: str) -> dict | None:
    wanted = " ".join(name.split()).casefold()
    if not wanted:
        return None
    for card in cards:
        title = " ".join(str(card.get("title") or "").split()).casefold()
        if title and title == wanted:
            return card
    return None


def _apply_mind_item(conn, adventure_id: str, card: dict, item: dict, *,
                     day: int) -> None:
    sheet = npc_assets.parse_sheet(card.get("sheet") or "")
    memory = " ".join(str(item.get("memory") or "").split())
    mood = " ".join(str(item.get("mood") or "").split())[:80]
    activity = " ".join(str(item.get("activity") or "").split())[:160]
    if memory:
        add_memory(conn, adventure_id, card["id"], "event", memory[:500], mood)
    sheet = write_life(sheet, {
        "mood": mood,
        "activity": activity,
        "day": day,
        "last_tick": db.utcnow(),
    })
    sentiments = item.get("sentiments")
    if isinstance(sentiments, str) and sentiments.strip():
        sheet["sentiments"] = npc_assets._field_text("sentiments", sentiments)
    raw_bonds = item.get("bonds") if isinstance(item.get("bonds"), dict) else {}
    bonds = npc_bonds.read_bonds(sheet)
    for key in npc_bonds.BOND_KEYS:
        if key not in raw_bonds:
            continue
        try:
            bonds[key] = npc_bonds.clamp(int(raw_bonds[key]))
        except (TypeError, ValueError):
            continue
    sheet = npc_bonds.write_bonds(sheet, bonds)
    db.update_card(conn, adventure_id, card["id"], {
        "sheet": json.dumps(sheet, ensure_ascii=False),
    })


def run_mind(adventure_id: str, transport: Any = None) -> dict:
    """Um passe de mente. Erro de LLM ou JSON é logado e engolido."""
    try:
        with db.session() as conn:
            adventure = db.get_adventure(conn, adventure_id)
            if not adventure:
                return {"updated": 0}
            cards = [
                card for card in db.list_cards(conn, adventure_id)
                if card.get("type") == "character"
            ]
            if not cards:
                return {"updated": 0}
            memories = recent_for_cards(conn, adventure_id, [card["id"] for card in cards])
            llm_cfg = settings_store.effective_llm_settings(conn)
            day = int(adventure.get("life_day") or 0)
            passage = _passage(conn, adventure_id)
        messages = mind_prompt(cards, memories, reason="turn")
        if passage:
            messages[1]["content"] += f"\n\nPassage:\n{passage}"
        content = llm_client.chat_completion(
            llm_cfg, messages, role="chronicle", temperature=0.4,
            max_tokens=700, transport=transport,
        )
        data = npc_assets._extract_json(content)
        items = data.get("characters") if isinstance(data, dict) else None
        if not isinstance(items, list):
            raise ValueError("resposta sem characters")
        updated = 0
        with db.session() as conn:
            fresh = [
                card for card in db.list_cards(conn, adventure_id)
                if card.get("type") == "character"
            ]
            for item in items:
                if not isinstance(item, dict):
                    continue
                card = _match_card(fresh, str(item.get("name") or ""))
                if card is None:
                    continue
                _apply_mind_item(conn, adventure_id, card, item, day=day)
                updated += 1
        return {"updated": updated}
    except Exception:
        log.exception("npc_life: mente falhou (adventure=%s)", adventure_id)
        return {"updated": 0}


def _tick_one(adventure_id: str, card: dict, llm_cfg: dict, *, day: int,
              reflection_after: int, transport: Any) -> None:
    with db.session() as conn:
        count = _memory_count(conn, adventure_id, card["id"])
        memories = recent_for_cards(conn, adventure_id, [card["id"]])
    ask_reflection = count >= reflection_after
    user = (
        npc_assets.card_canon_text(card)
        + f"\nDay: {day}\n"
        + ("This character has enough memories for a reflection.\n"
           if ask_reflection else
           "Do not write a reflection. reflection must be an empty string.\n")
    )
    if memories:
        user += "Recent memories:\n" + "\n".join(
            f"- {row.get('text') or ''}" for row in memories[:3]
        )
    content = llm_client.chat_completion(
        llm_cfg,
        [{"role": "system", "content": _TICK_SYSTEM},
         {"role": "user", "content": user}],
        role="chronicle", temperature=0.6, max_tokens=500, transport=transport,
    )
    data = npc_assets._extract_json(content)
    event = " ".join(str(data.get("event") or "").split())[:500]
    mood = " ".join(str(data.get("mood") or "").split())[:80]
    activity = " ".join(str(data.get("activity") or "").split())[:160]
    reflection = " ".join(str(data.get("reflection") or "").split())[:400]
    with db.session() as conn:
        current = db.get_card(conn, adventure_id, card["id"]) or card
        sheet = npc_assets.parse_sheet(current.get("sheet") or "")
        if event:
            add_memory(conn, adventure_id, card["id"], "event", event, mood)
        if ask_reflection and reflection:
            add_memory(conn, adventure_id, card["id"], "reflection", reflection, mood)
        sheet = write_life(sheet, {
            "mood": mood or life_state(sheet)["mood"],
            "activity": activity or life_state(sheet)["activity"],
            "day": day,
            "last_tick": db.utcnow(),
        })
        db.update_card(conn, adventure_id, card["id"], {
            "sheet": json.dumps(sheet, ensure_ascii=False),
        })


def tick_day(adventure_id: str, transport: Any = None) -> dict:
    """Avança o dia e inventa um evento fora de cena para cada personagem."""
    with db.session() as conn:
        adventure = db.get_adventure(conn, adventure_id)
        if not adventure:
            return {"day": 0, "updated": 0}
        actions = db.list_actions(conn, adventure_id)
        seq = max((int(action["seq"]) for action in actions), default=0)
        day = int(adventure.get("life_day") or 0) + 1
        db.update_adventure_life(conn, adventure_id, day=day, tick_seq=seq)
        cards = [
            card for card in db.list_cards(conn, adventure_id)
            if card.get("type") == "character"
        ]
        llm_cfg = settings_store.effective_llm_settings(conn)
    life_cfg = settings_store.load_file_settings().get("life") or {}
    reflection_after = int(life_cfg.get("reflection_after", 6))
    updated = 0
    for card in cards:
        try:
            _tick_one(
                adventure_id, card, llm_cfg, day=day,
                reflection_after=reflection_after, transport=transport,
            )
            updated += 1
        except Exception:
            log.exception("npc_life: tick falhou (card=%s)", card.get("id"))
    return {"day": day, "updated": updated}
