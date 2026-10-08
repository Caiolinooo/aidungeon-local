"""Autenticação Bearer — token HMAC-SHA256 (contrato: docs/openapi.yaml §auth).

Formato do token:  base64url(user).base64url(exp_unix).hexsig
onde hexsig = HMAC-SHA256(AUTH_SECRET, "{user}.{exp_unix}") em hexdigest.

Credenciais SOMENTE via ambiente/.env (nomes em settings.json → auth.*_env):
AUTH_USER, AUTH_PASSWORD_HASH (sha256 hex da senha), AUTH_SECRET (hex aleatório).

Fail closed: faltando qualquer uma das 3 variáveis, `auth_config()` devolve
None — `require_auth` responde 401 em todas as rotas protegidas e o login
responde 503 "auth_not_configured" (ver app/routes/auth.py). Nunca fail open.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import time
from datetime import datetime, timezone
from typing import NamedTuple

from fastapi import Request

from app.errors import GameError
from app.settings_store import load_file_settings, resolve_env_value


class AuthInfo(NamedTuple):
    """Resultado de `require_auth`: dono do token e expiração (unix)."""

    user: str
    exp: int


# ------------------------------------------------------------------- config
def auth_config() -> dict | None:
    """Credenciais efetivas (ambiente → .env). None se faltar qualquer uma.

    Resolvido a cada chamada (sem cache): barato para single-user e permite
    que testes troquem as envs via monkeypatch.
    """
    cfg = load_file_settings().get("auth", {})
    user = resolve_env_value(cfg.get("username_env", "AUTH_USER"))
    password_hash = resolve_env_value(cfg.get("password_hash_env", "AUTH_PASSWORD_HASH"))
    secret = resolve_env_value(cfg.get("secret_env", "AUTH_SECRET"))
    if not user or not password_hash or not secret:
        return None
    return {
        "user": user,
        "password_hash": password_hash.strip().lower(),
        "secret": secret,
        "ttl_hours": int(cfg.get("token_ttl_hours", 168)),
    }


# ------------------------------------------------------------------- tokens
def _b64url_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64url_decode(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _sign(secret: str, payload: str) -> str:
    return hmac.new(secret.encode("utf-8"), payload.encode("utf-8"),
                    hashlib.sha256).hexdigest()


def make_token(user: str, secret: str, ttl_h: int) -> str:
    """base64url(user).base64url(exp_unix).hexsig — hexsig = HMAC(secret, "user.exp")."""
    exp = int(time.time()) + int(ttl_h * 3600)
    payload = f"{user}.{exp}"
    return ".".join((
        _b64url_encode(user.encode("utf-8")),
        _b64url_encode(str(exp).encode("ascii")),
        _sign(secret, payload),
    ))


def verify_token(token: str, secret: str) -> tuple[bool, str | None, int | None]:
    """(ok, user, exp) — valida assinatura (hmac.compare_digest) e expiração."""
    try:
        user_b64, exp_b64, sig = token.strip().split(".")
        user = _b64url_decode(user_b64).decode("utf-8")
        exp = int(_b64url_decode(exp_b64).decode("ascii"))
    except (ValueError, UnicodeDecodeError):
        return False, None, None
    if not hmac.compare_digest(sig, _sign(secret, f"{user}.{exp}")):
        return False, None, None
    if exp <= int(time.time()):
        return False, None, None
    return True, user, exp


def iso_utc(ts: int) -> str:
    """Unix → ISO-8601 UTC com 'Z' (formato date-time do contrato)."""
    return datetime.fromtimestamp(ts, timezone.utc).isoformat(
        timespec="milliseconds").replace("+00:00", "Z")


# --------------------------------------------------------------- dependency
def require_auth(request: Request) -> AuthInfo:
    """Dependency FastAPI: exige `Authorization: Bearer <token>` válido.

    401 {"error": {"code": "unauthorized"}} se ausente/inválido/expirado — e
    também quando a auth não está configurada (fail closed, nunca aberto).
    """
    cfg = auth_config()
    header = request.headers.get("Authorization", "")
    scheme, _, token = header.partition(" ")
    if cfg is None or scheme.lower() != "bearer" or not token.strip():
        raise GameError(401, "unauthorized", "missing or invalid bearer token")
    ok, user, exp = verify_token(token, cfg["secret"])
    if not ok:
        raise GameError(401, "unauthorized", "missing or invalid bearer token")
    return AuthInfo(user=user, exp=exp)
