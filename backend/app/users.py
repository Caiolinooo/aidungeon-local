"""Usuários do app. O Caio é admin legado e não pode ser apagado.

Senhas novas ficam como `scrypt$<salt_hex>$<hash_hex>`. O login ainda aceita
o sha256 hex legado (AUTH_PASSWORD_HASH antigo) e regrava em scrypt. A conta
do ambiente é criada aqui na primeira subida, com o hash que já estava no .env.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import re

from app import db

ADMIN_USERNAME = "Caio"
_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{1,31}$")
_LEGACY_SHA256 = re.compile(r"^[0-9a-f]{64}$")
MIN_PASSWORD = 8
_SCRYPT_N = 2**14
_SCRYPT_R = 8
_SCRYPT_P = 1
_SCRYPT_SALT_BYTES = 16


def password_hash(password: str) -> str:
    """scrypt$salt$hash. n=2**14, r=8, p=1, sal de 16 bytes."""
    salt = os.urandom(_SCRYPT_SALT_BYTES)
    digest = hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt,
        n=_SCRYPT_N,
        r=_SCRYPT_R,
        p=_SCRYPT_P,
    )
    return f"scrypt${salt.hex()}${digest.hex()}"


def is_legacy_password_hash(hashed: str) -> bool:
    return _LEGACY_SHA256.fullmatch((hashed or "").strip().lower()) is not None


def password_matches(password: str, hashed: str) -> bool:
    stored = (hashed or "").strip()
    lowered = stored.lower()
    if lowered.startswith("scrypt$"):
        parts = lowered.split("$")
        if len(parts) != 3:
            return False
        try:
            salt = bytes.fromhex(parts[1])
            expected = bytes.fromhex(parts[2])
        except ValueError:
            return False
        if not salt or not expected:
            return False
        try:
            digest = hashlib.scrypt(
                password.encode("utf-8"),
                salt=salt,
                n=_SCRYPT_N,
                r=_SCRYPT_R,
                p=_SCRYPT_P,
                dklen=len(expected),
            )
        except (ValueError, OSError):
            return False
        if len(digest) != len(expected):
            return False
        return hmac.compare_digest(digest, expected)
    if not is_legacy_password_hash(stored):
        return False
    actual = hashlib.sha256(password.encode("utf-8")).hexdigest()
    return hmac.compare_digest(actual, lowered)


def upgrade_password_hash(conn, username: str, password: str) -> None:
    """Regrava a senha que acabou de autenticar. Não reaplica o mínimo de tamanho."""
    conn.execute(
        "UPDATE users SET password_hash=? WHERE username=?",
        (password_hash(password), username),
    )


def is_admin(username: str) -> bool:
    """Admin legado (Caio) ou papel `admin` na tabela. Caio não precisa de linha."""
    if username == ADMIN_USERNAME:
        return True
    if not username:
        return False
    with db.session() as conn:
        row = conn.execute(
            "SELECT role FROM users WHERE username=?",
            (username,),
        ).fetchone()
    return row is not None and row["role"] == "admin"


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
    """Cria a conta do ambiente se ela ainda não existe. Não troca senha.

    Essa conta é admin, mesmo quando AUTH_USER não é o Caio. Linha antiga com
    outro papel é corrigida. O valor gravado é AUTH_PASSWORD_HASH já pronto
    (sha256 legado ou scrypt): a senha em claro não passa por aqui, então não
    dá para chamar password_hash(). O login regrava o formato legado.
    """
    name = cfg["user"]
    if get_user(conn, name) is None:
        conn.execute(
            "INSERT INTO users(username, password_hash, role, created_at)"
            " VALUES(?,?,?,?)",
            (name, cfg["password_hash"], "admin", db.utcnow()),
        )
    else:
        conn.execute(
            "UPDATE users SET role='admin' WHERE username=? AND COALESCE(role, '')!='admin'",
            (name,),
        )
    if name != ADMIN_USERNAME:
        conn.execute(
            "UPDATE users SET role='admin' WHERE username=? AND COALESCE(role, '')!='admin'",
            (ADMIN_USERNAME,),
        )


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
