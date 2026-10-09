"""Testes das rotas de geração (SSE): desconexão do cliente não pode vazar
o lock da aventura nem perder a ação gerada (bug: iterator síncrono no
StreamingResponse bloqueava no socket morto e travava a aventura em
generation_in_progress até reiniciar o serviço).
"""

from __future__ import annotations

import asyncio

from app.routes.generation import _sse


def test_sse_consumer_abandon_still_drains_events():
    """Se o cliente some no meio do stream, a pump continua consumindo o
    generator até o fim — o `finally` dele (persistência + release do lock)
    sempre roda."""
    finished = []

    def events():
        try:
            yield {"event": "token", "data": {"token": "a"}}
            yield {"event": "token", "data": {"token": "b"}}
            yield {"event": "done", "data": {"action": {}}}
        finally:
            finished.append(True)

    async def run():
        agen = _sse(events())
        first = await agen.__anext__()
        assert first.startswith("event: token\n")
        await agen.aclose()  # simula o cliente desconectando
        for _ in range(200):
            if finished:
                break
            await asyncio.sleep(0.005)
        assert finished, "pump não drenou o generator após abandono do consumidor"

    asyncio.run(run())


def test_sse_streams_all_events_in_order():
    def events():
        yield {"event": "token", "data": {"token": "a"}}
        yield {"event": "token", "data": {"token": "b"}}
        yield {"event": "done", "data": {"action": {"text": "ab"}}}

    async def run():
        out = []
        async for chunk in _sse(events()):
            out.append(chunk)
        return out

    chunks = asyncio.run(run())
    assert chunks[0] == 'event: token\ndata: {"token": "a"}\n\n'
    assert chunks[1] == 'event: token\ndata: {"token": "b"}\n\n'
    assert chunks[2].startswith("event: done\n")
