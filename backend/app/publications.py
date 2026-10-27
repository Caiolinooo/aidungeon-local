"""Catálogo de publicações: snapshot canônico, versões imutáveis e remix.

O snapshot não acompanha a origem. Republicar gera outra versão só quando
o hash muda. Aventura publicada não leva actions, memórias, jogadores nem
retratos; leva links e os scripts da própria aventura (não o global).
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from pathlib import Path

from app import access, db
from app.errors import GameError
from app.scripting import SCRIPT_HOOKS

log = logging.getLogger("app.publications")

_MIGRATION = Path(__file__).resolve().parents[1] / "migrations" / "037_publications.sql"
_KINDS = ("scenario", "adventure")
_SLUG = re.compile(r"[^a-z0-9]+")

_UPDATE_TRIGGER = """
CREATE TRIGGER publication_versions_immutable
BEFORE UPDATE ON publication_versions
BEGIN
  SELECT RAISE(ABORT, 'publication_versions are immutable');
END
"""

_DELETE_TRIGGER = """
CREATE TRIGGER publication_versions_no_delete
BEFORE DELETE ON publication_versions
WHEN EXISTS (SELECT 1 FROM publication_version_delete_guard WHERE id = 1)
BEGIN
  SELECT RAISE(ABORT, 'publication_versions are immutable');
