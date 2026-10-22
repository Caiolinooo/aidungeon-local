"""Router /api/adventures — ciclo de vida da aventura + undo."""

from __future__ import annotations

import json
import logging
import sqlite3
import urllib.parse
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Response

import exporter
from app import (auth, consistency, db, events, game_engine, jobs, npc_assets,
                 stats as stats_mod)
from app.errors import GameError
from app.schemas import (
    AdventureCreate, AdventureFromDraft, AdventureShareCreate, AdventureUpdate,
    PlayerCharacterUpsert,
)

log = logging.getLogger("app.adventures")
_RULES_CAP = 4000
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


def _complete_npc_sheets(conn, adventure_id: str) -> int:
    """Todo NPC nasce com status, classe e atributos.

    O rascunho do assistente já vem com a ficha completa, mas cards de
    cenário importado podem chegar sem status/classe/stats. Aqui o que é
    determinístico (status, classe pelos textos, atributos estimados) é
    preenchido na hora; os textos vivos dependem do LLM e ficam para o
    reparo da aba Check. Devolve quantas fichas foram completadas.
    """
    filled = 0
    for card in db.list_cards(conn, adventure_id):
        if (card.get("type") or "") != "character":
            continue
        if (card.get("player_username") or "").strip():
            continue  # ficha do jogador tem outro schema
        sheet = npc_assets.parse_sheet(card.get("sheet"))
        complete = npc_assets.ensure_sheet_complete(
            sheet, entry=card.get("entry") or "", card=card)
        if complete == sheet:
            continue
        updated = db.update_card(conn, adventure_id, card["id"],
                                 {"sheet": json.dumps(complete, ensure_ascii=False)})
        if updated:
            filled += 1
    return filled


def _copy_scenario_cards(conn, adventure_id: str, cards: list) -> None:
    for card in cards or []:
        if not isinstance(card, dict):
            continue
        db.insert_card(
            conn, adventure_id,
            title=card.get("title") or "", type=card.get("type") or "other",
            triggers=card.get("triggers") or [], entry=card.get("entry") or "",
            pinned=bool(card.get("pinned")),
            use_for_character_creation=bool(card.get("use_for_character_creation")),
            portrait_prompt=card.get("portrait_prompt") or "",
            sheet=(json.dumps(card["sheet"], ensure_ascii=False)
                   if isinstance(card.get("sheet"), dict) else ""),
        )


def _enqueue_creation_jobs(adventure_id: str) -> None:
    """Retratos e fichas incompletas. Falha aqui não desfaz a aventura já commitada."""
    try:
        with db.session() as conn:
            jobs.enqueue_all_portraits(conn, adventure_id)
            _complete_npc_sheets(conn, adventure_id)
            jobs.enqueue_incomplete_sheets(conn, adventure_id)
    except Exception as exc:
        log.warning("adventures: jobs de criação falharam (aventura mantida): %s", exc)


def _bind_ruleset(conn, adventure_id: str, ruleset_id: str, username: str) -> None:
    """Mesma cópia de regras que POST /adventures/{id}/rulesets/apply (mode=replace)."""
    ruleset = db.get_ruleset(conn, ruleset_id)
    if ruleset is None:
        raise GameError.not_found(f"ruleset {ruleset_id} not found")
    incoming = ruleset.get("rules") or ""
    updated = db.update_adventure(
        conn, adventure_id, character_rules=incoming[:_RULES_CAP])
    if updated is None:
        raise GameError.not_found(f"adventure {adventure_id} not found")
    from app.sim import packs, rules as sim_rules, state as sim_state
    pack_id = packs.pack_for_ruleset(ruleset_id)
    sim = sim_state.load(updated)
    sim["pack"] = pack_id
    if pack_id:
        card = db.player_character(conn, adventure_id, username)
        sheet = npc_assets.parse_sheet((card or {}).get("sheet") or "")
        title = ((card or {}).get("title") or "").strip() or username or "Jogador"
        sim_rules.ensure_actor(
            sim, packs.load_pack(pack_id), username, title,
            sheet.get("stats") if isinstance(sheet.get("stats"), dict) else None,
        )
    db.set_sim_state(conn, adventure_id, sim)


@router.post("", status_code=201)
def create_adventure(body: AdventureCreate,
                     info: auth.AuthInfo = Depends(auth.require_auth)) -> dict:
    """Nova aventura a partir de scenario (herda campos + action start) ou vazia."""
    report = None
    with db.session() as conn:
        if body.scenario_id:
            sc = db.get_scenario(conn, body.scenario_id)
            if not sc:
                raise GameError.not_found(f"scenario {body.scenario_id} not found")
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
            _copy_scenario_cards(conn, adv["id"], sc["story_cards"])
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
        adventure_id = adv["id"]
    if body.scenario_id:
        _enqueue_creation_jobs(adventure_id)
        try:
            with db.session() as conn:
                report = consistency.audit(conn, adventure_id)
        except Exception as exc:
            log.warning("adventures: auditoria falhou (aventura mantida): %s", exc)
    with db.session() as conn:
        state = db.adventure_state(conn, adventure_id)
    if state is not None and report is not None:
        state["consistency"] = report
    return state


