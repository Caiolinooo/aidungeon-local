"""Quem pode abrir uma história. O Caio vê todas; os outros, só as suas.

Aventura sem dono (testes e bancos antigos antes da migração) continua
visível para quem está logado. Na migração, as histórias existentes passam
a ser do Caio.
"""

from __future__ import annotations

import re

from fastapi import Request

from app import auth, db
from app.users import is_admin

_ADVENTURE = re.compile(r"^/api/adventures/([0-9a-fA-F]{32})(?:/|$)")
_IMAGE = re.compile(r"^/api/images/([0-9a-fA-F]{32})(?:/|$)")
_ACTION = re.compile(r"^/api/actions/(\d+)(?:/|$)")


def user_from_request(request: Request) -> str | None:
    cfg = auth.auth_config()
    if cfg is None:
        return None
    header = request.headers.get("Authorization", "")
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        return None
    ok, user, _exp = auth.verify_token(token.strip(), cfg["secret"])
    return user if ok else None


def can_see(username: str, owner: str | None) -> bool:
    if is_admin(username):
        return True
    owner = owner or ""
    return owner == "" or owner == username


def adventure_visible(conn, username: str, adventure_id: str) -> bool:
    row = conn.execute(
        "SELECT owner FROM adventures WHERE id=?", (adventure_id,)
    ).fetchone()
    if row is None:
        return True  # a rota responde o 404 de aventura inexistente
    return can_see(username, row["owner"])


def hidden_path(path: str, username: str) -> bool:
    """True quando a URL aponta para história de outra pessoa."""
    adventure_id = None
    match = _ADVENTURE.match(path)
    if match:
        adventure_id = match.group(1)
    else:
        match = _IMAGE.match(path)
        if match:
            with db.session() as conn:
                row = conn.execute(
                    "SELECT adventure_id FROM images WHERE id=?", (match.group(1),)
                ).fetchone()
            if row is None:
                return False
            adventure_id = row["adventure_id"]
        else:
            match = _ACTION.match(path)
            if match:
                with db.session() as conn:
                    row = conn.execute(
                        "SELECT adventure_id FROM actions WHERE id=?",
                        (int(match.group(1)),),
                    ).fetchone()
                if row is None:
                    return False
                adventure_id = row["adventure_id"]
    if not adventure_id:
        return False
    with db.session() as conn:
        return not adventure_visible(conn, username, adventure_id)
