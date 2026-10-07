"""Router /api/adventures/{id}/cards — CRUD de story cards."""

from __future__ import annotations

from fastapi import APIRouter, Response

from app import db
from app.errors import GameError
from app.schemas import StoryCardUpsert

router = APIRouter()


def _adventure_or_404(adventure_id: str) -> None:
    with db.session() as conn:
        if not db.get_adventure(conn, adventure_id):
            raise GameError.not_found(f"adventure {adventure_id} not found")


@router.post("/{adventure_id}/cards", status_code=201)
def create_card(adventure_id: str, body: StoryCardUpsert) -> dict:
    _adventure_or_404(adventure_id)
    with db.session() as conn:
        return db.insert_card(
            conn, adventure_id,
            title=body.title or "", type=body.type or "other",
            triggers=body.triggers or [], entry=body.entry or "",
            pinned=bool(body.pinned),
            use_for_character_creation=bool(body.use_for_character_creation),
        )


@router.get("/{adventure_id}/cards")
def list_cards(adventure_id: str) -> list[dict]:
    _adventure_or_404(adventure_id)
    with db.session() as conn:
        return db.list_cards(conn, adventure_id)


@router.get("/{adventure_id}/cards/{card_id}")
def get_card(adventure_id: str, card_id: str) -> dict:
    with db.session() as conn:
        card = db.get_card(conn, adventure_id, card_id)
    if not card:
        raise GameError.not_found(f"card {card_id} not found")
    return card


@router.patch("/{adventure_id}/cards/{card_id}")
def update_card(adventure_id: str, card_id: str, body: StoryCardUpsert) -> dict:
    fields = body.model_dump(exclude_unset=True)
    with db.session() as conn:
        card = db.update_card(conn, adventure_id, card_id, fields)
    if not card:
        raise GameError.not_found(f"card {card_id} not found")
    return card


@router.delete("/{adventure_id}/cards/{card_id}", status_code=204)
def delete_card(adventure_id: str, card_id: str) -> Response:
    with db.session() as conn:
        ok = db.delete_card(conn, adventure_id, card_id)
    if not ok:
        raise GameError.not_found(f"card {card_id} not found")
    return Response(status_code=204)
