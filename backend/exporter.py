"""Exportador — gera .zip NO MESMO FORMATO do backup do AI Dungeon (round-trip).

Saída: metadata.json + actions-001.json (parte única), espelhando o mapeamento
inverso do importador (PLANO.md §2.1). Os textos das ações são gravados
verbatim (do/say já vivem formatados no banco — igual ao backup original).
"""

from __future__ import annotations

import io
import json
import re
import zipfile
from typing import Any

from app import db
from app.errors import GameError


def _card_keys(triggers: list[str]) -> str:
    """[\"Baldur's Gate\"] → \", Baldur's Gate ,\" (estilo do backup original)."""
    triggers = [t.strip() for t in triggers if t and t.strip()]
    return ", " + ", ".join(triggers) + " ," if triggers else ""


def build_metadata(adv: dict, actions: list[dict], cards: list[dict],
                   memories: list[dict]) -> dict:
    story_cards = []
    for c in cards:
        story_cards.append({
            "id": c["id"],
            "createdAt": c["created_at"],
            "updatedAt": c["updated_at"],
            "keys": _card_keys(c["triggers"]),
            "value": c["entry"],
            "type": c["type"],
            "title": c["title"],
            "description": c["entry"],
            "isPinned": c["pinned"],
            "useForCharacterCreation": c["use_for_character_creation"],
        })

    mems: list[dict[str, Any]] = []
    for m in memories:
        action_ids: list[str] = []
        if m["seq_start"] is not None and m["seq_end"] is not None:
            action_ids = [str(s) for s in range(m["seq_start"], m["seq_end"] + 1)]
        mems.append({
            "text": m["text"],
            "actionIds": action_ids,
            "lastRelevantActionId": str(m["seq_end"]) if m["seq_end"] is not None else "",
        })

    return {
        "exportedAt": db.utcnow(),
        "adventure": {
            "id": adv["id"],
            "title": adv["title"],
            "description": adv["description"],
            "memory": adv["plot_essentials"],
            "authorsNote": adv["authors_note"],
            "tags": adv["tags"],
            "thirdPerson": adv["third_person"],
            "contentRating": "Unrated",
            "createdAt": adv["created_at"],
            "updatedAt": adv["updated_at"],
        },
        "state": {
            "type": "adventure",
            "storySummary": adv["story_summary"],
            "storyCardInstructions": "",
            "storyCardStoryInformation": "",
            "imageStyle": adv.get("image_style") or "",
            "instructions": {
                "type": "scenario",
                "scenario": adv["ai_instructions"],
            },
            "storyCards": story_cards,
            "memories": mems,
        },
        "totalActionCount": len(actions),
        "totalParts": 1,
    }


def build_actions_part(actions: list[dict]) -> dict:
    return {
        "partNumber": 1,
        "totalParts": 1,
        "actions": [
            {
                "id": str(a["seq"]),
                "type": a["type"],
                "text": a["text"],
                "createdAt": a["created_at"],
                "updatedAt": a["edited_at"] or a["created_at"],
            }
            for a in actions
        ],
    }


def export_zip(adventure_id: str) -> tuple[bytes, str]:
    """Devolve (bytes do zip, título da aventura). 404 se não existir."""
    with db.session() as conn:
        adv = db.get_adventure(conn, adventure_id)
        if not adv:
            raise GameError.not_found(f"adventure {adventure_id} not found")
        actions = db.list_actions(conn, adventure_id)
        cards = db.list_cards(conn, adventure_id)
        memories = db.list_memories(conn, adventure_id)

    metadata = build_metadata(adv, actions, cards, memories)
    actions_part = build_actions_part(actions)

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("metadata.json", json.dumps(metadata, ensure_ascii=False, indent=2))
        zf.writestr("actions-001.json", json.dumps(actions_part, ensure_ascii=False, indent=2))
    return buf.getvalue(), adv["title"]


def _slug(title: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "-", title).strip("-")[:40] or "adventure"


def export_all_zip() -> tuple[bytes, int]:
    """Backup completo: um .zip com uma pasta por aventura (NNN-slug/), cada
    pasta no formato do backup individual (metadata.json + actions-001.json).
    Devolve (bytes, nº de aventuras)."""
    with db.session() as conn:
        ids = [r["id"] for r in conn.execute(
            "SELECT id FROM adventures ORDER BY updated_at DESC").fetchall()]
        bundles = [
            (db.get_adventure(conn, aid), db.list_actions(conn, aid),
             db.list_cards(conn, aid), db.list_memories(conn, aid))
            for aid in ids
        ]

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for i, (adv, actions, cards, memories) in enumerate(bundles, 1):
            folder = f"{i:03d}-{_slug(adv['title'])}"
            metadata = build_metadata(adv, actions, cards, memories)
            actions_part = build_actions_part(actions)
            zf.writestr(f"{folder}/metadata.json",
                        json.dumps(metadata, ensure_ascii=False, indent=2))
            zf.writestr(f"{folder}/actions-001.json",
                        json.dumps(actions_part, ensure_ascii=False, indent=2))
    return buf.getvalue(), len(bundles)
