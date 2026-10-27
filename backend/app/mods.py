"""Catálogo de mods, versões imutáveis e aplicação seletiva numa aventura.

O pacote JSON guarda cards, links, append (ai_instructions, plot_essentials,
authors_note) e os três hooks de script. Aplicar só insere cards e links
novos, marcados com a versão de origem, e a linha em adventure_mods.
Não atualiza card existente, action, texto da aventura, sim_state nem map_state.

Append e scripts entram na composição só enquanto enabled=1. Desativar não
apaga conteúdo. Remover apaga só as linhas cuja origem é aquela versão.

Bancos novos marcam a migration 038 sem rodá-la (o DDL também precisa estar
no schema.sql). Este módulo não cria tabela sozinho.
"""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
from typing import Any

from app import db, scripting
from app.errors import GameError
from app.users import is_admin

log = logging.getLogger("aidungeon.mods")

APPEND_FIELDS = ("ai_instructions", "plot_essentials", "authors_note")
VISIBILITIES = ("public", "private")

_MAX_CARDS = 60
_MAX_LINKS = 120
_MAX_PACKAGE_CHARS = 200_000
_MAX_TEXT = 8_000
_MAX_SCRIPT = 20_000
_MAX_TITLE = 120
_MAX_DESCRIPTION = 2_000

_GUARD_UPDATE = """
CREATE TRIGGER IF NOT EXISTS mod_versions_no_update
BEFORE UPDATE ON mod_versions
BEGIN
  SELECT RAISE(ABORT, 'mod_versions are immutable');
END
"""

_GUARD_DELETE = """
CREATE TRIGGER IF NOT EXISTS mod_versions_no_delete
BEFORE DELETE ON mod_versions
BEGIN
  SELECT RAISE(ABORT, 'mod_versions are immutable');
END
"""


def install_version_guards(conn: sqlite3.Connection) -> None:
    """Impede UPDATE e DELETE em mod_versions. Idempotente.

    CREATE TRIGGER commita a transação aberta no SQLite. Chamar no início
    do session, antes de inserir conteúdo.
    """
    conn.execute(_GUARD_UPDATE)
    conn.execute(_GUARD_DELETE)


def schema_ready(conn: sqlite3.Connection) -> bool:
    try:
        names = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name IN ('mods', 'mod_versions', 'adventure_mods')"
            )
        }
        if names != {"mods", "mod_versions", "adventure_mods"}:
            return False
        card_cols = {row[1] for row in conn.execute("PRAGMA table_info(story_cards)")}
        link_cols = {row[1] for row in conn.execute("PRAGMA table_info(location_links)")}
    except sqlite3.Error:
        return False
    return "origin_mod_version_id" in card_cols and "origin_mod_version_id" in link_cols


def require_schema(conn: sqlite3.Connection) -> None:
    if not schema_ready(conn):
        raise GameError(
            500,
            "schema_missing",
            "mods schema is not installed (migration 038 and schema.sql)",
        )
    install_version_guards(conn)


