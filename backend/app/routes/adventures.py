"""Router /api/adventures — ciclo de vida da aventura + undo."""

from __future__ import annotations

import json
import urllib.parse
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Response

import exporter
from app import auth, db, game_engine, jobs
from app.errors import GameError
from app.schemas import AdventureCreate, AdventureUpdate
from app.users import is_admin

router = APIRouter()


@router.post("", status_code=201)
def create_adventure(body: AdventureCreate,
                     info: auth.AuthInfo = Depends(auth.require_auth)) -> dict:
    """Nova aventura a partir de scenario (herda campos + action start) ou vazia."""
    with db.session() as conn:
        if body.scenario_id:
            sc = db.get_scenario(conn, body.scenario_id)
            if not sc:
                raise GameError.not_found(f"scenario {body.scenario_id} not found")
            adv = db.insert_adventure(
                conn, scenario_id=sc["id"], owner=info.user,
                title=body.title or sc["title"],
                description=sc["description"],
                ai_instructions=body.ai_instructions if body.ai_instructions is not None else sc["ai_instructions"],
                plot_essentials=body.plot_essentials if body.plot_essentials is not None else sc["plot_essentials"],
                authors_note=body.authors_note if body.authors_note is not None else sc["authors_note"],
                image_style=body.image_style if body.image_style is not None else (sc.get("image_style") or ""),
                tags=body.tags if body.tags is not None else sc["tags"],
                third_person=body.third_person if body.third_person is not None else sc["third_person"],
                source="new",
            )
            db.insert_action(conn, adv["id"], 0, "start", sc["opening_text"])
            # v2: cards do rascunho do assistente trilham scenario → aventura
            for card in sc["story_cards"]:
                if not isinstance(card, dict):
                    continue
                db.insert_card(
                    conn, adv["id"],
                    title=card.get("title") or "", type=card.get("type") or "other",
                    triggers=card.get("triggers") or [], entry=card.get("entry") or "",
                    pinned=bool(card.get("pinned")),
                    use_for_character_creation=bool(card.get("use_for_character_creation")),
                    portrait_prompt=card.get("portrait_prompt") or "",
                    # v0.5.0: ficha estruturada trilha scenario → aventura
                    sheet=(json.dumps(card["sheet"], ensure_ascii=False)
                           if isinstance(card.get("sheet"), dict) else ""),
                )
            # v0.3.0: pipeline assíncrono — retratos dos NPCs começam a gerar
            # em background; a UI aplica cada um quando o job conclui.
            jobs.enqueue_all_portraits(conn, adv["id"])
        else:
            adv = db.insert_adventure(
                conn, owner=info.user,
                title=body.title or "Untitled Adventure",
                ai_instructions=body.ai_instructions or "",
                plot_essentials=body.plot_essentials or "",
                authors_note=body.authors_note or "",
                image_style=body.image_style or "",
                tags=body.tags or [],
                third_person=bool(body.third_person),
                source="new",
            )
        return db.adventure_state(conn, adv["id"])


@router.get("")
def list_adventures(info: auth.AuthInfo = Depends(auth.require_auth)) -> list[dict]:
    with db.session() as conn:
        rows = db.list_adventure_summaries(conn)
    if is_admin(info.user):
        return rows
    return [row for row in rows if (row.get("owner") or "") in ("", info.user)]


@router.get("/export")
def export_all_adventures(info: auth.AuthInfo = Depends(auth.require_auth)) -> Response:
    """Backup completo: um .zip com uma pasta por aventura (formato do backup
    individual) — restaurável via POST /api/import/aidungeon.

    Registrada ANTES de /{adventure_id} para não ser capturada por ele.
    """
    data, count = exporter.export_all_zip(None if is_admin(info.user) else info.user)
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    quoted = urllib.parse.quote(f"aidungeon-backup-{stamp}.zip")
    headers = {
        "Content-Disposition": (
            f"attachment; filename=\"aidungeon-backup.zip\"; filename*=UTF-8''{quoted}"
        ),
        "X-Adventure-Count": str(count),
    }
    return Response(content=data, media_type="application/zip", headers=headers)


@router.get("/{adventure_id}")
def get_adventure(adventure_id: str) -> dict:
    with db.session() as conn:
        state = db.adventure_state(conn, adventure_id)
    if not state:
        raise GameError.not_found(f"adventure {adventure_id} not found")
    return state


@router.patch("/{adventure_id}")
def patch_adventure(adventure_id: str, body: AdventureUpdate) -> dict:
    """Edita metadados (título/descrição/tags). Campos ausentes não mudam."""
    with db.session() as conn:
        updated = db.update_adventure(conn, adventure_id,
                                      **body.model_dump(exclude_unset=True))
        if not updated:
            raise GameError.not_found(f"adventure {adventure_id} not found")
        return db.adventure_state(conn, adventure_id)


@router.delete("/{adventure_id}")
def delete_adventure(adventure_id: str) -> dict:
    """Remove a aventura e todos os dependentes (ações, cards, memórias...)."""
    with db.session() as conn:
        if not db.delete_adventure(conn, adventure_id):
            raise GameError.not_found(f"adventure {adventure_id} not found")
    return {"deleted": True, "id": adventure_id}


@router.post("/{adventure_id}/undo")
def post_undo(adventure_id: str) -> dict:
    return game_engine.handle_undo(adventure_id)
