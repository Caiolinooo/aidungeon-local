"""Router /api/auth — login (PÚBLICO, security: []) + check (protegido).

Contrato: docs/openapi.yaml (/api/auth/login, /api/auth/check).
- Login compara o usuário e a senha com hmac.compare_digest. A senha aceita
  scrypt$salt$hash e, ainda, sha256 hex legado. Falha → 401 genérico
  "invalid_credentials" (não distingue usuário de senha). Hash legado que
  acerta é regravado em scrypt.
- Throttle em memória, por IP (request.client.host; sem host → "unknown"):
  5 falhas seguidas → 429 "rate_limited" por 60s naquele IP — durante o
  lockout até credenciais corretas são bloqueadas. Login certo zera a chave.
  Entradas vencidas saem do mapa. Reiniciar o processo zera o throttle.
- Sem as envs de auth configuradas: login responde 503 "auth_not_configured"
  (fail closed; as demais rotas /api respondem 401 via require_auth).
"""

from __future__ import annotations

import hmac
import time

from fastapi import APIRouter, Depends, Request

from app import auth, db, users
from app.errors import GameError
from app.schemas import LoginRequest, PasswordChange

router = APIRouter()

MAX_FAILURES = 5
LOCKOUT_SECONDS = 60

# host -> (fail_count, locked_until, touched) em time.monotonic()
_attempts: dict[str, tuple[int, float, float]] = {}


def reset_throttle() -> None:
    """Zera o throttle (usado pelos testes)."""
    _attempts.clear()


def _client_host(request: Request) -> str:
    client = request.client
    host = client.host if client is not None else ""
    host = str(host or "").strip()
    return host or "unknown"


def _prune_attempts(now: float) -> None:
    """Tira lockout vencido e rachas parados há mais que a janela."""
    stale = [
        host
        for host, (count, locked_until, touched) in _attempts.items()
        if locked_until <= now and (count == 0 or touched + LOCKOUT_SECONDS <= now)
    ]
    for host in stale:
        _attempts.pop(host, None)


def _locked(host: str, now: float) -> bool:
    row = _attempts.get(host)
    return row is not None and now < row[1]


def _record_failure(host: str, now: float) -> None:
    count, locked_until, _touched = _attempts.get(host, (0, 0.0, now))
    if now < locked_until:
        return
    count += 1
    if count >= MAX_FAILURES:
        _attempts[host] = (0, now + LOCKOUT_SECONDS, now)
    else:
        _attempts[host] = (count, 0.0, now)


@router.post("/auth/login")
def login(body: LoginRequest, request: Request) -> dict:
    """Autentica e devolve {token, expires_at} (LoginResponse do contrato)."""
    now = time.monotonic()
    host = _client_host(request)
    _prune_attempts(now)
    if _locked(host, now):
        raise GameError(429, "rate_limited",
                        "too many failed attempts — wait 60 seconds and try again")

    cfg = auth.auth_config()
    if cfg is None:
        raise GameError(503, "auth_not_configured",
                        "authentication is not configured on this server")

    with db.session() as conn:
        users.ensure_seed(conn, cfg)
        account = users.get_user(conn, body.username)
        if account is not None:
            user_ok = hmac.compare_digest(body.username, account["username"])
            pw_ok = users.password_matches(body.password, account["password_hash"])
            if user_ok and pw_ok and users.is_legacy_password_hash(account["password_hash"]):
                users.upgrade_password_hash(conn, account["username"], body.password)
        else:
            # Conta que ainda não entrou na tabela: o par do ambiente.
            user_ok = hmac.compare_digest(body.username, cfg["user"])
            pw_ok = users.password_matches(body.password, cfg["password_hash"])
    if not (user_ok and pw_ok):
        _record_failure(host, now)
        raise GameError(401, "invalid_credentials", "invalid username or password")

    _attempts.pop(host, None)
    token = auth.make_token(body.username, cfg["secret"], cfg["ttl_hours"])
    _, _, exp = auth.verify_token(token, cfg["secret"])
    return {"token": token, "expires_at": auth.iso_utc(exp)}


@router.get("/auth/check")
def check(info: auth.AuthInfo = Depends(auth.require_auth)) -> dict:
    """Confirma que o Bearer token atual é válido (usado pelo AuthGate da SPA)."""
    return {
        "ok": True,
        "user": info.user,
        "admin": users.is_admin(info.user),
        "expires_at": auth.iso_utc(info.exp),
    }


@router.post("/auth/password")
def change_password(body: PasswordChange,
                    info: auth.AuthInfo = Depends(auth.require_auth)) -> dict:
    """Troca a senha de quem está logado. Pede a senha atual."""
    cfg = auth.auth_config()
    if cfg is None:
        raise GameError(503, "auth_not_configured",
                        "authentication is not configured on this server")
    with db.session() as conn:
        users.ensure_seed(conn, cfg)
        account = users.get_user(conn, info.user)
        if account is None or not users.password_matches(body.current_password,
                                                         account["password_hash"]):
            raise GameError(401, "invalid_credentials", "invalid username or password")
        try:
            users.set_password(conn, info.user, body.new_password)
        except ValueError as exc:
            raise GameError.bad_request("invalid_password", str(exc)) from exc
    return {"ok": True}
