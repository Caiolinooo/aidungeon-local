"""Fixtures compartilhadas — banco temporário, app FastAPI e mock do LLM."""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import httpx
import pytest

BACKEND_DIR = Path(__file__).resolve().parents[1] / "backend"
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app import auth as auth_core  # noqa: E402
from app import choices_service, db, events, llm_client, llm_scheduler  # noqa: E402

# Credenciais de TESTE para a auth (nunca as do .env real — ver `_auth_env` e
# `_clean_llm_state`, que aponta AIDUNGEON_ENV_PATH para um arquivo inexistente).
TEST_AUTH_USER = "tester"
TEST_AUTH_PASSWORD = "correct-horse-battery-staple"
TEST_AUTH_SECRET = "test-secret-0123456789abcdef"


@pytest.fixture()
def tmp_db(tmp_path, monkeypatch):
    """Banco SQLite isolado por teste (env AIDUNGEON_DB_PATH)."""
    monkeypatch.setenv("AIDUNGEON_DB_PATH", str(tmp_path / "test.db"))
    db.init_db()
    # A suíte legada mocka SDWebUI; o default FineView é testado explicitamente.
    with db.session() as conn:
        db.set_setting(conn, "image.provider", json.dumps("sdwebui"))
    return tmp_path / "test.db"


@pytest.fixture(autouse=True)
def _clean_llm_state(monkeypatch):
    """Cache de descoberta limpo + api_key de ambiente neutralizada por teste."""
    llm_client.invalidate_discovery_cache()
    monkeypatch.setenv("AIDUNGEON_CHRONICLE", "0")
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    monkeypatch.setenv("AIDUNGEON_ENV_PATH", str(Path("/nonexistent/.env")))
    yield
    llm_client.invalidate_discovery_cache()


@pytest.fixture(autouse=True)
def _scheduler_reset():
    """Fila, slots ocupados e breaker do llm_scheduler não vazam entre testes."""
    llm_scheduler.reset()
    yield
    llm_scheduler.reset()


@pytest.fixture(autouse=True)
def _events_reset():
    """Canais de eventos e cache de choices não vazam entre testes."""
    events.reset()
    choices_service.reset()
    yield
    choices_service.reset()
    events.reset()


@pytest.fixture(autouse=True)
def _auth_env(monkeypatch):
    """Auth configurada com credenciais de teste (todas as rotas /api, exceto
    login, exigem Bearer — sem isto os testes existentes tomariam 401)."""
    monkeypatch.setenv("AUTH_USER", TEST_AUTH_USER)
    monkeypatch.setenv(
        "AUTH_PASSWORD_HASH",
        hashlib.sha256(TEST_AUTH_PASSWORD.encode("utf-8")).hexdigest(),
    )
    monkeypatch.setenv("AUTH_SECRET", TEST_AUTH_SECRET)


@pytest.fixture(autouse=True)
def _jobs_worker_off(monkeypatch):
    """Worker de jobs desligado nos testes — a fila é drenada explicitamente
    via jobs.process_pending(transport) para determinismo."""
    monkeypatch.setenv("AIDUNGEON_JOBS_WORKER", "0")


@pytest.fixture()
def auth_token() -> str:
    """Bearer token válido emitido com as credenciais de teste."""
    return auth_core.make_token(TEST_AUTH_USER, TEST_AUTH_SECRET, ttl_h=1)



@pytest.fixture()
def admin_token() -> str:
    """Bearer do admin para testes de operações globais privilegiadas."""
    return auth_core.make_token("Caio", TEST_AUTH_SECRET, ttl_h=1)

@pytest.fixture()
def client(tmp_db, auth_token):
    """TestClient autenticado (Authorization: Bearer em todas as chamadas)."""
    from fastapi.testclient import TestClient

    from app.main import app
    app.dependency_overrides.clear()
    with TestClient(app) as c:
        c.headers["Authorization"] = f"Bearer {auth_token}"
        yield c
    app.dependency_overrides.clear()


# ------------------------------------------------------------------ mock LLM
def sse_body(tokens, reasoning_only: bool = False,
             finish_reason: str | None = None) -> str:
    """Corpo SSE fake de /chat/completions."""
    lines = []
    for t in tokens:
        delta = {"reasoning_content": t} if reasoning_only else {"content": t}
        lines.append(f"data: {json.dumps({'choices': [{'delta': delta}]})}\n\n")
    if finish_reason:
        lines.append(
            "data: "
            + json.dumps({"choices": [{"delta": {}, "finish_reason": finish_reason}]})
            + "\n\n"
        )
    lines.append("data: [DONE]\n\n")
    return "".join(lines)


def make_llm_transport(tokens=("You", " open"), model_id="mock-model",
                       capture: list | None = None, fail: str | None = None,
                       reasoning_only: bool = False,
                       finish_reason: str | None = None) -> httpx.MockTransport:
    """MockTransport do servidor llama.cpp: /models + /chat/completions (SSE).

    fail: 'connect' (queima conexão), 'auth' (401), 'http500'.
    capture: lista que recebe os payloads JSON de /chat/completions.
    """
    def handler(request: httpx.Request) -> httpx.Response:
        if fail == "connect":
            raise httpx.ConnectError("mock: connection refused")
        path = request.url.path
        if path.endswith("/models"):
            if fail == "auth":
                return httpx.Response(401, json={"error": "unauthorized"})
            return httpx.Response(200, json={
                "object": "list",
                "data": [{"id": model_id, "object": "model"}],
            })
        if path.endswith("/chat/completions"):
            if fail == "auth":
                return httpx.Response(401, json={"error": "unauthorized"})
            if fail == "http500":
                return httpx.Response(500, text="boom")
            if capture is not None:
                capture.append(json.loads(request.content.decode("utf-8")))
            return httpx.Response(
                200, headers={"content-type": "text/event-stream"},
                text=sse_body(tokens, reasoning_only=reasoning_only,
                              finish_reason=finish_reason),
            )
        return httpx.Response(404, json={"error": f"no mock route: {path}"})

    return httpx.MockTransport(handler)


# --------------------------------------------------------------- dados base
def make_adventure(**fields) -> dict:
    """Cria aventura direto no banco (sem passar pela API)."""
    with db.session() as conn:
        return db.insert_adventure(conn, title=fields.pop("title", "Test Adventure"),
                                   **fields)


def add_action(adventure_id: str, seq: int, type_: str, text: str) -> dict:
    with db.session() as conn:
        return db.insert_action(conn, adventure_id, seq, type_, text)
