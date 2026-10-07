"""Camada de dados — SQLite (stdlib sqlite3) em data/aidungeon.db.

- Aplica `backend/schema.sql` (PLANO.md §4.1) na primeira execução.
- `row_factory = sqlite3.Row` e helpers de serialização que convertem:
  - `tags`/`triggers` (TEXT JSON no banco) → arrays;
  - booleans (INTEGER 0/1) → `true/false`.
- Caminho do banco: env `AIDUNGEON_DB_PATH` (testes) ou `<repo>/data/aidungeon.db`.
"""

from __future__ import annotations

import json
import os
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

REPO_ROOT = Path(__file__).resolve().parents[2]
SCHEMA_PATH = Path(__file__).resolve().parents[1] / "schema.sql"
DEFAULT_DB_PATH = REPO_ROOT / "data" / "aidungeon.db"

PLAYER_TYPES = ("do", "say", "story")
ACTION_TYPES = ("start", "continue", "do", "say", "story", "see")


# --------------------------------------------------------------------- base
def db_path() -> str:
    return os.environ.get("AIDUNGEON_DB_PATH") or str(DEFAULT_DB_PATH)


def utcnow() -> str:
    """ISO-8601 UTC com milissegundos (mesmo estilo do backup: '...Z')."""
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def new_id() -> str:
    return uuid.uuid4().hex


def connect() -> sqlite3.Connection:
    path = db_path()
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


@contextmanager
def session() -> Iterator[sqlite3.Connection]:
    """Contexto de escrita/leitura: commit no sucesso, rollback na exceção."""
    conn = connect()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db() -> None:
    with connect() as conn:
        conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))


# ----------------------------------------------------------- serialização
def _loads(raw: Any, default: Any) -> Any:
    if raw is None or raw == "":
        return default
    if isinstance(raw, (list, dict)):
        return raw
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return default


def _dumps(value: Any, default: Any) -> str:
    if value is None:
        value = default
    return json.dumps(value, ensure_ascii=False)


