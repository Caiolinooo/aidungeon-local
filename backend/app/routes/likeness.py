"""Referência de pessoa real: Wikipedia, upload e remoção (contrato C8)."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, File, Form, Query, Response, UploadFile
from pydantic import BaseModel
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


def _media_type(blob: bytes) -> str:
    if blob.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if blob.startswith(b"RIFF") and blob[8:12] == b"WEBP":
        return "image/webp"
    return "image/jpeg"


@router.get("/adventures/{adventure_id}/cards/{card_id}/reference")
def get_reference(adventure_id: str, card_id: str,
                  index: int = Query(default=0, ge=0)) -> Response:
    """Devolve a foto de referência salva (upload manual ou Wikipedia)."""
    card = _card_or_404(adventure_id, card_id)
    if likeness.is_minor(card):
        raise GameError.not_found("reference not found")
    blobs = likeness.visual_reference_images(card)
    if not blobs and likeness.is_real_person(card):
        single = likeness.reference_bytes(card)
        blobs = [single] if single else []
    if not blobs or index >= len(blobs):
        raise GameError.not_found("reference not found")
    content = blobs[index]
    return Response(
        content=content,
        media_type=_media_type(content),
        headers={"Cache-Control": "private, no-store"},
    )


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
                  mode: str = Form("reference"),
                  transport=Depends(get_http_transport)) -> dict:
    """Upload manual; também atualiza os campos visuais do card pela foto."""
    card = _card_or_404(adventure_id, card_id)
    content = file.file.read(likeness.MAX_IMAGE_BYTES + 1)
    with db.session() as conn:
        current = db.get_card(conn, adventure_id, card_id) or card
        llm_cfg = settings_store.effective_llm_settings(conn)
        updated = likeness.save_upload(
            conn, current, content, filename=file.filename or "", mode=mode,
        )
        updated = likeness.apply_visual_card(
            conn, updated, content, llm_cfg=llm_cfg, transport=transport,
        )
        return db.attach_portrait(conn, updated)

class PrimaryReferenceRequest(BaseModel):
    image_path: str


@router.post("/adventures/{adventure_id}/cards/{card_id}/reference/primary")
def set_primary_reference(adventure_id: str, card_id: str, body: PrimaryReferenceRequest) -> dict:
    card = _card_or_404(adventure_id, card_id)
    with db.session() as conn:
        current = db.get_card(conn, adventure_id, card_id) or card
        updated = likeness.set_primary_reference(conn, current, body.image_path)
        return db.attach_portrait(conn, updated)


@router.delete("/adventures/{adventure_id}/cards/{card_id}/reference")
def delete_reference(adventure_id: str, card_id: str) -> dict:
    card = _card_or_404(adventure_id, card_id)
    with db.session() as conn:
        current = db.get_card(conn, adventure_id, card_id) or card
        updated = likeness.clear_reference(conn, current)
        return db.attach_portrait(conn, updated)
