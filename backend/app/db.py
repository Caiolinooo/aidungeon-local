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
import re
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

REPO_ROOT = Path(__file__).resolve().parents[2]
SCHEMA_PATH = Path(__file__).resolve().parents[1] / "schema.sql"
MIGRATIONS_DIR = Path(__file__).resolve().parents[1] / "migrations"
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


# ------------------------------------------------------------- migrations
def _migration_files() -> list[tuple[int, Path]]:
    """Arquivos backend/migrations/00X_*.sql ordenados pelo número X."""
    out: list[tuple[int, Path]] = []
    if MIGRATIONS_DIR.is_dir():
        for path in MIGRATIONS_DIR.glob("*.sql"):
            match = re.match(r"(\d+)_.*\.sql$", path.name)
            if match:
                out.append((int(match.group(1)), path))
    return sorted(out)


def _apply_migration(conn: sqlite3.Connection, path: Path) -> None:
    """Aplica um arquivo de migração statement a statement.

    `ALTER TABLE ... ADD COLUMN` não tem IF NOT EXISTS no SQLite: statements
    desse tipo são pulados quando a coluna já existe (pragma table_info).
    Linhas de comentário `--` são removidas antes do split por ';'.
    """
    sql = path.read_text(encoding="utf-8")
    sql = "\n".join(line for line in sql.splitlines()
                    if not line.strip().startswith("--"))
    for stmt in sql.split(";"):
        stmt = stmt.strip()
        if not stmt:
            continue
        match = re.search(r"ALTER\s+TABLE\s+(\w+)\s+ADD\s+COLUMN\s+(\w+)",
                          stmt, re.IGNORECASE)
        if match:
            table, column = match.group(1), match.group(2)
            cols = [r["name"] for r in conn.execute(f"PRAGMA table_info({table})")]
            if column in cols:
                continue
        conn.execute(stmt)


def init_db() -> None:
    """Aplica schema.sql na primeira execução (idempotente — schema é contrato,
    sem IF NOT EXISTS, então verificamos se já foi aplicado) e depois roda as
    migrations versionadas (PRAGMA user_version): aplica 00X_*.sql quando
    user_version < X e grava user_version=X após cada uma. Bancos frescos já
    recebem os objetos v2 pelo schema.sql, então só marcam a versão mais alta."""
    with connect() as conn:
        applied = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='scenarios'"
        ).fetchone()
        migrations = _migration_files()
        latest = migrations[-1][0] if migrations else 0
        if not applied:
            conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
            conn.execute(f"PRAGMA user_version = {latest}")
            return
        version = int(conn.execute("PRAGMA user_version").fetchone()[0])
        for number, path in migrations:
            if version < number:
                _apply_migration(conn, path)
                conn.execute(f"PRAGMA user_version = {number}")
                version = number


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
        "story_cards": _loads(row["story_cards"], []),
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
        "image_style": row["image_style"] or "",
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
        # imagem vinculada (actions type=see) — preenchido por list/get_action
        "image": None,
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
        # v0.3.0: assets do NPC
        "portrait_image_id": row["portrait_image_id"],
        "portrait_anchor_id": row["portrait_anchor_id"],
        "portrait_prompt": row["portrait_prompt"] or "",
        "voice": row["voice"] or "",
        "vrm_id": row["vrm_id"] or "",
        # v0.5.0: ficha estruturada do NPC (JSON ou '')
        "sheet": row["sheet"] or "",
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def attach_portrait(conn: sqlite3.Connection, card: dict) -> dict:
    """Embute card['portrait'] = image_ref do retrato atual (None se ausente)."""
    card["portrait"] = None
    if card.get("portrait_image_id"):
        card["portrait"] = image_ref(get_image(conn, card["portrait_image_id"]))
    return card


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
                    tags: list | None = None, story_cards: list | None = None,
                    third_person: bool = False,
                    created_at: str | None = None, updated_at: str | None = None) -> dict:
    now = utcnow()
    sid = new_id()
    conn.execute(
        "INSERT INTO scenarios(id, title, description, ai_instructions, plot_essentials,"
        " authors_note, opening_text, tags, story_cards, third_person, created_at, updated_at)"
        " VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
        (sid, title, description, ai_instructions, plot_essentials, authors_note,
         opening_text, _dumps(tags, []), _dumps(story_cards, []), int(third_person),
         created_at or now, updated_at or now),
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
                     story_summary: str = "", image_style: str = "",
                     tags: list | None = None,
                     third_person: bool = False, source: str = "new",
                     created_at: str | None = None, updated_at: str | None = None) -> dict:
    now = utcnow()
    aid = new_id()
    conn.execute(
        "INSERT INTO adventures(id, scenario_id, title, description, ai_instructions,"
        " plot_essentials, authors_note, story_summary, image_style, tags, third_person,"
        " source, created_at, updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (aid, scenario_id, title, description, ai_instructions, plot_essentials,
         authors_note, story_summary, image_style, _dumps(tags, []), int(third_person),
         source, created_at or now, updated_at or now),
    )
    return adventure_to_dict(conn.execute("SELECT * FROM adventures WHERE id=?", (aid,)).fetchone())


