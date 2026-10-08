"""Smoke REAL contra a VM (PLANO.md §11) — pulado automaticamente sem LLM_API_KEY.

Roda o fluxo feliz de ponta a ponta contra o llama.cpp REAL
(http://vm.groupabz.com:1234 — config/settings.json) e as credenciais REAIS
do `.env` da raiz (que NUNCA são impressas nem commitadas — .env é gitignored):

    1. middleware Bearer ativo (rota protegida sem token → 401);
    2. POST /api/auth/login com senha errada → 401 genérico (prova o endpoint
       contra o AUTH_PASSWORD_HASH real — o .env guarda só o HASH da senha,
       então o token do fluxo é emitido pelo MESMO mecanismo do login:
       make_token(AUTH_USER, AUTH_SECRET));
    3. GET /api/auth/check com o token → 200 e usuário real;
    4. GET /api/health/llm → status ok + model_discovered preenchido;
    5. aventura sintética vazia → 1 action `do` com SSE até `done`
       (texto não-vazio, sem vazar reasoning do Qwen3.x);
    6. undo → 0 ações;
    7. a aventura sintética é apagada DIRETO NO SQLITE (a API não tem DELETE).

SEGURANÇA DOS DADOS REAIS: o banco é SEMPRE temporário (fixture `tmp_db` do
conftest → AIDUNGEON_DB_PATH em tmp_path). O teste se recusa a rodar se o
banco efetivo for data\\aidungeon.db — onde vive a aventura "Faerûn: Caio",
que jamais é tocada.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
ENV_PATH = REPO_ROOT / ".env"


def _dotenv_key(name: str) -> str | None:
    """Lê UMA chave do .env real (valor nunca é impresso/logado)."""
    try:
        for line in ENV_PATH.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, _, value = line.partition("=")
                if key.strip() == name:
                    return value.strip().strip('"').strip("'") or None
    except OSError:
        pass
    return None


_HAS_LLM_KEY = bool(os.environ.get("LLM_API_KEY") or _dotenv_key("LLM_API_KEY"))

pytestmark = pytest.mark.skipif(
    not _HAS_LLM_KEY,
    reason="LLM_API_KEY ausente (env/.env) — CI sem acesso à VM pula o smoke",
)


def parse_sse(body: str) -> list[tuple[str, dict]]:
    events: list[tuple[str, dict]] = []
    for block in body.split("\n\n"):
        block = block.strip()
        if not block:
            continue
        event = data = None
        for line in block.splitlines():
            if line.startswith("event:"):
                event = line[len("event:"):].strip()
            elif line.startswith("data:"):
                data = line[len("data:"):].strip()
        events.append((event, json.loads(data)))
    return events


@pytest.fixture()
def real_vm_env(tmp_db, monkeypatch):
    """Aponta a config para o .env REAL (credenciais + LLM_API_KEY).

    O conftest neutraliza o ambiente por teste (AIDUNGEON_ENV_PATH para um
    arquivo inexistente e AUTH_* de teste) — aqui desfazemos isso: o .env
    real volta a ser a fonte de LLM_API_KEY, AUTH_USER, AUTH_PASSWORD_HASH e
    AUTH_SECRET. O banco continua sendo o temporário do `tmp_db`.
    """
    from app import db

    monkeypatch.setenv("AIDUNGEON_ENV_PATH", str(ENV_PATH))
    for name in ("AUTH_USER", "AUTH_PASSWORD_HASH", "AUTH_SECRET"):
        monkeypatch.delenv(name, raising=False)  # cai no .env real

    # Guarda de segurança: NUNCA rodar contra o banco real.
    assert Path(db.db_path()) != db.DEFAULT_DB_PATH, (
        "smoke recusado: banco efetivo é data/aidungeon.db (Faerûn: Caio)")
    return tmp_db


def test_smoke_vm_real_flow(real_vm_env):
    from fastapi.testclient import TestClient

    from app import auth as auth_core
    from app import db
    from app.main import app
    from app.routes import auth as auth_routes

    auth_cfg = auth_core.auth_config()
    assert auth_cfg is not None, "credenciais reais não carregaram do .env"

    adv_id = None
    try:
        with TestClient(app) as client:
            # 1 — middleware Bearer ativo em rota protegida
            res = client.get("/api/adventures")
            assert res.status_code == 401

            # 2 — login real: senha errada → 401 genérico (endpoint vivo contra
            # o hash real). O .env só guarda o HASH; o token do fluxo abaixo é
            # emitido pelo mesmo mecanismo do login (HMAC com AUTH_SECRET).
            bad = client.post("/api/auth/login", json={
                "username": auth_cfg["user"],
                "password": "senha-propositalmente-errada",
            })
            assert bad.status_code == 401
            assert bad.json()["error"]["code"] == "invalid_credentials"
            auth_routes.reset_throttle()  # não deixa contador p/ outros testes

            token = auth_core.make_token(auth_cfg["user"], auth_cfg["secret"], ttl_h=1)
            client.headers["Authorization"] = f"Bearer {token}"

            # 3 — token válido contra o AUTH_SECRET real
            check = client.get("/api/auth/check")
            assert check.status_code == 200
            assert check.json()["user"] == auth_cfg["user"]

            # 4 — health do LLM real (VM): ok + modelo descoberto
            health = client.get("/api/health/llm").json()
            assert health["status"] == "ok", f"LLM não saudável: {health}"
            assert health["model_loaded"] is True
            assert health["model_discovered"], f"sem modelo descoberto: {health}"

            # 5 — aventura sintética vazia + 1 action `do` com SSE até done
            created = client.post("/api/adventures",
                                  json={"title": "QA smoke (sintética — apagar)"})
            assert created.status_code == 201
            adv_id = created.json()["id"]

            t0 = time.monotonic()
            # (sem `timeout` no TestClient — deprecado; quem governa é o
            # timeout de 180s do llm_client, que em falha vira evento `error`)
            res = client.post(
                f"/api/adventures/{adv_id}/action",
                json={"type": "do",
                      "text": "look around and take in every detail of the room"},
            )
            elapsed = time.monotonic() - t0
            assert res.status_code == 200, f"action falhou: {res.status_code}"
            events = parse_sse(res.text)
            kinds = [e for e, _ in events]
            assert "error" not in kinds, f"stream com erro: {events[-1]}"
            assert kinds[-1] == "done"
            done = events[-1][1]
            text = done["action"]["text"]
            assert text.strip(), "resposta da IA vazia"
            # sem vazar reasoning do Qwen3.x híbrido (disable_thinking ativo)
            for leak in ("<think>", "</think>", "reasoning_content"):
                assert leak not in text
            n_tokens = sum(1 for e, _ in events if e == "token")
            print(f"\n[smoke-vm] modelo={health['model_discovered']} · "
                  f"geração em {elapsed:.1f}s ({n_tokens} eventos token, "
                  f"~{n_tokens / elapsed:.1f} tok/s) · resposta: {text[:80]!r}")

            # 6 — undo volta o turno (aventura vazia → 0 ações)
            und = client.post(f"/api/adventures/{adv_id}/undo")
            assert und.status_code == 200
            assert und.json()["action_count"] == 0
    finally:
        # 7 — remove a aventura sintética DIRETO NO SQLITE (a API não tem
        # DELETE). Banco = tmp_db; "Faerûn: Caio" (data/aidungeon.db) intacta.
        if adv_id:
            with db.session() as conn:
                conn.execute("DELETE FROM actions WHERE adventure_id=?", (adv_id,))
                conn.execute("DELETE FROM story_cards WHERE adventure_id=?", (adv_id,))
                conn.execute("DELETE FROM memories WHERE adventure_id=?", (adv_id,))
                conn.execute("DELETE FROM adventures WHERE id=?", (adv_id,))
            with db.session() as conn:
                assert db.get_adventure(conn, adv_id) is None
