"""Fixtures compartilhadas — banco temporário, app FastAPI e mock do LLM."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import httpx
import pytest

BACKEND_DIR = Path(__file__).resolve().parents[1] / "backend"
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app import db, llm_client  # noqa: E402


@pytest.fixture()
def tmp_db(tmp_path, monkeypatch):
    """Banco SQLite isolado por teste (env AIDUNGEON_DB_PATH)."""
    monkeypatch.setenv("AIDUNGEON_DB_PATH", str(tmp_path / "test.db"))
    db.init_db()
    return tmp_path / "test.db"


@pytest.fixture(autouse=True)
def _clean_llm_state(monkeypatch):
    """Cache de descoberta limpo + api_key de ambiente neutralizada por teste."""
    llm_client.invalidate_discovery_cache()
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    monkeypatch.setenv("AIDUNGEON_ENV_PATH", str(Path("/nonexistent/.env")))
    yield
    llm_client.invalidate_discovery_cache()


@pytest.fixture()
def client(tmp_db):
    from fastapi.testclient import TestClient

    from app.main import app
    with TestClient(app) as c:
        yield c


# ------------------------------------------------------------------ mock LLM
def sse_body(tokens, reasoning_only: bool = False) -> str:
    """Corpo SSE fake de /chat/completions."""
    lines = []
    for t in tokens:
        delta = {"reasoning_content": t} if reasoning_only else {"content": t}
        lines.append(f"data: {json.dumps({'choices': [{'delta': delta}]})}\n\n")
    lines.append("data: [DONE]\n\n")
    return "".join(lines)


def make_llm_transport(tokens=("You", " open"), model_id="mock-model",
                       capture: list | None = None, fail: str | None = None,
                       reasoning_only: bool = False) -> httpx.MockTransport:
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
                text=sse_body(tokens, reasoning_only=reasoning_only),
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