def get_adventure(conn: sqlite3.Connection, adventure_id: str) -> dict | None:
    row = conn.execute("SELECT * FROM adventures WHERE id=?", (adventure_id,)).fetchone()
    return adventure_to_dict(row) if row else None


def touch_adventure(conn: sqlite3.Connection, adventure_id: str) -> None:
    conn.execute("UPDATE adventures SET updated_at=? WHERE id=?", (utcnow(), adventure_id))


def update_adventure(conn: sqlite3.Connection, adventure_id: str, *,
                     title: str | None = None, description: str | None = None,
                     tags: list[str] | None = None) -> dict | None:
    """Edita metadados da aventura (título/descrição/tags). None = não altera."""
    current = get_adventure(conn, adventure_id)
    if not current:
        return None
    merged = {
        "title": current["title"] if title is None else title,
        "description": current["description"] if description is None else description,
        "tags": current["tags"] if tags is None else tags,
    }
    conn.execute(
        "UPDATE adventures SET title=?, description=?, tags=?, updated_at=? WHERE id=?",
        (merged["title"], merged["description"], _dumps(merged["tags"], []),
         utcnow(), adventure_id),
    )
    return get_adventure(conn, adventure_id)


def delete_adventure(conn: sqlite3.Connection, adventure_id: str) -> bool:
    """Remove a aventura e todos os dependentes (schema não tem ON DELETE CASCADE)."""
    for table in ("jobs", "images", "memories", "story_cards", "actions"):
        conn.execute(f"DELETE FROM {table} WHERE adventure_id=?", (adventure_id,))
    cur = conn.execute("DELETE FROM adventures WHERE id=?", (adventure_id,))
    return cur.rowcount > 0


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
    if not row:
        return None
    action = action_to_dict(row)
    img = conn.execute(
        "SELECT * FROM images WHERE adventure_id=? AND action_seq=?",
        (action["adventure_id"], action["seq"])).fetchone()
    if img:
        action["image"] = image_ref(image_to_dict(img))
    return action


def list_actions(conn: sqlite3.Connection, adventure_id: str) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM actions WHERE adventure_id=? ORDER BY seq ASC", (adventure_id,)
    ).fetchall()
    images = _images_by_seq(conn, adventure_id)
    out = []
    for r in rows:
        action = action_to_dict(r)
        img = images.get(action["seq"])
        if img:
            action["image"] = image_ref(img)
        out.append(action)
    return out


def delete_action(conn: sqlite3.Connection, action_id: int) -> None:
    conn.execute("DELETE FROM actions WHERE id=?", (action_id,))


def update_action_text(conn: sqlite3.Connection, action_id: int, text: str) -> None:
    conn.execute("UPDATE actions SET text=?, edited_at=? WHERE id=?",
                 (text, utcnow(), action_id))


def max_seq(conn: sqlite3.Connection, adventure_id: str) -> int | None:
    row = conn.execute("SELECT MAX(seq) AS m FROM actions WHERE adventure_id=?",
                       (adventure_id,)).fetchone()
    return row["m"]


# ------------------------------------------------------------------ images
def image_to_dict(row: sqlite3.Row) -> dict:
    return {
        "id": row["id"],
        "adventure_id": row["adventure_id"],
        "action_seq": row["action_seq"],
        "prompt": row["prompt"] or "",
        "negative_prompt": row["negative_prompt"] or "",
        "file_path": row["file_path"],
        "provider": row["provider"] or "",
        "width": row["width"],
        "height": row["height"],
        "kind": row["kind"] or "scene",
        "ref_id": row["ref_id"] or "",
        "created_at": row["created_at"],
        "url": f"/api/images/{row['id']}",
    }


