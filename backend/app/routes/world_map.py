"""Mapa vivo, journal e geração de locais por IA."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from app import db, image_client, jobs, settings_store, world_map
from app.deps import get_http_transport
from app.errors import GameError

router = APIRouter()


class LocationGenerateRequest(BaseModel):
    name: str = Field(min_length=2)
    reference: str = ""
    web_search: bool = False


class LocationImageRequest(BaseModel):
    reference: str = ""


@router.get("/adventures/{adventure_id}/map")
def get_map(adventure_id: str) -> dict:
    with db.session() as conn:
        if not db.get_adventure(conn, adventure_id):
            raise GameError.not_found("adventure not found")
        return world_map.map_payload(conn, adventure_id)


@router.get("/adventures/{adventure_id}/journal")
def get_journal(adventure_id: str, limit: int = 40) -> dict:
    with db.session() as conn:
        if not db.get_adventure(conn, adventure_id):
            raise GameError.not_found("adventure not found")
        return {"entries": world_map.journal_for(conn, adventure_id, limit=limit)}


@router.post("/adventures/{adventure_id}/map/travel/{card_id}")
def post_travel(adventure_id: str, card_id: str) -> dict:
    with db.session() as conn:
        if not db.get_adventure(conn, adventure_id):
            raise GameError.not_found("adventure not found")
        try:
            result = world_map.travel(conn, adventure_id, card_id)
        except ValueError as exc:
            raise GameError(409, "travel_not_allowed", str(exc)) from exc
        return result


@router.post("/adventures/{adventure_id}/map/locations")
def post_generate_location(adventure_id: str, body: LocationGenerateRequest,
                           transport: Any = Depends(get_http_transport)) -> dict:
    with db.session() as conn:
        if not db.get_adventure(conn, adventure_id):
            raise GameError.not_found("adventure not found")
        llm_cfg = settings_store.effective_llm_settings(conn)
        try:
            result = world_map.generate_location(
                conn, adventure_id, name=body.name, reference=body.reference,
                llm_cfg=llm_cfg, transport=transport, web_search=body.web_search,
            )
        except ValueError as exc:
            raise GameError(422, "location_generation_failed", str(exc)) from exc
        return result


@router.post("/adventures/{adventure_id}/map/locations/{card_id}/image")
def post_location_image(adventure_id: str, card_id: str,
                        body: LocationImageRequest,
                        transport: Any = Depends(get_http_transport)) -> dict:
    with db.session() as conn:
        adventure = db.get_adventure(conn, adventure_id)
        card = db.get_card(conn, adventure_id, card_id)
        if not adventure or not card or (card.get("type") or "") != "location":
            raise GameError.not_found("location not found")
        prompt = world_map.generate_location_prompt(conn, adventure_id, card, adventure)
        if body.reference.strip():
            prompt = f"{prompt}, visual reference: {body.reference.strip()}"[:1400]
        img_cfg = image_client.effective_settings(conn)
        if not img_cfg["enabled"]:
            raise GameError(503, "image_generation_disabled", "Image generation is disabled")
        cache_key = image_client.request_key(img_cfg, prompt, width=1344, height=768)
        cached = db.find_cached_image(conn, adventure_id, kind="location", ref_id=card_id,
                                      cache_key=cache_key)
        if cached:
            world_map.attach_location_image(conn, adventure_id, card_id, cached["id"],
                                            state_key=cache_key[:40])
            return {"status": "ready", "image": db.image_ref(cached)}
        job = jobs.enqueue(
            conn, "location_image", adventure_id, card_id,
            {"prompt": prompt, "cache_key": cache_key},
            priority=jobs.PRIORITY_BACKDROP,
            supersede_key=f"location:{card_id}",
        )
        return {"status": "pending", "job_id": job["id"]}
