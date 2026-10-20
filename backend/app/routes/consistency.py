"""Cruzamento ponta a ponta da aventura (ficha ↔ retrato ↔ memória ↔ cena).

`GET /adventures/{id}/consistency` audita a cadeia e devolve o relatório.
`POST /adventures/{id}/consistency/repair` refaz o elo quebrado (ficha,
retrato ou embedding de memória) e devolve o que foi refeito.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from app import auth, consistency, db, settings_store
from app.deps import get_http_transport
from app.errors import GameError

router = APIRouter()


class ConsistencyRepairRequest(BaseModel):
    check: str = Field(description="id do check: card_sheets, portraits ou memories")


def _visible_adventure(conn, adventure_id: str, username: str) -> dict:
    adv = db.get_adventure(conn, adventure_id)
    if not adv or not db.can_access_adventure(conn, adv, username):
        raise GameError.not_found(f"adventure {adventure_id} not found")
    return adv


@router.get("/{adventure_id}/consistency")
def get_consistency(adventure_id: str,
                    info: auth.AuthInfo = Depends(auth.require_auth)) -> dict:
    """Audita a cadeia da aventura: fichas, retratos, memórias, mentes, cânone, cena."""
    with db.session() as conn:
        _visible_adventure(conn, adventure_id, info.user)
        return consistency.audit(conn, adventure_id)


@router.post("/{adventure_id}/consistency/repair")
def post_consistency_repair(adventure_id: str,
                             body: ConsistencyRepairRequest,
                             info: auth.AuthInfo = Depends(auth.require_auth),
                             transport: Any = Depends(get_http_transport)) -> dict:
    """Refaz o elo quebrado: ficha do NPC, retrato ou embedding das memórias."""
    with db.session() as conn:
        _visible_adventure(conn, adventure_id, info.user)
        llm_cfg = settings_store.effective_llm_settings(conn)
        result = consistency.repair(conn, adventure_id, body.check,
                                    llm_cfg=llm_cfg, transport=transport)
        result["report"] = consistency.audit(conn, adventure_id)
        return result
