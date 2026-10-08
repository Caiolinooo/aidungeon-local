"""Router /api/settings/llm — configuração do servidor LLM.

GET: config efetiva (arquivo + overrides), api_key SEMPRE mascarada.
PUT: persiste overrides na tabela settings; mudança de base_url/model_alias
     invalida o cache de descoberta e refaz GET {base_url}/models (best-effort).
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends

from app import db, llm_client, settings_store
from app.deps import get_http_transport
from app.schemas import LlmSettingsUpdate

router = APIRouter()


def _settings_response(conn, transport: Any = None) -> dict:
    cfg = settings_store.effective_llm_settings(conn)
    return {
        "provider": cfg["provider"],
        "base_url": cfg["base_url"],
        "api_key": settings_store.mask_api_key(cfg["api_key"]),
        "model_alias": cfg["model_alias"],
        "model_discovered": llm_client.cached_discovery(cfg["base_url"], cfg["model_alias"]),
        "context_size": cfg["context_size"],
        "max_tokens": cfg["max_tokens"],
        "streaming": cfg["streaming"],
        "disable_thinking": cfg["disable_thinking"],
        "timeout_seconds": cfg["timeout_seconds"],
        "temperature": cfg["temperature"],
        "top_p": cfg["top_p"],
        "repetition_penalty": cfg["repetition_penalty"],
    }


@router.get("/settings/llm")
def get_llm_settings() -> dict:
    with db.session() as conn:
        return _settings_response(conn)


@router.put("/settings/llm")
def put_llm_settings(body: LlmSettingsUpdate,
                     transport: Any = Depends(get_http_transport)) -> dict:
    update = body.model_dump(exclude_unset=True)
    with db.session() as conn:
        changed = settings_store.apply_llm_update(conn, update)
        if changed & {"base_url", "model_alias"}:
            # refaz a descoberta de modelo contra o novo endpoint
            llm_client.invalidate_discovery_cache()
            cfg = settings_store.effective_llm_settings(conn)
            try:
                if (cfg.get("model_alias") or "auto") == "auto":
                    llm_client.resolve_model(cfg, transport)
            except llm_client.LlmError:
                pass  # configuração vale mesmo com o LLM fora do ar
        return _settings_response(conn, transport)
