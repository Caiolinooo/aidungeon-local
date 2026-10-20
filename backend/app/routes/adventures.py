"""Router /api/adventures — ciclo de vida da aventura + undo."""

from __future__ import annotations

import json
import sqlite3
import urllib.parse
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Response

import exporter
from app import auth, db, game_engine, jobs
from app.errors import GameError
from app.schemas import (
    AdventureCreate, AdventureShareCreate, AdventureUpdate, PlayerCharacterUpsert,
)
from app.users import get_user, is_admin

router = APIRouter()


def _may_manage(adv: dict, username: str) -> bool:
    """Dono ou admin. Aventura legada sem dono continua editável por quem a vê."""
    if is_admin(username):
        return True
    owner = adv.get("owner") or ""
    if owner == "":
        return True
    return owner == username


def _visible_adventure(conn, adventure_id: str, username: str) -> dict:
    adv = db.get_adventure(conn, adventure_id)
    if not adv or not db.can_access_adventure(conn, adv, username):
        raise GameError.not_found(f"adventure {adventure_id} not found")
    return adv


def _managed_adventure(conn, adventure_id: str, username: str) -> dict:
    adv = _visible_adventure(conn, adventure_id, username)
    if not _may_manage(adv, username):
        raise GameError(403, "forbidden", "only the owner can change this adventure")
    return adv


def _sharing_view(conn, adventure_id: str) -> dict:
    adv = db.get_adventure(conn, adventure_id)
    return {
        "owner": (adv or {}).get("owner") or "",
        "participants": db.adventure_participants(conn, adv),
        "shares": db.list_shares(conn, adventure_id),
        "turn_mode": bool((adv or {}).get("turn_mode")),
        "turn_user": (adv or {}).get("turn_user") or "",
    }


@router.post("", status_code=201)
def create_adventure(body: AdventureCreate,
                     info: auth.AuthInfo = Depends(auth.require_auth)) -> dict:
    """Nova aventura a partir de scenario (herda campos + action start) ou vazia."""
    with db.session() as conn:
        if body.scenario_id:
            sc = db.get_scenario(conn, body.scenario_id)
            if not sc:
                raise GameError.not_found(f"scenario {body.scenario_id} not found")
            # Sem coluna nova no scenario: herda se o dict já trouxer a chave
            # (banco futuro ou JSON). O POST também manda a string direto.
            rules = body.character_rules
            if rules is None:
                rules = sc.get("character_rules") or ""
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
                character_rules=rules or "",
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
                character_rules=body.character_rules or "",
                source="new",
            )
        return db.adventure_state(conn, adv["id"])


@router.get("")
def list_adventures(info: auth.AuthInfo = Depends(auth.require_auth)) -> list[dict]:
    with db.session() as conn:
        rows = db.list_adventure_summaries(conn)
        visible = []
        for row in rows:
            owner = row.get("owner") or ""
            mine = owner in ("", info.user)
            shared = (not mine) and info.user in db.list_shares(conn, row["id"])
            if not is_admin(info.user) and not mine and not shared:
                continue
            row["shared_by"] = owner if owner and owner != info.user else ""
            visible.append(row)
        return visible


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
def get_adventure(adventure_id: str,
                  info: auth.AuthInfo = Depends(auth.require_auth)) -> dict:
    with db.session() as conn:
        _visible_adventure(conn, adventure_id, info.user)
        state = db.adventure_state(conn, adventure_id)
    if not state:
        raise GameError.not_found(f"adventure {adventure_id} not found")
    return state


@router.patch("/{adventure_id}")
def patch_adventure(adventure_id: str, body: AdventureUpdate,
                    info: auth.AuthInfo = Depends(auth.require_auth)) -> dict:
    """Edita metadados (título/descrição/tags). Campos ausentes não mudam."""
    fields = body.model_dump(exclude_unset=True)
    turn_mode = fields.pop("turn_mode", None)
    with db.session() as conn:
        adv = _managed_adventure(conn, adventure_id, info.user)
        updated = db.update_adventure(conn, adventure_id, **fields)
        if not updated:
            raise GameError.not_found(f"adventure {adventure_id} not found")
        if turn_mode is not None:
            db.set_turn_mode(conn, adventure_id, bool(turn_mode), adv.get("owner") or "")
        return db.adventure_state(conn, adventure_id)


