"""Referência de pessoa real: Wikipedia, upload e remoção (contrato C8)."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, File, Form, Query, UploadFile

from app import db, likeness, settings_store
from app.deps import get_http_transport
from app.errors import GameError

router = APIRouter()


def _card_or_404(adventure_id: str, card_id: str) -> dict:
    with db.session() as conn:
        if not db.get_adventure(conn, adventure_id):
            raise GameError.not_found(f"adventure {adventure_id} not found")
        card = db.get_card(conn, adventure_id, card_id)
    if not card:
        raise GameError.not_found(f"card {card_id} not found")
    return card


@router.post("/adventures/{adventure_id}/cards/{card_id}/likeness/resolve")
def resolve_likeness(adventure_id: str, card_id: str,
                     title: str | None = Query(default=None),
                     force: bool = Query(default=False),
                     transport: Any = Depends(get_http_transport)) -> dict:
    """Busca a Wikipedia, grava a foto em data/references e o descritor na ficha."""
    card = _card_or_404(adventure_id, card_id)
    with db.session() as conn:
        current = db.get_card(conn, adventure_id, card_id) or card
        llm_cfg = settings_store.effective_llm_settings(conn)
        updated = likeness.resolve_card(
            conn, current, query=title, transport=transport,
            llm_cfg=llm_cfg, force=force,
        )
        return db.attach_portrait(conn, updated)


@router.put("/adventures/{adventure_id}/cards/{card_id}/reference")
def put_reference(adventure_id: str, card_id: str,
                  file: UploadFile = File(...),
                  mode: str = Form("reference")) -> dict:
    """Upload manual. `reference` é NPC fictício; `real_person` marca a ficha."""
    card = _card_or_404(adventure_id, card_id)
    content = file.file.read(likeness.MAX_IMAGE_BYTES + 1)
    with db.session() as conn:
        current = db.get_card(conn, adventure_id, card_id) or card
        updated = likeness.save_upload(
            conn, current, content, filename=file.filename or "", mode=mode,
        )
        return db.attach_portrait(conn, updated)


@router.delete("/adventures/{adventure_id}/cards/{card_id}/reference")
def delete_reference(adventure_id: str, card_id: str) -> dict:
    card = _card_or_404(adventure_id, card_id)
    with db.session() as conn:
        current = db.get_card(conn, adventure_id, card_id) or card
        updated = likeness.clear_reference(conn, current)
        return db.attach_portrait(conn, updated)
