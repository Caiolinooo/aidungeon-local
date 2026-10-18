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


def _mark_applied(conn: sqlite3.Connection, numbers: list[int]) -> None:
    conn.execute("CREATE TABLE IF NOT EXISTS schema_migrations("
                 "number INTEGER PRIMARY KEY, applied_at TEXT)")
    conn.executemany("INSERT OR IGNORE INTO schema_migrations(number, applied_at)"
                     " VALUES(?, ?)", [(number, utcnow()) for number in numbers])


def _applied_migrations(conn: sqlite3.Connection, migrations: list[tuple[int, Path]]) -> set[int]:
    """Números já aplicados. Banco de antes da tabela ganha os números até o
    user_version, menos a faixa 010–019: o runner antigo aplicava em sequência
    e essa faixa (Kimi) só nasceu depois da 020, então nunca rodou."""
    exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_migrations'"
    ).fetchone()
    if not exists:
        version = int(conn.execute("PRAGMA user_version").fetchone()[0])
        _mark_applied(conn, [number for number, _ in migrations
                             if number <= version and not 10 <= number <= 19])
    return {row[0] for row in conn.execute("SELECT number FROM schema_migrations")}


def init_db() -> None:
    """Aplica schema.sql na primeira execução (idempotente — schema é contrato,
    sem IF NOT EXISTS, então verificamos se já foi aplicado) e depois roda as
    migrations: cada 0XX_*.sql roda uma vez, em ordem, registrada pelo número
    em `schema_migrations` — uma 010 nova entra mesmo num banco que já passou
    da 020. Bancos frescos já recebem tudo pelo schema.sql e só registram os
    números. user_version fica com o maior número aplicado."""
    with connect() as conn:
        applied = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='scenarios'"
        ).fetchone()
        migrations = _migration_files()
        latest = migrations[-1][0] if migrations else 0
        if not applied:
            conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
            _mark_applied(conn, [number for number, _ in migrations])
            conn.execute(f"PRAGMA user_version = {latest}")
            return
        done = _applied_migrations(conn, migrations)
        for number, path in migrations:
            if number not in done:
                _apply_migration(conn, path)
                _mark_applied(conn, [number])
        version = int(conn.execute("PRAGMA user_version").fetchone()[0])
        if latest > version:
            conn.execute(f"PRAGMA user_version = {latest}")


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
    keys = row.keys() if hasattr(row, "keys") else ()
    return {
        "id": row["id"],
        "title": row["title"],
        "description": row["description"] or "",
        "ai_instructions": row["ai_instructions"] or "",
        "plot_essentials": row["plot_essentials"] or "",
        "authors_note": row["authors_note"] or "",
        "opening_text": row["opening_text"] or "",
        "image_style": (row["image_style"] if "image_style" in keys else "") or "",
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
        # v0.7.0 — controles dinâmicos: manifesto gerado pela IA + estado dos
        # widgets ('' no banco → None/{} na API).
        "ui_profile": _loads(row["ui_profile"], None) if row["ui_profile"] else None,
        "ui_state": _loads(row["ui_state"], {}) if row["ui_state"] else {},
        "tags": _loads(row["tags"], []),
        "third_person": bool(row["third_person"]),
        "source": row["source"] or "new",
        "owner": (row["owner"] if "owner" in row.keys() else "") or "",
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


def memory_to_dict(row: sqlite3.Row, include_vector: bool = False, *,
                   include_embedding: bool = False) -> dict:
    inc = include_vector or include_embedding
    d = {
        "id": row["id"],
        "adventure_id": row["adventure_id"],
        "text": row["text"],
        "seq_start": row["seq_start"],
        "seq_end": row["seq_end"],
        "created_at": row["created_at"],
    }
    if inc:
        keys = row.keys() if hasattr(row, "keys") else ()
        d["embedding"] = row["embedding"] if "embedding" in keys else None
        d["embedding_model"] = row["embedding_model"] if "embedding_model" in keys else None
        d["embedding_dim"] = row["embedding_dim"] if "embedding_dim" in keys else None
    return d


# --------------------------------------------------------------- scenarios
def insert_scenario(conn: sqlite3.Connection, *, title: str, description: str = "",
                    ai_instructions: str = "", plot_essentials: str = "",
                    authors_note: str = "", opening_text: str = "",
                    image_style: str = "",
                    tags: list | None = None, story_cards: list | None = None,
                    third_person: bool = False,
                    created_at: str | None = None, updated_at: str | None = None) -> dict:
    now = utcnow()
    sid = new_id()
    conn.execute(
        "INSERT INTO scenarios(id, title, description, ai_instructions, plot_essentials,"
        " authors_note, opening_text, image_style, tags, story_cards, third_person, created_at, updated_at)"
        " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (sid, title, description, ai_instructions, plot_essentials, authors_note,
         opening_text, image_style, _dumps(tags, []), _dumps(story_cards, []), int(third_person),
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


def update_scenario(conn: sqlite3.Connection, scenario_id: str, **fields: Any) -> dict | None:
    """Edita um scenario existente (campos omitidos/None permanecem)."""
    current = get_scenario(conn, scenario_id)
    if not current:
        return None
    merged = {
        "title": current["title"] if fields.get("title") is None else fields["title"],
        "description": current["description"] if fields.get("description") is None else fields["description"],
        "ai_instructions": current["ai_instructions"] if fields.get("ai_instructions") is None else fields["ai_instructions"],
        "plot_essentials": current["plot_essentials"] if fields.get("plot_essentials") is None else fields["plot_essentials"],
        "authors_note": current["authors_note"] if fields.get("authors_note") is None else fields["authors_note"],
        "opening_text": current["opening_text"] if fields.get("opening_text") is None else fields["opening_text"],
        "image_style": current["image_style"] if fields.get("image_style") is None else fields["image_style"],
        "tags": current["tags"] if fields.get("tags") is None else fields["tags"],
        "story_cards": current["story_cards"] if fields.get("story_cards") is None else fields["story_cards"],
        "third_person": current["third_person"] if fields.get("third_person") is None else bool(fields["third_person"]),
    }
    conn.execute(
        "UPDATE scenarios SET title=?, description=?, ai_instructions=?, plot_essentials=?,"
        " authors_note=?, opening_text=?, image_style=?, tags=?, story_cards=?, third_person=?,"
        " updated_at=? WHERE id=?",
        (
            merged["title"], merged["description"], merged["ai_instructions"],
            merged["plot_essentials"], merged["authors_note"], merged["opening_text"],
            merged["image_style"], _dumps(merged["tags"], []),
            _dumps(merged["story_cards"], []), int(merged["third_person"]),
            utcnow(), scenario_id,
        ),
    )
    return get_scenario(conn, scenario_id)


def delete_scenario(conn: sqlite3.Connection, scenario_id: str) -> bool:
    """Remove o scenario e desvincula aventuras existentes (SET scenario_id=NULL)."""
    conn.execute("UPDATE adventures SET scenario_id=NULL WHERE scenario_id=?", (scenario_id,))
    cur = conn.execute("DELETE FROM scenarios WHERE id=?", (scenario_id,))
    return cur.rowcount > 0


# -------------------------------------------------------------- adventures
def insert_adventure(conn: sqlite3.Connection, *, title: str, scenario_id: str | None = None,
                     description: str = "", ai_instructions: str = "",
                     plot_essentials: str = "", authors_note: str = "",
                     story_summary: str = "", image_style: str = "",
                     tags: list | None = None,
                     third_person: bool = False, source: str = "new",
                     owner: str = "",
                     created_at: str | None = None, updated_at: str | None = None) -> dict:
    now = utcnow()
    aid = new_id()
    conn.execute(
        "INSERT INTO adventures(id, scenario_id, title, description, ai_instructions,"
        " plot_essentials, authors_note, story_summary, image_style, tags, third_person,"
        " source, owner, created_at, updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (aid, scenario_id, title, description, ai_instructions, plot_essentials,
         authors_note, story_summary, image_style, _dumps(tags, []), int(third_person),
         source, owner or "", created_at or now, updated_at or now),
    )
    return adventure_to_dict(conn.execute("SELECT * FROM adventures WHERE id=?", (aid,)).fetchone())


def get_adventure(conn: sqlite3.Connection, adventure_id: str) -> dict | None:
    row = conn.execute("SELECT * FROM adventures WHERE id=?", (adventure_id,)).fetchone()
    return adventure_to_dict(row) if row else None


def touch_adventure(conn: sqlite3.Connection, adventure_id: str) -> None:
    conn.execute("UPDATE adventures SET updated_at=? WHERE id=?", (utcnow(), adventure_id))


_ADVENTURE_PATCH_FIELDS = (
    "title", "description", "tags", "ai_instructions", "plot_essentials",
    "authors_note", "story_summary", "image_style", "third_person",
)


def update_adventure(conn: sqlite3.Connection, adventure_id: str, **fields: Any) -> dict | None:
    """Edita metadados e configurações da aventura. None = não altera.

    O UPDATE lista só as colunas passadas com valor não-None. Um PATCH só com
    `title` não regrava `story_summary` lido no início — o summarizer pode
    tê-lo gravado nesse meio tempo. Quem passa o objeto inteiro continua
    atualizando cada campo enviado.
    """
    current = get_adventure(conn, adventure_id)
    if not current:
        return None
    sets: list[str] = []
    params: list[Any] = []
    for key in _ADVENTURE_PATCH_FIELDS:
        if key not in fields or fields[key] is None:
            continue
        value = fields[key]
        if key == "tags":
            value = _dumps(value, [])
        elif key == "third_person":
            value = int(bool(value))
        sets.append(f"{key}=?")
        params.append(value)
    if not sets:
        return current
    sets.append("updated_at=?")
    params.append(utcnow())
    params.append(adventure_id)
    conn.execute(
        f"UPDATE adventures SET {', '.join(sets)} WHERE id=?",
        params,
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
                            authors_note: str | None = None,
                            story_summary: str | None = None,
                            ai_instructions: str | None = None,
                            image_style: str | None = None,
                            third_person: bool | None = None) -> None:
    if plot_essentials is not None:
        conn.execute("UPDATE adventures SET plot_essentials=? WHERE id=?",
                     (plot_essentials, adventure_id))
    if authors_note is not None:
        conn.execute("UPDATE adventures SET authors_note=? WHERE id=?",
                     (authors_note, adventure_id))
    if story_summary is not None:
        conn.execute("UPDATE adventures SET story_summary=? WHERE id=?",
                     (story_summary, adventure_id))
    if ai_instructions is not None:
        conn.execute("UPDATE adventures SET ai_instructions=? WHERE id=?",
                     (ai_instructions, adventure_id))
    if image_style is not None:
        conn.execute("UPDATE adventures SET image_style=? WHERE id=?",
                     (image_style, adventure_id))
    if third_person is not None:
        conn.execute("UPDATE adventures SET third_person=? WHERE id=?",
                     (int(bool(third_person)), adventure_id))
    touch_adventure(conn, adventure_id)


def update_adventure_summary(conn: sqlite3.Connection, adventure_id: str, story_summary: str) -> None:
    conn.execute("UPDATE adventures SET story_summary=?, updated_at=? WHERE id=?",
                 (story_summary, utcnow(), adventure_id))


def update_adventure_ui_profile(conn: sqlite3.Connection, adventure_id: str,
                                profile: dict | None) -> None:
    """v0.7.0 — grava o manifesto de controles gerado pela IA ('' = ausente)."""
    conn.execute("UPDATE adventures SET ui_profile=?, updated_at=? WHERE id=?",
                 ("" if profile is None else _dumps(profile, {}), utcnow(), adventure_id))


def update_adventure_ui_state(conn: sqlite3.Connection, adventure_id: str,
                              state: dict) -> None:
    """v0.7.0 — grava o estado dos widgets do painel (HP, inventário, ficha)."""
    conn.execute("UPDATE adventures SET ui_state=?, updated_at=? WHERE id=?",
                 (_dumps(state, {}), utcnow(), adventure_id))


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
            "tags": adv["tags"], "source": adv["source"], "owner": adv.get("owner") or "",
            "action_count": count,
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


def list_cards(conn: sqlite3.Connection, adventure_id: str,
               q: str | None = None, card_type: str | None = None,
               pinned: bool | None = None) -> list[dict]:
    query = "SELECT * FROM story_cards WHERE adventure_id=?"
    params: list[Any] = [adventure_id]
    if q and q.strip():
        term = f"%{q.strip()}%"
        query += " AND (title LIKE ? OR triggers LIKE ? OR entry LIKE ?)"
        params.extend([term, term, term])
    if card_type and card_type.strip():
        query += " AND type = ?"
        params.append(card_type.strip())
    if pinned is not None:
        query += " AND pinned = ?"
        params.append(1 if pinned else 0)
    query += " ORDER BY pinned DESC, created_at ASC"
    rows = conn.execute(query, params).fetchall()
    return [card_to_dict(r) for r in rows]


def bulk_cards_action(conn: sqlite3.Connection, adventure_id: str, action: str,
                      card_ids: list[str], new_type: str | None = None) -> int:
    if not card_ids:
        return 0
    placeholders = ",".join("?" for _ in card_ids)
    now = utcnow()
    if action == "delete":
        cur = conn.execute(
            f"DELETE FROM story_cards WHERE adventure_id=? AND id IN ({placeholders})",
            [adventure_id, *card_ids],
        )
        return cur.rowcount
    if action == "pin":
        cur = conn.execute(
            f"UPDATE story_cards SET pinned=1, updated_at=? WHERE adventure_id=? AND id IN ({placeholders})",
            [now, adventure_id, *card_ids],
        )
        return cur.rowcount
    if action == "unpin":
        cur = conn.execute(
            f"UPDATE story_cards SET pinned=0, updated_at=? WHERE adventure_id=? AND id IN ({placeholders})",
            [now, adventure_id, *card_ids],
        )
        return cur.rowcount
    if action == "retype":
        cur = conn.execute(
            f"UPDATE story_cards SET type=?, updated_at=? WHERE adventure_id=? AND id IN ({placeholders})",
            [new_type, now, adventure_id, *card_ids],
        )
        return cur.rowcount
    raise ValueError(f"Unknown bulk action: {action}")


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
                  created_at: str | None = None,
                  embedding: bytes | None = None,
                  embedding_model: str | None = None,
                  embedding_dim: int | None = None,
                  *,
                  include_vector: bool = False,
                  include_embedding: bool = False) -> dict:
    cols = [r["name"] for r in conn.execute("PRAGMA table_info(memories)")]
    if "embedding_model" in cols:
        cur = conn.execute(
            "INSERT INTO memories(adventure_id, text, seq_start, seq_end, "
            "embedding, embedding_model, embedding_dim, created_at)"
            " VALUES(?,?,?,?,?,?,?,?)",
            (adventure_id, text, seq_start, seq_end, embedding,
             embedding_model, embedding_dim, created_at or utcnow()),
        )
    else:
        cur = conn.execute(
            "INSERT INTO memories(adventure_id, text, seq_start, seq_end, created_at, embedding)"
            " VALUES(?,?,?,?,?,?)",
            (adventure_id, text, seq_start, seq_end, created_at or utcnow(), embedding),
        )
    row = conn.execute("SELECT * FROM memories WHERE id=?", (cur.lastrowid,)).fetchone()
    inc = include_vector or include_embedding
    return memory_to_dict(row, inc)


