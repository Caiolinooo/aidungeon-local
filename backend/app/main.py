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
from app import access, db, jobs, settings_store, users
from app.auth import require_auth
from app.errors import GameError
from app.routes import (actions, adventures, assistant, cards, choices,
                        generation, health, images, import_export, jobs as jobs_routes,
                        memory, scenarios, scene as scene_routes, settings, ui_profile,
                        voice)
from app.routes import auth as auth_routes
from app.routes import events as events_routes
from app.routes import likeness as likeness_routes
from app.routes import users as users_routes

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db()
    cfg = auth_core.auth_config()
    if cfg is not None:
        with db.session() as conn:
            users.ensure_seed(conn, cfg)
    if auth_core.auth_config() is None:
        log.warning(
            "AUTH NÃO CONFIGURADA — defina AUTH_USER, AUTH_PASSWORD_HASH e AUTH_SECRET "
            "no .env da raiz. Fail closed: rotas /api respondem 401 e "
            "POST /api/auth/login responde 503."
        )
    jobs.start_worker(app)
    try:
        yield
    finally:
        jobs.stop_worker(app)


app = FastAPI(title="AI Dungeon Local — API", version="0.4.0", lifespan=lifespan)

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


@app.middleware("http")
async def own_stories(request: Request, call_next):
    """História de outra pessoa responde 404, o mesmo de um id que não existe."""
    user = access.user_from_request(request)
    if user and access.hidden_path(request.url.path, user):
        return JSONResponse(
            status_code=404,
            content={"error": {"code": "not_found", "message": "not found"}},
        )
    return await call_next(request)

_protected = [Depends(require_auth)]
app.include_router(users_routes.router, prefix="/api", tags=["users"],
                   dependencies=_protected)
app.include_router(scenarios.router, prefix="/api/scenarios", tags=["scenarios"],
                   dependencies=_protected)
app.include_router(adventures.router, prefix="/api/adventures", tags=["adventures"],
                   dependencies=_protected)
app.include_router(scene_routes.router, prefix="/api/adventures", tags=["scene"],
                   dependencies=_protected)
app.include_router(generation.router, prefix="/api/adventures", tags=["generation"],
                   dependencies=_protected)
app.include_router(choices.router, prefix="/api/adventures", tags=["generation"],
                   dependencies=_protected)
app.include_router(ui_profile.router, prefix="/api/adventures", tags=["ui-profile"],
                   dependencies=_protected)
app.include_router(images.router, prefix="/api", tags=["images"],
                   dependencies=_protected)
app.include_router(assistant.router, prefix="/api", tags=["assistant"],
                   dependencies=_protected)
app.include_router(actions.router, prefix="/api/actions", tags=["actions"],
                   dependencies=_protected)
app.include_router(cards.router, prefix="/api/adventures", tags=["cards"],
                   dependencies=_protected)
app.include_router(likeness_routes.router, prefix="/api", tags=["likeness"],
                   dependencies=_protected)
app.include_router(jobs_routes.router, prefix="/api/adventures", tags=["assets"],
                   dependencies=_protected)
app.include_router(memory.router, prefix="/api/adventures", tags=["memory"],
                   dependencies=_protected)
app.include_router(import_export.router, prefix="/api", tags=["import-export"],
                   dependencies=_protected)
app.include_router(settings.router, prefix="/api", tags=["settings"],
                   dependencies=_protected)
app.include_router(voice.router, prefix="/api", tags=["voice"],
                   dependencies=_protected)
app.include_router(health.router, prefix="/api", tags=["health"],
                   dependencies=_protected)

# --- Grok (G4): eventos em SSE. Fora do _protected porque o EventSource do
# navegador não manda header: a rota aceita Bearer ou ?token= e checa o dono.
app.include_router(events_routes.router, prefix="/api/adventures", tags=["events"])
# --- Grok (G5): GET /api/jobs/{id}, polling do See assíncrono (C6).
app.include_router(jobs_routes.by_id, prefix="/api", tags=["assets"],
                   dependencies=_protected)


# ------------------------------------------------------------------ frontend
# Serve o build do frontend (frontend/dist) na raiz, com fallback p/ index.html
# (rotas client-side como /login e /play/:id). Montado DEPOIS dos routers:
# /api/* mantém precedência; 404s de /api/* não caem no fallback SPA.
class _SpaStaticFiles(StaticFiles):
    async def get_response(self, path: str, scope):
        served = path
        try:
            response = await super().get_response(path, scope)
        except StarletteHTTPException as exc:
            if exc.status_code == 404 and not path.startswith("api/"):
                # SPA fallback: client routes such as /play/:id serve index.html.
                served = "index.html"
                response = await super().get_response(served, scope)
            else:
                raise
        else:
            # GET / is a directory. Starlette serves dist/index.html with path ".".
            if path in (".", ""):
                served = "index.html"
        if served in ("index.html", "sw.js", "manifest.webmanifest"):
            response.headers["Cache-Control"] = "no-cache"
        elif served.startswith("assets/"):
            response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
        return response


_frontend_dist = Path(__file__).resolve().parents[2] / "frontend" / "dist"
if _frontend_dist.is_dir():
    app.mount("/", _SpaStaticFiles(directory=_frontend_dist, html=True), name="spa")
