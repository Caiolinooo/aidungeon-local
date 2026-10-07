"""Router de geração (SSE): /action, /continue, /retry.

Responde `text/event-stream` com eventos `token` → `done` | `error`.
409 (JSON Error) quando já há geração em andamento para a aventura.
"""

from __future__ import annotations

import json
from typing import Any, Iterator

from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse

from app import game_engine
from app.deps import get_http_transport
from app.schemas import ActionRequest

router = APIRouter()


def _sse(events: Iterator[dict]) -> Iterator[str]:
    for ev in events:
        yield f"event: {ev['event']}\ndata: {json.dumps(ev['data'], ensure_ascii=False)}\n\n"


@router.post("/{adventure_id}/action")
def post_action(adventure_id: str, body: ActionRequest,
                transport: Any = Depends(get_http_transport)) -> StreamingResponse:
    events = game_engine.handle_action(adventure_id, body.type, body.text,
                                       transport=transport)
    return StreamingResponse(_sse(events), media_type="text/event-stream")


@router.post("/{adventure_id}/continue")
def post_continue(adventure_id: str,
                  transport: Any = Depends(get_http_transport)) -> StreamingResponse:
    events = game_engine.handle_continue(adventure_id, transport=transport)
    return StreamingResponse(_sse(events), media_type="text/event-stream")


@router.post("/{adventure_id}/retry")
def post_retry(adventure_id: str,
               transport: Any = Depends(get_http_transport)) -> StreamingResponse:
    events = game_engine.handle_retry(adventure_id, transport=transport)
    return StreamingResponse(_sse(events), media_type="text/event-stream")
