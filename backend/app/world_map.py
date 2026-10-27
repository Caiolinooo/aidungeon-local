"""Mapa vivo e journal cruzado.

Locais são cards `type=location`. Conexões, posição, imagens e entradas do
journal usam essas mesmas entidades, então mapa, narrativa, memória e imagens
compartilham referências em vez de inventar objetos separados.

Fog usa `location_links.discovered`: o payload mostra o local atual, locais
sem link (mundos já existentes) e locais com link descoberto. O destino de um
link novo não descoberto fica de fora, sem journal, imagem ou coordenada.
Viajar, gerar ou revelar sobe `discovered` e não rebaixa progresso antigo.
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


def _link_rows(conn, adventure_id: str) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM location_links WHERE adventure_id=?",
        (adventure_id,),
    ).fetchall()
    return [dict(row) for row in rows]


def _viewer_sees_unlinked(adventure: dict, viewer: str | None) -> bool:
    """Local sem rota: dono (e admin) vê. Sem plateia, o mapa legado continua.
    Jogador que não é dono não vê.
    """
    if viewer is None:
        return True
    return db.is_adventure_owner(adventure, viewer)


def _revealed_ids(cards: list[dict], links: list[dict],
                  current_id: str | None, *,
                  include_unlinked: bool = True) -> set[str]:
    """Quem o mapa pode mostrar.

    O destino de um link `discovered=0` fica oculto. A origem desse link, o
    local atual e quem tem link descoberto continuam visíveis. Local sem
    nenhum link só entra com `include_unlinked` (dono ou chamada sem plateia).
    """
    has_any: set[str] = set()
    has_discovered: set[str] = set()
    concealed_targets: set[str] = set()
    for link in links:
        origin = link.get("from_card_id")
        dest = link.get("to_card_id")
        if origin:
            has_any.add(origin)
        if dest:
            has_any.add(dest)
        if int(link.get("discovered") or 0):
            if origin:
                has_discovered.add(origin)
            if dest:
                has_discovered.add(dest)
        elif dest:
            concealed_targets.add(dest)
    revealed: set[str] = set()
    for card in cards:
        card_id = card["id"]
        if current_id and card_id == current_id:
            revealed.add(card_id)
        elif card_id in has_discovered:
            revealed.add(card_id)
        elif card_id not in has_any:
            if include_unlinked:
                revealed.add(card_id)
        elif card_id not in concealed_targets:
            revealed.add(card_id)
    return revealed


def _hidden_ids(conn, adventure_id: str, *, include_unlinked: bool = True) -> set[str]:
    cards = _location_cards(conn, adventure_id)
    current = current_location(conn, adventure_id)
    current_id = current["id"] if current else None
    revealed = _revealed_ids(
        cards, _link_rows(conn, adventure_id), current_id,
        include_unlinked=include_unlinked,
    )
    return {card["id"] for card in cards if card["id"] not in revealed}


def current_location(conn, adventure_id: str) -> dict | None:
    row = conn.execute(
        "SELECT current_card_id FROM map_state WHERE adventure_id=?",
        (adventure_id,),
    ).fetchone()
    if row and row["current_card_id"]:
        return db.get_card(conn, adventure_id, row["current_card_id"])
    cards = _location_cards(conn, adventure_id)
    if not cards:
        return None
    revealed = _revealed_ids(cards, _link_rows(conn, adventure_id), None)
    for card in cards:
        if card["id"] in revealed:
            return card
    return None


def location_visible(conn, adventure_id: str, card_id: str, *,
                     viewer: str | None = None) -> bool:
    card = db.get_card(conn, adventure_id, card_id)
    if not card or (card.get("type") or "") != "location":
        return False
    adventure = db.get_adventure(conn, adventure_id) or {}
    current = current_location(conn, adventure_id)
    current_id = current["id"] if current else None
    cards = _location_cards(conn, adventure_id)
    return card_id in _revealed_ids(
        cards, _link_rows(conn, adventure_id), current_id,
        include_unlinked=_viewer_sees_unlinked(adventure, viewer),
    )


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

WORLD_TYPES = ("real", "fictional", "franchise", "alternate_history")


def world_type_of(adventure: dict) -> str:
    state = adventure.get("ui_state") or {}
    if isinstance(state, dict):
        return str(state.get("world_type") or "real")
    return "real"


def set_world_type(conn, adventure_id: str, world_type: str) -> None:
    if world_type not in WORLD_TYPES:
        raise ValueError("invalid_world_type")
    adventure = db.get_adventure(conn, adventure_id)
    if not adventure:
        return
    state = adventure.get("ui_state") or {}
    if not isinstance(state, dict):
        state = {}
    state["world_type"] = world_type
    db.update_adventure_ui_state(conn, adventure_id, state)


def location_hierarchy(card: dict) -> dict:
    sheet = npc_assets.parse_sheet(card.get("sheet") or "")
    return {
        "world": str(sheet.get("world") or ""),
        "region": str(sheet.get("region") or ""),
        "city": str(sheet.get("city") or ""),
        "district": str(sheet.get("district") or ""),
        "coordinates": sheet.get("coordinates") if isinstance(sheet.get("coordinates"), dict) else {},
    }


def coherent_location_context(conn, adventure_id: str, card: dict) -> str:
    """Contexto geográfico para gerar locais que fazem sentido no mundo."""
    adventure = db.get_adventure(conn, adventure_id) or {}
    world_type = world_type_of(adventure)
    hierarchy = location_hierarchy(card)
    current = current_location(conn, adventure_id)
    current_hierarchy = location_hierarchy(current) if current else {}
    parts = [
        f"World type: {world_type}",
        f"World: {adventure.get('title') or ''}",
        f"Plot: {adventure.get('plot_essentials') or ''}",
    ]
    if current_hierarchy:
        parts.append(
            "Current geography: "
            + " / ".join(
                value for value in (
                    current_hierarchy.get("world"), current_hierarchy.get("region"),
                    current_hierarchy.get("city"), current_hierarchy.get("district"),
                ) if value
            )
        )
    if hierarchy:
        parts.append(
            "Location hierarchy: "
            + " / ".join(
                value for value in (
                    hierarchy.get("world"), hierarchy.get("region"),
                    hierarchy.get("city"), hierarchy.get("district"),
                ) if value
            )
        )
    return "\n".join(part for part in parts if part and not part.endswith(": "))


def ensure_link(conn, adventure_id: str, from_id: str, to_id: str, *,
                travel_text: str = "", travel_cost: str = "",
                bidirectional: bool = True, discovered: bool = True) -> dict:
    """Cria a rota. `discovered=False` só vale na inserção: não rebaixa fog já aberto."""
    existing = conn.execute(
        "SELECT * FROM location_links WHERE adventure_id=? AND from_card_id=? AND to_card_id=?",
        (adventure_id, from_id, to_id),
    ).fetchone()
    if existing:
        if discovered and not int(existing["discovered"] or 0):
            conn.execute(
                "UPDATE location_links SET discovered=1 WHERE id=?",
                (existing["id"],),
            )
            existing = conn.execute(
                "SELECT * FROM location_links WHERE id=?",
                (existing["id"],),
            ).fetchone()
        return dict(existing)
    link_id = db.new_id()
    conn.execute(
        "INSERT INTO location_links("
        "id,adventure_id,from_card_id,to_card_id,travel_text,travel_cost,"
        "bidirectional,discovered,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
        (link_id, adventure_id, from_id, to_id, travel_text, travel_cost,
         int(bidirectional), int(discovered), db.utcnow()),
    )
    return dict(conn.execute(
        "SELECT * FROM location_links WHERE id=?",
        (link_id,),
    ).fetchone())


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


def _promote_links(conn, adventure_id: str, card_id: str) -> int:
    """Abre só as rotas entre este local e locais já visíveis. Vizinhos ocultos ficam."""
    cards = _location_cards(conn, adventure_id)
    links = _link_rows(conn, adventure_id)
    current = current_location(conn, adventure_id)
    current_id = current["id"] if current else None
    known = _revealed_ids(cards, links, current_id)
    updated = 0
    for link in links:
        if int(link.get("discovered") or 0):
            continue
        origin = link.get("from_card_id")
        dest = link.get("to_card_id")
        if card_id not in (origin, dest):
            continue
        other = dest if origin == card_id else origin
        if other not in known:
            continue
        conn.execute(
            "UPDATE location_links SET discovered=1 WHERE id=? AND discovered=0",
            (link["id"],),
        )
        updated += 1
    return updated


def reveal_location(conn, adventure_id: str, card_id: str) -> dict:
    """Marca o local como descoberto se ele liga a um local já visível."""
    card = db.get_card(conn, adventure_id, card_id)
    if not card or (card.get("type") or "") != "location":
        raise ValueError("location_not_found")
    _promote_links(conn, adventure_id, card_id)
    if not location_visible(conn, adventure_id, card_id):
        raise ValueError("location_not_connected")
    return card


def _public_links(conn, adventure_id: str, card_id: str,
                  revealed: set[str]) -> list[dict]:
    """Saída conhecida leva título e destino. Saída na névoa não leva
    to_card_id, título nem entry."""
    raw = links_for(conn, adventure_id, card_id, include_undiscovered=True)
    public: list[dict] = []
    seen: set[tuple] = set()
    seen_hidden: set[str] = set()
    for link in raw:
        dest = link.get("to_card_id")
        key = (link.get("id"), dest)
        if key in seen:
            continue
        seen.add(key)
        if dest not in revealed:
            if not dest or dest in seen_hidden:
                continue
            seen_hidden.add(dest)
            public.append({
                "id": link.get("id"),
                "from_card_id": link.get("from_card_id"),
                "bidirectional": int(link.get("bidirectional") or 0),
                "discovered": 0,
                "hidden": True,
            })
            continue
        item = dict(link)
        item["hidden"] = False
        item["discovered"] = int(link.get("discovered") or 0)
        public.append(item)
    return public


def travel(conn, adventure_id: str, to_card_id: str) -> dict:
    current = current_location(conn, adventure_id)
    if current is None:
        raise ValueError("no_current_location")
    if current["id"] == to_card_id:
        return {"location": current, "travelled": False, "links": []}
    options = links_for(conn, adventure_id, current["id"], include_undiscovered=True)
    if not any(item["to_card_id"] == to_card_id for item in options):
        raise ValueError("location_not_connected")
    reveal_location(conn, adventure_id, to_card_id)
    destination = set_current_location(conn, adventure_id, to_card_id)
    add_journal(
        conn, adventure_id, kind="travel", title=f"Viagem para {destination['title']}",
        text=f"O jogador saiu de {current['title']} e chegou a {destination['title']}.",
        entity_type="location", entity_id=to_card_id,
        related=[{"type": "location", "id": current["id"], "title": current["title"]}],
    )
    cards = _location_cards(conn, adventure_id)
    revealed = _revealed_ids(cards, _link_rows(conn, adventure_id), destination["id"])
    return {
        "location": destination,
        "travelled": True,
        "from": current,
        "links": _public_links(conn, adventure_id, destination["id"], revealed),
    }


def map_payload(conn, adventure_id: str, *, viewer: str | None = None) -> dict:
    adventure = db.get_adventure(conn, adventure_id) or {}
    cards = _location_cards(conn, adventure_id)
    current = current_location(conn, adventure_id)
    current_id = current["id"] if current else None
    revealed = _revealed_ids(
        cards, _link_rows(conn, adventure_id), current_id,
        include_unlinked=_viewer_sees_unlinked(adventure, viewer),
    )
    revealed_cards = [card for card in cards if card["id"] in revealed]
    nodes = []
    for card in revealed_cards:
        image = latest_location_image(conn, adventure_id, card["id"])
        nodes.append({
            "id": card["id"],
            "title": card.get("title") or "",
            "entry": card.get("entry") or "",
            "current": bool(current_id and current_id == card["id"]),
            "image": image,
            "links": _public_links(conn, adventure_id, card["id"], revealed),
            "hierarchy": location_hierarchy(card),
        })
    player = db.player_character(conn, adventure_id, adventure.get("owner") or "")
    from app import progression
    return {
        "current": current,
        "world_type": world_type_of(adventure),
        "locations": nodes,
        "journal": visible_journal(conn, adventure_id, viewer=viewer),
        "progression": progression.progression_of(
            npc_assets.parse_sheet((player or {}).get("sheet") or "")
        ),
        "npc_relations": progression.relationship_context(revealed_cards),
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


def _journal_item(row) -> dict:
    item = dict(row)
    try:
        item["related"] = json.loads(item.pop("related_json") or "[]")
    except ValueError:
        item["related"] = []
    return item


def journal_for(conn, adventure_id: str, *, limit: int = 40) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM journal_entries WHERE adventure_id=? "
        "ORDER BY created_at DESC, id DESC LIMIT ?",
        (adventure_id, limit),
    ).fetchall()
    return [_journal_item(row) for row in rows]


def visible_journal(conn, adventure_id: str, *, limit: int = 40,
                    viewer: str | None = None) -> list[dict]:
    """Journal do mapa.

    Névoa e notas `visibility=dm` ficam de fora para quem não é dono.
    Com dm_mode, o dono também vê a entrada do local ainda escondido.
    Os nós do mapa (coordenada, imagem) continuam na névoa.
    """
    adventure = db.get_adventure(conn, adventure_id) or {}
    owner = bool(viewer) and db.is_adventure_owner(adventure, viewer)
    include_hidden = owner and bool(adventure.get("dm_mode"))
    include_dm = owner
    hidden: set[str] = set() if include_hidden else _hidden_ids(
        conn, adventure_id,
        include_unlinked=_viewer_sees_unlinked(adventure, viewer),
    )
    if not hidden:
        result = journal_for(conn, adventure_id, limit=limit)
    else:
        placeholders = ",".join("?" * len(hidden))
        rows = conn.execute(
            "SELECT * FROM journal_entries WHERE adventure_id=? "
            f"AND NOT (entity_type='location' AND entity_id IN ({placeholders})) "
            "ORDER BY created_at DESC, id DESC LIMIT ?",
            (adventure_id, *sorted(hidden), limit),
        ).fetchall()
        result = []
        for row in rows:
            item = _journal_item(row)
            item["related"] = [
                rel for rel in (item.get("related") or [])
                if not (
                    isinstance(rel, dict)
                    and rel.get("type") == "location"
                    and rel.get("id") in hidden
                )
            ]
            result.append(item)
    if not include_dm:
        result = [
            item for item in result
            if (item.get("visibility") or "public") != "dm"
        ]
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
    story = coherent_location_context(conn, adventure_id, current or {})
    if current:
        story += f"\nCurrent location: {current.get('title') or ''}"
    result = assistant.generate_single_card(
        llm_cfg or {}, concept=f"{name} {reference}".strip(),
        card_type=card_type, story_context=story + ("\n" + research if research else ""),
        instruction=(
            "Create a location with concrete geography, atmosphere, sensory detail, "
            "visual landmarks and story hooks. Preserve the reference's identity. "
            "Respect the world type and geographic hierarchy; do not invent a place "
            "that contradicts the established world or region."
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
                    travel_text=f"Travel to {card['title']}", discovered=True)
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
