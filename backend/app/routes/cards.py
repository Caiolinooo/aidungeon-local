"""Router /api/adventures/{id}/cards — CRUD de story cards."""

from __future__ import annotations

import json

from fastapi import APIRouter, Depends, Response

from app import db, events, llm_client, npc_assets, settings_store
from app.deps import get_http_transport
from app.errors import GameError
from app.schemas import CardBulkAction, CardRegenerateRequest, StoryCardUpsert

router = APIRouter()


def _adventure_or_404(adventure_id: str) -> None:
    with db.session() as conn:
        if not db.get_adventure(conn, adventure_id):
            raise GameError.not_found(f"adventure {adventure_id} not found")


def _sheet_str(sheet: dict | None) -> str:
    """Ficha do body (dict) → JSON canônico p/ a coluna; None/∅ → ''."""
    return json.dumps(sheet, ensure_ascii=False) if sheet else ""


def _publish_card(adventure_id: str, card_id: str, action: str) -> None:
    """Avisa a mesa depois do commit. action: created|updated|deleted|regenerated."""
    events.publish(adventure_id, "card_changed",
                   {"card_id": card_id, "action": action})


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
        card = db.attach_portrait(conn, card)
    _publish_card(adventure_id, card["id"], "created")
    return card


@router.get("/{adventure_id}/cards")
def list_cards(
    adventure_id: str,
    q: str | None = None,
    type: str | None = None,
    pinned: bool | None = None,
) -> list[dict]:
    _adventure_or_404(adventure_id)
    with db.session() as conn:
        return [
            db.attach_portrait(conn, c)
            for c in db.list_cards(conn, adventure_id, q=q, card_type=type, pinned=pinned)
        ]


@router.post("/{adventure_id}/cards/bulk")
def bulk_cards(adventure_id: str, body: CardBulkAction) -> dict:
    _adventure_or_404(adventure_id)
    if body.action == "retype":
        if not body.new_type or not body.new_type.strip():
            raise GameError.bad_request("invalid_payload", "new_type is required for retype action")
    with db.session() as conn:
        new_type = body.new_type.strip() if body.new_type else None
        updated_count = db.bulk_cards_action(
            conn, adventure_id, body.action, body.card_ids, new_type=new_type
        )
    if updated_count:
        kind = "deleted" if body.action == "delete" else "updated"
        for card_id in body.card_ids:
            _publish_card(adventure_id, card_id, kind)
    return {"updated": updated_count, "action": body.action}


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
    with db.session() as conn:
        current = db.get_card(conn, adventure_id, card_id)
        if current and "sheet" in fields:
            fields["sheet"] = db._merge_player_sheet(
                current.get("sheet") or "", _sheet_str(fields["sheet"]))
        card = db.update_card(conn, adventure_id, card_id, fields)
        if card and "sheet" in fields:
            try:
                from app import portrait_sync
                portrait_sync.sync_card(conn, adventure_id, card_id)
                card = db.get_card(conn, adventure_id, card_id)
            except Exception:
                pass
        if card:
            db.attach_portrait(conn, card)
    if not card:
        raise GameError.not_found(f"card {card_id} not found")
    _publish_card(adventure_id, card_id, "updated")
    return card


def _card_or_404(conn, adventure_id: str, card_id: str) -> tuple[dict, dict]:
    adv = db.get_adventure(conn, adventure_id)
    if not adv:
        raise GameError.not_found(f"adventure {adventure_id} not found")
    card = db.get_card(conn, adventure_id, card_id)
    if not card:
        raise GameError.not_found(f"card {card_id} not found")
    return adv, card


@router.post("/{adventure_id}/cards/{card_id}/regenerate")
def regenerate_card(adventure_id: str, card_id: str, body: CardRegenerateRequest,
                    transport=Depends(get_http_transport)) -> dict:
    """Regenera um campo da ficha, ou os nove textos vivos."""
    with db.session() as conn:
        adv, card = _card_or_404(conn, adventure_id, card_id)
        llm_cfg = settings_store.effective_llm_settings(conn)
        try:
            result = npc_assets.regenerate_card(
                conn, adv, card, llm_cfg, scope=body.scope, field=body.field,
                overwrite=body.overwrite, transport=transport,
            )
        except llm_client.LlmError as exc:
            raise GameError(503, "llm_unreachable", "LLM service unavailable") from exc
    _publish_card(adventure_id, card_id, "regenerated")
    return result


@router.post("/{adventure_id}/cards/{card_id}/enrich")
def enrich_card(adventure_id: str, card_id: str,
                transport=Depends(get_http_transport)) -> dict:
    """v0.5.0: LLM preenche a ficha estruturada + portrait_prompt do card."""
    with db.session() as conn:
        adv, card = _card_or_404(conn, adventure_id, card_id)
        llm_cfg = settings_store.effective_llm_settings(conn)
        try:
            card = npc_assets.enrich_card(conn, adv, card, llm_cfg, transport=transport)
        except llm_client.LlmError as exc:
            raise GameError(503, "llm_unreachable", "LLM service unavailable") from exc
        card = db.attach_portrait(conn, card)
    _publish_card(adventure_id, card_id, "updated")
    return card


@router.get("/{adventure_id}/cards/{card_id}/memories")
def list_card_memories(adventure_id: str, card_id: str) -> dict:
    from app import npc_life
    with db.session() as conn:
        if not db.get_card(conn, adventure_id, card_id):
            raise GameError.not_found(f"card {card_id} not found")
        return {"memories": npc_life.list_memories(conn, adventure_id, card_id, limit=20)}


@router.delete("/{adventure_id}/cards/{card_id}/portrait", status_code=204)
def delete_portrait(adventure_id: str, card_id: str) -> Response:
    """Apaga o retrato e a âncora. 204 mesmo se já não havia imagem."""
    with db.session() as conn:
        card = db.get_card(conn, adventure_id, card_id)
        if not card:
            raise GameError.not_found(f"card {card_id} not found")
        seen: list[str] = []
        for key in ("portrait_image_id", "portrait_anchor_id"):
            image_id = str(card.get(key) or "")
            if image_id and image_id not in seen:
                seen.append(image_id)
        for image_id in seen:
            db.delete_image(conn, image_id)
        db.clear_card_portrait(conn, card_id)
    _publish_card(adventure_id, card_id, "updated")
    return Response(status_code=204)


@router.delete("/{adventure_id}/cards/{card_id}", status_code=204)
def delete_card(adventure_id: str, card_id: str) -> Response:
    with db.session() as conn:
        ok = db.delete_card(conn, adventure_id, card_id)
    if not ok:
        raise GameError.not_found(f"card {card_id} not found")
    _publish_card(adventure_id, card_id, "deleted")
    return Response(status_code=204)

