"""Router GET /api/adventures/{id}/events — eventos da aventura em SSE (C2).

Fica fora do `_protected` do main: o `EventSource` do navegador não manda
header, então a rota aceita o Bearer ou `?token=` e faz ela mesma a checagem
de dono (história alheia responde 404, como no middleware). Reconexão com
`Last-Event-ID` ou `?last_event_id=`; `: ping` a cada `PING_S`.
"""

from __future__ import annotations

import json
import logging
import re

from fastapi import APIRouter, Query, Request
from fastapi.responses import StreamingResponse

from app import access, auth, db, events, llm_scheduler
from app.errors import GameError

router = APIRouter()

PING_S = 15.0

_TOKEN_IN_QUERY = re.compile(r"([?&]token=)[^&\s]+")


class _RedactToken(logging.Filter):
    """O `?token=` do EventSource não pode ir para o access log do uvicorn."""

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.args, tuple) and any(
                isinstance(arg, str) and "token=" in arg for arg in record.args):
            record.args = tuple(_TOKEN_IN_QUERY.sub(r"\1<redacted>", arg)
                                if isinstance(arg, str) else arg for arg in record.args)
        return True


logging.getLogger("uvicorn.access").addFilter(_RedactToken())


def _user(request: Request, token: str | None) -> str:
    cfg = auth.auth_config()
    header = request.headers.get("Authorization", "")
    scheme, _, bearer = header.partition(" ")
    candidate = bearer.strip() if scheme.lower() == "bearer" else ""
    candidate = candidate or (token or "").strip()
    if cfg is None or not candidate:
        raise GameError(401, "unauthorized", "missing or invalid bearer token")
    ok, user, _exp = auth.verify_token(candidate, cfg["secret"])
    if not ok or not user:
        raise GameError(401, "unauthorized", "missing or invalid bearer token")
    return user


def _last_event_id(request: Request, query_value: int | None) -> int | None:
    raw = request.headers.get("Last-Event-ID")
    if raw is not None:
        try:
            return int(raw.strip())
        except ValueError:
            return None
    return query_value


@router.get("/{adventure_id}/events")
def adventure_events(adventure_id: str, request: Request,
                     token: str | None = Query(None),
                     last_event_id: int | None = Query(None)) -> StreamingResponse:
    user = _user(request, token)
    with db.session() as conn:
        if db.get_adventure(conn, adventure_id) is None \
                or not access.adventure_visible(conn, user, adventure_id):
            raise GameError.not_found(f"adventure {adventure_id} not found")
    subscription = events.subscribe(adventure_id, _last_event_id(request, last_event_id))
    breaker = llm_scheduler.snapshot()["breaker"]

    async def stream():
        try:
            # `ready` fixa o Last-Event-ID do navegador desde já: cair antes do
            # primeiro evento não pode perder o que sair até a reconexão.
            yield f"retry: 3000\nid: {subscription.since}\nevent: ready\ndata: {{}}\n\n"
            if breaker["state"] == "open":
                data = json.dumps({"reason": f"{breaker['failures']} consecutive llm timeouts",
                                   "until": breaker["until"]})
                yield f"event: llm_degraded\ndata: {data}\n\n"
            while True:
                event = await subscription.aget(PING_S)
                if event is not None:
                    yield event.sse()
                elif subscription.closed:
                    return
                else:
                    yield ": ping\n\n"
        finally:
            subscription.close()

    return StreamingResponse(stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache",
                                      "X-Accel-Buffering": "no"})