def image_ref(image: dict | None) -> dict | None:
    """Recorte público embutido na Action (schema Action.image do openapi)."""
    if not image:
        return None
    return {"id": image["id"], "url": image["url"], "prompt": image["prompt"]}


def _images_by_seq(conn: sqlite3.Connection, adventure_id: str) -> dict[int, dict]:
    rows = conn.execute("SELECT * FROM images WHERE adventure_id=?",
                        (adventure_id,)).fetchall()
    return {r["action_seq"]: image_to_dict(r) for r in rows}


def insert_image(conn: sqlite3.Connection, *, adventure_id: str, action_seq: int,
                 file_path: str, prompt: str = "", negative_prompt: str = "",
                 provider: str = "", width: int | None = None,
                 height: int | None = None, image_id: str | None = None,
                 kind: str = "scene", ref_id: str = "",
                 created_at: str | None = None) -> dict:
    iid = image_id or new_id()
    conn.execute(
        "INSERT INTO images(id, adventure_id, action_seq, prompt, negative_prompt,"
        " file_path, provider, width, height, kind, ref_id, created_at)"
        " VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
        (iid, adventure_id, action_seq, prompt, negative_prompt, file_path,
         provider, width, height, kind, ref_id, created_at or utcnow()),
    )
    return image_to_dict(conn.execute("SELECT * FROM images WHERE id=?", (iid,)).fetchone())


def get_image(conn: sqlite3.Connection, image_id: str) -> dict | None:
    row = conn.execute("SELECT * FROM images WHERE id=?", (image_id,)).fetchone()
    return image_to_dict(row) if row else None


# ------------------------------------------------------------------- cards
def insert_card(conn: sqlite3.Connection, adventure_id: str, *, title: str = "",
                type: str = "other", triggers: list | None = None, entry: str = "",
                pinned: bool = False, use_for_character_creation: bool = False,
                portrait_prompt: str = "", voice: str = "", vrm_id: str = "",
                sheet: str = "",
                created_at: str | None = None, updated_at: str | None = None) -> dict:
    now = utcnow()
    cid = new_id()
    conn.execute(
        "INSERT INTO story_cards(id, adventure_id, title, type, triggers, entry, pinned,"
        " use_for_character_creation, portrait_prompt, voice, vrm_id, sheet,"
        " created_at, updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (cid, adventure_id, title, type or "other", _dumps(triggers, []), entry,
         int(pinned), int(use_for_character_creation), portrait_prompt, voice, vrm_id,
         sheet, created_at or now, updated_at or now),
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
        " use_for_character_creation=?, portrait_prompt=?, voice=?, vrm_id=?, sheet=?,"
        " updated_at=? WHERE id=?",
        (merged["title"], merged["type"] or "other", _dumps(merged["triggers"], []),
         merged["entry"], int(merged["pinned"]), int(merged["use_for_character_creation"]),
         merged["portrait_prompt"], merged["voice"], merged["vrm_id"],
         merged["sheet"], utcnow(), card_id),
    )
    return get_card(conn, adventure_id, card_id)


def set_card_portrait(conn: sqlite3.Connection, card_id: str, image_id: str | None,
                      *, anchor_id: str | None = None) -> None:
    """Grava a foto exibida. anchor_id só preenche a âncora se ela ainda está vazia."""
    if anchor_id:
        conn.execute(
            "UPDATE story_cards SET portrait_image_id=?,"
            " portrait_anchor_id=COALESCE(NULLIF(portrait_anchor_id, ''), ?),"
            " updated_at=? WHERE id=?",
            (image_id, anchor_id, utcnow(), card_id),
        )
    else:
        conn.execute(
            "UPDATE story_cards SET portrait_image_id=?, updated_at=? WHERE id=?",
            (image_id, utcnow(), card_id),
        )


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


# -------------------------------------------------------------------- jobs
JOB_TYPES = ("character_portrait", "scene_backdrop")
JOB_ACTIVE = ("pending", "running")


def job_to_dict(row: sqlite3.Row) -> dict:
    return {
        "id": row["id"],
        "adventure_id": row["adventure_id"],
        "type": row["type"],
        "status": row["status"],
        "ref_id": row["ref_id"] or "",
        "payload": _loads(row["payload"], {}),
        "result": _loads(row["result"], None),
        "error": row["error"],
        "created_at": row["created_at"],
        "started_at": row["started_at"],
        "finished_at": row["finished_at"],
    }


