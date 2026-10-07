"""Router /api/adventures/{id}/memory — Plot Essentials + Author's Note."""

from __future__ import annotations

from fastapi import APIRouter

from app import db
from app.errors import GameError
from app.schemas import MemoryUpdate

router = APIRouter()


def _memory_state(adventure_id: str) -> dict:
    with db.session() as conn:
        adv = db.get_adventure(conn, adventure_id)
        if not adv:
            raise GameError.not_found(f"adventure {adventure_id} not found")
        return {
            "plot_essentials": adv["plot_essentials"],
            "authors_note": adv["authors_note"],
            "story_summary": adv["story_summary"],
        }


@router.get("/{adventure_id}/memory")
def get_memory(adventure_id: str) -> dict:
    return _memory_state(adventure_id)


@router.put("/{adventure_id}/memory")
def put_memory(adventure_id: str, body: MemoryUpdate) -> dict:
    fields = body.model_dump(exclude_unset=True)
    with db.session() as conn:
        if not db.get_adventure(conn, adventure_id):
            raise GameError.not_found(f"adventure {adventure_id} not found")
        db.update_adventure_memory(conn, adventure_id, **fields)
    return _memory_state(adventure_id)
