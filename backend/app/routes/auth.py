"""Router /api/auth — login (PÚBLICO, security: []) + check (protegido).

Contrato: docs/openapi.yaml (/api/auth/login, /api/auth/check).
- Login compara username == AUTH_USER e sha256(password) == AUTH_PASSWORD_HASH
  com hmac.compare_digest; falha → 401 genérico "invalid_credentials" (não
  distingue usuário de senha).
- Throttle em memória (o app é exposto na internet): 5 falhas seguidas →
  429 "rate_limited" por 60s — durante o lockout até credenciais corretas
  são bloqueadas. Estado em memória: reiniciar o processo zera o throttle.
- Sem as envs de auth configuradas: login responde 503 "auth_not_configured"
  (fail closed; as demais rotas /api respondem 401 via require_auth).
"""

from __future__ import annotations

import hashlib
import hmac
import time

from fastapi import APIRouter, Depends

from app import auth
from app.errors import GameError
from app.schemas import LoginRequest

router = APIRouter()

MAX_FAILURES = 5
LOCKOUT_SECONDS = 60

_fail_count = 0
_locked_until = 0.0  # time.monotonic() até quando o login está bloqueado


def reset_throttle() -> None:
    """Zera o throttle (usado pelos testes)."""
    global _fail_count, _locked_until
    _fail_count = 0
    _locked_until = 0.0


@router.post("/auth/login")
def login(body: LoginRequest) -> dict:
    """Autentica e devolve {token, expires_at} (LoginResponse do contrato)."""
    global _fail_count, _locked_until
    if time.monotonic() < _locked_until:
        raise GameError(429, "rate_limited",
                        "too many failed attempts — wait 60 seconds and try again")

    cfg = auth.auth_config()
    if cfg is None:
        raise GameError(503, "auth_not_configured",
                        "authentication is not configured on this server")

    # comparação em tempo constante; sem early-exit entre usuário e senha
    user_ok = hmac.compare_digest(body.username, cfg["user"])
    pw_hash = hashlib.sha256(body.password.encode("utf-8")).hexdigest()
    pw_ok = hmac.compare_digest(pw_hash, cfg["password_hash"])
    if not (user_ok and pw_ok):
        _fail_count += 1
        if _fail_count >= MAX_FAILURES:
            _locked_until = time.monotonic() + LOCKOUT_SECONDS
            _fail_count = 0
        raise GameError(401, "invalid_credentials", "invalid username or password")

    _fail_count = 0
    token = auth.make_token(cfg["user"], cfg["secret"], cfg["ttl_hours"])
    _, _, exp = auth.verify_token(token, cfg["secret"])
    return {"token": token, "expires_at": auth.iso_utc(exp)}


@router.get("/auth/check")
def check(info: auth.AuthInfo = Depends(auth.require_auth)) -> dict:
    """Confirma que o Bearer token atual é válido (usado pelo AuthGate da SPA)."""
    return {"ok": True, "user": info.user, "expires_at": auth.iso_utc(info.exp)}