def get_memory(conn: sqlite3.Connection, memory_id: int,
               adventure_id: str | None = None,
               include_vector: bool = False,
               *,
               include_embedding: bool = False) -> dict | None:
    inc = include_vector or include_embedding
    if adventure_id is not None:
        row = conn.execute("SELECT * FROM memories WHERE id=? AND adventure_id=?",
                           (memory_id, adventure_id)).fetchone()
    else:
        row = conn.execute("SELECT * FROM memories WHERE id=?", (memory_id,)).fetchone()
    return memory_to_dict(row, inc) if row else None


def list_memories(conn: sqlite3.Connection, adventure_id: str,
                  include_vector: bool = False,
                  *,
                  include_embedding: bool = False) -> list[dict]:
    rows = conn.execute("SELECT * FROM memories WHERE adventure_id=? ORDER BY id ASC",
                        (adventure_id,)).fetchall()
    inc = include_vector or include_embedding
    return [memory_to_dict(r, inc) for r in rows]


def update_memory(conn: sqlite3.Connection, memory_id: int,
                  text: str | None = None,
                  seq_start: int | None = None,
                  seq_end: int | None = None,
                  embedding: bytes | None = None,
                  embedding_model: str | None = None,
                  embedding_dim: int | None = None,
                  *,
                  model: str | None = None,
                  dim: int | None = None,
                  clear_embedding: bool = False,
                  adventure_id: str | None = None,
                  include_vector: bool = False,
                  include_embedding: bool = False) -> dict | None:
    model_name = embedding_model if embedding_model is not None else model
    dimension = embedding_dim if embedding_dim is not None else dim

    updates: list[str] = []
    params: list[Any] = []

    if text is not None:
        updates.append("text = ?")
        params.append(text)
    if seq_start is not None:
        updates.append("seq_start = ?")
        params.append(seq_start)
    if seq_end is not None:
        updates.append("seq_end = ?")
        params.append(seq_end)

    if clear_embedding:
        updates.append("embedding = NULL")
        cols = [r["name"] for r in conn.execute("PRAGMA table_info(memories)")]
        if "embedding_model" in cols:
            updates.append("embedding_model = NULL")
        if "embedding_dim" in cols:
            updates.append("embedding_dim = NULL")
    else:
        if embedding is not None:
            updates.append("embedding = ?")
            params.append(embedding)
        if model_name is not None:
            updates.append("embedding_model = ?")
            params.append(model_name)
        if dimension is not None:
            updates.append("embedding_dim = ?")
            params.append(dimension)

    inc = include_vector or include_embedding
    if not updates:
        return get_memory(conn, memory_id, adventure_id=adventure_id, include_vector=inc)

    where = "WHERE id = ?"
    params.append(memory_id)
    if adventure_id is not None:
        where += " AND adventure_id = ?"
        params.append(adventure_id)

    cur = conn.execute(f"UPDATE memories SET {', '.join(updates)} {where}", params)
    if cur.rowcount == 0:
        return None
    return get_memory(conn, memory_id, adventure_id=adventure_id, include_vector=inc)