END
"""

_COLUMN_DDL = (
    (
        "scenarios",
        "parent_publication_id",
        "ALTER TABLE scenarios ADD COLUMN parent_publication_id "
        "TEXT REFERENCES publications(id) ON DELETE SET NULL",
    ),
    (
        "scenarios",
        "parent_version",
        "ALTER TABLE scenarios ADD COLUMN parent_version INTEGER",
    ),
    (
        "adventures",
        "publication_id",
        "ALTER TABLE adventures ADD COLUMN publication_id "
        "TEXT REFERENCES publications(id) ON DELETE SET NULL",
    ),
    (
        "adventures",
        "publication_version",
        "ALTER TABLE adventures ADD COLUMN publication_version INTEGER",
    ),
)


def canonical_json(snapshot: dict) -> str:
    """JSON estável: chaves ordenadas, sem espaço, UTF-8."""
    return json.dumps(snapshot, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def content_hash(snapshot: dict) -> str:
    return hashlib.sha256(canonical_json(snapshot).encode("utf-8")).hexdigest()


def ensure_schema(conn) -> None:
    """Cria tabelas/colunas se o owner ainda não colou o DDL no schema.sql.

    O trigger não cabe na migration (o runner parte em ';') e é instalado aqui.
    """
    if not _has_table(conn, "publications"):
        db._apply_migration(conn, _MIGRATION)
    else:
        _ensure_columns(conn)
        _ensure_guard(conn)
    _ensure_triggers(conn)


def publish(conn, *, actor: str, kind: str, source_id: str,
            cover_image: str | None = None) -> dict:
    ensure_schema(conn)
    kind = (kind or "").strip()
    source_id = (source_id or "").strip()
    if kind not in _KINDS:
        raise GameError.bad_request("invalid_kind", "kind must be scenario or adventure")
    if not source_id:
        raise GameError.bad_request("invalid_source", "source_id is required")
    source, snapshot = _load_source(conn, kind, source_id)
    if not access.can_manage(actor, source.get("owner") or ""):
        raise GameError(403, "forbidden", "only the owner or admin can publish this")
    digest = content_hash(snapshot)
    canonical = canonical_json(snapshot)
    existing = conn.execute(
        "SELECT * FROM publications WHERE kind=? AND source_id=? AND owner=?",
        (kind, source_id, actor),
    ).fetchone()
    now = db.utcnow()
    if existing:
        same = conn.execute(
            "SELECT version FROM publication_versions "
            "WHERE publication_id=? AND content_hash=?",
            (existing["id"], digest),
        ).fetchone()
        if same:
            return get_publication(
                conn, existing["id"], viewer=actor,
                version=int(same["version"]), created=False,
            )
        version = conn.execute(
            "SELECT COALESCE(MAX(version), 0) + 1 AS n FROM publication_versions "
            "WHERE publication_id=?",
            (existing["id"],),
        ).fetchone()["n"]
        cover = _clean_cover(cover_image) if cover_image is not None else (existing["cover_image"] or "")
        conn.execute(
            "INSERT INTO publication_versions("
            "publication_id, version, snapshot, content_hash, cover_image, created_at) "
            "VALUES(?,?,?,?,?,?)",
            (existing["id"], int(version), canonical, digest, cover, now),
        )
        conn.execute(
            "UPDATE publications SET title=?, description=?, cover_image=?, tags=?, "
            "latest_version=?, updated_at=? WHERE id=?",
            (
                _title(snapshot.get("title")),
                _text(snapshot.get("description")),
                cover,
                json.dumps(snapshot.get("tags") or [], ensure_ascii=False),
                int(version),
                now,
                existing["id"],
            ),
        )
        _stamp_adventure(conn, kind, source_id, existing["id"], int(version))
        log.info("publications: %s version %s of %s", actor, version, existing["id"])
        return get_publication(
            conn, existing["id"], viewer=actor, version=int(version), created=True,
        )
    publication_id = db.new_id()
    cover = _clean_cover(cover_image or "")
    conn.execute(
        "INSERT INTO publications("
        "id, kind, source_id, owner, title, description, cover_image, tags, "
        "latest_version, created_at, updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        (
            publication_id, kind, source_id, actor,
            _title(snapshot.get("title")),
            _text(snapshot.get("description")),
            cover,
            json.dumps(snapshot.get("tags") or [], ensure_ascii=False),
            1, now, now,
        ),
    )
    conn.execute(
        "INSERT INTO publication_versions("
        "publication_id, version, snapshot, content_hash, cover_image, created_at) "
        "VALUES(?,?,?,?,?,?)",
        (publication_id, 1, canonical, digest, cover, now),
    )
    _stamp_adventure(conn, kind, source_id, publication_id, 1)
    log.info("publications: %s published %s %s as %s", actor, kind, source_id, publication_id)
    return get_publication(conn, publication_id, viewer=actor, version=1, created=True)


def list_publications(conn, *, q: str | None = None, tag: str | None = None,
                      sort: str = "new", viewer: str = "", limit: int = 100) -> list[dict]:
    ensure_schema(conn)
    if sort not in ("new", "likes"):
        raise GameError.bad_request("invalid_sort", "sort must be new or likes")
    limit = max(1, min(int(limit or 100), 200))
    rows = conn.execute(
        "SELECT p.id, p.kind, p.source_id, p.owner, p.title, p.description, "
        "p.cover_image, p.tags, p.latest_version, p.created_at, p.updated_at, "
        "(SELECT COUNT(*) FROM publication_likes l WHERE l.publication_id=p.id) AS like_count, "
        "(SELECT v.content_hash FROM publication_versions v "
        "  WHERE v.publication_id=p.id AND v.version=p.latest_version) AS content_hash, "
        "EXISTS(SELECT 1 FROM publication_likes l "
        "       WHERE l.publication_id=p.id AND l.username=?) AS liked "
        "FROM publications p",
        (viewer or "",),
    ).fetchall()
    items = [_summary(row) for row in rows]
    if tag and tag.strip():
        want = tag.strip()
        items = [item for item in items if want in item["tags"]]
    if q and q.strip():
        needle = q.strip().casefold()
        items = [
            item for item in items
            if needle in item["title"].casefold() or needle in item["description"].casefold()
        ]
    if sort == "likes":
        items.sort(key=lambda item: (item["like_count"], item["updated_at"] or "", item["id"]), reverse=True)
    else:
        items.sort(key=lambda item: (item["updated_at"] or "", item["id"]), reverse=True)
    return items[:limit]


def get_publication(conn, publication_id: str, *, viewer: str = "",
                    version: int | None = None, created: bool = False) -> dict:
    ensure_schema(conn)
    row = _require_publication(conn, publication_id)
    chosen = int(row["latest_version"] if version is None else version)
    if chosen < 1:
        raise GameError.bad_request("invalid_version", "version must be >= 1")
    vrow = conn.execute(
        "SELECT version, snapshot, content_hash, cover_image, created_at "
        "FROM publication_versions WHERE publication_id=? AND version=?",
        (publication_id, chosen),
    ).fetchone()
    if not vrow:
        raise GameError.not_found(f"publication {publication_id} version {chosen} not found")
    try:
        snapshot = json.loads(vrow["snapshot"])
    except (TypeError, ValueError) as exc:
        raise GameError.bad_request("invalid_snapshot", "stored snapshot is not valid JSON") from exc
    if not isinstance(snapshot, dict):
        raise GameError.bad_request("invalid_snapshot", "stored snapshot is not an object")
    versions = [
        {
            "version": int(item["version"]),
            "content_hash": item["content_hash"],
            "cover_image": item["cover_image"] or "",
            "created_at": item["created_at"],
        }
        for item in conn.execute(
            "SELECT version, content_hash, cover_image, created_at "
            "FROM publication_versions WHERE publication_id=? ORDER BY version DESC",
            (publication_id,),
        ).fetchall()
    ]
    likes = conn.execute(
        "SELECT COUNT(*) AS c FROM publication_likes WHERE publication_id=?",
        (publication_id,),
    ).fetchone()["c"]
    liked = conn.execute(
        "SELECT 1 FROM publication_likes WHERE publication_id=? AND username=?",
        (publication_id, viewer or ""),
    ).fetchone() is not None
    return {
        "id": row["id"],
        "kind": row["kind"],
        "source_id": row["source_id"],
        "owner": row["owner"] or "",
        "title": _text(snapshot.get("title")) or (row["title"] or ""),
        "description": _text(snapshot.get("description")),
        "cover_image": vrow["cover_image"] or "",
        "tags": list(snapshot.get("tags") or []),
        "latest_version": int(row["latest_version"]),
        "selected_version": int(vrow["version"]),
        "like_count": int(likes or 0),
        "liked": liked,
        "content_hash": vrow["content_hash"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "created": bool(created),
        "snapshot": snapshot,
        "versions": versions,
    }


def delete_publication(conn, publication_id: str, *, actor: str) -> None:
    ensure_schema(conn)
    row = _require_publication(conn, publication_id)
    if not access.can_manage(actor, row["owner"] or ""):
        raise GameError(403, "forbidden", "only the owner or admin can delete this publication")
    conn.execute(
        "UPDATE scenarios SET parent_publication_id=NULL, parent_version=NULL "
        "WHERE parent_publication_id=?",
        (publication_id,),
    )
    conn.execute(
        "UPDATE adventures SET publication_id=NULL, publication_version=NULL "
        "WHERE publication_id=?",
        (publication_id,),
    )
    conn.execute("DELETE FROM publication_likes WHERE publication_id=?", (publication_id,))
    conn.execute("DELETE FROM publication_version_delete_guard WHERE id=1")
    try:
        conn.execute(
            "DELETE FROM publication_versions WHERE publication_id=?",
            (publication_id,),
        )
    finally:
        conn.execute("INSERT OR IGNORE INTO publication_version_delete_guard(id) VALUES (1)")
    conn.execute("DELETE FROM publications WHERE id=?", (publication_id,))
    log.info("publications: %s deleted %s", actor, publication_id)


def like_publication(conn, publication_id: str, *, actor: str) -> dict:
    ensure_schema(conn)
    _require_publication(conn, publication_id)
    _require_actor(actor)
    conn.execute(
        "INSERT OR IGNORE INTO publication_likes(publication_id, username, created_at) "
        "VALUES(?,?,?)",
        (publication_id, actor, db.utcnow()),
    )
    return _like_state(conn, publication_id, actor)


def unlike_publication(conn, publication_id: str, *, actor: str) -> dict:
    ensure_schema(conn)
    _require_publication(conn, publication_id)
    _require_actor(actor)
    conn.execute(
        "DELETE FROM publication_likes WHERE publication_id=? AND username=?",
        (publication_id, actor),
    )
    return _like_state(conn, publication_id, actor)


def remix(conn, publication_id: str, *, actor: str,
          version: int | None = None, title: str | None = None) -> dict:
    """Copia a versão para um scenario do usuário e grava a linhagem."""
    ensure_schema(conn)
    _require_actor(actor)
    detail = get_publication(conn, publication_id, viewer=actor, version=version)
    snapshot = detail["snapshot"]
    chosen = int(detail["selected_version"])
    if title is None:
        new_title = _title(snapshot.get("title"))
    else:
        new_title = " ".join(str(title).split())
        if not new_title:
            raise GameError.bad_request("invalid_title", "title is required")
    try:
        scenario = db.insert_scenario(
            conn,
            title=new_title,
            description=_text(snapshot.get("description")),
            ai_instructions=_text(snapshot.get("ai_instructions")),
            plot_essentials=_text(snapshot.get("plot_essentials")),
            authors_note=_text(snapshot.get("authors_note")),
            opening_text=_text(snapshot.get("opening_text")),
            story_starts=list(snapshot.get("story_starts") or []),
            image_style=_text(snapshot.get("image_style")),
            tags=list(snapshot.get("tags") or []),
            story_cards=list(snapshot.get("story_cards") or []),
            third_person=bool(snapshot.get("third_person")),
            owner=actor,
        )
    except ValueError as exc:
        raise GameError.bad_request("invalid_snapshot", str(exc)) from exc
    conn.execute(
        "UPDATE scenarios SET parent_publication_id=?, parent_version=? WHERE id=?",
        (publication_id, chosen, scenario["id"]),
    )
    if _has_column(conn, "scenarios", "character_rules"):
        conn.execute(
            "UPDATE scenarios SET character_rules=? WHERE id=?",
            (_text(snapshot.get("character_rules")), scenario["id"]),
        )
    scenario = db.get_scenario(conn, scenario["id"]) or scenario
    log.info(
        "publications: %s remixed %s v%s into scenario %s",
        actor, publication_id, chosen, scenario["id"],
    )
    return {
        "scenario_id": scenario["id"],
        "parent_publication_id": publication_id,
        "parent_version": chosen,
        "title": scenario["title"],
        "scenario": scenario,
    }


# ---------------------------------------------------------------- snapshot


def _load_source(conn, kind: str, source_id: str) -> tuple[dict, dict]:
    if kind == "scenario":
        source = db.get_scenario(conn, source_id)
        if not source:
            raise GameError.not_found(f"scenario {source_id} not found")
        return source, _snapshot_scenario(source)
    source = db.get_adventure(conn, source_id)
    if not source:
        raise GameError.not_found(f"adventure {source_id} not found")
    return source, _snapshot_adventure(conn, source)


def _snapshot_scenario(source: dict) -> dict:
    starts = _starts(source.get("story_starts"))
    opening = starts[0]["text"] if starts else _text(source.get("opening_text")).strip()
    if not starts and opening:
        starts = [{"id": "opening", "title": "Opening", "text": opening}]
    return _pack(
        title=_text(source.get("title")).strip(),
        description=_text(source.get("description")),
        ai_instructions=_text(source.get("ai_instructions")),
        plot_essentials=_text(source.get("plot_essentials")),
        authors_note=_text(source.get("authors_note")),
        opening_text=opening,
        story_starts=starts,
        image_style=_text(source.get("image_style")),
        tags=_tags(source.get("tags")),
        third_person=bool(source.get("third_person")),
        character_rules=_text(source.get("character_rules")),
        story_cards=_public_cards(source.get("story_cards") or []),
        links=[],
        scripts={hook: "" for hook in SCRIPT_HOOKS},
    )


def _snapshot_adventure(conn, source: dict) -> dict:
    adventure_id = source["id"]
    opening = _opening_text(conn, adventure_id)
    starts = [{"id": "opening", "title": "Opening", "text": opening}] if opening else []
    cards, id_to_key = _adventure_cards(conn, adventure_id)
    return _pack(
        title=_text(source.get("title")).strip(),
        description=_text(source.get("description")),
        ai_instructions=_text(source.get("ai_instructions")),
        plot_essentials=_text(source.get("plot_essentials")),
        authors_note=_text(source.get("authors_note")),
        opening_text=opening,
        story_starts=starts,
        image_style=_text(source.get("image_style")),
        tags=_tags(source.get("tags")),
        third_person=bool(source.get("third_person")),
        character_rules=_text(source.get("character_rules")),
        story_cards=cards,
        links=_adventure_links(conn, adventure_id, id_to_key),
        scripts=_adventure_scripts(conn, adventure_id),
    )


def _pack(*, title, description, ai_instructions, plot_essentials, authors_note,
          opening_text, story_starts, image_style, tags, third_person,
          character_rules, story_cards, links, scripts) -> dict:
    return {
        "title": title,
        "description": description,
        "ai_instructions": ai_instructions,
        "plot_essentials": plot_essentials,
        "authors_note": authors_note,
        "opening_text": opening_text,
        "story_starts": story_starts,
        "image_style": image_style,
        "tags": tags,
        "third_person": bool(third_person),
        "character_rules": character_rules,
        "story_cards": story_cards,
        "links": links,
        "scripts": {hook: _text(scripts.get(hook)) for hook in SCRIPT_HOOKS},
    }


def _public_cards(raw) -> list[dict]:
    cards, _ = _collect_cards(raw if isinstance(raw, list) else [])
    return cards


def _adventure_cards(conn, adventure_id: str) -> tuple[list[dict], dict[str, str]]:
    return _collect_cards(db.list_cards(conn, adventure_id), keep_ids=True)


def _collect_cards(raw: list, *, keep_ids: bool = False) -> tuple[list[dict], dict[str, str]]:
    cards: list[dict] = []
    id_to_key: dict[str, str] = {}
    used: set[str] = set()
    for item in raw:
        if not isinstance(item, dict) or _is_player_card(item):
            continue
        key = _card_key(item, len(cards), used)
        if keep_ids and item.get("id"):
            id_to_key[str(item["id"])] = key
        cards.append(_public_card(item, key))
    return cards, id_to_key


def _public_card(card: dict, key: str) -> dict:
    card_type = str(card.get("type") or "").strip() or "other"
    return {
        "key": key,
        "title": str(card.get("title") or ""),
        "type": card_type,
        "triggers": _triggers(card.get("triggers")),
        "entry": str(card.get("entry") or ""),
        "pinned": bool(card.get("pinned")),
        "use_for_character_creation": bool(card.get("use_for_character_creation")),
        "sheet": _sheet(card.get("sheet")),
    }


def _is_player_card(card: dict) -> bool:
    if str(card.get("player_username") or "").strip():
        return True
    return str(card.get("type") or "").strip().casefold() == "player"


def _card_key(card: dict, index: int, used: set[str]) -> str:
    explicit = str(card.get("key") or "").strip()
    if explicit:
        base = explicit[:80]
    else:
        base = _slug(str(card.get("title") or "")) or f"card-{index + 1}"
    key = base
    n = 2
    while key in used:
        suffix = f"-{n}"
        key = f"{base[:80 - len(suffix)]}{suffix}"
        n += 1
    used.add(key)
    return key


def _slug(value: str) -> str:
    return _SLUG.sub("-", value.casefold()).strip("-")[:60]


def _adventure_links(conn, adventure_id: str, id_to_key: dict[str, str]) -> list[dict]:
    if not _has_table(conn, "location_links"):
        return []
    # Publicação leva só rota já descoberta. travel_text de discovered=0 não sai.
    discovered_sql = ""
    if _has_column(conn, "location_links", "discovered"):
        discovered_sql = " AND discovered=1"
    rows = conn.execute(
        "SELECT from_card_id, to_card_id, travel_text, travel_cost, bidirectional "
        f"FROM location_links WHERE adventure_id=?{discovered_sql}",
        (adventure_id,),
    ).fetchall()
    links = []
    for row in rows:
        from_key = id_to_key.get(str(row["from_card_id"]))
        to_key = id_to_key.get(str(row["to_card_id"]))
        if not from_key or not to_key:
            continue
        links.append({
            "from_key": from_key,
            "to_key": to_key,
            "travel_text": str(row["travel_text"] or ""),
            "travel_cost": str(row["travel_cost"] or ""),
            "bidirectional": bool(row["bidirectional"]),
            "discovered": True,
        })
    links.sort(key=lambda item: (
        item["from_key"], item["to_key"], item["travel_text"], item["travel_cost"],
    ))
    return links


def _adventure_scripts(conn, adventure_id: str) -> dict:
    scripts = {}
    for hook in SCRIPT_HOOKS:
        value = db.get_setting(conn, f"script:{adventure_id}:{hook}")
        scripts[hook] = value if isinstance(value, str) else ""
    return scripts


def _opening_text(conn, adventure_id: str) -> str:
    row = conn.execute(
        "SELECT text FROM actions WHERE adventure_id=? AND type='start' "
        "ORDER BY seq ASC LIMIT 1",
        (adventure_id,),
    ).fetchone()
    if not row:
        return ""
    return str(row["text"] or "").strip()


def _stamp_adventure(conn, kind: str, source_id: str, publication_id: str, version: int) -> None:
    if kind != "adventure" or not _has_column(conn, "adventures", "publication_id"):
        return
    conn.execute(
        "UPDATE adventures SET publication_id=?, publication_version=? WHERE id=?",
        (publication_id, version, source_id),
    )


# ---------------------------------------------------------------- rows


def _summary(row) -> dict:
    return {
        "id": row["id"],
        "kind": row["kind"],
        "source_id": row["source_id"],
        "owner": row["owner"] or "",
        "title": row["title"] or "",
        "description": row["description"] or "",
        "cover_image": row["cover_image"] or "",
        "tags": _parse_tags(row["tags"]),
        "latest_version": int(row["latest_version"]),
        "selected_version": int(row["latest_version"]),
        "like_count": int(row["like_count"] or 0),
        "liked": bool(row["liked"]),
        "content_hash": row["content_hash"] or "",
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def _require_publication(conn, publication_id: str):
    row = conn.execute(
        "SELECT * FROM publications WHERE id=?",
        (publication_id,),
    ).fetchone()
    if not row:
        raise GameError.not_found(f"publication {publication_id} not found")
    return row


def _like_state(conn, publication_id: str, actor: str) -> dict:
    count = conn.execute(
        "SELECT COUNT(*) AS c FROM publication_likes WHERE publication_id=?",
        (publication_id,),
    ).fetchone()["c"]
    liked = conn.execute(
        "SELECT 1 FROM publication_likes WHERE publication_id=? AND username=?",
        (publication_id, actor),
    ).fetchone() is not None
    return {"publication_id": publication_id, "liked": liked, "likes": int(count or 0)}


def _require_actor(actor: str) -> None:
    if not str(actor or "").strip():
        raise GameError(401, "unauthorized", "missing or invalid bearer token")


def _starts(raw) -> list[dict]:
    if not isinstance(raw, list):
        return []
    out = []
    seen: set[str] = set()
    for item in raw:
        if not isinstance(item, dict):
            continue
        title = str(item.get("title") or "").strip()
        text = str(item.get("text") or "").strip()
        sid = str(item.get("id") or "").strip()
        if not title or not text or not sid or sid in seen:
            continue
        seen.add(sid)
        out.append({"id": sid, "title": title, "text": text})
    return out


def _triggers(value) -> list[str]:
    if isinstance(value, str):
        value = db._loads(value, [])
    if not isinstance(value, list):
        return []
    return [str(item) for item in value if str(item).strip()]


def _tags(value) -> list[str]:
    if isinstance(value, str):
        value = db._loads(value, [])
    if not isinstance(value, list):
        return []
    out: list[str] = []
    seen: set[str] = set()
    for item in value:
        tag = " ".join(str(item).split())
        if not tag:
            continue
        marker = tag.casefold()
        if marker in seen:
            continue
        seen.add(marker)
        out.append(tag)
    return out


def _parse_tags(raw) -> list[str]:
    data = db._loads(raw, [])
    if not isinstance(data, list):
        return []
    return [str(item) for item in data if str(item).strip()]


def _sheet(value) -> str:
    if value is None:
        return ""
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return str(value)


def _text(value) -> str:
    if value is None:
        return ""
    return str(value)


def _title(value) -> str:
    text = _text(value).strip()
    return text or "Untitled"


def _clean_cover(value: str) -> str:
    text = "".join(ch for ch in str(value or "") if ch >= " " or ch == "\t").strip()
    if len(text) > 2000:
        raise GameError.bad_request("invalid_cover", "cover_image is too long")
    return text


def _has_table(conn, name: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (name,),
    ).fetchone() is not None


def _has_column(conn, table: str, column: str) -> bool:
    if not _has_table(conn, table):
        return False
    return column in {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}


def _has_trigger(conn, name: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='trigger' AND name=?",
        (name,),
    ).fetchone() is not None


def _ensure_columns(conn) -> None:
    if not _has_table(conn, "publications"):
        return
    for table, column, ddl in _COLUMN_DDL:
        if _has_table(conn, table) and not _has_column(conn, table, column):
            conn.execute(ddl)


def _ensure_guard(conn) -> None:
    conn.execute(
        "CREATE TABLE IF NOT EXISTS publication_version_delete_guard("
        "id INTEGER PRIMARY KEY CHECK (id = 1))"
    )
    conn.execute("INSERT OR IGNORE INTO publication_version_delete_guard(id) VALUES (1)")


def _ensure_triggers(conn) -> None:
    _ensure_guard(conn)
    if not _has_trigger(conn, "publication_versions_immutable"):
        conn.execute(_UPDATE_TRIGGER)
    if not _has_trigger(conn, "publication_versions_no_delete"):
        conn.execute(_DELETE_TRIGGER)