@router.delete("/{adventure_id}")
def delete_adventure(adventure_id: str,
                     info: auth.AuthInfo = Depends(auth.require_auth)) -> dict:
    """Remove a aventura e todos os dependentes (ações, cards, memórias...)."""
    with db.session() as conn:
        _managed_adventure(conn, adventure_id, info.user)
        if not db.delete_adventure(conn, adventure_id):
            raise GameError.not_found(f"adventure {adventure_id} not found")
    game_engine.forget_adventure(adventure_id)
    return {"deleted": True, "id": adventure_id}


@router.get("/{adventure_id}/sharing")
def get_sharing(adventure_id: str,
                info: auth.AuthInfo = Depends(auth.require_auth)) -> dict:
    with db.session() as conn:
        _visible_adventure(conn, adventure_id, info.user)
        return _sharing_view(conn, adventure_id)


@router.post("/{adventure_id}/sharing", status_code=201)
def post_sharing(adventure_id: str, body: AdventureShareCreate,
                 info: auth.AuthInfo = Depends(auth.require_auth)) -> dict:
    with db.session() as conn:
        adv = _managed_adventure(conn, adventure_id, info.user)
        username = body.username.strip()
        if username == (adv.get("owner") or ""):
            raise GameError.bad_request("cannot_share_owner",
                                        "cannot share the adventure with its owner")
        if get_user(conn, username) is None:
            raise GameError(404, "user_not_found", f"user {username} not found")
        try:
            db.share_adventure(conn, adventure_id, username)
        except sqlite3.IntegrityError:
            raise GameError.conflict("already_shared",
                                     f"{username} already has access") from None
        return _sharing_view(conn, adventure_id)


@router.delete("/{adventure_id}/sharing/{username}")
def delete_sharing(adventure_id: str, username: str,
                   info: auth.AuthInfo = Depends(auth.require_auth)) -> dict:
    with db.session() as conn:
        _managed_adventure(conn, adventure_id, info.user)
        db.unshare_adventure(conn, adventure_id, username)
        return _sharing_view(conn, adventure_id)


def _character_view(conn, card: dict | None) -> dict | None:
    if not card:
        return None
    return db.attach_portrait(conn, card)


@router.get("/{adventure_id}/characters")
def list_characters(adventure_id: str,
                    info: auth.AuthInfo = Depends(auth.require_auth)) -> list[dict]:
    """Personagens dos jogadores da mesa (cards com player_username)."""
    with db.session() as conn:
        _visible_adventure(conn, adventure_id, info.user)
        cards = db.list_player_characters(conn, adventure_id)
        return [
            {"username": card["player_username"], "card": _character_view(conn, card)}
            for card in cards
        ]


@router.get("/{adventure_id}/character")
def get_character(adventure_id: str,
                  info: auth.AuthInfo = Depends(auth.require_auth)) -> dict:
    with db.session() as conn:
        _visible_adventure(conn, adventure_id, info.user)
        card = db.player_character(conn, adventure_id, info.user)
        return {"card": _character_view(conn, card)}


@router.put("/{adventure_id}/character")
def put_character(adventure_id: str, body: PlayerCharacterUpsert,
                  info: auth.AuthInfo = Depends(auth.require_auth)) -> dict:
    """Cria ou atualiza o personagem de quem está logado. Preserva id e retrato."""
    title = body.title.strip()
    if not title or len(title) > 80:
        raise GameError.bad_request("invalid_title", "title must be 1–80 characters")
    sheet = json.dumps(body.sheet.model_dump(), ensure_ascii=False)
    with db.session() as conn:
        _visible_adventure(conn, adventure_id, info.user)
        card = db.upsert_player_character(
            conn, adventure_id, info.user,
            title=title, sheet=sheet, portrait_prompt=body.portrait_prompt,
        )
        return {"card": _character_view(conn, card)}


@router.post("/{adventure_id}/life/tick", status_code=202)
def post_life_tick(adventure_id: str,
                   info: auth.AuthInfo = Depends(auth.require_auth)) -> dict:
    """Enfileira o dia fora de cena. O LLM roda no worker, não neste request."""
    with db.session() as conn:
        _visible_adventure(conn, adventure_id, info.user)
        job = jobs.enqueue_day_tick(conn, adventure_id, reason="manual")
    return {"job": job}


@router.post("/{adventure_id}/undo")
def post_undo(adventure_id: str) -> dict:
    return game_engine.handle_undo(adventure_id)
