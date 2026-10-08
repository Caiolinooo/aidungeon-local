"""Router /api/assistant — rascunhos de cenário via LLM (v2, não persiste)."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends

from app import assistant, db, llm_client, settings_store
from app.deps import get_http_transport
from app.errors import GameError
from app.schemas import AssistantFieldRequest, AssistantScenarioRequest

router = APIRouter()


def _llm_cfg() -> dict:
    with db.session() as conn:
        return settings_store.effective_llm_settings(conn)


def _assistant_file_cfg() -> dict:
    return settings_store.load_file_settings().get("assistant", {})


@router.post("/assistant/scenario")
def post_assistant_scenario(body: AssistantScenarioRequest,
                            transport: Any = Depends(get_http_transport)) -> dict:
    """brief → ScenarioDraft completo (não persiste — revisar e POST /api/scenarios)."""
    try:
        return assistant.generate_scenario_draft(
            _llm_cfg(), _assistant_file_cfg(), body.brief, body.language,
            transport=transport)
    except llm_client.LlmError as exc:
        raise GameError(503, exc.code, exc.message) from exc


@router.post("/assistant/field")
def post_assistant_field(body: AssistantFieldRequest,
                         transport: Any = Depends(get_http_transport)) -> dict:
    """Regenera UM campo do rascunho (coerência cruzada via brief/current/draft)."""
    try:
        return assistant.regenerate_field(
            _llm_cfg(), _assistant_file_cfg(), body.field,
            brief=body.brief, current=body.current, draft=body.draft,
            transport=transport)
    except llm_client.LlmError as exc:
        raise GameError(503, exc.code, exc.message) from exc
