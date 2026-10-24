"""Cancelamento das chamadas TTS/STT quando o cliente ASGI desconecta."""

from __future__ import annotations

import asyncio
import io

import httpx
import pytest
from fastapi import UploadFile
from starlette.datastructures import Headers
from starlette.requests import ClientDisconnect

from app.routes import voice as voice_routes
from app.schemas import TtsRequest


class DisconnectingRequest:
    def __init__(self) -> None:
        self.headers: dict[str, str] = {}
        self._checks = 0

    async def is_disconnected(self) -> bool:
        self._checks += 1
        return self._checks >= 2


class BlockingTransport(httpx.AsyncBaseTransport):
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.cancelled = asyncio.Event()

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.cancelled.set()
            raise
        raise AssertionError("provider request should remain pending until cancellation")


def test_tts_disconnect_cancels_provider_request(tmp_db):
    async def scenario() -> None:
        transport = BlockingTransport()
        with pytest.raises(ClientDisconnect):
            await voice_routes.post_tts(
                DisconnectingRequest(),
                TtsRequest(text="A porta range."),
                transport=transport,
            )
        assert transport.started.is_set()
        assert transport.cancelled.is_set()

    asyncio.run(scenario())


def test_stt_disconnect_cancels_provider_request(tmp_db):
    async def scenario() -> None:
        transport = BlockingTransport()
        upload = UploadFile(
            file=io.BytesIO(b"RIFFfake-wav"),
            filename="voz.wav",
            headers=Headers({"content-type": "audio/wav"}),
        )
        with pytest.raises(ClientDisconnect):
            await voice_routes.post_stt(
                DisconnectingRequest(),
                file=upload,
                language="pt",
                transport=transport,
            )
        assert transport.started.is_set()
        assert transport.cancelled.is_set()

    asyncio.run(scenario())
