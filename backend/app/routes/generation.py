"""Router de geração (SSE): /action, /continue, /retry.

Responde `text/event-stream`. Antes dos tokens podem sair `rules_roll`,
`action_check` e `adjudication` (decisão, desfecho, reações, iniciativa).
O stream encerra em `done` ou `error`. `done.adjudication.relation_changes`
só fecha turno do/say/continue; retry e story não reaplicam relação, visual
nem pós-turno. 409 (JSON Error) quando já há geração em andamento.
"""

from __future__ import annotations

import asyncio
import json
import queue as queue_mod
import threading
import time
from typing import Any, AsyncIterator, Callable, Iterator
from fastapi import APIRouter, Depends, Response
from fastapi.responses import JSONResponse, StreamingResponse

from app import auth, db, events, game_engine
from app.deps import get_http_transport
from app.errors import GameError
from app.schemas import ActionRequest

router = APIRouter()


_active_guard = threading.Lock()
_active: dict[str, threading.Event] = {}


def _begin(adventure_id: str) -> threading.Event:
    with _active_guard:
        if adventure_id in _active:
            raise GameError.conflict(
                "generation_in_progress", "a generation is already running for this adventure")
        cancel = threading.Event()
        _active[adventure_id] = cancel
        return cancel


def _end(adventure_id: str, cancel: threading.Event) -> None:
    with _active_guard:
        if _active.get(adventure_id) is cancel:
            _active.pop(adventure_id, None)


def _response(adventure_id: str, factory: Callable[[threading.Event], Iterator[dict]]):
    cancel = _begin(adventure_id)
    try:
        events = factory(cancel)
    except Exception:
        _end(adventure_id, cancel)
        raise
    return StreamingResponse(
        _sse(events, on_complete=lambda: _end(adventure_id, cancel)),
        media_type="text/event-stream")


def _require_character(conn, adv: dict, username: str) -> None:
    """Mesa com shares: quem age precisa de personagem, dono inclusive."""
    if len(db.adventure_participants(conn, adv)) > 1:
        if db.player_character(conn, adv["id"], username) is None:
            raise GameError(428, "character_required",
                            "crie seu personagem para jogar")


def _require_turn(adventure_id: str, info: auth.AuthInfo, *,
                  dm_narration: bool = False) -> None:
    """Quem não vê a aventura toma 404. Com turno ligado, só quem tem a vez age.

    Com dm_mode, o dono ignora a roda para narrar no meio dela. Continue, retry
    e cancel (`dm_narration`) ficam só com o dono: o share toma 403 mesmo na
    própria vez e com o turno desligado, antes de gerar ou cancelar. Mesa com
    shares exige personagem de quem age — inclusive o dono. See não passa por aqui.
    """
    with db.session() as conn:
        adv = db.get_adventure(conn, adventure_id)
        if not adv or not db.can_access_adventure(conn, adv, info.user):
            raise GameError.not_found(f"adventure {adventure_id} not found")
        if (dm_narration and adv.get("dm_mode")
                and not db.is_adventure_owner(adv, info.user)):
            raise GameError(403, "forbidden",
                            "só o mestre pode continuar, regenerar ou cancelar")
        dm_owner = bool(adv.get("dm_mode")) and db.is_adventure_owner(adv, info.user)
        if (adv.get("turn_mode") and not dm_owner
                and (adv.get("turn_user") or "") != info.user):
            raise GameError(403, "not_your_turn", f"vez de {adv.get('turn_user') or ''}")
        _require_character(conn, adv, info.user)

_SENTINEL = object()


async def _sse(events: Iterator[dict], on_complete: Callable[[], None] | None = None) -> AsyncIterator[str]:
    """Converte o generator síncrono do game engine em stream SSE assíncrono.

    A geração roda numa thread própria que sempre a consome até o fim — assim
    a ação é persistida e o lock da aventura é liberado mesmo se o cliente
    desconectar no meio do stream (fechar a aba, perder a rede). Com um
    iterator síncrono direto no StreamingResponse, a thread de streaming do
    servidor bloqueava no envio para o socket morto e o lock vazava,
    travando a aventura em `generation_in_progress` até reiniciar o serviço.
    """
    loop = asyncio.get_running_loop()
    q: queue_mod.Queue = queue_mod.Queue()

    def pump() -> None:
        try:
            for ev in events:
                q.put(ev)
        finally:
            if on_complete is not None:
                on_complete()
            q.put(_SENTINEL)

    pump_future = loop.run_in_executor(None, pump)
    try:
        while True:
            item = await loop.run_in_executor(None, q.get)
            if item is _SENTINEL:
                break
            yield f"event: {item['event']}\ndata: {json.dumps(item['data'], ensure_ascii=False)}\n\n"
    finally:
        if pump_future.done() and not pump_future.cancelled():
            pump_future.result()  # propaga falha inesperada da thread


@router.post("/{adventure_id}/action")
async def post_action(adventure_id: str, body: ActionRequest,
                      transport: Any = Depends(get_http_transport),
                      info: auth.AuthInfo = Depends(auth.require_auth),
                      ) -> Response:
    """Dono (e dm_mode desligado) streama. Jogador com dm_mode só enfileira."""
    with db.session() as conn:
        adv = db.get_adventure(conn, adventure_id)
        if not adv or not db.can_access_adventure(conn, adv, info.user):
            raise GameError.not_found(f"adventure {adventure_id} not found")
        if adv.get("dm_mode") and not db.is_adventure_owner(adv, info.user):
            _require_character(conn, adv, info.user)
            intent = db.insert_intent(
                conn, adventure_id, info.user, body.type, body.text)
            events.record(conn, adventure_id, "intent_pending", intent, actor=info.user)
            return JSONResponse(status_code=202, content={"intent": intent})
    _require_turn(adventure_id, info)
    return _response(adventure_id, lambda cancel: game_engine.handle_action(
        adventure_id, body.type, body.text, transport=transport,
        web_search=bool(body.web_search), user=info.user, cancel=cancel))


@router.post("/{adventure_id}/continue")
async def post_continue(adventure_id: str,
                        transport: Any = Depends(get_http_transport),
                        info: auth.AuthInfo = Depends(auth.require_auth)) -> StreamingResponse:
    _require_turn(adventure_id, info, dm_narration=True)
    return _response(adventure_id, lambda cancel: game_engine.handle_continue(
        adventure_id, transport=transport, cancel=cancel))


@router.post("/{adventure_id}/retry")
async def post_retry(adventure_id: str,
                     transport: Any = Depends(get_http_transport),
                     info: auth.AuthInfo = Depends(auth.require_auth)) -> StreamingResponse:
    _require_turn(adventure_id, info, dm_narration=True)
    return _response(adventure_id, lambda cancel: game_engine.handle_retry(
        adventure_id, transport=transport, cancel=cancel))


@router.post("/{adventure_id}/generation/cancel")
def post_cancel(adventure_id: str,
                info: auth.AuthInfo = Depends(auth.require_auth)) -> dict:
    _require_turn(adventure_id, info, dm_narration=True)
    with _active_guard:
        cancel = _active.get(adventure_id)
    if cancel is None or not game_engine.is_generation_active(adventure_id):
        raise GameError.conflict("no_generation_in_progress", "no generation is running")
    cancel.set()
    deadline = time.monotonic() + 5.0
    while game_engine.is_generation_active(adventure_id) and time.monotonic() < deadline:
        time.sleep(0.05)
    return {"status": "cancelling" if game_engine.is_generation_active(adventure_id) else "cancelled"}
