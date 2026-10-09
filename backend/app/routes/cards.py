"""Router /api/adventures/{id}/cards — CRUD de story cards."""

from __future__ import annotations

import json

from fastapi import APIRouter, Depends, Response

from app import db, npc_assets, settings_store
from app.deps import get_http_transport
from app.errors import GameError
from app.schemas import StoryCardUpsert

router = APIRouter()


def _adventure_or_404(adventure_id: str) -> None:
    with db.session() as conn:
        if not db.get_adventure(conn, adventure_id):
            raise GameError.not_found(f"adventure {adventure_id} not found")


def _sheet_str(sheet: dict | None) -> str:
    """Ficha do body (dict) → JSON canônico p/ a coluna; None/∅ → ''."""
    return json.dumps(sheet, ensure_ascii=False) if sheet else ""


@router.post("/{adventure_id}/cards", status_code=201)
def create_card(adventure_id: str, body: StoryCardUpsert) -> dict:
    _adventure_or_404(adventure_id)
    with db.session() as conn:
        card = db.insert_card(
            conn, adventure_id,
            title=body.title or "", type=body.type or "other",
            triggers=body.triggers or [], entry=body.entry or "",
            pinned=bool(body.pinned),
            use_for_character_creation=bool(body.use_for_character_creation),
            portrait_prompt=body.portrait_prompt or "",
            voice=body.voice or "", vrm_id=body.vrm_id or "",
            sheet=_sheet_str(body.sheet),
        )
        return db.attach_portrait(conn, card)


@router.get("/{adventure_id}/cards")
def list_cards(adventure_id: str) -> list[dict]:
    _adventure_or_404(adventure_id)
    with db.session() as conn:
        return [db.attach_portrait(conn, c) for c in db.list_cards(conn, adventure_id)]


@router.get("/{adventure_id}/cards/{card_id}")
def get_card(adventure_id: str, card_id: str) -> dict:
    with db.session() as conn:
        card = db.get_card(conn, adventure_id, card_id)
        if card:
            db.attach_portrait(conn, card)
    if not card:
        raise GameError.not_found(f"card {card_id} not found")
    return card


@router.patch("/{adventure_id}/cards/{card_id}")
def update_card(adventure_id: str, card_id: str, body: StoryCardUpsert) -> dict:
    fields = body.model_dump(exclude_unset=True)
    if "sheet" in fields:
        fields["sheet"] = _sheet_str(fields["sheet"])
    with db.session() as conn:
        card = db.update_card(conn, adventure_id, card_id, fields)
        if card:
            db.attach_portrait(conn, card)
    if not card:
        raise GameError.not_found(f"card {card_id} not found")
    return card


def _card_or_404(conn, adventure_id: str, card_id: str) -> tuple[dict, dict]:
    adv = db.get_adventure(conn, adventure_id)
    if not adv:
        raise GameError.not_found(f"adventure {adventure_id} not found")
    card = db.get_card(conn, adventure_id, card_id)
    if not card:
        raise GameError.not_found(f"card {card_id} not found")
    return adv, card


@router.post("/{adventure_id}/cards/{card_id}/enrich")
def enrich_card(adventure_id: str, card_id: str,
                transport=Depends(get_http_transport)) -> dict:
    """v0.5.0: LLM preenche a ficha estruturada + portrait_prompt do card."""
    with db.session() as conn:
        adv, card = _card_or_404(conn, adventure_id, card_id)
        llm_cfg = settings_store.effective_llm_settings(conn)
        card = npc_assets.enrich_card(conn, adv, card, llm_cfg, transport=transport)
        return db.attach_portrait(conn, card)


@router.delete("/{adventure_id}/cards/{card_id}", status_code=204)
def delete_card(adventure_id: str, card_id: str) -> Response:
    with db.session() as conn:
        ok = db.delete_card(conn, adventure_id, card_id)
    if not ok:
        raise GameError.not_found(f"card {card_id} not found")
    return Response(status_code=204)