@router.post("/from-draft", status_code=201)
def create_adventure_from_draft(
    body: AdventureFromDraft,
    info: auth.AuthInfo = Depends(auth.require_auth),
) -> dict:
    """Cenário, aventura, ficha e ruleset na mesma transação. Jobs ficam fora dela."""
    draft = body.draft
    report = None
    with db.session() as conn:
        scenario = db.insert_scenario(
            conn,
            title=draft.title,
            description=draft.description,
            ai_instructions=draft.ai_instructions,
            plot_essentials=draft.plot_essentials,
            authors_note=draft.authors_note,
            opening_text=draft.opening_text,
            image_style=draft.image_style,
            tags=draft.tags,
            story_cards=[c.model_dump(exclude_none=True) for c in draft.story_cards],
            third_person=draft.third_person,
        )
        adv = db.insert_adventure(
            conn, scenario_id=scenario["id"], owner=info.user,
            title=scenario["title"],
            description=scenario["description"],
            ai_instructions=scenario["ai_instructions"],
            plot_essentials=scenario["plot_essentials"],
            authors_note=scenario["authors_note"],
            image_style=scenario.get("image_style") or "",
            tags=scenario["tags"],
            third_person=bool(scenario["third_person"]),
            source="new",
        )
        db.insert_action(conn, adv["id"], 0, "start", scenario["opening_text"])
        _copy_scenario_cards(conn, adv["id"], scenario["story_cards"])
        if body.character is not None:
            title = body.character.title.strip()
            sheet = json.dumps(
                _complete_player_sheet(body.character.sheet.model_dump(by_alias=True)),
                ensure_ascii=False,
            )
            db.upsert_player_character(
                conn, adv["id"], info.user,
                title=title, sheet=sheet, portrait_prompt=body.character.portrait_prompt,
            )
        if body.ruleset_id:
            _bind_ruleset(conn, adv["id"], body.ruleset_id, info.user)
        adventure_id = adv["id"]
    _enqueue_creation_jobs(adventure_id)
    try:
        with db.session() as conn:
            report = consistency.audit(conn, adventure_id)
        with db.session() as conn:
            state = db.adventure_state(conn, adventure_id)
    except Exception as exc:
        log.warning("adventures: auditoria falhou (aventura mantida): %s", exc)
        return {"id": adventure_id, "status": "created", "audit": "failed"}
    if state is not None and report is not None:
        state["consistency"] = report
    return state


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
    changed = list(fields.keys())
    turn_mode = fields.pop("turn_mode", None)
    with db.session() as conn:
        adv = _managed_adventure(conn, adventure_id, info.user)
        updated = db.update_adventure(conn, adventure_id, **fields)
        if not updated:
            raise GameError.not_found(f"adventure {adventure_id} not found")
        if turn_mode is not None:
            db.set_turn_mode(conn, adventure_id, bool(turn_mode), adv.get("owner") or "")
        state = db.adventure_state(conn, adventure_id)
    events.publish(adventure_id, "adventure_updated", {"fields": changed})
    return state


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


def _complete_player_sheet(raw: dict) -> dict:
    """Grava `class` pelo alias e não deixa a ficha sem as seis habilidades.

    Stats vazios (ou só com chaves extras) são estimados pela classe,
    aparência e notas. O que o jogador mandou de número entra normalizado;
    chave extra (`hp`, por exemplo) segue do jeito que veio.
    """
    stored = dict(raw)
    role = stored.get("class")
    if isinstance(role, str) and role.strip():
        stored["class"] = role.strip()
    else:
        stored.pop("class", None)
    stats_in = stored.get("stats") if isinstance(stored.get("stats"), dict) else {}
    if stats_mod.has_numeric_ability(stats_in):
        stored["stats"] = stats_mod.normalize_stats(stats_in)
    else:
        extras = {key: value for key, value in stats_in.items() if key not in stats_mod.ABILITIES}
        blob = " ".join(
            part.strip()
            for part in (str(stored.get("appearance") or ""), str(stored.get("notes") or ""))
            if str(part or "").strip()
        )
        stored["stats"] = {
            **stats_mod.estimate_stats(blob, str(stored.get("class") or "")),
            **extras,
        }
    return stored


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
    sheet = json.dumps(
        _complete_player_sheet(body.sheet.model_dump(by_alias=True)),
        ensure_ascii=False,
    )
    with db.session() as conn:
        _visible_adventure(conn, adventure_id, info.user)
        card = db.upsert_player_character(
            conn, adventure_id, info.user,
            title=title, sheet=sheet, portrait_prompt=body.portrait_prompt,
        )
        payload = {"card": _character_view(conn, card)}
    events.publish(adventure_id, "character_changed", {"username": info.user})
    return payload


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