def insert_job(conn: sqlite3.Connection, *, adventure_id: str, type: str,
               ref_id: str = "", payload: dict | None = None,
               job_id: str | None = None) -> dict:
    if type not in JOB_TYPES:
        raise ValueError(f"invalid job type: {type}")
    jid = job_id or new_id()
    conn.execute(
        "INSERT INTO jobs(id, adventure_id, type, status, ref_id, payload, created_at)"
        " VALUES(?,?,?,'pending',?,?,?)",
        (jid, adventure_id, type, ref_id, _dumps(payload, {}), utcnow()),
    )
    return job_to_dict(conn.execute("SELECT * FROM jobs WHERE id=?", (jid,)).fetchone())


def get_job(conn: sqlite3.Connection, job_id: str) -> dict | None:
    row = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
    return job_to_dict(row) if row else None


def list_jobs(conn: sqlite3.Connection, adventure_id: str,
              status: str | None = None) -> list[dict]:
    sql = "SELECT * FROM jobs WHERE adventure_id=?"
    params: list = [adventure_id]
    if status:
        sql += " AND status=?"
        params.append(status)
    rows = conn.execute(sql + " ORDER BY created_at ASC", params).fetchall()
    return [job_to_dict(r) for r in rows]


def latest_job(conn: sqlite3.Connection, type: str, ref_id: str) -> dict | None:
    """Job mais recente (qualquer status) para o mesmo alvo."""
    row = conn.execute(
        "SELECT * FROM jobs WHERE type=? AND ref_id=? ORDER BY created_at DESC LIMIT 1",
        (type, ref_id),
    ).fetchone()
    return job_to_dict(row) if row else None


def latest_image(conn: sqlite3.Connection, adventure_id: str, kind: str,
                 ref_id: str | None = None) -> dict | None:
    """Imagem mais recente de um kind. ref_id=None devolve qualquer uma do kind."""
    if ref_id is None:
        row = conn.execute(
            "SELECT * FROM images WHERE adventure_id=? AND kind=?"
            " ORDER BY created_at DESC LIMIT 1",
            (adventure_id, kind),
        ).fetchone()
    else:
        row = conn.execute(
            "SELECT * FROM images WHERE adventure_id=? AND kind=? AND ref_id=?"
            " ORDER BY created_at DESC LIMIT 1",
            (adventure_id, kind, ref_id),
        ).fetchone()
    return image_to_dict(row) if row else None


def find_active_job(conn: sqlite3.Connection, type: str, ref_id: str) -> dict | None:
    """Job pendente/rodando para o mesmo alvo (dedupe de enfileiramento)."""
    row = conn.execute(
        "SELECT * FROM jobs WHERE type=? AND ref_id=?"
        " AND status IN ('pending','running') ORDER BY created_at DESC LIMIT 1",
        (type, ref_id),
    ).fetchone()
    return job_to_dict(row) if row else None


def claim_next_job(conn: sqlite3.Connection) -> dict | None:
    """pending → running do job mais antigo (o worker é único por processo)."""
    row = conn.execute(
        "SELECT id FROM jobs WHERE status='pending' ORDER BY created_at ASC LIMIT 1"
    ).fetchone()
    if not row:
        return None
    cur = conn.execute(
        "UPDATE jobs SET status='running', started_at=? WHERE id=? AND status='pending'",
        (utcnow(), row["id"]),
    )
    if cur.rowcount == 0:  # outro processo pegou antes
        return None
    return get_job(conn, row["id"])


def finish_job(conn: sqlite3.Connection, job_id: str, status: str,
               result: dict | None = None, error: str | None = None) -> None:
    conn.execute(
        "UPDATE jobs SET status=?, result=?, error=?, finished_at=? WHERE id=?",
        (status, _dumps(result, None) if result is not None else None,
         error, utcnow(), job_id),
    )


def cancel_job(conn: sqlite3.Connection, job_id: str) -> dict | None:
    """Cancela apenas jobs pendentes (running é cooperativo — v1 não interrompe)."""
    cur = conn.execute(
        "UPDATE jobs SET status='canceled', finished_at=? WHERE id=? AND status='pending'",
        (utcnow(), job_id),
    )
    if cur.rowcount == 0:
        return None
    return get_job(conn, job_id)
