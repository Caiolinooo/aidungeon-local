"""Router /api/scenarios — biblioteca de scenarios (CRUD completo v0.9.0)."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends

from app import auth, db
from app.errors import GameError
from app.schemas import ScenarioCreate, ScenarioUpdate

log = logging.getLogger("app.scenarios")

router = APIRouter()


@router.post("", status_code=201)
def create_scenario(body: ScenarioCreate,
                    info: auth.AuthInfo = Depends(auth.require_auth)) -> dict:
    log.info("scenarios: %s criou o cenário %r", info.user, body.title)
    with db.session() as conn:
        return db.insert_scenario(
            conn,
            title=body.title,
            description=body.description,
            ai_instructions=body.ai_instructions,
            plot_essentials=body.plot_essentials,
            authors_note=body.authors_note,
            opening_text=body.opening_text,
            image_style=body.image_style,
            tags=body.tags,
            story_cards=[c.model_dump(exclude_none=True) for c in body.story_cards],
            third_person=body.third_person,
        )


@router.get("")
def list_scenarios(tag: str | None = None, q: str | None = None) -> list[dict]:
    with db.session() as conn:
        return db.list_scenarios(conn, tag=tag, q=q)


@router.get("/{scenario_id}")
def get_scenario(scenario_id: str) -> dict:
    with db.session() as conn:
        scenario = db.get_scenario(conn, scenario_id)
    if not scenario:
        raise GameError.not_found(f"scenario {scenario_id} not found")
    return scenario


@router.patch("/{scenario_id}")
@router.put("/{scenario_id}")
def patch_scenario(scenario_id: str, body: ScenarioUpdate) -> dict:
    fields = body.model_dump(exclude_unset=True)
    if "story_cards" in fields and fields["story_cards"] is not None:
        fields["story_cards"] = [
            c.model_dump(exclude_none=True) if hasattr(c, "model_dump") else c
            for c in body.story_cards
        ]
    with db.session() as conn:
        updated = db.update_scenario(conn, scenario_id, **fields)
    if not updated:
        raise GameError.not_found(f"scenario {scenario_id} not found")
    return updated


@router.delete("/{scenario_id}")
def delete_scenario(scenario_id: str) -> dict:
    with db.session() as conn:
        if not db.delete_scenario(conn, scenario_id):
            raise GameError.not_found(f"scenario {scenario_id} not found")
    return {"deleted": True, "id": scenario_id}