def delete_memory(conn: sqlite3.Connection, memory_id: int,
                  adventure_id: str | None = None) -> bool:
    if adventure_id is not None:
        cur = conn.execute("DELETE FROM memories WHERE id=? AND adventure_id=?",
                           (memory_id, adventure_id))
    else:
        cur = conn.execute("DELETE FROM memories WHERE id=?", (memory_id,))
    return cur.rowcount > 0


def clear_memories(conn: sqlite3.Connection, adventure_id: str) -> int:
    cur = conn.execute("DELETE FROM memories WHERE adventure_id=?", (adventure_id,))
    return cur.rowcount


def list_unembedded_memories(conn: sqlite3.Connection,
                            model: str | None = None,
                            dim: int | None = None,
                            *,
                            adventure_id: str | None = None) -> list[dict]:
    """Finds memories that lack embeddings or have mismatched model/dimension."""
    cols = [r["name"] for r in conn.execute("PRAGMA table_info(memories)")]
    conditions: list[str] = []
    params: list[Any] = []

    if "embedding_model" in cols and "embedding_dim" in cols:
        if model is not None and dim is not None:
            conditions.append(
                "(embedding IS NULL OR embedding_model IS NULL OR embedding_model != ? "
                "OR embedding_dim IS NULL OR embedding_dim != ?)"
            )
            params.extend([model, dim])
        elif model is not None:
            conditions.append("(embedding IS NULL OR embedding_model IS NULL OR embedding_model != ?)")
            params.append(model)
        elif dim is not None:
            conditions.append("(embedding IS NULL OR embedding_dim IS NULL OR embedding_dim != ?)")
            params.append(dim)
        else:
            conditions.append("embedding IS NULL")
    else:
        conditions.append("embedding IS NULL")

    if adventure_id is not None:
        conditions.append("adventure_id = ?")
        params.append(adventure_id)

    where_clause = " WHERE " + " AND ".join(conditions) if conditions else ""
    sql = f"SELECT * FROM memories{where_clause} ORDER BY id ASC"
    rows = conn.execute(sql, params).fetchall()
    return [memory_to_dict(r, include_vector=True) for r in rows]


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
JOB_TYPES = ("character_portrait", "scene_backdrop", "see_image")
JOB_ACTIVE = ("preparing", "pending", "running")


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
        "priority": row["priority"],
        "supersede_key": row["supersede_key"] or "",
        "created_at": row["created_at"],
        "started_at": row["started_at"],
        "finished_at": row["finished_at"],
    }