def canonical_json(package: dict) -> str:
    return json.dumps(package, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def content_hash(package: dict) -> str:
    return hashlib.sha256(canonical_json(package).encode("utf-8")).hexdigest()


def _stable_id(kind: str, adventure_id: str, version_id: str, key: str) -> str:
    raw = f"{kind}\0{adventure_id}\0{version_id}\0{key}".encode()
    return hashlib.sha256(raw).hexdigest()[:32]


def _as_bool(value: Any, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and value in (0, 1):
        return bool(value)
    raise ValueError("expected a boolean")


def _clean_text(value: Any, field: str, limit: int, *, required: bool = False) -> str:
    if value is None:
        value = ""
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a string")
    if field in ("title", "description", "key", "from_key", "to_key", "type"):
        text = value.strip()
    else:
        text = value
    if required and not str(value).strip():
        raise ValueError(f"{field} is required")
    if len(text) > limit:
        raise ValueError(f"{field} is too long")
    return text


def normalize_package(raw: Any) -> dict:
    """Pacote canônico. Chaves desconhecidas (actions, sim, map) são descartadas."""
    if not isinstance(raw, dict):
        raise ValueError("package must be an object")
    cards_in = raw.get("cards") or []
    links_in = raw.get("links") or []
    append_in = raw.get("append") or {}
    scripts_in = raw.get("scripts") or {}
    if not isinstance(cards_in, list) or not isinstance(links_in, list):
        raise ValueError("cards and links must be arrays")
    if not isinstance(append_in, dict) or not isinstance(scripts_in, dict):
        raise ValueError("append and scripts must be objects")
    if len(cards_in) > _MAX_CARDS:
        raise ValueError("too many cards")
    if len(links_in) > _MAX_LINKS:
        raise ValueError("too many links")

    cards: list[dict] = []
    keys: set[str] = set()
    for index, card in enumerate(cards_in):
        if not isinstance(card, dict):
            raise ValueError(f"cards[{index}] must be an object")
        key = _clean_text(card.get("key"), "key", 64, required=True)
        if any(ord(ch) < 32 for ch in key):
            raise ValueError("key must not contain control characters")
        if key in keys:
            raise ValueError(f"duplicate card key {key}")
        keys.add(key)
        triggers_in = card.get("triggers") or []
        if not isinstance(triggers_in, list):
            raise ValueError(f"cards[{index}].triggers must be an array")
        if len(triggers_in) > 32:
            raise ValueError("too many triggers")
        triggers: list[str] = []
        for trigger in triggers_in:
            if not isinstance(trigger, str):
                raise ValueError("triggers must be strings")
            item = trigger.strip()
            if not item:
                continue
            if len(item) > 80:
                raise ValueError("trigger is too long")
            triggers.append(item)
        card_type = _clean_text(card.get("type") or "other", "type", 40).strip() or "other"
        cards.append({
            "key": key,
            "title": _clean_text(card.get("title") or "", "title", 200).strip(),
            "type": card_type,
            "triggers": triggers,
            "entry": _clean_text(card.get("entry") or "", "entry", _MAX_TEXT),
            "pinned": _as_bool(card.get("pinned"), False),
        })

    links: list[dict] = []
    seen_pairs: set[tuple[str, str]] = set()
    for index, link in enumerate(links_in):
        if not isinstance(link, dict):
            raise ValueError(f"links[{index}] must be an object")
        from_key = _clean_text(link.get("from_key"), "from_key", 64, required=True)
        to_key = _clean_text(link.get("to_key"), "to_key", 64, required=True)
        if from_key not in keys or to_key not in keys:
            raise ValueError("link keys must reference cards in the package")
        if from_key == to_key:
            raise ValueError("link cannot point at the same card")
        pair = (from_key, to_key)
        if pair in seen_pairs:
            raise ValueError("duplicate link")
        seen_pairs.add(pair)
        links.append({
            "from_key": from_key,
            "to_key": to_key,
            "travel_text": _clean_text(link.get("travel_text") or "", "travel_text", 500),
            "travel_cost": _clean_text(link.get("travel_cost") or "", "travel_cost", 80),
            "bidirectional": _as_bool(link.get("bidirectional"), True),
            "discovered": _as_bool(link.get("discovered"), False),
        })

    append: dict[str, str] = {}
    for field in APPEND_FIELDS:
        append[field] = _clean_text(append_in.get(field) or "", field, _MAX_TEXT)

    scripts: dict[str, str] = {}
    for hook in scripting.SCRIPT_HOOKS:
        script = _clean_text(scripts_in.get(hook) or "", hook, _MAX_SCRIPT)
        if script.strip():
            scripting.validate_script_ast(script)
        scripts[hook] = script

    package = {"cards": cards, "links": links, "append": append, "scripts": scripts}
    if not cards and not links and not any(append.values()) and not any(s.strip() for s in scripts.values()):
        raise ValueError("package is empty")
    encoded = canonical_json(package)
    if len(encoded) > _MAX_PACKAGE_CHARS:
        raise ValueError("package is too large")
    return package


def _clean_title(title: str) -> str:
    text = _clean_text(title, "title", _MAX_TITLE, required=True).strip()
    return text


def _clean_visibility(value: str) -> str:
    visibility = str(value or "public").strip().lower()
    if visibility not in VISIBILITIES:
        raise ValueError("visibility must be public or private")
    return visibility


def _clean_description(value: str) -> str:
    return _clean_text(value or "", "description", _MAX_DESCRIPTION).strip()


def _loads_package(raw: str | None) -> dict | None:
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _can_read(row: sqlite3.Row | dict, username: str) -> bool:
    if is_admin(username):
        return True
    owner = row["owner"] or ""
    if owner == username:
        return True
    return (row["visibility"] or "") == "public"


def _can_edit(row: sqlite3.Row | dict, username: str) -> bool:
    return is_admin(username) or (row["owner"] or "") == username


def _version_summary(row: sqlite3.Row) -> dict:
    return {
        "id": row["id"],
        "version": int(row["version"]),
        "content_hash": row["content_hash"],
        "created_at": row["created_at"],
    }


def _version_detail(row: sqlite3.Row) -> dict:
    detail = _version_summary(row)
    detail["mod_id"] = row["mod_id"]
    detail["package"] = _loads_package(row["package_json"]) or {}
    return detail


def _mod_dict(row: sqlite3.Row, versions: list[dict]) -> dict:
    return {
        "id": row["id"],
        "owner": row["owner"] or "",
        "title": row["title"] or "",
        "description": row["description"] or "",
        "visibility": row["visibility"] or "public",
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "versions": versions,
    }


def _versions_for(conn: sqlite3.Connection, mod_id: str) -> list[dict]:
    rows = conn.execute(
        "SELECT id, version, content_hash, created_at FROM mod_versions "
        "WHERE mod_id=? ORDER BY version ASC",
        (mod_id,),
    ).fetchall()
    return [_version_summary(row) for row in rows]


def _mod_or_none(conn: sqlite3.Connection, mod_id: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM mods WHERE id=?", (mod_id,)).fetchone()


def publish_mod(conn: sqlite3.Connection, owner: str, *, title: str,
                description: str = "", visibility: str = "public",
                package: Any) -> dict:
    require_schema(conn)
    normalized = normalize_package(package)
    now = db.utcnow()
    mod_id = db.new_id()
    version_id = db.new_id()
    digest = content_hash(normalized)
    conn.execute(
        "INSERT INTO mods(id, owner, title, description, visibility, created_at, updated_at) "
        "VALUES(?,?,?,?,?,?,?)",
        (mod_id, owner or "", _clean_title(title), _clean_description(description),
         _clean_visibility(visibility), now, now),
    )
    conn.execute(
        "INSERT INTO mod_versions(id, mod_id, version, content_hash, package_json, created_at) "
        "VALUES(?,?,?,?,?,?)",
        (version_id, mod_id, 1, digest, canonical_json(normalized), now),
    )
    mod = _mod_or_none(conn, mod_id)
    version = conn.execute("SELECT * FROM mod_versions WHERE id=?", (version_id,)).fetchone()
    return {"mod": _mod_dict(mod, [_version_summary(version)]), "version": _version_detail(version)}


def publish_version(conn: sqlite3.Connection, username: str, mod_id: str, package: Any) -> dict:
    require_schema(conn)
    mod = _mod_or_none(conn, mod_id)
    if mod is None or not _can_read(mod, username):
        raise GameError.not_found("mod not found")
    if not _can_edit(mod, username):
        raise GameError(403, "forbidden", "only the mod owner can publish a version")
    normalized = normalize_package(package)
    current = conn.execute(
        "SELECT COALESCE(MAX(version), 0) FROM mod_versions WHERE mod_id=?",
        (mod_id,),
    ).fetchone()[0]
    now = db.utcnow()
    version_id = db.new_id()
    conn.execute(
        "INSERT INTO mod_versions(id, mod_id, version, content_hash, package_json, created_at) "
        "VALUES(?,?,?,?,?,?)",
        (version_id, mod_id, int(current) + 1, content_hash(normalized),
         canonical_json(normalized), now),
    )
    conn.execute("UPDATE mods SET updated_at=? WHERE id=?", (now, mod_id))
    version = conn.execute("SELECT * FROM mod_versions WHERE id=?", (version_id,)).fetchone()
    fresh = _mod_or_none(conn, mod_id)
    return {"mod": _mod_dict(fresh, _versions_for(conn, mod_id)), "version": _version_detail(version)}


def list_mods(conn: sqlite3.Connection, username: str, q: str = "") -> dict:
    require_schema(conn)
    query = "SELECT * FROM mods WHERE (visibility='public' OR owner=? OR ?=1)"
    params: list[Any] = [username or "", 1 if is_admin(username) else 0]
    term = (q or "").strip()[:80]
    if term:
        escaped = term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        query += " AND (title LIKE ? ESCAPE '\\' OR description LIKE ? ESCAPE '\\')"
        like = f"%{escaped}%"
        params.extend([like, like])
    query += " ORDER BY updated_at DESC, id ASC LIMIT 40"
    mods = []
    for row in conn.execute(query, params):
        mods.append(_mod_dict(row, _versions_for(conn, row["id"])))
    return {"mods": mods}


def get_mod(conn: sqlite3.Connection, username: str, mod_id: str) -> dict:
    require_schema(conn)
    mod = _mod_or_none(conn, mod_id)
    if mod is None or not _can_read(mod, username):
        raise GameError.not_found("mod not found")
    return _mod_dict(mod, _versions_for(conn, mod_id))


def get_version(conn: sqlite3.Connection, username: str, mod_id: str, version_id: str) -> dict:
    require_schema(conn)
    row = conn.execute(
        "SELECT v.*, m.owner, m.visibility FROM mod_versions v "
        "JOIN mods m ON m.id=v.mod_id WHERE v.id=? AND v.mod_id=?",
        (version_id, mod_id),
    ).fetchone()
    if row is None or not _can_read(row, username):
        raise GameError.not_found("mod version not found")
    return _version_detail(row)


def _load_readable_version(conn: sqlite3.Connection, version_id: str, username: str) -> sqlite3.Row:
    row = conn.execute(
        "SELECT v.*, m.owner, m.visibility, m.title, m.description "
        "FROM mod_versions v JOIN mods m ON m.id=v.mod_id WHERE v.id=?",
        (version_id,),
    ).fetchone()
    if row is None or not _can_read(row, username):
        raise GameError.not_found("mod version not found")
    return row


def _insert_origin_card(conn: sqlite3.Connection, adventure_id: str, version_id: str,
                        card: dict, card_id: str) -> None:
    now = db.utcnow()
    conn.execute(
        "INSERT INTO story_cards("
        "id, adventure_id, title, type, triggers, entry, pinned,"
        " use_for_character_creation, portrait_prompt, voice, vrm_id, sheet,"
        " player_username, created_at, updated_at, origin_mod_version_id"
        ") VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            card_id, adventure_id, card["title"], card["type"],
            json.dumps(card["triggers"], ensure_ascii=False), card["entry"],
            int(card["pinned"]), 0, "", "", "", "", "", now, now, version_id,
        ),
    )


def _insert_origin_link(conn: sqlite3.Connection, adventure_id: str, version_id: str,
                        link: dict, link_id: str, from_id: str, to_id: str) -> None:
    conn.execute(
        "INSERT INTO location_links("
        "id, adventure_id, from_card_id, to_card_id, travel_text, travel_cost,"
        " bidirectional, discovered, created_at, origin_mod_version_id"
        ") VALUES(?,?,?,?,?,?,?,?,?,?)",
        (
            link_id, adventure_id, from_id, to_id, link["travel_text"], link["travel_cost"],
            int(link["bidirectional"]), int(bool(link.get("discovered", False))),
            db.utcnow(), version_id,
        ),
    )


def apply_version(conn: sqlite3.Connection, adventure_id: str, version_id: str,
                  username: str) -> dict:
    """Insere só cards/links que ainda não existem. Não reescreve nada do save."""
    require_schema(conn)
    version = _load_readable_version(conn, version_id, username)
    package = _loads_package(version["package_json"])
    if package is None:
        raise GameError.bad_request("invalid_package", "stored package is unreadable")

    key_to_id: dict[str, str | None] = {}
    inserted_cards = 0
    for card in package.get("cards") or []:
        if not isinstance(card, dict) or not card.get("key"):
            continue
        card_id = _stable_id("card", adventure_id, version_id, card["key"])
        existing = conn.execute(
            "SELECT id, adventure_id, origin_mod_version_id FROM story_cards WHERE id=?",
            (card_id,),
        ).fetchone()
        if existing is not None:
            same_origin = (
                existing["adventure_id"] == adventure_id
                and existing["origin_mod_version_id"] == version_id
            )
            key_to_id[card["key"]] = card_id if same_origin else None
            continue
        _insert_origin_card(conn, adventure_id, version_id, card, card_id)
        key_to_id[card["key"]] = card_id
        inserted_cards += 1

    inserted_links = 0
    for link in package.get("links") or []:
        if not isinstance(link, dict):
            continue
        from_id = key_to_id.get(link.get("from_key"))
        to_id = key_to_id.get(link.get("to_key"))
        if not from_id or not to_id:
            continue
        link_id = _stable_id(
            "link", adventure_id, version_id, f"{link['from_key']}->{link['to_key']}",
        )
        if conn.execute("SELECT 1 FROM location_links WHERE id=?", (link_id,)).fetchone():
            continue
        pair = conn.execute(
            "SELECT 1 FROM location_links WHERE adventure_id=? AND from_card_id=? AND to_card_id=?",
            (adventure_id, from_id, to_id),
        ).fetchone()
        if pair is not None:
            continue
        try:
            _insert_origin_link(conn, adventure_id, version_id, link, link_id, from_id, to_id)
        except sqlite3.IntegrityError:
            log.warning("skipped mod link %s on adventure %s", link_id, adventure_id)
            continue
        inserted_links += 1

    existing_apply = conn.execute(
        "SELECT enabled FROM adventure_mods WHERE adventure_id=? AND mod_version_id=?",
        (adventure_id, version_id),
    ).fetchone()
    already = existing_apply is not None
    if existing_apply is None:
        conn.execute(
            "INSERT INTO adventure_mods(adventure_id, mod_version_id, enabled, applied_at) "
            "VALUES(?,?,1,?)",
            (adventure_id, version_id, db.utcnow()),
        )
        enabled = True
    else:
        enabled = bool(existing_apply["enabled"])
    view = _applied_view(conn, adventure_id, version_id)
    view.update({
        "inserted_cards": inserted_cards,
        "inserted_links": inserted_links,
        "already_applied": already,
        "enabled": enabled,
    })
    return view


def _applied_view(conn: sqlite3.Connection, adventure_id: str, version_id: str) -> dict:
    row = conn.execute(
        "SELECT am.adventure_id, am.mod_version_id, am.enabled, am.applied_at, "
        "v.mod_id, v.version, v.content_hash, m.title, m.description, m.owner "
        "FROM adventure_mods am "
        "JOIN mod_versions v ON v.id=am.mod_version_id "
        "JOIN mods m ON m.id=v.mod_id "
        "WHERE am.adventure_id=? AND am.mod_version_id=?",
        (adventure_id, version_id),
    ).fetchone()
    if row is None:
        raise GameError.not_found("applied mod not found")
    return {
        "adventure_id": row["adventure_id"],
        "mod_id": row["mod_id"],
        "mod_version_id": row["mod_version_id"],
        "version": int(row["version"]),
        "title": row["title"] or "",
        "description": row["description"] or "",
        "owner": row["owner"] or "",
        "content_hash": row["content_hash"],
        "enabled": bool(row["enabled"]),
        "applied_at": row["applied_at"],
    }


def list_applied(conn: sqlite3.Connection, adventure_id: str) -> dict:
    require_schema(conn)
    rows = conn.execute(
        "SELECT am.mod_version_id FROM adventure_mods am "
        "WHERE am.adventure_id=? ORDER BY am.applied_at ASC, am.rowid ASC",
        (adventure_id,),
    ).fetchall()
    return {"mods": [_applied_view(conn, adventure_id, row["mod_version_id"]) for row in rows]}


def set_enabled(conn: sqlite3.Connection, adventure_id: str, version_id: str,
                enabled: bool) -> dict:
    require_schema(conn)
    row = conn.execute(
        "SELECT 1 FROM adventure_mods WHERE adventure_id=? AND mod_version_id=?",
        (adventure_id, version_id),
    ).fetchone()
    if row is None:
        raise GameError.not_found("applied mod not found")
    conn.execute(
        "UPDATE adventure_mods SET enabled=? WHERE adventure_id=? AND mod_version_id=?",
        (1 if enabled else 0, adventure_id, version_id),
    )
    return _applied_view(conn, adventure_id, version_id)


def remove_applied(conn: sqlite3.Connection, adventure_id: str, version_id: str) -> dict:
    """Apaga a aplicação e só cards/links com esta origem. O catálogo fica."""
    require_schema(conn)
    row = conn.execute(
        "SELECT 1 FROM adventure_mods WHERE adventure_id=? AND mod_version_id=?",
        (adventure_id, version_id),
    ).fetchone()
    if row is None:
        raise GameError.not_found("applied mod not found")
    deleted_links = conn.execute(
        "DELETE FROM location_links WHERE adventure_id=? AND origin_mod_version_id=?",
        (adventure_id, version_id),
    ).rowcount
    deleted_cards = conn.execute(
        "DELETE FROM story_cards WHERE adventure_id=? AND origin_mod_version_id=?",
        (adventure_id, version_id),
    ).rowcount
    conn.execute(
        "DELETE FROM adventure_mods WHERE adventure_id=? AND mod_version_id=?",
        (adventure_id, version_id),
    )
    return {
        "removed": True,
        "adventure_id": adventure_id,
        "mod_version_id": version_id,
        "deleted_cards": max(int(deleted_cards), 0),
        "deleted_links": max(int(deleted_links), 0),
    }


def _enabled_packages(conn: sqlite3.Connection, adventure_id: str) -> list[dict]:
    if not adventure_id or not schema_ready(conn):
        return []
    rows = conn.execute(
        "SELECT v.package_json FROM adventure_mods am "
        "JOIN mod_versions v ON v.id=am.mod_version_id "
        "WHERE am.adventure_id=? AND am.enabled=1 "
        "ORDER BY am.applied_at ASC, am.rowid ASC",
        (adventure_id,),
    ).fetchall()
    packages = []
    for row in rows:
        package = _loads_package(row["package_json"])
        if package is not None:
            packages.append(package)
    return packages


def enabled_appends(conn: sqlite3.Connection, adventure_id: str) -> dict[str, str]:
    """Texto de append dos mods habilitados, na ordem de aplicação. Não grava."""
    grouped = {field: [] for field in APPEND_FIELDS}
    for package in _enabled_packages(conn, adventure_id):
        append = package.get("append") or {}
        if not isinstance(append, dict):
            continue
        for field in APPEND_FIELDS:
            text = append.get(field) or ""
            if isinstance(text, str) and text.strip():
                grouped[field].append(text.strip())
    return {field: "\n\n".join(parts) for field, parts in grouped.items()}


def enabled_hook_scripts(conn: sqlite3.Connection, adventure_id: str, hook: str) -> list[str]:
    """Scripts do hook só dos mods habilitados, na ordem de aplicação."""
    if hook not in scripting.SCRIPT_HOOKS:
        return []
    scripts: list[str] = []
    for package in _enabled_packages(conn, adventure_id):
        block = package.get("scripts") or {}
        if not isinstance(block, dict):
            continue
        script = block.get(hook) or ""
        if isinstance(script, str) and script.strip():
            scripts.append(script)
    return scripts
