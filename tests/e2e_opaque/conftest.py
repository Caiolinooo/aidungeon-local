"""Shared fixtures and opaque test infrastructure for 4-Tier E2E Test Suite."""

from __future__ import annotations

import base64
import io
import json
import re
import sys
import zipfile
from pathlib import Path
from typing import Any, Callable, Iterator
from unittest.mock import MagicMock

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

BACKEND = ROOT / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

import httpx
import pytest

from app import db, deps, llm_client, settings_store
from conftest import sse_body

# 1x1 transparent PNG
PNG_B64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8"
    "z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)
PNG_BYTES = base64.b64decode(PNG_B64)

# Fake MP3 stream bytes
MP3_BYTES = b"ID3\x04\x00\x00\x00\x00\x00\x21fake-mp3-audio-bytes"


class OpaqueMockTransport:
    """Configurable unified mock transport simulating all VM services."""

    def __init__(
        self,
        *,
        chat_tokens: tuple[str, ...] = ("The", " dungeon", " door", " opens", " slowly."),
        non_stream_content: str = '{"choices": [{"type": "do", "text": "inspect the chest"}]}',
        stt_text: str = "I search the room for hidden doors",
        fail: str | None = None,
        chat_handler: Callable[[dict], str | tuple[str, ...]] | None = None,
    ):
        self.chat_tokens = chat_tokens
        self.non_stream_content = non_stream_content
        self.stt_text = stt_text
        self.fail = fail
        self.chat_handler = chat_handler
        self.captured_requests: list[dict[str, Any]] = []

    def handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path

        if self.fail == "connect":
            raise httpx.ConnectError("mock: connection refused")

        if path.endswith("/models"):
            if self.fail == "auth":
                return httpx.Response(401, json={"error": "unauthorized"})
            return httpx.Response(200, json={
                "object": "list",
                "data": [{"id": "mock-narrator-qwen", "object": "model"}],
            })

        if path.endswith("/chat/completions"):
            if self.fail == "auth":
                return httpx.Response(401, json={"error": "unauthorized"})
            if self.fail == "http500":
                return httpx.Response(500, text="Internal Server Error")

            payload = json.loads(request.content.decode("utf-8"))
            self.captured_requests.append({
                "path": path,
                "method": request.method,
                "payload": payload,
            })

            is_stream = bool(payload.get("stream", False))

            if self.chat_handler:
                result = self.chat_handler(payload)
                if is_stream:
                    tokens = result if isinstance(result, tuple) else (str(result),)
                    return httpx.Response(
                        200,
                        headers={"content-type": "text/event-stream"},
                        text=sse_body(tokens),
                    )
                else:
                    content_str = result if isinstance(result, str) else "".join(result)
                    return httpx.Response(200, json={
                        "choices": [{"message": {"content": content_str}}],
                    })

            if is_stream:
                return httpx.Response(
                    200,
                    headers={"content-type": "text/event-stream"},
                    text=sse_body(self.chat_tokens),
                )
            else:
                return httpx.Response(200, json={
                    "choices": [{"message": {"content": self.non_stream_content}}],
                })

        if path.endswith("/sdapi/v1/txt2img") or path.endswith("/sdapi/v1/img2img"):
            if self.fail == "image_down":
                raise httpx.ConnectError("mock: image service unreachable")
            payload = {}
            if request.content:
                try:
                    payload = json.loads(request.content.decode("utf-8"))
                except Exception:
                    pass
            self.captured_requests.append({
                "path": path,
                "method": request.method,
                "payload": payload,
            })
            return httpx.Response(200, json={"images": [PNG_B64], "parameters": {}})

        if path.endswith("/audio/speech"):
            if self.fail == "voice_down":
                raise httpx.ConnectError("mock: voice service unreachable")
            payload = json.loads(request.content.decode("utf-8"))
            self.captured_requests.append({
                "path": path,
                "method": request.method,
                "payload": payload,
            })
            return httpx.Response(200, content=MP3_BYTES, headers={"content-type": "audio/mpeg"})

        if path.endswith("/audio/transcriptions"):
            if self.fail == "voice_down":
                raise httpx.ConnectError("mock: voice service unreachable")
            self.captured_requests.append({
                "path": path,
                "method": request.method,
                "content_len": len(request.content),
            })
            return httpx.Response(200, json={"text": self.stt_text})

        if path.endswith("/embeddings"):
            return httpx.Response(200, json={
                "data": [{"embedding": [0.01 * i for i in range(128)]}],
            })

        return httpx.Response(404, json={"error": f"no mock route: {path}"})


