"""Rulesets de criação de personagem.

Leitura aberta a qualquer usuário autenticado. Criar, editar e apagar exige
admin (`users.is_admin`). Apply copia `rules` para `adventures.character_rules`:
replace substitui, append concatena `existente + "\\n\\n" + regras`. O resultado
é cortado em 4000 caracteres. A resposta é o registro da aventura
(`adventure_to_dict`), não o estado completo com ações e cards.
"""

from __future__ import annotations

import sqlite3

from fastapi import APIRouter, Depends

from app import auth, db, users
from app.errors import GameError
from app.routes.adventures import _managed_adventure
from app.schemas import RulesetApply, RulesetUpsert

router = APIRouter()

_RULES_CAP = 4000


def _admin(info: auth.AuthInfo) -> None:
    if not users.is_admin(info.user):
        raise GameError(403, "forbidden", "only the admin can manage rulesets")


def _checked(fields: dict, *, creating: bool) -> dict:
    if creating or "name" in fields:
        name = (fields.get("name") or "").strip()
        if not name:
            raise GameError(422, "validation", "name is required")
        fields["name"] = name
    if creating or "rules" in fields:
        rules = fields.get("rules")
        if not isinstance(rules, str) or not rules.strip():
            raise GameError(422, "validation", "rules are required")
    if "description" in fields and fields["description"] is None:
        fields["description"] = ""
    return fields


@router.get("/rulesets")
def list_rulesets(info: auth.AuthInfo = Depends(auth.require_auth)) -> list[dict]:
    del info
    with db.session() as conn:
        return db.list_rulesets(conn)


@router.post("/rulesets", status_code=201)
def create_ruleset(body: RulesetUpsert,
                   info: auth.AuthInfo = Depends(auth.require_auth)) -> dict:
    _admin(info)
    fields = _checked(body.model_dump(exclude_unset=True), creating=True)
    try:
        with db.session() as conn:
            return db.insert_ruleset(
                conn,
                name=fields["name"],
                rules=fields["rules"],
                description=fields.get("description") or "",
            )
    except sqlite3.IntegrityError as exc:
        raise GameError(422, "validation", "a ruleset with this name already exists") from exc


@router.get("/rulesets/{ruleset_id}")
def get_ruleset(ruleset_id: str,
                info: auth.AuthInfo = Depends(auth.require_auth)) -> dict:
    del info
    with db.session() as conn:
        row = db.get_ruleset(conn, ruleset_id)
    if row is None:
        raise GameError.not_found(f"ruleset {ruleset_id} not found")
    return row


@router.put("/rulesets/{ruleset_id}")
def update_ruleset(ruleset_id: str, body: RulesetUpsert,
                   info: auth.AuthInfo = Depends(auth.require_auth)) -> dict:
    _admin(info)
    fields = _checked(body.model_dump(exclude_unset=True), creating=False)
    try:
        with db.session() as conn:
            updated = db.update_ruleset(conn, ruleset_id, **fields)
    except sqlite3.IntegrityError as exc:
        raise GameError(422, "validation", "a ruleset with this name already exists") from exc
    if updated is None:
        raise GameError.not_found(f"ruleset {ruleset_id} not found")
    return updated


@router.delete("/rulesets/{ruleset_id}")
def delete_ruleset(ruleset_id: str,
                   info: auth.AuthInfo = Depends(auth.require_auth)) -> dict:
    _admin(info)
    with db.session() as conn:
        if not db.delete_ruleset(conn, ruleset_id):
            raise GameError.not_found(f"ruleset {ruleset_id} not found")
    return {"deleted": True, "id": ruleset_id}


@router.post("/adventures/{adventure_id}/rulesets/apply")
def apply_ruleset(adventure_id: str, body: RulesetApply,
                  info: auth.AuthInfo = Depends(auth.require_auth)) -> dict:
    """Copia as regras para character_rules e devolve o registro da aventura."""
    with db.session() as conn:
        adv = _managed_adventure(conn, adventure_id, info.user)
        ruleset = db.get_ruleset(conn, body.ruleset_id)
        if ruleset is None:
            raise GameError.not_found(f"ruleset {body.ruleset_id} not found")
        existing = adv.get("character_rules") or ""
        incoming = ruleset.get("rules") or ""
        if body.mode == "append":
            merged = f"{existing}\n\n{incoming}"
        else:
            merged = incoming
        updated = db.update_adventure(
            conn, adventure_id, character_rules=merged[:_RULES_CAP])
    if updated is None:
        raise GameError.not_found(f"adventure {adventure_id} not found")
    return updated
