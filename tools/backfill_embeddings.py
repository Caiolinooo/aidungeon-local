#!/usr/bin/env python3
"""Backfill vector embeddings for AI Dungeon Local memories.

Iterates over memories in SQLite where embedding is NULL or model/dim differs,
generates embeddings via the CPU embeddings client, and updates the database rows.
Idempotent and restartable.
"""

from __future__ import annotations

import argparse
import logging
import os
import sqlite3
import struct
import sys
from pathlib import Path
from typing import Any, Callable

# Add backend to sys.path
REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND_DIR = REPO_ROOT / "backend"
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

try:
    from app import db, embeddings_client, settings_store
except ImportError:
    from backend.app import db, embeddings_client, settings_store  # type: ignore

log = logging.getLogger("aidungeon.backfill")


def backfill_memories(
    conn: sqlite3.Connection,
    *,
    adventure_id: str | None = None,
    model: str | None = None,
    dim: int | None = None,
    base_url: str | None = None,
    timeout_seconds: float | None = None,
    batch_size: int = 16,
    force: bool = False,
    dry_run: bool = False,
    client: Any = None,
    transport: Any = None,
    on_progress: Callable[[int, int, dict], None] | None = None,
) -> dict[str, int]:
    """Backfills embeddings into the memories table.

    Returns a dict with statistics:
    {"total": int, "processed": int, "updated": int, "failed": int, "skipped": int}
    """
    file_settings = settings_store.load_file_settings()
    emb_cfg = dict(file_settings.get("embeddings", {}))

    target_model = model or emb_cfg.get("model", "Qwen3-Embedding-0.6B")
    target_dim = dim or emb_cfg.get("dimension", 1024)
    target_base_url = base_url or emb_cfg.get("base_url", "http://127.0.0.1:8081/v1")
    timeout = timeout_seconds or float(emb_cfg.get("timeout_seconds", 30))

    active_client = client or embeddings_client
    call_cfg = {
        "base_url": target_base_url,
        "model": target_model,
        "timeout_seconds": timeout,
        "enabled": True,
    }

    if force:
        # Re-embed all memories
        sql = "SELECT * FROM memories"
        params: list[Any] = []
        if adventure_id:
            sql += " WHERE adventure_id = ?"
            params.append(adventure_id)
        sql += " ORDER BY id ASC"
        rows = conn.execute(sql, params).fetchall()
        targets = [db.memory_to_dict(r, include_embedding=True) for r in rows]
    else:
        targets = db.list_unembedded_memories(
            conn, model=target_model, dim=target_dim, adventure_id=adventure_id
        )

    total = len(targets)
    if total == 0:
        log.info("No unembedded memories found. Database is fully up-to-date.")
        return {"total": 0, "processed": 0, "updated": 0, "failed": 0, "skipped": 0}

    if dry_run:
        log.info("Dry run: %d memories need embeddings (model=%s, dim=%d).",
                 total, target_model, target_dim)
        return {"total": total, "processed": 0, "updated": 0, "failed": 0, "skipped": total}

    log.info("Starting backfill for %d memories (model=%s, dim=%d)...",
             total, target_model, target_dim)

    updated = 0
    failed = 0
    processed = 0

    for i in range(0, total, batch_size):
        chunk = targets[i:i + batch_size]
        texts = [m["text"] for m in chunk]

        # Attempt batch generation if supported
        vectors: list[list[float] | None] | None = None
        if hasattr(active_client, "get_embeddings_batch"):
            try:
                vectors = active_client.get_embeddings_batch(
                    texts, cfg=call_cfg, transport=transport
                )
            except Exception:
                vectors = None

        if vectors is None or len(vectors) != len(chunk):
            # Fall back to sequential generation
            vectors = []
            for text in texts:
                try:
                    vec = active_client.get_embedding(
                        text, cfg=call_cfg, transport=transport
                    )
                except Exception:
                    log.warning("Embedding request threw an error for text: %s...", text[:40], exc_info=True)
                    vec = None
                vectors.append(vec)

        for memory, vec in zip(chunk, vectors):
            processed += 1
            if vec is not None and len(vec) > 0:
                blob = struct.pack(f"<{len(vec)}f", *vec)
                db.update_memory(
                    conn,
                    memory["id"],
                    embedding=blob,
                    embedding_model=target_model,
                    embedding_dim=len(vec),
                )
                conn.commit()  # Immediate commit for crash-resilience
                updated += 1
            else:
                failed += 1
                log.warning("Failed to obtain embedding for memory id=%s", memory["id"])

            if on_progress:
                on_progress(processed, total, memory)

    log.info("Backfill complete. Total: %d, Updated: %d, Failed: %d.",
             total, updated, failed)
    return {
        "total": total,
        "processed": processed,
        "updated": updated,
        "failed": failed,
        "skipped": total - processed,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Backfill memory embeddings")
    parser.add_argument("--db-path", default=None, help="Path to SQLite database")
    parser.add_argument("--adventure-id", default=None, help="Backfill specific adventure ID")
    parser.add_argument("--model", default=None, help="Model name (e.g. Qwen3-Embedding-0.6B)")
    parser.add_argument("--dim", type=int, default=None, help="Vector dimension (e.g. 1024)")
    parser.add_argument("--base-url", default=None, help="Embeddings server URL")
    parser.add_argument("--batch-size", type=int, default=16, help="Batch size (default: 16)")
    parser.add_argument("--force", action="store_true", help="Re-embed memories even if already embedded")
    parser.add_argument("--dry-run", action="store_true", help="Inspect without updating database")
    parser.add_argument("--verbose", "-v", action="store_true", help="Enable verbose debug logging")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    if args.db_path:
        os.environ["AIDUNGEON_DB_PATH"] = args.db_path

    db.init_db()
    with db.session() as conn:
        stats = backfill_memories(
            conn,
            adventure_id=args.adventure_id,
            model=args.model,
            dim=args.dim,
            base_url=args.base_url,
            batch_size=args.batch_size,
            force=args.force,
            dry_run=args.dry_run,
        )
        print(f"Summary: {stats}")
        if stats["failed"] > 0:
            sys.exit(1)


if __name__ == "__main__":
    main()
