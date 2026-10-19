"""Router /api/adventures/{id}/choices — sugestões de próxima ação (v2).

As opções (type do/say/story, texto ≤ 120 chars, em inglês) são pré-geradas
logo depois do `done` por `choices_service` (C7). A rota devolve o cache da
última ação ou espera no máximo `choices.wait_s` pela geração em curso, e
falha limpa (503) em vez de travar a aventura; nunca 409. Desabilitado
(settings.choices.enabled=false) → 200 {"choices": []}.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends

from app import choices_service, db
from app.deps import get_http_transport
from app.errors import GameError

router = APIRouter()


@router.post("/{adventure_id}/choices")
def post_choices(adventure_id: str,
                 transport: Any = Depends(get_http_transport)) -> dict:
    with db.session() as conn:
        if not db.get_adventure(conn, adventure_id):
            raise GameError.not_found(f"adventure {adventure_id} not found")
    try:
        choices = choices_service.get(adventure_id, transport=transport)
    except choices_service.ChoicesPending as exc:
        raise GameError(503, "llm_timeout", str(exc)) from exc
    except choices_service.ChoicesFailed as exc:
        raise GameError(503, exc.code, exc.message) from exc
    return {"choices": choices}