def insert_job(conn: sqlite3.Connection, *, adventure_id: str, type: str,
               ref_id: str = "", payload: dict | None = None,
               job_id: str | None = None, priority: int = 2,
               supersede_key: str = "", status: str = "pending") -> dict:
    if type not in JOB_TYPES:
        raise ValueError(f"invalid job type: {type}")
    if status not in ("preparing", "pending"):
        raise ValueError(f"invalid initial job status: {status}")
    jid = job_id or new_id()
    conn.execute(
        "INSERT INTO jobs(id, adventure_id, type, status, ref_id, payload, priority,"
        " supersede_key, created_at) VALUES(?,?,?,?,?,?,?,?,?)",
        (jid, adventure_id, type, status, ref_id, _dumps(payload, {}), int(priority),
         supersede_key, utcnow()),
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
    """Job ativo (preparando, pendente ou rodando) para o mesmo alvo (dedupe)."""
    row = conn.execute(
        "SELECT * FROM jobs WHERE type=? AND ref_id=?"
        " AND status IN ('preparing','pending','running') ORDER BY created_at DESC LIMIT 1",
        (type, ref_id),
    ).fetchone()
    return job_to_dict(row) if row else None


def claim_next_job(conn: sqlite3.Connection, *,
                   max_priority: int | None = None) -> dict | None:
    """pending → running do job de menor prioridade e, nela, do mais antigo (o
    worker é único por processo). `max_priority` deixa de fora os menos urgentes."""
    sql = "SELECT id FROM jobs WHERE status='pending'"
    params: list = []
    if max_priority is not None:
        sql += " AND priority<=?"
        params.append(int(max_priority))
    row = conn.execute(sql + " ORDER BY priority ASC, created_at ASC, rowid ASC LIMIT 1",
                       params).fetchone()
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
    """Cancela jobs que ainda não rodam (running é cooperativo — v1 não interrompe)."""
    cur = conn.execute(
        "UPDATE jobs SET status='canceled', finished_at=? WHERE id=?"
        " AND status IN ('preparing','pending')",
        (utcnow(), job_id),
    )
    if cur.rowcount == 0:
        return None
    return get_job(conn, job_id)


def supersede_jobs(conn: sqlite3.Connection, supersede_key: str,
                   keep_id: str) -> list[dict]:
    """Cancela os pendentes com a mesma chave, menos `keep_id`; devolve os cancelados."""
    rows = conn.execute(
        "SELECT id FROM jobs WHERE supersede_key=? AND id<>?"
        " AND status IN ('preparing','pending')",
        (supersede_key, keep_id),
    ).fetchall()
    canceled = []
    for row in rows:
        conn.execute(
            "UPDATE jobs SET status='canceled', error='superseded', finished_at=?"
            " WHERE id=? AND status IN ('preparing','pending')",
            (utcnow(), row["id"]),
        )
        canceled.append(get_job(conn, row["id"]))
    return canceled


def raise_job_priority(conn: sqlite3.Connection, job_id: str, priority: int) -> dict | None:
    """Job pendente passa à frente (nunca para trás)."""
    conn.execute(
        "UPDATE jobs SET priority=? WHERE id=? AND status IN ('preparing','pending')"
        " AND priority>?",
        (int(priority), job_id, int(priority)),
    )
    return get_job(conn, job_id)


def promote_job(conn: sqlite3.Connection, job_id: str, payload: dict) -> dict | None:
    """preparing → pending com o payload completo; None se foi cancelado antes."""
    cur = conn.execute(
        "UPDATE jobs SET status='pending', payload=? WHERE id=? AND status='preparing'",
        (_dumps(payload, {}), job_id),
    )
    if cur.rowcount == 0:
        return None
    return get_job(conn, job_id)


def requeue_interrupted_jobs(conn: sqlite3.Connection) -> tuple[int, int]:
    """Depois de um reinício: retrato e fundo que rodavam voltam para a fila; o
    See interrompido vira erro (o jogador já seguiu). Devolve (refilados, falhos)."""
    failed = conn.execute(
        "UPDATE jobs SET status='error', error='interrupted by a restart', result=?,"
        " finished_at=? WHERE type='see_image' AND status IN ('preparing','pending','running')",
        (_dumps({"code": "interrupted"}, None), utcnow()),
    ).rowcount
    requeued = conn.execute(
        "UPDATE jobs SET status='pending', started_at=NULL"
        " WHERE type<>'see_image' AND status='running'"
    ).rowcount
    return requeued, failed
