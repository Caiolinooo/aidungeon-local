"""Mapa vivo e journal cruzado.

Locais são cards `type=location`. Conexões, posição, imagens e entradas do
journal usam essas mesmas entidades, então mapa, narrativa, memória e imagens
compartilham referências em vez de inventar objetos separados.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from app import db, llm_client, npc_assets

log = logging.getLogger("aidungeon.world_map")


def _location_cards(conn, adventure_id: str) -> list[dict]:
    return [
        card for card in db.list_cards(conn, adventure_id)
        if (card.get("type") or "") == "location"
    ]


def current_location(conn, adventure_id: str) -> dict | None:
    row = conn.execute(
        "SELECT current_card_id FROM map_state WHERE adventure_id=?",
        (adventure_id,),
    ).fetchone()
    if row and row["current_card_id"]:
        return db.get_card(conn, adventure_id, row["current_card_id"])
    locations = _location_cards(conn, adventure_id)
    return locations[0] if locations else None


def set_current_location(conn, adventure_id: str, card_id: str) -> dict:
    card = db.get_card(conn, adventure_id, card_id)
    if not card or (card.get("type") or "") != "location":
        raise ValueError("location_not_found")
    conn.execute(
        "INSERT INTO map_state(adventure_id,current_card_id,updated_at) VALUES(?,?,?) "
        "ON CONFLICT(adventure_id) DO UPDATE SET current_card_id=excluded.current_card_id,"
        "updated_at=excluded.updated_at",
        (adventure_id, card_id, db.utcnow()),
    )
    return card


def ensure_link(conn, adventure_id: str, from_id: str, to_id: str, *,
                travel_text: str = "", travel_cost: str = "",
                bidirectional: bool = True, discovered: bool = True) -> dict:
    link_id = db.new_id()
    conn.execute(
        "INSERT OR IGNORE INTO location_links("
        "id,adventure_id,from_card_id,to_card_id,travel_text,travel_cost,"
        "bidirectional,discovered,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
        (link_id, adventure_id, from_id, to_id, travel_text, travel_cost,
         int(bidirectional), int(discovered), db.utcnow()),
    )
    return conn.execute(
        "SELECT * FROM location_links WHERE adventure_id=? AND from_card_id=? AND to_card_id=?",
        (adventure_id, from_id, to_id),
    ).fetchone()


def links_for(conn, adventure_id: str, card_id: str, *,
              include_undiscovered: bool = False) -> list[dict]:
    query = (
        "SELECT l.*, c.title AS to_title, c.entry AS to_entry, c.id AS to_id "
        "FROM location_links l JOIN story_cards c ON c.id=l.to_card_id "
        "WHERE l.adventure_id=? AND l.from_card_id=?"
    )
    params: list[Any] = [adventure_id, card_id]
    if not include_undiscovered:
        query += " AND l.discovered=1"
    rows = conn.execute(query, params).fetchall()
    result = [dict(row) for row in rows]
    reverse = conn.execute(
        "SELECT l.*, c.title AS to_title, c.entry AS to_entry, c.id AS to_id "
        "FROM location_links l JOIN story_cards c ON c.id=l.from_card_id "
        "WHERE l.adventure_id=? AND l.to_card_id=? AND l.bidirectional=1"
        + ("" if include_undiscovered else " AND l.discovered=1"),
        (adventure_id, card_id),
    ).fetchall()
    for row in reverse:
        item = dict(row)
        item["from_card_id"], item["to_card_id"] = item["to_card_id"], item["from_card_id"]
        result.append(item)
    return result


def travel(conn, adventure_id: str, to_card_id: str) -> dict:
    current = current_location(conn, adventure_id)
    if current is None:
        raise ValueError("no_current_location")
    if current["id"] == to_card_id:
        return {"location": current, "travelled": False, "links": []}
    options = links_for(conn, adventure_id, current["id"])
    if not any(item["to_card_id"] == to_card_id for item in options):
        raise ValueError("location_not_connected")
    destination = set_current_location(conn, adventure_id, to_card_id)
    add_journal(
        conn, adventure_id, kind="travel", title=f"Viagem para {destination['title']}",
        text=f"O jogador saiu de {current['title']} e chegou a {destination['title']}.",
        entity_type="location", entity_id=to_card_id,
        related=[{"type": "location", "id": current["id"], "title": current["title"]}],
    )
    return {
        "location": destination,
        "travelled": True,
        "from": current,
        "links": links_for(conn, adventure_id, to_card_id),
    }


def map_payload(conn, adventure_id: str) -> dict:
    locations = _location_cards(conn, adventure_id)
    current = current_location(conn, adventure_id)
    nodes = []
    for card in locations:
        image = latest_location_image(conn, adventure_id, card["id"])
        nodes.append({
            "id": card["id"],
            "title": card.get("title") or "",
            "entry": card.get("entry") or "",
            "current": bool(current and current["id"] == card["id"]),
            "image": image,
            "links": links_for(conn, adventure_id, card["id"]),
        })
    return {
        "current": current,
        "locations": nodes,
        "journal": journal_for(conn, adventure_id),
    }


def add_journal(conn, adventure_id: str, *, kind: str, title: str, text: str,
                entity_type: str = "", entity_id: str = "",
                related: list[dict] | None = None,
                action_id: int | None = None,
                visibility: str = "public") -> dict:
    entry_id = db.new_id()
    conn.execute(
        "INSERT OR IGNORE INTO journal_entries("
        "id,adventure_id,action_id,kind,title,text,entity_type,entity_id,"
        "related_json,visibility,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        (entry_id, adventure_id, action_id, kind, title, text,
         entity_type, entity_id, json.dumps(related or [], ensure_ascii=False),
         visibility, db.utcnow()),
    )
    row = conn.execute(
        "SELECT * FROM journal_entries WHERE adventure_id=? AND action_id IS ? "
        "AND kind=? AND entity_id=?",
        (adventure_id, action_id, kind, entity_id),
    ).fetchone()
    return dict(row) if row else {}


def journal_for(conn, adventure_id: str, *, limit: int = 40) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM journal_entries WHERE adventure_id=? "
        "ORDER BY created_at DESC, id DESC LIMIT ?",
        (adventure_id, limit),
    ).fetchall()
    result = []
    for row in rows:
        item = dict(row)
        try:
            item["related"] = json.loads(item.pop("related_json") or "[]")
        except ValueError:
            item["related"] = []
        result.append(item)
    return result


def latest_location_image(conn, adventure_id: str, card_id: str) -> dict | None:
    row = conn.execute(
        "SELECT i.id, i.file_path, i.prompt, i.width, i.height, li.state_key "
        "FROM location_images li JOIN images i ON i.id=li.image_id "
        "WHERE li.adventure_id=? AND li.card_id=? "
        "ORDER BY li.created_at DESC LIMIT 1",
        (adventure_id, card_id),
    ).fetchone()
    if not row:
        return None
    return {
        "id": row["id"],
        "url": f"/api/images/{row['id']}",
        "prompt": row["prompt"],
        "width": row["width"],
        "height": row["height"],
        "state_key": row["state_key"],
    }


def attach_location_image(conn, adventure_id: str, card_id: str,
                          image_id: str, *, state_key: str = "") -> None:
    conn.execute(
        "INSERT OR IGNORE INTO location_images("
        "id,adventure_id,card_id,image_id,state_key,created_at) VALUES(?,?,?,?,?,?)",
        (db.new_id(), adventure_id, card_id, image_id, state_key, db.utcnow()),
    )


def reference_context(card: dict, *, reference: str = "") -> str:
    """Referência real ou fictícia usada para gerar um local."""
    bits = [card.get("title") or "", card.get("entry") or ""]
    if reference.strip():
        bits.append(f"Reference: {reference.strip()}")
    return "\n".join(bit for bit in bits if bit)


def generate_location(conn, adventure_id: str, *, name: str,
                      reference: str = "", card_type: str = "location",
                      llm_cfg: dict | None = None,
                      transport: Any = None,
                      web_search: bool = False) -> dict:
    """Cria um local real ou fictício com IA e conecta ao mapa."""
    from app import assistant, web_search as web_search_mod
    adventure = db.get_adventure(conn, adventure_id)
    if not adventure:
        raise ValueError("adventure_not_found")
    research = ""
    sources = []
    if web_search:
        result = web_search_mod.research_context(
            f"{name} {reference}", llm_cfg=llm_cfg, language="pt",
            mode="always", max_results=4, transport=transport,
        )
        research = result.get("context_block") or ""
        sources = result.get("sources") or []
    current = current_location(conn, adventure_id)
    story = (
        f"World: {adventure.get('title') or ''}\n"
        f"Plot: {adventure.get('plot_essentials') or ''}\n"
        f"Current location: {(current or {}).get('title') or ''}"
    )
    result = assistant.generate_single_card(
        llm_cfg or {}, concept=f"{name} {reference}".strip(),
        card_type=card_type, story_context=story + ("\n" + research if research else ""),
        instruction=(
            "Create a location with concrete geography, atmosphere, sensory detail, "
            "visual landmarks and story hooks. Preserve the reference's identity."
        ),
        web_search=False, language="pt", transport=transport,
    )
    card_data = result.get("card") or {}
    card = db.insert_card(
        conn, adventure_id, type="location",
        title=card_data.get("title") or name,
        triggers=card_data.get("triggers") or [name],
        entry=card_data.get("entry") or "",
        pinned=False,
    )
    if current:
        ensure_link(conn, adventure_id, current["id"], card["id"],
                    travel_text=f"Travel to {card['title']}")
    add_journal(
        conn, adventure_id, kind="discovery", title=f"Local descoberto: {card['title']}",
        text=card.get("entry") or "",
        entity_type="location", entity_id=card["id"],
        related=[{"type": "web_source", "title": s.get("title"), "url": s.get("url")}
                 for s in sources],
    )
    return {"card": card, "web_sources": sources}


def generate_location_prompt(conn, adventure_id: str, card: dict,
                             adventure: dict | None = None) -> str:
    """Prompt visual do local a partir do card e do estado do mundo."""
    adventure = adventure or db.get_adventure(conn, adventure_id) or {}
    from app import consequences
    effects = consequences.visual_context(adventure, target=card.get("title") or "")
    parts = [
        "wide cinematic establishing shot",
        card.get("title") or "",
        (card.get("entry") or "")[:700],
        adventure.get("image_style") or "",
        effects,
        "detailed environment, no people, no text, no watermark",
    ]
    return ", ".join(part for part in parts if part)[:1400]
