"""Router /api/assistant — Story Studio, refinamento de campos, cards e Web Search."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Request

from app import access, assistant, db, llm_client, settings_store, web_search
from app.deps import get_http_transport
from app.errors import GameError
from app.schemas import (
    AssistantCardRequest,
    AssistantFieldRequest,
    AssistantGenerateRequest,
    AssistantScenarioRequest,
    AssistantSyncPlotRequest,
    WebSearchRequest,
)

router = APIRouter()


def _llm_cfg() -> dict:
    with db.session() as conn:
        return settings_store.effective_llm_settings(conn)


def _assistant_file_cfg() -> dict:
    return settings_store.load_file_settings().get("assistant", {})


@router.post("/assistant/scenario")
def post_assistant_scenario(body: AssistantScenarioRequest,
                            transport: Any = Depends(get_http_transport)) -> dict:
    """brief (+ parâmetros opcionais do Story Studio) → ScenarioDraft completo."""
    try:
        return assistant.generate_scenario_draft(
            _llm_cfg(),
            _assistant_file_cfg(),
            body.brief,
            body.language,
            transport=transport,
            genre=body.genre,
            tone=body.tone,
            pov=body.pov,
            protagonist_name=body.protagonist_name,
            protagonist_role=body.protagonist_role,
            protagonist_appearance=body.protagonist_appearance,
            image_style=body.image_style,
            card_count=body.card_count,
            web_search_mode=body.web_search_mode,
        )
    except llm_client.LlmError as exc:
        raise GameError(503, exc.code, exc.message) from exc


@router.post("/assistant/generate")
def post_assistant_generate(body: AssistantGenerateRequest,
                            transport: Any = Depends(get_http_transport)) -> dict:
    """Rascunho completo. Com deep_characters, cada personagem ganha a ficha viva."""
    llm_cfg = _llm_cfg()
    try:
        draft = assistant.generate_scenario_draft(
            llm_cfg,
            _assistant_file_cfg(),
            body.brief,
            body.language,
            transport=transport,
            genre=body.genre or None,
            tone=body.tone or None,
            pov=body.pov,
            protagonist_name=body.protagonist_name or None,
            protagonist_role=body.protagonist_role or None,
            protagonist_appearance=body.protagonist_appearance or None,
            image_style=body.image_style or None,
            card_count=body.card_count,
            web_search_mode=body.web_search_mode,
            current=body.current,
        )
        if body.deep_characters:
            draft = assistant.deepen_characters(llm_cfg, draft, transport)
        return draft
    except llm_client.LlmError as exc:
        raise GameError(503, exc.code, exc.message) from exc


@router.post("/assistant/field")
def post_assistant_field(body: AssistantFieldRequest,
                         transport: Any = Depends(get_http_transport)) -> dict:
    """Regenera ou refina UM campo do rascunho (com instrução e web search opcionais)."""
    try:
        return assistant.regenerate_field(
            _llm_cfg(),
            _assistant_file_cfg(),
            body.field,
            brief=body.brief,
            current=body.current,
            draft=body.draft,
            instruction=body.instruction,
            web_search=body.web_search,
            language=body.language,
            transport=transport,
        )
    except llm_client.LlmError as exc:
        raise GameError(503, exc.code, exc.message) from exc


@router.post("/assistant/card")
def post_assistant_card(body: AssistantCardRequest, request: Request,
                        transport: Any = Depends(get_http_transport)) -> dict:
    """Gera ou enriquece 1 Story Card individual (com suporte a Web Search)."""
    story_ctx = ""
    if body.adventure_id:
        with db.session() as conn:
            viewer = access.user_from_request(request) or ""
            adv = (db.get_adventure(conn, body.adventure_id)
                   if access.adventure_visible(conn, viewer, body.adventure_id) else None)
            if adv:
                actions = db.list_actions(conn, body.adventure_id)
                recent = "\n".join(a["text"] for a in actions[-4:] if a.get("text"))
                story_ctx = (
                    f"Title: {adv['title']}\n"
                    f"Plot: {adv['plot_essentials']}\n"
                    f"Recent: {recent}"
                )
    elif isinstance(body.draft, dict):
        story_ctx = (
            f"Title: {body.draft.get('title') or ''}\n"
            f"Plot: {body.draft.get('plot_essentials') or ''}\n"
            f"Opening: {body.draft.get('opening_text') or ''}"
        )
    try:
        return assistant.generate_single_card(
            _llm_cfg(),
            concept=body.concept,
            card_type=body.card_type,
            story_context=story_ctx,
            current_card=body.current_card,
            instruction=body.instruction,
            web_search=body.web_search,
            language=body.language,
            transport=transport,
        )
    except llm_client.LlmError as exc:
        raise GameError(503, exc.code, exc.message) from exc


@router.post("/assistant/sync-plot")
def post_assistant_sync_plot(body: AssistantSyncPlotRequest, request: Request,
                             transport: Any = Depends(get_http_transport)) -> dict:
    """Analisa a história recente da aventura e sugere atualização de Plot Essentials / Author's Note."""
    with db.session() as conn:
        viewer = access.user_from_request(request) or ""
        adv = db.get_adventure(conn, body.adventure_id)
        if not adv or not access.adventure_visible(conn, viewer, body.adventure_id):
            raise GameError.not_found(f"adventure {body.adventure_id} not found")
        actions = db.list_actions(conn, body.adventure_id)
        llm_cfg = settings_store.effective_llm_settings(conn)
    try:
        return assistant.sync_adventure_plot(
            llm_cfg,
            adventure=adv,
            recent_actions=actions,
            instruction=body.instruction,
            language=body.language,
            transport=transport,
        )
    except llm_client.LlmError as exc:
        raise GameError(503, exc.code, exc.message) from exc


@router.post("/assistant/web-search")
def post_assistant_web_search(body: WebSearchRequest,
                              transport: Any = Depends(get_http_transport)) -> dict:
    """Pesquisa direta na internet (Wikipedia + DuckDuckGo) para consulta na UI."""
    results = web_search.search_web(
        body.query,
        language=body.language,
        max_results=body.max_results,
        transport=transport,
    )
    return {"query": body.query, "results": results}
