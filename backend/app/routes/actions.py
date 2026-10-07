"""Router /api/actions — Edit (PATCH marca edited_at)."""

from __future__ import annotations

from fastapi import APIRouter

from app import game_engine
from app.schemas import ActionPatch

router = APIRouter()


@router.patch("/{action_id}")
def patch_action(action_id: int, body: ActionPatch) -> dict:
    return game_engine.handle_edit(action_id, body.text)
