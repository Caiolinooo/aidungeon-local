"""Testes de autenticação (PLANO.md §11; contrato: docs/openapi.yaml §auth).

Credenciais de TESTE via fixtures do conftest (`_auth_env` + monkeypatch) —
NUNCA o .env real (AIDUNGEON_ENV_PATH aponta para arquivo inexistente).
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parents[1] / "backend"
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app import auth as auth_core  # noqa: E402
from app.routes import auth as auth_routes  # noqa: E402

from conftest import TEST_AUTH_PASSWORD, TEST_AUTH_SECRET, TEST_AUTH_USER  # noqa: E402


@pytest.fixture(autouse=True)
def _reset_login_throttle():
    """Throttle do login é estado global em memória — zera entre testes."""
    auth_routes.reset_throttle()
    yield
    auth_routes.reset_throttle()


@pytest.fixture()
def raw_client(tmp_db):
    """TestClient SEM Authorization — para exercitar 401s e o login público."""
    from fastapi.testclient import TestClient

    from app.main import app
    with TestClient(app) as c:
        yield c


def _login(client, username=TEST_AUTH_USER, password=TEST_AUTH_PASSWORD):
    return client.post("/api/auth/login",
                       json={"username": username, "password": password})


# --------------------------------------------------------------------- login
def test_login_ok_returns_token_and_check_passes(raw_client):
    res = _login(raw_client)  # sem Authorization: o login é público
    assert res.status_code == 200
    body = res.json()
    assert body["token"].count(".") == 2  # base64url(user).base64url(exp).hexsig
    assert body["expires_at"]

    check = raw_client.get("/api/auth/check",
                           headers={"Authorization": f"Bearer {body['token']}"})
    assert check.status_code == 200
    data = check.json()
    assert data["ok"] is True
    assert data["user"] == TEST_AUTH_USER
    assert data["expires_at"] == body["expires_at"]


def test_login_wrong_password_generic_401(raw_client):
    res = _login(raw_client, password="wrong-password")
    assert res.status_code == 401
    assert res.json()["error"]["code"] == "invalid_credentials"


def test_login_wrong_user_identical_401(raw_client):
    """Usuário errado devolve EXATAMENTE o mesmo 401 da senha errada."""
    by_user = _login(raw_client, username="nobody")
    by_pass = _login(raw_client, password="wrong-password")
    assert by_user.status_code == 401 == by_pass.status_code
    assert by_user.json() == by_pass.json()


def test_login_malformed_payload_422(raw_client):
    assert raw_client.post("/api/auth/login", json={}).status_code == 422


def test_login_throttle_429_after_5_failures(raw_client):
    for _ in range(5):
        assert _login(raw_client, password="nope").status_code == 401
    res = _login(raw_client, password="nope")
    assert res.status_code == 429
    assert res.json()["error"]["code"] == "rate_limited"
    # durante o lockout até a credencial correta é bloqueada
    assert _login(raw_client).status_code == 429


# --------------------------------------------------------- rotas protegidas
def test_protected_route_without_token_401(raw_client):
    res = raw_client.get("/api/adventures")
    assert res.status_code == 401
    assert res.json()["error"]["code"] == "unauthorized"


def test_protected_route_with_token_200(raw_client, auth_token):
    res = raw_client.get("/api/adventures",
                         headers={"Authorization": f"Bearer {auth_token}"})
    assert res.status_code == 200
    assert res.json() == []


def test_tampered_token_401(raw_client, auth_token):
    bad = auth_token[:-2] + ("aa" if not auth_token.endswith("aa") else "bb")
    res = raw_client.get("/api/adventures",
                         headers={"Authorization": f"Bearer {bad}"})
    assert res.status_code == 401


def test_expired_token_401(raw_client):
    expired = auth_core.make_token(TEST_AUTH_USER, TEST_AUTH_SECRET, ttl_h=-1)
    res = raw_client.get("/api/adventures",
                         headers={"Authorization": f"Bearer {expired}"})
    assert res.status_code == 401


def test_garbage_authorization_header_401(raw_client):
    for header in ("Token abc", "Bearer", "Bearer token-sem-formato", "Bearer a.b"):
        res = raw_client.get("/api/adventures", headers={"Authorization": header})
        assert res.status_code == 401, header


# ------------------------------------------------------------------ estático
def test_spa_static_public_without_token(raw_client):
    """Arquivos da SPA (fora de /api) são públicos — o login precisa carregar."""
    dist = Path(__file__).resolve().parents[1] / "frontend" / "dist"
    if not (dist / "index.html").is_file():
        pytest.skip("frontend/dist não existe (rode npm run build)")
    res = raw_client.get("/")
    assert res.status_code == 200
    assert "text/html" in res.headers["content-type"]


# --------------------------------------------------------- não configurado
def test_auth_not_configured_fail_closed(raw_client, monkeypatch):
    monkeypatch.delenv("AUTH_USER", raising=False)
    monkeypatch.delenv("AUTH_PASSWORD_HASH", raising=False)
    monkeypatch.delenv("AUTH_SECRET", raising=False)

    res = _login(raw_client)
    assert res.status_code == 503
    assert res.json()["error"]["code"] == "auth_not_configured"

    # demais rotas /api: 401 mesmo com token bem formado — fail closed
    token = auth_core.make_token("x", "y", 1)
    res = raw_client.get("/api/adventures",
                         headers={"Authorization": f"Bearer {token}"})
    assert res.status_code == 401
