"""Catálogo de mods e aplicação por aventura.

O owner monta este router em `app.main`:

    app.include_router(mods_routes.router, prefix="/api", tags=["mods"],
                       dependencies=_protected)

Rotas:

    POST   /api/mods
    GET    /api/mods?q=
    GET    /api/mods/{mod_id}
    POST   /api/mods/{mod_id}/versions
    GET    /api/mods/{mod_id}/versions/{version_id}
    GET    /api/adventures/{adventure_id}/mods
    POST   /api/adventures/{adventure_id}/mods
    POST   /api/adventures/{adventure_id}/mods/{version_id}/enable
    POST   /api/adventures/{adventure_id}/mods/{version_id}/disable
    DELETE /api/adventures/{adventure_id}/mods/{version_id}

Aplicar, ativar, desativar e remover exigem aventura gerenciada (dono ou admin).
"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from app import auth, db, scripting
from app.errors import GameError
from app.mods import (
    apply_version,
    get_mod,
    get_version,
    list_applied,
    list_mods,
    publish_mod,
    publish_version,
    remove_applied,
    set_enabled,
)
from app.users import is_admin

router = APIRouter()


def _managed_adventure(conn, adventure_id: str, username: str) -> dict:
    """Dono ou admin. Aventura legada sem dono continua editável por quem a vê."""
    adv = db.get_adventure(conn, adventure_id)
    if not adv or not db.can_access_adventure(conn, adv, username):
        raise GameError.not_found(f"adventure {adventure_id} not found")
    owner = adv.get("owner") or ""
    if not (is_admin(username) or owner in ("", username)):
        raise GameError(403, "forbidden", "only the owner can change this adventure")
    return adv


class ModPublishBody(BaseModel):
    title: str
    description: str = ""
    visibility: str = "public"
    package: dict


class ModVersionBody(BaseModel):
    package: dict


class ModApplyBody(BaseModel):
    mod_version_id: str


def _reject_package(exc: Exception) -> GameError:
    if isinstance(exc, scripting.SecurityViolation):
        return GameError(422, "script_rejected", str(exc))
    if isinstance(exc, SyntaxError):
        detail = exc.msg or "invalid syntax"
        return GameError(422, "script_rejected", f"syntax error: {detail}")
    if isinstance(exc, ValueError):
        return GameError.bad_request("invalid_package", str(exc))
    return GameError.bad_request("invalid_package", str(exc))


@router.post("/mods")
def post_mod(body: ModPublishBody,
             info: auth.AuthInfo = Depends(auth.require_auth)) -> dict:
    try:
        with db.session() as conn:
            return publish_mod(
                conn, info.user, title=body.title, description=body.description,
                visibility=body.visibility, package=body.package,
            )
    except GameError:
        raise
    except (scripting.SecurityViolation, SyntaxError, ValueError) as exc:
        raise _reject_package(exc) from exc


@router.get("/mods")
def get_mods(q: str = "",
             info: auth.AuthInfo = Depends(auth.require_auth)) -> dict:
    with db.session() as conn:
        return list_mods(conn, info.user, q)


@router.get("/mods/{mod_id}")
def get_one_mod(mod_id: str,
                info: auth.AuthInfo = Depends(auth.require_auth)) -> dict:
    with db.session() as conn:
        return get_mod(conn, info.user, mod_id)


@router.post("/mods/{mod_id}/versions")
def post_version(mod_id: str, body: ModVersionBody,
                 info: auth.AuthInfo = Depends(auth.require_auth)) -> dict:
    try:
        with db.session() as conn:
            return publish_version(conn, info.user, mod_id, body.package)
    except GameError:
        raise
    except (scripting.SecurityViolation, SyntaxError, ValueError) as exc:
        raise _reject_package(exc) from exc


@router.get("/mods/{mod_id}/versions/{version_id}")
def get_one_version(mod_id: str, version_id: str,
                    info: auth.AuthInfo = Depends(auth.require_auth)) -> dict:
    with db.session() as conn:
        return get_version(conn, info.user, mod_id, version_id)


@router.get("/adventures/{adventure_id}/mods")
def get_applied(adventure_id: str,
                info: auth.AuthInfo = Depends(auth.require_auth)) -> dict:
    with db.session() as conn:
        _managed_adventure(conn, adventure_id, info.user)
        return list_applied(conn, adventure_id)


@router.post("/adventures/{adventure_id}/mods")
def post_apply(adventure_id: str, body: ModApplyBody,
               info: auth.AuthInfo = Depends(auth.require_auth)) -> dict:
    version_id = (body.mod_version_id or "").strip()
    if not version_id:
        raise GameError.bad_request("invalid_package", "mod_version_id is required")
    with db.session() as conn:
        _managed_adventure(conn, adventure_id, info.user)
        return apply_version(conn, adventure_id, version_id, info.user)


@router.post("/adventures/{adventure_id}/mods/{version_id}/enable")
def post_enable(adventure_id: str, version_id: str,
                info: auth.AuthInfo = Depends(auth.require_auth)) -> dict:
    with db.session() as conn:
        _managed_adventure(conn, adventure_id, info.user)
        return set_enabled(conn, adventure_id, version_id, True)


@router.post("/adventures/{adventure_id}/mods/{version_id}/disable")
def post_disable(adventure_id: str, version_id: str,
                 info: auth.AuthInfo = Depends(auth.require_auth)) -> dict:
    with db.session() as conn:
        _managed_adventure(conn, adventure_id, info.user)
        return set_enabled(conn, adventure_id, version_id, False)


@router.delete("/adventures/{adventure_id}/mods/{version_id}")
def delete_applied(adventure_id: str, version_id: str,
                   info: auth.AuthInfo = Depends(auth.require_auth)) -> dict:
    with db.session() as conn:
        _managed_adventure(conn, adventure_id, info.user)
        return remove_applied(conn, adventure_id, version_id)
