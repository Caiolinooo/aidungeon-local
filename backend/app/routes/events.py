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

from app import access, auth, consequences, db, events, llm_scheduler
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


_INTENT_TYPES = frozenset({"intent_pending", "intent_approved", "intent_rejected"})


def _intent_visible(event_type: str, data: dict, *, user: str, owner: bool) -> bool:
    """Intenção de outro jogador não sai no stream nem no histórico do share."""
    if owner or event_type not in _INTENT_TYPES:
        return True
    return (data.get("username") or "") == user


def _sse_for(event, hide_secrets: bool, *, user: str) -> str | None:
    """O barramento é único. O share recebe a cópia sem private_facts.

    Intenção alheia some por completo: o dono lista a fila, o jogador só a sua.
    """
    data = event.data if isinstance(event.data, dict) else {}
    if not _intent_visible(event.type, data, user=user, owner=not hide_secrets):
        return None
    if not hide_secrets:
        return event.sse()
    payload = json.dumps(
        consequences.without_private_facts(data),
        ensure_ascii=False, separators=(",", ":"))
    cursor = f"id: {event.id}\n" if event.durable else ""
    return f"{cursor}event: {event.type}\ndata: {payload}\n\n"


def _last_event_id(request: Request, query_value: int | None) -> int | None:
    raw = request.headers.get("Last-Event-ID")
    if raw is not None:
        try:
            return int(raw.strip())
        except ValueError:
            return None
    return query_value


@router.get("/{adventure_id}/events/history")
def adventure_event_history(adventure_id: str, request: Request,
                            token: str | None = Query(None),
                            after: int = Query(0, ge=0),
                            limit: int = Query(100, ge=1, le=500)) -> dict:
    """Página ascendente e estável do log; `after` é um event_id exclusivo."""
    user = _user(request, token)
    with db.session() as conn:
        adv = db.get_adventure(conn, adventure_id)
        if adv is None or not access.adventure_visible(conn, user, adventure_id):
            raise GameError.not_found(f"adventure {adventure_id} not found")
        page, has_more = events.list_history(
            conn, adventure_id, after=after, limit=limit)
        hide_secrets = not db.is_adventure_owner(adv, user)
        raw_page = page
        visible = []
        for item in raw_page:
            payload = item.get("payload") if isinstance(item.get("payload"), dict) else {}
            if not _intent_visible(item.get("type") or "", payload,
                                   user=user, owner=not hide_secrets):
                continue
            if hide_secrets:
                item = {**item, "payload": consequences.without_private_facts(payload)}
            visible.append(item)
        page = visible
        bounds = conn.execute(
            "SELECT MIN(event_id) AS first_id, MAX(event_id) AS last_id"
            " FROM world_events WHERE adventure_id=?", (adventure_id,),
        ).fetchone()
    latest = int(bounds["last_id"] or 0)
    first = bounds["first_id"]
    reset_required = after > latest or (first is not None and after < int(first) - 1)
    return {
        "events": page,
        "next_cursor": raw_page[-1]["event_id"] if raw_page else after,
        "has_more": has_more,
        "latest_event_id": latest,
        "reset_required": reset_required,
    }


@router.get("/{adventure_id}/events")
def adventure_events(adventure_id: str, request: Request,
                     token: str | None = Query(None),
                     last_event_id: int | None = Query(None)) -> StreamingResponse:
    user = _user(request, token)
    cursor = _last_event_id(request, last_event_id)
    snapshot = None
    with db.session() as conn:
        adv = db.get_adventure(conn, adventure_id)
        if adv is None or not access.adventure_visible(conn, user, adventure_id):
            raise GameError.not_found(f"adventure {adventure_id} not found")
        hide_secrets = not db.is_adventure_owner(adv, user)
        if cursor is None:
            row = conn.execute(
                "SELECT MAX(event_id) AS last_id FROM world_events WHERE adventure_id=?",
                (adventure_id,),
            ).fetchone()
            latest = int(row["last_id"] or 0)
            backlog = []
            reset_required = False
        else:
            backlog, reset_required, latest = events.replay(
                conn, adventure_id, cursor)
            if reset_required:
                snapshot = db.adventure_state(conn, adventure_id, viewer=user)
    subscription = events.subscribe(
        adventure_id, latest if reset_required else cursor,
        backlog=backlog, since=latest if cursor is None else None,
    )
    breaker = llm_scheduler.snapshot()["breaker"]

    async def stream():
        try:
            if reset_required:
                data = json.dumps({
                    "reason": "cursor_expired",
                    "cursor": cursor,
                    "latest_event_id": latest,
                    "snapshot": snapshot,
                }, ensure_ascii=False, separators=(",", ":"))
                yield f"event: reset_required\ndata: {data}\n\n"
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
                    frame = _sse_for(event, hide_secrets, user=user)
                    if frame:
                        yield frame
                elif subscription.closed:
                    return
                else:
                    yield ": ping\n\n"
        finally:
            subscription.close()

    return StreamingResponse(stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache",
                                      "X-Accel-Buffering": "no"})