def make_mock_transport(**kwargs) -> tuple[httpx.MockTransport, OpaqueMockTransport]:
    mock = OpaqueMockTransport(**kwargs)
    return httpx.MockTransport(mock.handle), mock


def parse_sse_events(raw_text: str) -> list[dict[str, Any]]:
    """Parse SSE response string into structured event objects."""
    events: list[dict[str, Any]] = []
    current_event: str | None = None
    current_data: str | None = None

    for line in raw_text.splitlines():
        line = line.strip()
        if not line:
            if current_event or current_data is not None:
                parsed_data = current_data
                if current_data:
                    try:
                        parsed_data = json.loads(current_data)
                    except json.JSONDecodeError:
                        pass
                events.append({"event": current_event or "message", "data": parsed_data})
                current_event = None
                current_data = None
            continue

        if line.startswith("event:"):
            current_event = line[len("event:"):].strip()
        elif line.startswith("data:"):
            data_part = line[len("data:"):].strip()
            current_data = data_part if current_data is None else current_data + "\n" + data_part

    if current_event or current_data is not None:
        parsed_data = current_data
        if current_data:
            try:
                parsed_data = json.loads(current_data)
            except json.JSONDecodeError:
                pass
        events.append({"event": current_event or "message", "data": parsed_data})

    return events


def build_synthetic_backup_zip(
    title: str = "Faerûn Tale",
    description: str = "A heroic journey",
    actions: list[dict[str, Any]] | None = None,
    story_cards: list[dict[str, Any]] | None = None,
    memories: list[str] | None = None,
) -> bytes:
    """Build in-memory valid AI Dungeon backup ZIP byte stream."""
    if actions is None:
        actions = [
            {"id": "act-0", "type": "start", "text": "You stand before the ancient gate."},
            {"id": "act-1", "type": "continue", "text": " The wind howls across the desolate plain."},
        ]

    metadata = {
        "adventure": {
            "title": title,
            "description": description,
            "tags": ["fantasy", "adventure"],
            "thirdPerson": False,
            "memory": "Remember the king is secretly ill.",
            "authorsNote": "Write in dark fantasy style.",
        },
        "state": {
            "instructions": {"scenario": "A perilous adventure in the Forgotten Realms."},
            "storySummary": "The hero arrived at the sealed gate.",
            "storyCards": [
                {
                    "title": c.get("title", "Gatekeeper"),
                    "type": c.get("type", "character"),
                    "keys": c.get("keys", "gate, keeper, guard"),
                    "value": c.get("value", "An armored sentinel guarding the portal."),
                    "isPinned": c.get("isPinned", False),
                    "useForCharacterCreation": False,
                }
                for c in (story_cards or [])
            ],
            "memories": [
                {"text": m, "actionIds": ["act-0"]} for m in (memories or ["The party crossed the river."])
            ],
        },
    }

    actions_payload = {
        "partNumber": 1,
        "actions": actions,
    }

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("metadata.json", json.dumps(metadata, indent=2))
        zf.writestr("actions-001.json", json.dumps(actions_payload, indent=2))
    return buf.getvalue()


@pytest.fixture
def mock_transport():
    """Default mock transport providing deterministic responses for all VM endpoints."""
    transport, tracker = make_mock_transport()
    return transport, tracker


@pytest.fixture
def e2e_client(client, mock_transport, monkeypatch):
    """Authenticated TestClient wired with mock transport and isolated summarizer."""
    transport, tracker = mock_transport
    client.app.dependency_overrides[deps.get_http_transport] = lambda: transport

    # Keep summarizer from doing unmocked network calls during routine turns
    def fake_maybe_update(adventure_id, *args, **kwargs):
        pass

    monkeypatch.setattr("memory.summarizer.maybe_update", fake_maybe_update)

    # Attach tracker to client for easy assertion in tests
    client.mock_tracker = tracker
    try:
        yield client
    finally:
        client.app.dependency_overrides.pop(deps.get_http_transport, None)


@pytest.fixture
def raw_client(tmp_db):
    """Client without default Authorization header to test unauthenticated flows."""
    from fastapi.testclient import TestClient

    from app.main import app
    with TestClient(app) as c:
        yield c
