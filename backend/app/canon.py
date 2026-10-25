"""Foto lógica de um turno e a decisão de gravar trabalho assíncrono depois dele.

O undo restaura esta foto (aventuras, cards, memórias). IDs técnicos e
embeddings ficam de fora do hash lógico. Um passe de crônica cujo action de
origem sumiu — undo ou retry — não grava.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from app import db

_ADVENTURE_FIELDS = (
    "title", "description", "ai_instructions", "plot_essentials", "authors_note",
    "story_summary", "tags", "third_person", "image_style", "ui_profile", "ui_state",
    "character_rules", "turn_mode", "turn_user", "life_day", "life_tick_seq", "sim_state",
)


def _dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def logical_view(conn, adventure_id: str) -> dict:
    """Estado comparável, sem ids técnicos de action nem timestamps de linha."""
    adventure = db.get_adventure(conn, adventure_id)
    if adventure is None:
        return {}
    cards = []
    for card in db.list_cards(conn, adventure_id):
        cards.append({
            "id": card.get("id"),
            "title": card.get("title") or "",
            "type": card.get("type") or "",
            "triggers": card.get("triggers") or [],
            "entry": card.get("entry") or "",
            "pinned": bool(card.get("pinned")),
            "use_for_character_creation": bool(card.get("use_for_character_creation")),
            "portrait_image_id": card.get("portrait_image_id"),
            "portrait_anchor_id": card.get("portrait_anchor_id"),
            "portrait_prompt": card.get("portrait_prompt") or "",
            "voice": card.get("voice") or "",
            "vrm_id": card.get("vrm_id") or "",
            "sheet": card.get("sheet") or "",
            "player_username": card.get("player_username") or "",
        })
    cards.sort(key=lambda row: str(row["id"]))
    memories = [
        {
            "id": row["id"],
            "text": row["text"],
            "seq_start": row["seq_start"],
            "seq_end": row["seq_end"],
        }
        for row in conn.execute(
            "SELECT id, text, seq_start, seq_end FROM memories WHERE adventure_id=? ORDER BY id",
            (adventure_id,),
        )
    ]
    npc_memories = [
        {
            "id": row["id"],
            "card_id": row["card_id"],
            "kind": row["kind"],
            "text": row["text"],
            "mood": row["mood"] or "",
        }
        for row in conn.execute(
            "SELECT id, card_id, kind, text, mood FROM npc_memories "
            "WHERE adventure_id=? ORDER BY id",
            (adventure_id,),
        )
    ]
    return {
        "adventure": {key: adventure.get(key) for key in _ADVENTURE_FIELDS},
        "cards": cards,
        "memories": memories,
        "npc_memories": npc_memories,
    }


def fingerprint(conn, adventure_id: str) -> str:
    payload = _dumps(logical_view(conn, adventure_id))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def capture(conn, adventure_id: str, generation_id: str,
            anchor_action_id: int | None = None) -> None:
    """Grava a foto se esta geração ainda não tiver uma. Não substitui."""
    if not generation_id:
        return
    conn.execute(
        """INSERT INTO turn_canon(
             adventure_id, generation_id, anchor_action_id, snapshot_json, created_at)
           VALUES(?,?,?,?,?)
           ON CONFLICT(adventure_id, generation_id) DO NOTHING""",
        (adventure_id, generation_id, anchor_action_id,
         _dumps(logical_view(conn, adventure_id)), db.utcnow()),
    )


def attach(conn, adventure_id: str, generation_id: str, action_id: int) -> None:
    """Amarra um continue novo numa foto que ainda não tem action."""
    if not generation_id or not action_id:
        return
    conn.execute(
        """UPDATE turn_canon SET anchor_action_id=?
           WHERE adventure_id=? AND generation_id=? AND anchor_action_id IS NULL""",
        (int(action_id), adventure_id, generation_id),
    )


def reanchor(conn, adventure_id: str, from_action_id: int, to_action_id: int) -> None:
    """Retry troca o id do continue; a foto continua sendo a de antes do turno."""
    if not from_action_id or not to_action_id:
        return
    conn.execute(
        """UPDATE turn_canon SET anchor_action_id=?
           WHERE adventure_id=? AND anchor_action_id=?""",
        (int(to_action_id), adventure_id, int(from_action_id)),
    )


def discard_unanchored(conn, adventure_id: str, generation_id: str) -> None:
    if not generation_id:
        return
    conn.execute(
        """DELETE FROM turn_canon
           WHERE adventure_id=? AND generation_id=? AND anchor_action_id IS NULL""",
        (adventure_id, generation_id),
    )


def restore(conn, adventure_id: str, action_ids: list[int]) -> bool:
    """Restaura a foto ancorada em qualquer um dos actions. False se não houver."""
    ids = [int(action_id) for action_id in action_ids if action_id]
    if not ids:
        return False
    placeholders = ",".join("?" for _ in ids)
    row = conn.execute(
        f"""SELECT generation_id, snapshot_json FROM turn_canon
            WHERE adventure_id=? AND anchor_action_id IN ({placeholders})
            ORDER BY created_at DESC LIMIT 1""",
        (adventure_id, *ids),
    ).fetchone()
    if row is None:
        return False
    _apply(conn, adventure_id, json.loads(row["snapshot_json"]))
    conn.execute(
        "DELETE FROM turn_canon WHERE adventure_id=? AND generation_id=?",
        (adventure_id, row["generation_id"]),
    )
    return True


def chronicle_gate(conn, adventure_id: str, source_action_id: int | None) -> str:
    """`ok`, `stale` (action sumiu) ou `duplicate` (o mesmo action já aplicou)."""
    if not source_action_id:
        return "ok"
    action = db.get_action(conn, int(source_action_id))
    if action is None or action.get("adventure_id") != adventure_id:
        return "stale"
    rules = db.get_action_rules(conn, int(source_action_id)) or {}
    if rules.get("chronicle_applied"):
        return "duplicate"
    return "ok"


def _apply(conn, adventure_id: str, snapshot: dict) -> None:
    adventure = snapshot.get("adventure") if isinstance(snapshot.get("adventure"), dict) else {}
    ui_profile = adventure.get("ui_profile")
    conn.execute(
        """UPDATE adventures SET
             title=?, description=?, ai_instructions=?, plot_essentials=?, authors_note=?,
             story_summary=?, tags=?, third_person=?, image_style=?, ui_profile=?, ui_state=?,
             character_rules=?, turn_mode=?, turn_user=?, life_day=?, life_tick_seq=?, sim_state=?
           WHERE id=?""",
        (
            adventure.get("title") or "",
            adventure.get("description") or "",
            adventure.get("ai_instructions") or "",
            adventure.get("plot_essentials") or "",
            adventure.get("authors_note") or "",
            adventure.get("story_summary") or "",
            _dumps(adventure.get("tags") or []),
            int(bool(adventure.get("third_person"))),
            adventure.get("image_style") or "",
            "" if ui_profile is None else _dumps(ui_profile),
            _dumps(adventure.get("ui_state") or {}),
            adventure.get("character_rules") or "",
            int(bool(adventure.get("turn_mode"))),
            adventure.get("turn_user") or "",
            int(adventure.get("life_day") or 0),
            int(adventure.get("life_tick_seq") or 0),
            _dumps(adventure.get("sim_state") or {}),
            adventure_id,
        ),
    )
    cards = snapshot.get("cards") if isinstance(snapshot.get("cards"), list) else []
    kept: list[str] = []
    for card in cards:
        if not isinstance(card, dict) or not card.get("id"):
            continue
        card_id = str(card["id"])
        kept.append(card_id)
        conn.execute(
            """INSERT INTO story_cards(
                 id, adventure_id, title, type, triggers, entry, pinned,
                 use_for_character_creation, portrait_image_id, portrait_anchor_id,
                 portrait_prompt, voice, vrm_id, sheet, player_username, created_at, updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(id) DO UPDATE SET
                 adventure_id=excluded.adventure_id, title=excluded.title, type=excluded.type,
                 triggers=excluded.triggers, entry=excluded.entry, pinned=excluded.pinned,
                 use_for_character_creation=excluded.use_for_character_creation,
                 portrait_image_id=excluded.portrait_image_id,
                 portrait_anchor_id=excluded.portrait_anchor_id,
                 portrait_prompt=excluded.portrait_prompt, voice=excluded.voice,
                 vrm_id=excluded.vrm_id, sheet=excluded.sheet,
                 player_username=excluded.player_username, updated_at=excluded.updated_at""",
            (
                card_id, adventure_id, card.get("title") or "", card.get("type") or "other",
                _dumps(card.get("triggers") or []), card.get("entry") or "",
                int(bool(card.get("pinned"))), int(bool(card.get("use_for_character_creation"))),
                card.get("portrait_image_id"), card.get("portrait_anchor_id"),
                card.get("portrait_prompt") or "", card.get("voice") or "",
                card.get("vrm_id") or "", card.get("sheet") or "",
                card.get("player_username") or "", db.utcnow(), db.utcnow(),
            ),
        )
    existing = [row["id"] for row in conn.execute(
        "SELECT id FROM story_cards WHERE adventure_id=?", (adventure_id,),
    )]
    for card_id in existing:
        if card_id not in kept:
            db.delete_card(conn, adventure_id, card_id)
    conn.execute("DELETE FROM npc_memories WHERE adventure_id=?", (adventure_id,))
    for row in snapshot.get("npc_memories") or []:
        if not isinstance(row, dict) or not row.get("id") or not row.get("card_id"):
            continue
        conn.execute(
            """INSERT INTO npc_memories(id, adventure_id, card_id, kind, text, mood, created_at)
               VALUES(?,?,?,?,?,?,?)""",
            (str(row["id"]), adventure_id, str(row["card_id"]), row.get("kind") or "event",
             row.get("text") or "", row.get("mood") or "", db.utcnow()),
        )
    conn.execute("DELETE FROM memories WHERE adventure_id=?", (adventure_id,))
    for row in snapshot.get("memories") or []:
        if not isinstance(row, dict) or row.get("id") is None:
            continue
        conn.execute(
            """INSERT INTO memories(id, adventure_id, text, seq_start, seq_end, created_at)
               VALUES(?,?,?,?,?,?)""",
            (int(row["id"]), adventure_id, row.get("text") or "",
             row.get("seq_start"), row.get("seq_end"), db.utcnow()),
        )
