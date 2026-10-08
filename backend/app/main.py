"""App FastAPI — AI Dungeon Local (contrato: docs/openapi.yaml).

- CORS liberado para localhost (qualquer porta).
- Erros no schema `Error`: {"error": {"code", "message"}}.
- Banco SQLite inicializado no startup (backend/schema.sql).

Rodar a partir de backend/:  uvicorn app.main:app --port 8000
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

from app import db
from app.errors import GameError
from app.routes import (actions, adventures, cards, generation, health,
                        import_export, memory, scenarios, settings)

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s %(message)s")


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db()
    yield


app = FastAPI(title="AI Dungeon Local — API", version="0.1.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=r"^https?://(localhost|127\.0\.0\.1)(:\d+)?$",
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
app.include_router(scenarios.router, prefix="/api/scenarios", tags=["scenarios"])
app.include_router(adventures.router, prefix="/api/adventures", tags=["adventures"])
app.include_router(generation.router, prefix="/api/adventures", tags=["generation"])
app.include_router(actions.router, prefix="/api/actions", tags=["actions"])
app.include_router(cards.router, prefix="/api/adventures", tags=["cards"])
app.include_router(memory.router, prefix="/api/adventures", tags=["memory"])
app.include_router(import_export.router, prefix="/api", tags=["import-export"])
app.include_router(settings.router, prefix="/api", tags=["settings"])
app.include_router(health.router, prefix="/api", tags=["health"])


# ------------------------------------------------------------------ frontend
# Serve o build do frontend (frontend/dist) na raiz, com fallback p/ index.html
# (rotas client-side como /play/:id). Montado DEPOIS dos routers: /api/* mantém
# precedência; 404s de /api/* não caem no fallback SPA.
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
