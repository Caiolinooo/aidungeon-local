"""Router /api/scenarios — biblioteca de scenarios."""

from __future__ import annotations

from fastapi import APIRouter

from app import db
from app.errors import GameError
from app.schemas import ScenarioCreate

router = APIRouter()


@router.post("", status_code=201)
def create_scenario(body: ScenarioCreate) -> dict:
    with db.session() as conn:
        return db.insert_scenario(conn, title=body.title, description=body.description,
                                  ai_instructions=body.ai_instructions,
                                  plot_essentials=body.plot_essentials,
                                  authors_note=body.authors_note,
                                  opening_text=body.opening_text,
                                  tags=body.tags, third_person=body.third_person)


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
