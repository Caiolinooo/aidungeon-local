"""App FastAPI — AI Dungeon Local (contrato: docs/openapi.yaml).

- Auth Bearer obrigatória em TODAS as rotas /api, exceto POST /api/auth/login
  (app/auth.py; credenciais via .env — fail closed, nunca aberto, se ausentes).
- CORS: origins de config/settings.json → cors_origins (domínio Vercel entra lá).
- Erros no schema `Error`: {"error": {"code", "message"}}.
- Banco SQLite inicializado no startup (backend/schema.sql).
- Estáticos da SPA (fora de /api) são PÚBLICOS — a página de login precisa carregar.

Rodar a partir de backend/:  uvicorn app.main:app --port 8000
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

from app import auth as auth_core
from app import db, settings_store
from app.auth import require_auth
from app.errors import GameError
from app.routes import (actions, adventures, cards, generation, health,
                        import_export, memory, scenarios, settings)
from app.routes import auth as auth_routes

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db()
    if auth_core.auth_config() is None:
        log.warning(
            "AUTH NÃO CONFIGURADA — defina AUTH_USER, AUTH_PASSWORD_HASH e AUTH_SECRET "
            "no .env da raiz. Fail closed: rotas /api respondem 401 e "
            "POST /api/auth/login responde 503."
        )
    yield


app = FastAPI(title="AI Dungeon Local — API", version="0.1.0", lifespan=lifespan)

# CORS: lista vem de config/settings.json (cors_origins) — adicionar lá o
# domínio da Vercel quando o frontend for publicado.
_cors_origins = settings_store.load_file_settings().get("cors_origins") or [
    "http://127.0.0.1:8000", "http://localhost:8000", "http://localhost:5173",
]
app.add_middleware(
    CORSMiddleware,
    allow_origins=list(_cors_origins),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ------------------------------------------------------- exception handlers
@app.exception_handler(GameError)
def game_error_handler(request: Request, exc: GameError) -> JSONResponse:
    return JSONResponse(status_code=exc.status,
                        content={"error": {"code": exc.code, "message": exc.message}})


@app.exception_handler(RequestValidationError)
def validation_error_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    messages = "; ".join(
        f"{'.'.join(str(p) for p in e.get('loc', []))}: {e.get('msg', '')}"
        for e in exc.errors()
    )
    return JSONResponse(status_code=422,
                        content={"error": {"code": "validation_error",
                                           "message": messages or "invalid payload"}})


# ------------------------------------------------------------------ routers
# POST /api/auth/login é PÚBLICO (security: [] no contrato); TODAS as demais
# rotas /api exigem Bearer — a dependência vai no include_router, sem editar
# cada rota individualmente.
app.include_router(auth_routes.router, prefix="/api", tags=["auth"])

_protected = [Depends(require_auth)]
app.include_router(scenarios.router, prefix="/api/scenarios", tags=["scenarios"],
                   dependencies=_protected)
app.include_router(adventures.router, prefix="/api/adventures", tags=["adventures"],
                   dependencies=_protected)
app.include_router(generation.router, prefix="/api/adventures", tags=["generation"],
                   dependencies=_protected)
app.include_router(actions.router, prefix="/api/actions", tags=["actions"],
                   dependencies=_protected)
app.include_router(cards.router, prefix="/api/adventures", tags=["cards"],
                   dependencies=_protected)
app.include_router(memory.router, prefix="/api/adventures", tags=["memory"],
                   dependencies=_protected)
app.include_router(import_export.router, prefix="/api", tags=["import-export"],
                   dependencies=_protected)
app.include_router(settings.router, prefix="/api", tags=["settings"],
                   dependencies=_protected)
app.include_router(health.router, prefix="/api", tags=["health"],
                   dependencies=_protected)


# ------------------------------------------------------------------ frontend
# Serve o build do frontend (frontend/dist) na raiz, com fallback p/ index.html
# (rotas client-side como /login e /play/:id). Montado DEPOIS dos routers:
# /api/* mantém precedência; 404s de /api/* não caem no fallback SPA.
class _SpaStaticFiles(StaticFiles):
    async def get_response(self, path: str, scope):
        try:
            return await super().get_response(path, scope)
        except StarletteHTTPException as exc:
            if exc.status_code == 404 and not path.startswith("api/"):
                return await super().get_response("index.html", scope)
            raise


_frontend_dist = Path(__file__).resolve().parents[2] / "frontend" / "dist"
if _frontend_dist.is_dir():
    app.mount("/", _SpaStaticFiles(directory=_frontend_dist, html=True), name="spa")
