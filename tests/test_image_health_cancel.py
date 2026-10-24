"""Health não gerativo, seed/cache e cancelamento cooperativo de imagens."""

from __future__ import annotations

import json
import threading

import httpx

from app import auth as auth_core
from app import db, deps, events, image_client, jobs
from conftest import TEST_AUTH_SECRET, make_adventure
from test_v3 import PNG_B64, use_tmp_storage


def _cfg(**extra):
    cfg = {
        "enabled": True,
        "provider": "sdwebui",
        "sdwebui_base_url": "http://sd.local",
        "sdwebui_steps": 20,
        "sdwebui_width": 512,
        "sdwebui_height": 512,
        "negative_prompt": "",
        "txt2img_path": "/sdapi/v1/txt2img",
        "storage_dir": "data/images-test",
        "timeout_seconds": 5,
    }
    cfg.update(extra)
    return cfg


def test_sd_health_uses_only_options(client):
    paths = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append((request.method, request.url.path))
        if request.url.path == "/sdapi/v1/options":
            return httpx.Response(200, json={"sd_model_checkpoint": "model.safetensors"})
        return httpx.Response(500, text="generation endpoint must not be called")

    client.app.dependency_overrides[deps.get_http_transport] = (
        lambda: httpx.MockTransport(handler))
    resp = client.get("/api/health/image")
    assert resp.status_code == 200
    assert resp.json() == {
        "enabled": True,
        "provider": "sdwebui",
        "status": "ok",
        "checkpoint": "model.safetensors",
    }
    assert paths == [("GET", "/sdapi/v1/options")]


def test_openai_health_uses_models_and_provider_failure_is_200(client):
    with db.session() as conn:
        db.set_setting(conn, "image.provider", json.dumps("openai_compatible"))
        db.set_setting(conn, "image.openai_base_url", json.dumps("http://images.local/v1"))
        db.set_setting(conn, "image.openai_model", json.dumps("image-model"))

    def healthy(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.path == "/v1/models"
        return httpx.Response(200, json={"data": [{"id": "image-model"}]})

    client.app.dependency_overrides[deps.get_http_transport] = (
        lambda: httpx.MockTransport(healthy))
    resp = client.get("/api/health/image")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"
    assert resp.json()["model_available"] is True

    def unavailable(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("offline")

    client.app.dependency_overrides[deps.get_http_transport] = (
        lambda: httpx.MockTransport(unavailable))
    resp = client.get("/api/health/image")
    assert resp.status_code == 200
    assert resp.json()["status"] == "unreachable"


def test_seed_and_variation_change_cache_without_changing_default():
    baseline = image_client.request_key(_cfg(), "a lantern")
    assert image_client.request_key(_cfg(), "a lantern", variation=0) == baseline
    assert image_client.request_key(_cfg(seed=41), "a lantern") != baseline
    assert image_client.request_key(_cfg(seed=42), "a lantern") != baseline
    assert image_client.request_key(_cfg(), "a lantern", variation=1) != baseline
    assert image_client.request_key(_cfg(), "a lantern", variation=1) == (
        image_client.request_key(_cfg(), "a lantern", variation=1))


def test_seed_is_sent_to_sdwebui(tmp_path):
    captured = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(json.loads(request.content))
        return httpx.Response(200, json={"images": [PNG_B64]})

    image_client.generate_image(
        _cfg(seed=123, storage_dir=str(tmp_path)), "a lantern",
        adventure_id="adv", image_id="img", transport=httpx.MockTransport(handler),
    )
    assert captured[0]["seed"] == 123


def test_cancel_running_interrupts_and_never_publishes(client, tmp_path):
    use_tmp_storage(tmp_path)
    adv = make_adventure()
    with db.session() as conn:
        job = db.insert_job(
            conn, adventure_id=adv["id"], type="see_image",
            payload={"prompt": "a lantern", "caption": "A lantern."},
        )
    entered = threading.Event()
    release = threading.Event()
    interrupts = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/sdapi/v1/txt2img":
            entered.set()
            release.wait(5)
            return httpx.Response(200, json={"images": [PNG_B64]})
        if request.url.path == "/sdapi/v1/interrupt":
            interrupts.append(request)
            release.set()
            return httpx.Response(200)
        return httpx.Response(404)

    transport = httpx.MockTransport(handler)
    client.app.dependency_overrides[deps.get_http_transport] = lambda: transport
    with events.subscribe(adv["id"]) as sub:
        worker = threading.Thread(target=jobs.process_pending, args=(transport,))
        worker.start()
        assert entered.wait(5)
        resp = client.post(f"/api/jobs/{job['id']}/cancel")
        assert resp.status_code == 200, resp.text
        assert resp.json()["status"] == "canceled"
        worker.join(5)
        assert not worker.is_alive()
        event_types = []
        while (event := sub.get(0.05)) is not None:
            event_types.append(event.type)

    assert len(interrupts) == 1
    with db.session() as conn:
        assert db.get_job(conn, job["id"])["status"] == "canceled"
        assert db.latest_image(conn, adv["id"], "scene") is None
        assert [a["type"] for a in db.list_actions(conn, adv["id"])] == []
    assert "see_ready" not in event_types
    assert "see_failed" not in event_types


def test_cancel_requires_ownership_and_pending_does_not_interrupt(client):
    adv = make_adventure(owner="alice")
    with db.session() as conn:
        job = db.insert_job(conn, adventure_id=adv["id"], type="see_image",
                            payload={"prompt": "a lantern"})
    bob = auth_core.make_token("bob", TEST_AUTH_SECRET, ttl_h=1)
    alice = auth_core.make_token("alice", TEST_AUTH_SECRET, ttl_h=1)
    url = f"/api/jobs/{job['id']}/cancel"
    assert client.post(url, headers={"Authorization": f"Bearer {bob}"}).status_code == 404
    with db.session() as conn:
        assert db.get_job(conn, job["id"])["status"] == "pending"
    resp = client.post(url, headers={"Authorization": f"Bearer {alice}"})
    assert resp.status_code == 200
    assert resp.json()["status"] == "canceled"
