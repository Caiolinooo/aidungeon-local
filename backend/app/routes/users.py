"""Gerência de usuários. Só o Caio cria, apaga e redefine senha alheia."""

from __future__ import annotations

from fastapi import APIRouter, Depends

from app import auth, db, users
from app.errors import GameError
from app.schemas import UserCreate, UserPassword

router = APIRouter()


def _admin(info: auth.AuthInfo) -> None:
    if not users.is_admin(info.user):
        raise GameError(403, "forbidden", "only the admin can manage users")


@router.get("/users")
def get_users(info: auth.AuthInfo = Depends(auth.require_auth)) -> list[dict]:
    _admin(info)
    with db.session() as conn:
        return users.list_users(conn)


@router.post("/users", status_code=201)
def post_user(body: UserCreate,
              info: auth.AuthInfo = Depends(auth.require_auth)) -> dict:
    _admin(info)
    try:
        with db.session() as conn:
            return users.create_user(conn, body.username, body.password)
    except ValueError as exc:
        code = "user_exists" if "exists" in str(exc) or "reserved" in str(exc) else "invalid_user"
        status = 409 if code == "user_exists" else 400
        raise GameError(status, code, str(exc)) from exc


@router.delete("/users/{username}")
def remove_user(username: str,
                info: auth.AuthInfo = Depends(auth.require_auth)) -> dict:
    _admin(info)
    try:
        with db.session() as conn:
            users.delete_user(conn, username)
    except PermissionError as exc:
        raise GameError(403, "forbidden", "the admin account cannot be deleted") from exc
    except KeyError as exc:
        raise GameError.not_found(f"user {username} not found") from exc
    return {"deleted": True, "username": username}


@router.put("/users/{username}/password")
def reset_password(username: str, body: UserPassword,
                   info: auth.AuthInfo = Depends(auth.require_auth)) -> dict:
    _admin(info)
    try:
        with db.session() as conn:
            users.set_password(conn, username, body.password)
    except KeyError as exc:
        raise GameError.not_found(f"user {username} not found") from exc
    except ValueError as exc:
        raise GameError.bad_request("invalid_password", str(exc)) from exc
    return {"ok": True, "username": username}
