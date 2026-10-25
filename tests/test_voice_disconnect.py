"""Cancelamento das chamadas TTS/STT/clone quando o cliente ASGI desconecta."""

from __future__ import annotations

import asyncio
import base64
import io

import httpx
import pytest
from fastapi import UploadFile
from starlette.datastructures import Headers
from starlette.requests import ClientDisconnect

from app import auth
from app.routes import voice as voice_routes
from app.schemas import TtsRequest

AUTH = auth.AuthInfo(user="tester", exp=0)


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
        self.paths: list[str] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.paths.append(request.url.path)
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


def _clone_upload() -> UploadFile:
    return UploadFile(
        file=io.BytesIO(b"RIFFfake-wav"),
        filename="voz.wav",
        headers=Headers({"content-type": "audio/wav"}),
    )


@pytest.mark.parametrize(
    ("ref_text", "provider_path"),
    [("", "/audio/transcriptions"), ("Texto já transcrito.", "/audio/voices")],
)
def test_clone_disconnect_cancels_provider_request(tmp_db, ref_text, provider_path):
    async def scenario() -> None:
        transport = BlockingTransport()
        with pytest.raises(ClientDisconnect):
            await voice_routes.post_clone(
                DisconnectingRequest(),
                file=_clone_upload(),
                name="Clara",
                ref_text=ref_text,
                info=AUTH,
                transport=transport,
            )
        assert transport.started.is_set()
        assert transport.cancelled.is_set()
        assert len(transport.paths) == 1
        assert transport.paths[0].endswith(provider_path)

    asyncio.run(scenario())


class ConnectedRequest:
    headers: dict[str, str] = {}

    async def is_disconnected(self) -> bool:
        return False


class SlowCloneTransport(httpx.AsyncBaseTransport):
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.started.set()
        await self.release.wait()
        return httpx.Response(200, json={
            "seconds": 2.5,
            "rvq_b64": base64.b64encode(b"rvq-bytes").decode("ascii"),
        })


def test_clone_slow_provider_does_not_block_event_loop(tmp_db):
    async def scenario() -> None:
        transport = SlowCloneTransport()
        operation = asyncio.create_task(voice_routes.post_clone(
            ConnectedRequest(),
            file=_clone_upload(),
            name="Clara",
            ref_text="Texto já transcrito.",
            info=AUTH,
            transport=transport,
        ))
        try:
            await asyncio.wait_for(transport.started.wait(), timeout=1)
            # Um await curto representa health/SSE: precisa avançar enquanto o clone espera.
            await asyncio.wait_for(asyncio.sleep(0.01), timeout=0.1)
            assert not operation.done()
            transport.release.set()
            result = await asyncio.wait_for(operation, timeout=1)
            assert result["name"] == "Clara"
        finally:
            transport.release.set()
            if not operation.done():
                operation.cancel()
            await asyncio.gather(operation, return_exceptions=True)

    asyncio.run(scenario())
