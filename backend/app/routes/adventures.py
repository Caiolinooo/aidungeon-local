"""Router /api/adventures — ciclo de vida da aventura + undo."""

from __future__ import annotations

from fastapi import APIRouter

from app import db, game_engine
from app.errors import GameError
from app.schemas import AdventureCreate

router = APIRouter()


@router.post("", status_code=201)
def create_adventure(body: AdventureCreate) -> dict:
    """Nova aventura a partir de scenario (herda campos + action start) ou vazia."""
    with db.session() as conn:
        if body.scenario_id:
            sc = db.get_scenario(conn, body.scenario_id)
            if not sc:
                raise GameError.not_found(f"scenario {body.scenario_id} not found")
            adv = db.insert_adventure(
                conn, scenario_id=sc["id"],
                title=body.title or sc["title"],
                description=sc["description"],
                ai_instructions=sc["ai_instructions"],
                plot_essentials=sc["plot_essentials"],
                authors_note=sc["authors_note"],
                tags=body.tags if body.tags is not None else sc["tags"],
                third_person=sc["third_person"], source="new",
            )
            db.insert_action(conn, adv["id"], 0, "start", sc["opening_text"])
        else:
            adv = db.insert_adventure(conn, title=body.title or "Untitled Adventure",
                                      tags=body.tags or [], source="new")
        return db.adventure_state(conn, adv["id"])


@router.get("")
def list_adventures() -> list[dict]:
    with db.session() as conn:
        return db.list_adventure_summaries(conn)


@router.get("/{adventure_id}")
def get_adventure(adventure_id: str) -> dict:
    with db.session() as conn:
        state = db.adventure_state(conn, adventure_id)
    if not state:
        raise GameError.not_found(f"adventure {adventure_id} not found")
    return state


@router.post("/{adventure_id}/undo")
def post_undo(adventure_id: str) -> dict:
    return game_engine.handle_undo(adventure_id)
