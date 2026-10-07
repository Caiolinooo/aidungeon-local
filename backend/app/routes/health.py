"""Router /api/health/llm — sonda o servidor LLM em tempo real (sempre 200)."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends

from app import db, llm_client, settings_store
from app.deps import get_http_transport

router = APIRouter()


@router.get("/health/llm")
def health_llm(transport: Any = Depends(get_http_transport)) -> dict:
    with db.session() as conn:
        cfg = settings_store.effective_llm_settings(conn)
    return llm_client.health(cfg, transport=transport)