def scenario_to_dict(row: sqlite3.Row) -> dict:
    return {
        "id": row["id"],
        "title": row["title"],
        "description": row["description"] or "",
        "ai_instructions": row["ai_instructions"] or "",
        "plot_essentials": row["plot_essentials"] or "",
        "authors_note": row["authors_note"] or "",
        "opening_text": row["opening_text"] or "",
        "tags": _loads(row["tags"], []),
        "third_person": bool(row["third_person"]),
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def adventure_to_dict(row: sqlite3.Row) -> dict:
    return {
        "id": row["id"],
        "scenario_id": row["scenario_id"],
        "title": row["title"],
        "description": row["description"] or "",
        "ai_instructions": row["ai_instructions"] or "",
        "plot_essentials": row["plot_essentials"] or "",
        "authors_note": row["authors_note"] or "",
        "story_summary": row["story_summary"] or "",
        "tags": _loads(row["tags"], []),
        "third_person": bool(row["third_person"]),
        "source": row["source"] or "new",
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def action_to_dict(row: sqlite3.Row) -> dict:
    return {
        "id": row["id"],
        "adventure_id": row["adventure_id"],
        "seq": row["seq"],
        "type": row["type"],
        "text": row["text"],
        "created_at": row["created_at"],
        "edited_at": row["edited_at"],
    }


def card_to_dict(row: sqlite3.Row) -> dict:
    return {
        "id": row["id"],
        "adventure_id": row["adventure_id"],
        "title": row["title"] or "",
        "type": row["type"] or "other",
        "triggers": _loads(row["triggers"], []),
        "entry": row["entry"] or "",
        "pinned": bool(row["pinned"]),
        "use_for_character_creation": bool(row["use_for_character_creation"]),
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def memory_to_dict(row: sqlite3.Row) -> dict:
    return {
        "id": row["id"],
        "adventure_id": row["adventure_id"],
        "text": row["text"],
        "seq_start": row["seq_start"],
        "seq_end": row["seq_end"],
        "created_at": row["created_at"],
    }


# --------------------------------------------------------------- scenarios
def insert_scenario(conn: sqlite3.Connection, *, title: str, description: str = "",
                    ai_instructions: str = "", plot_essentials: str = "",
                    authors_note: str = "", opening_text: str = "",
                    tags: list | None = None, third_person: bool = False,
                    created_at: str | None = None, updated_at: str | None = None) -> dict:
    now = utcnow()
    sid = new_id()
    conn.execute(
        "INSERT INTO scenarios(id, title, description, ai_instructions, plot_essentials,"
        " authors_note, opening_text, tags, third_person, created_at, updated_at)"
        " VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        (sid, title, description, ai_instructions, plot_essentials, authors_note,
         opening_text, _dumps(tags, []), int(third_person), created_at or now, updated_at or now),
    )
    return scenario_to_dict(conn.execute("SELECT * FROM scenarios WHERE id=?", (sid,)).fetchone())


def get_scenario(conn: sqlite3.Connection, scenario_id: str) -> dict | None:
    row = conn.execute("SELECT * FROM scenarios WHERE id=?", (scenario_id,)).fetchone()
    return scenario_to_dict(row) if row else None


def list_scenarios(conn: sqlite3.Connection, tag: str | None = None,
                   q: str | None = None) -> list[dict]:
    rows = conn.execute("SELECT * FROM scenarios ORDER BY created_at DESC").fetchall()
    out = [scenario_to_dict(r) for r in rows]
    if tag:
        out = [s for s in out if tag in s["tags"]]
    if q:
        needle = q.casefold()
        out = [s for s in out if needle in s["title"].casefold()
               or needle in s["description"].casefold()]
    return out


# -------------------------------------------------------------- adventures
def insert_adventure(conn: sqlite3.Connection, *, title: str, scenario_id: str | None = None,
                     description: str = "", ai_instructions: str = "",
                     plot_essentials: str = "", authors_note: str = "",
                     story_summary: str = "", tags: list | None = None,
                     third_person: bool = False, source: str = "new",
                     created_at: str | None = None, updated_at: str | None = None) -> dict:
    now = utcnow()
    aid = new_id()
    conn.execute(
        "INSERT INTO adventures(id, scenario_id, title, description, ai_instructions,"
        " plot_essentials, authors_note, story_summary, tags, third_person, source,"
        " created_at, updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (aid, scenario_id, title, description, ai_instructions, plot_essentials,
         authors_note, story_summary, _dumps(tags, []), int(third_person), source,
         created_at or now, updated_at or now),
    )
    return adventure_to_dict(conn.execute("SELECT * FROM adventures WHERE id=?", (aid,)).fetchone())


def get_adventure(conn: sqlite3.Connection, adventure_id: str) -> dict | None:
    row = conn.execute("SELECT * FROM adventures WHERE id=?", (adventure_id,)).fetchone()
    return adventure_to_dict(row) if row else None


def touch_adventure(conn: sqlite3.Connection, adventure_id: str) -> None:
    conn.execute("UPDATE adventures SET updated_at=? WHERE id=?", (utcnow(), adventure_id))


def update_adventure_memory(conn: sqlite3.Connection, adventure_id: str, *,
                            plot_essentials: str | None = None,
                            authors_note: str | None = None) -> None:
    if plot_essentials is not None:
        conn.execute("UPDATE adventures SET plot_essentials=? WHERE id=?",
                     (plot_essentials, adventure_id))
    if authors_note is not None:
        conn.execute("UPDATE adventures SET authors_note=? WHERE id=?",
                     (authors_note, adventure_id))
    touch_adventure(conn, adventure_id)


def update_adventure_summary(conn: sqlite3.Connection, adventure_id: str, story_summary: str) -> None:
    conn.execute("UPDATE adventures SET story_summary=?, updated_at=? WHERE id=?",
                 (story_summary, utcnow(), adventure_id))


def list_adventure_summaries(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute("SELECT * FROM adventures ORDER BY updated_at DESC").fetchall()
    out = []
    for r in rows:
        adv = adventure_to_dict(r)
        count = conn.execute("SELECT COUNT(*) AS c FROM actions WHERE adventure_id=?",
                             (adv["id"],)).fetchone()["c"]
        last = conn.execute(
            "SELECT text FROM actions WHERE adventure_id=? ORDER BY seq DESC LIMIT 1",
            (adv["id"],)).fetchone()
        excerpt = ""
        if last and last["text"]:
            excerpt = " ".join(last["text"].split())[:160]
        out.append({
            "id": adv["id"], "title": adv["title"], "description": adv["description"],
            "tags": adv["tags"], "source": adv["source"], "action_count": count,
            "last_excerpt": excerpt, "updated_at": adv["updated_at"],
        })
    return out


def adventure_state(conn: sqlite3.Connection, adventure_id: str) -> dict | None:
    adv = get_adventure(conn, adventure_id)
    if not adv:
        return None
    adv["actions"] = list_actions(conn, adventure_id)
    adv["story_cards"] = list_cards(conn, adventure_id)
    adv["memories"] = list_memories(conn, adventure_id)
    return adv


# ----------------------------------------------------------------- actions
def insert_action(conn: sqlite3.Connection, adventure_id: str, seq: int, type: str,
                  text: str, created_at: str | None = None) -> dict:
    if type not in ACTION_TYPES:
        raise ValueError(f"invalid action type: {type}")
    cur = conn.execute(
        "INSERT INTO actions(adventure_id, seq, type, text, created_at) VALUES(?,?,?,?,?)",
        (adventure_id, seq, type, text, created_at or utcnow()),
    )
    row = conn.execute("SELECT * FROM actions WHERE id=?", (cur.lastrowid,)).fetchone()
    return action_to_dict(row)


def get_action(conn: sqlite3.Connection, action_id: int) -> dict | None:
    row = conn.execute("SELECT * FROM actions WHERE id=?", (action_id,)).fetchone()
    return action_to_dict(row) if row else None


def list_actions(conn: sqlite3.Connection, adventure_id: str) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM actions WHERE adventure_id=? ORDER BY seq ASC", (adventure_id,),
    ).fetchall()
    return [action_to_dict(r) for r in rows]


def delete_action(conn: sqlite3.Connection, action_id: int) -> None:
    conn.execute("DELETE FROM actions WHERE id=?", (action_id,))


def update_action_text(conn: sqlite3.Connection, action_id: int, text: str) -> None:
    conn.execute("UPDATE actions SET text=?, edited_at=? WHERE id=?",
                 (text, utcnow(), action_id))


def max_seq(conn: sqlite3.Connection, adventure_id: str) -> int | None:
    row = conn.execute("SELECT MAX(seq) AS m FROM actions WHERE adventure_id=?",
                       (adventure_id,)).fetchone()
    return row["m"]


# ------------------------------------------------------------------- cards
def insert_card(conn: sqlite3.Connection, adventure_id: str, *, title: str = "",
                type: str = "other", triggers: list | None = None, entry: str = "",
                pinned: bool = False, use_for_character_creation: bool = False,
                created_at: str | None = None, updated_at: str | None = None) -> dict:
    now = utcnow()
    cid = new_id()
    conn.execute(
        "INSERT INTO story_cards(id, adventure_id, title, type, triggers, entry, pinned,"
        " use_for_character_creation, created_at, updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
        (cid, adventure_id, title, type or "other", _dumps(triggers, []), entry,
         int(pinned), int(use_for_character_creation), created_at or now, updated_at or now),
    )
    return card_to_dict(conn.execute("SELECT * FROM story_cards WHERE id=?", (cid,)).fetchone())


def get_card(conn: sqlite3.Connection, adventure_id: str, card_id: str) -> dict | None:
    row = conn.execute("SELECT * FROM story_cards WHERE id=? AND adventure_id=?",
                       (card_id, adventure_id)).fetchone()
    return card_to_dict(row) if row else None


def list_cards(conn: sqlite3.Connection, adventure_id: str) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM story_cards WHERE adventure_id=?"
        " ORDER BY pinned DESC, created_at ASC", (adventure_id,),
    ).fetchall()
    return [card_to_dict(r) for r in rows]


