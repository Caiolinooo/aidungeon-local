"""Usuários do app. O Caio é o admin e não pode ser apagado.

A senha fica como sha256 hex, o mesmo formato de AUTH_PASSWORD_HASH. O
login consulta esta tabela; a conta do ambiente é criada aqui na primeira
subida, com a senha que já estava no .env.
"""

from __future__ import annotations

import hashlib
import hmac
import re

from app import db

ADMIN_USERNAME = "Caio"
_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{1,31}$")
MIN_PASSWORD = 8


def password_hash(password: str) -> str:
    return hashlib.sha256(password.encode("utf-8")).hexdigest()


def password_matches(password: str, hashed: str) -> bool:
    return hmac.compare_digest(password_hash(password), (hashed or "").strip().lower())


def is_admin(username: str) -> bool:
    return username == ADMIN_USERNAME


def validate_username(username: str) -> str:
    name = (username or "").strip()
    if not _NAME.fullmatch(name):
        raise ValueError("username must be 2–32 letters, digits, _ or -")
    if name.casefold() == ADMIN_USERNAME.casefold() and name != ADMIN_USERNAME:
        raise ValueError("that name is reserved")
    return name


def validate_password(password: str) -> str:
    if len(password or "") < MIN_PASSWORD:
        raise ValueError(f"password must be at least {MIN_PASSWORD} characters")
    return password


def get_user(conn, username: str) -> dict | None:
    row = conn.execute(
        "SELECT username, password_hash, role, created_at FROM users WHERE username=?",
        (username,),
    ).fetchone()
    if not row:
        return None
    return {
        "username": row["username"],
        "password_hash": row["password_hash"],
        "role": "admin" if row["username"] == ADMIN_USERNAME else (row["role"] or "user"),
        "created_at": row["created_at"],
    }


def list_users(conn) -> list[dict]:
    rows = conn.execute(
        "SELECT username, role, created_at FROM users ORDER BY username COLLATE NOCASE"
    ).fetchall()
    out = []
    for row in rows:
        count = conn.execute(
            "SELECT COUNT(*) AS c FROM adventures WHERE owner=?",
            (row["username"],),
        ).fetchone()["c"]
        out.append({
            "username": row["username"],
            "role": "admin" if row["username"] == ADMIN_USERNAME else (row["role"] or "user"),
            "created_at": row["created_at"],
            "adventure_count": count,
        })
    return out


def ensure_seed(conn, cfg: dict) -> None:
    """Cria a conta do ambiente se ela ainda não existe. Não troca senha."""
    name = cfg["user"]
    if get_user(conn, name) is None:
        conn.execute(
            "INSERT INTO users(username, password_hash, role, created_at)"
            " VALUES(?,?,?,?)",
            (name, cfg["password_hash"],
             "admin" if name == ADMIN_USERNAME else "user", db.utcnow()),
        )
    if get_user(conn, ADMIN_USERNAME):
        conn.execute("UPDATE users SET role='admin' WHERE username=?", (ADMIN_USERNAME,))


def create_user(conn, username: str, password: str) -> dict:
    name = validate_username(username)
    secret = validate_password(password)
    if name == ADMIN_USERNAME or get_user(conn, name):
        raise ValueError("user already exists")
    taken = conn.execute(
        "SELECT username FROM users WHERE username=? COLLATE NOCASE", (name,)
    ).fetchone()
    if taken:
        raise ValueError("user already exists")
    conn.execute(
        "INSERT INTO users(username, password_hash, role, created_at) VALUES(?,?,?,?)",
        (name, password_hash(secret), "user", db.utcnow()),
    )
    return {"username": name, "role": "user"}


def set_password(conn, username: str, password: str) -> None:
    if get_user(conn, username) is None:
        raise KeyError(username)
    conn.execute(
        "UPDATE users SET password_hash=? WHERE username=?",
        (password_hash(validate_password(password)), username),
    )


def delete_user(conn, username: str) -> None:
    """Apaga o usuário e as histórias dele. O Caio fica."""
    if username == ADMIN_USERNAME:
        raise PermissionError(username)
    if get_user(conn, username) is None:
        raise KeyError(username)
    ids = [row["id"] for row in conn.execute(
        "SELECT id FROM adventures WHERE owner=?", (username,)).fetchall()]
    for adventure_id in ids:
        db.delete_adventure(conn, adventure_id)
    conn.execute("DELETE FROM users WHERE username=?", (username,))
