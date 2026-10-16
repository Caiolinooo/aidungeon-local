"""Router /api/adventures/{id}/memory e /api/adventures/{id}/memories.

- /memory (GET, PUT): Plot Essentials, Author's Note, Story Summary.
- /memories (GET, POST): Listagem e criação no Memory Bank.
- /memories/{memory_id} (PATCH, DELETE): Edição e exclusão de memória individual.
- /memories/regenerate (POST): Regeneração / backfill de vetores das memórias.
"""

from __future__ import annotations

from typing import Any
from fastapi import APIRouter, Depends, Response, status

from app import db, embeddings_client, settings_store
from app.deps import get_http_transport
from app.errors import GameError
from app.schemas import MemoryCreate, MemoryPatch, MemoryUpdate

router = APIRouter()


def _ensure_adventure(conn, adventure_id: str) -> dict:
    adv = db.get_adventure(conn, adventure_id)
    if not adv:
        raise GameError.not_found(f"adventure {adventure_id} not found")
    return adv


def _memory_state(adventure_id: str) -> dict:
    with db.session() as conn:
        adv = _ensure_adventure(conn, adventure_id)
        return {
            "plot_essentials": adv["plot_essentials"],
            "authors_note": adv["authors_note"],
            "story_summary": adv["story_summary"],
            "memories": db.list_memories(conn, adventure_id, include_vector=False),
        }


# ------------------------------------------------------------- /memory
@router.get("/{adventure_id}/memory")
def get_memory(adventure_id: str) -> dict:
    return _memory_state(adventure_id)


@router.put("/{adventure_id}/memory")
def put_memory(adventure_id: str, body: MemoryUpdate) -> dict:
    fields = body.model_dump(exclude_unset=True)
    with db.session() as conn:
        _ensure_adventure(conn, adventure_id)
        db.update_adventure_memory(conn, adventure_id, **fields)
    return _memory_state(adventure_id)


# ----------------------------------------------------------- /memories CRUD
@router.get("/{adventure_id}/memories")
def list_memories_endpoint(adventure_id: str) -> list[dict]:
    with db.session() as conn:
        _ensure_adventure(conn, adventure_id)
        return db.list_memories(conn, adventure_id, include_vector=False)


@router.post("/{adventure_id}/memories", status_code=status.HTTP_201_CREATED)
def create_memory_endpoint(
    adventure_id: str,
    body: MemoryCreate,
    transport: Any = Depends(get_http_transport),
) -> dict:
    text = body.text.strip()
    if not text:
        raise GameError.bad_request("invalid_payload", "memory text cannot be empty")

    embedding_bytes = None
    model_name = None
    dim = None

    with db.session() as conn:
        _ensure_adventure(conn, adventure_id)
        cfg = settings_store.effective_embeddings_settings(conn)

    if cfg.get("enabled", True):
        vec = embeddings_client.get_embedding(text, cfg=cfg, transport=transport)
        if vec is not None:
            embedding_bytes = embeddings_client.pack_vector(vec)
            model_name = cfg.get("model", "Qwen3-Embedding-0.6B")
            dim = len(vec)

    with db.session() as conn:
        _ensure_adventure(conn, adventure_id)
        created = db.insert_memory(
            conn,
            adventure_id,
            text,
            seq_start=body.seq_start,
            seq_end=body.seq_end,
            embedding=embedding_bytes,
            embedding_model=model_name,
            embedding_dim=dim,
        )
    return created


@router.patch("/{adventure_id}/memories/{memory_id}")
def patch_memory_endpoint(
    adventure_id: str,
    memory_id: int,
    body: MemoryPatch,
    transport: Any = Depends(get_http_transport),
) -> dict:
    updates: dict[str, Any] = {}
    if body.seq_start is not None:
        updates["seq_start"] = body.seq_start
    if body.seq_end is not None:
        updates["seq_end"] = body.seq_end

    text = None
    if body.text is not None:
        text = body.text.strip()
        if not text:
            raise GameError.bad_request("invalid_payload", "memory text cannot be empty")
        updates["text"] = text

    with db.session() as conn:
        _ensure_adventure(conn, adventure_id)
        existing = db.get_memory(conn, memory_id)
        if not existing or existing["adventure_id"] != adventure_id:
            raise GameError.not_found(f"memory {memory_id} not found in adventure {adventure_id}")
        cfg = settings_store.effective_embeddings_settings(conn)

    if text is not None:
        vec = None
        if cfg.get("enabled", True):
            vec = embeddings_client.get_embedding(text, cfg=cfg, transport=transport)
        if vec is not None:
            updates["embedding"] = embeddings_client.pack_vector(vec)
            updates["embedding_model"] = cfg.get("model", "Qwen3-Embedding-0.6B")
            updates["embedding_dim"] = len(vec)
        else:
            updates["clear_embedding"] = True

    with db.session() as conn:
        _ensure_adventure(conn, adventure_id)
        updated = db.update_memory(
            conn,
            memory_id,
            adventure_id=adventure_id,
            **updates,
        )
    return updated


@router.delete("/{adventure_id}/memories/{memory_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_memory_endpoint(adventure_id: str, memory_id: int) -> Response:
    with db.session() as conn:
        _ensure_adventure(conn, adventure_id)
        existing = db.get_memory(conn, memory_id)
        if not existing or existing["adventure_id"] != adventure_id:
            raise GameError.not_found(f"memory {memory_id} not found in adventure {adventure_id}")
        db.delete_memory(conn, memory_id, adventure_id=adventure_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/{adventure_id}/memories/regenerate")
def regenerate_memories_endpoint(
    adventure_id: str,
    transport: Any = Depends(get_http_transport),
) -> dict:
    from memory import summarizer

    with db.session() as conn:
        _ensure_adventure(conn, adventure_id)
        count = summarizer.backfill_memories(conn, adventure_id, transport=transport, force_all=True)
    return {"status": "regenerated", "count": count}