def update_card(conn: sqlite3.Connection, adventure_id: str, card_id: str,
                fields: dict) -> dict | None:
    current = get_card(conn, adventure_id, card_id)
    if not current:
        return None
    merged = {**current, **{k: v for k, v in fields.items() if v is not None}}
    conn.execute(
        "UPDATE story_cards SET title=?, type=?, triggers=?, entry=?, pinned=?,"
        " use_for_character_creation=?, updated_at=? WHERE id=?",
        (merged["title"], merged["type"] or "other", _dumps(merged["triggers"], []),
         merged["entry"], int(merged["pinned"]), int(merged["use_for_character_creation"]),
         utcnow(), card_id),
    )
    return get_card(conn, adventure_id, card_id)


def delete_card(conn: sqlite3.Connection, adventure_id: str, card_id: str) -> bool:
    cur = conn.execute("DELETE FROM story_cards WHERE id=? AND adventure_id=?",
                       (card_id, adventure_id))
    return cur.rowcount > 0


# ---------------------------------------------------------------- memories
def insert_memory(conn: sqlite3.Connection, adventure_id: str, text: str,
                  seq_start: int | None = None, seq_end: int | None = None,
                  created_at: str | None = None) -> dict:
    cur = conn.execute(
        "INSERT INTO memories(adventure_id, text, seq_start, seq_end, created_at)"
        " VALUES(?,?,?,?,?)",
        (adventure_id, text, seq_start, seq_end, created_at or utcnow()),
    )
    row = conn.execute("SELECT * FROM memories WHERE id=?", (cur.lastrowid,)).fetchone()
    return memory_to_dict(row)


def list_memories(conn: sqlite3.Connection, adventure_id: str) -> list[dict]:
    rows = conn.execute("SELECT * FROM memories WHERE adventure_id=? ORDER BY id ASC",
                        (adventure_id,)).fetchall()
    return [memory_to_dict(r) for r in rows]


# ---------------------------------------------------------------- settings
def get_setting(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row["value"] if row else None


def set_setting(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute("INSERT INTO settings(key, value) VALUES(?,?)"
                 " ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))


def delete_setting(conn: sqlite3.Connection, key: str) -> None:
    conn.execute("DELETE FROM settings WHERE key=?", (key,))
