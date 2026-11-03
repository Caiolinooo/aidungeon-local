"""Runtime local/hybrid/Colab: defaults, rotas, URL efetiva e inscrição.

O catálogo de teste substitui `app.runtime_provisioning` para o contrato
ficar determinístico. Sampling e resolução do arquivo não podem mudar.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from app import auth as auth_core
from app import db, deps, llm_client, settings_store
from app.routes import runtimes

from conftest import TEST_AUTH_SECRET, TEST_AUTH_USER, make_llm_transport

LLM_ALIAS = "Qwen3.6-35B-A3B-Uncensored-HauhauCS-Aggressive-IQ2_M.gguf"
AUX_ALIAS = "qwen2.5-3b-instruct-q4_k_m"
KLEIN_UNET = "flux-2-klein-4b-fp8.safetensors"
FINEVIEW_UNET = "fineviewQWEN_qw21TurboFixed_3299367.safetensors"
WAN_UNET = "Wan2.2-TI2V-5B-Q8_0.gguf"
WAN_CLIP = "umt5_xxl_fp8_e4m3fn_scaled.safetensors"
LTXV_CLIP = "t5xxl_fp8_e4m3fn_scaled.safetensors"

CATALOG = {
    "models": {
        "llm": {
            "qwen3.6-35b-a3b": {
                "model_alias": LLM_ALIAS,
                "api_key": "sk-catalog-secret",
            },
            "qwen2.5-3b-instruct-q4_k_m": {"model_alias": AUX_ALIAS, "role": "fallback"},
        },
        "image": {
            "flux2_klein": {
                "engine": "flux2_klein",
                "quality": {
                    "unet": KLEIN_UNET,
                    "clip": "qwen_3_4b.safetensors",
                    "clip_type": "flux2",
                    "vae": "flux2-vae.safetensors",
                    "weight_dtype": "default",
                    "steps": 9,
                    "cfg": 9,
                    "width": 640,
                    "height": 640,
                },
            },
            "fineview_qwen": {
                "engine": "qwen",
                "quality": {
                    "unet": FINEVIEW_UNET,
                    "clip": "qwen3vl_8b_int8_convrot.safetensors",
                    "clip_type": "qwen_image",
                    "clip_device": "cpu",
                    "vae": "texture_fix_vae_for_qwen_image_2.1_bf16.safetensors",
                    "steps": 3,
                    "width": 512,
                },
            },
        },
        "video": {
            "ltxv": {
                "engine": "ltxv",
                "config": {
                    "engine": "ltxv",
                    "checkpoint": "ltxv-2b-0.9.6-distilled-04-25.safetensors",
                    "clip": LTXV_CLIP,
                    "steps": 3,
                    "width": 320,
                    "height": 240,
                    "frames": 9,
                    "fps": 8,
                },
            },
            "wan": {
                "engine": "wan",
                "config": {
                    "engine": "wan",
                    "unet": WAN_UNET,
                    "clip": WAN_CLIP,
                    "vae": "wan2.2_vae.safetensors",
                    "steps": 20,
                    "cfg": 5,
                    "width": 1280,
                    "height": 704,
                    "frames": 49,
                    "fps": 16,
                },
            },
        },
    }
}


def _validate(config, catalog=None, require_colab=False):
    catalog = catalog or CATALOG
    mode = config.get("mode")
    if mode not in ("local", "hybrid", "colab"):
        raise ValueError("mode must be local, hybrid, or colab")
    placements = dict(config.get("placements") or {})
    if mode == "local":
        placements = {"llm": "local", "comfyui": "local"}
    elif mode == "colab":
        placements = {"llm": "colab", "comfyui": "colab"}
    else:
        if placements.get("llm") == placements.get("comfyui"):
            raise ValueError("hybrid requires one local and one colab placement")
        if placements.get("llm") not in ("local", "colab"):
            raise ValueError("llm placement")
        if placements.get("comfyui") not in ("local", "colab"):
            raise ValueError("comfyui placement")
    models = config.get("models") or {}
    for kind in ("llm", "image", "video"):
        model_id = models.get(kind)
        if model_id not in (catalog.get("models") or {}).get(kind, {}):
            raise ValueError(f"unknown {kind} model: {model_id}")
    endpoints = {
        key: "" if (config.get("endpoints") or {}).get(key) is None
        else str((config.get("endpoints") or {}).get(key))
        for key in ("local_llm", "local_comfyui", "colab_llm", "colab_comfyui")
    }
    if require_colab and mode == "local":
        raise ValueError("require_colab exige ao menos um placement colab")
    return {
        "mode": mode,
        "placements": placements,
        "models": {kind: models[kind] for kind in ("llm", "image", "video")},
        "endpoints": endpoints,
        "project_url": "" if config.get("project_url") is None else str(config.get("project_url")),
    }


def _notebook(config, catalog=None):
    source = json.dumps({"config": config, "catalog": catalog}, ensure_ascii=False)
    return {
        "nbformat": 4,
        "nbformat_minor": 5,
        "metadata": {},
        "cells": [{
            "cell_type": "code",
            "metadata": {},
            "execution_count": None,
            "outputs": [],
            "source": [source],
        }],
    }


@pytest.fixture(autouse=True)
def fake_provisioning(monkeypatch):
    """O módulo real pode ainda estar sendo escrito. O teste usa o contrato."""
    rp = SimpleNamespace(
        load_catalog=lambda: CATALOG,
        validate_runtime=_validate,
        generate_notebook=_notebook,
        notebook_bytes=lambda notebook: json.dumps(notebook).encode("utf-8"),
        local_hardware=lambda: {"gpu": "Test", "vram_mib": 8192},
    )
    monkeypatch.setattr(settings_store, "_provisioning", lambda: rp)
    monkeypatch.setattr(runtimes, "_provisioning", lambda: rp)


@pytest.fixture()
def auth_token(admin_token):
    return admin_token


def _as_user(client):
    token = auth_core.make_token(TEST_AUTH_USER, TEST_AUTH_SECRET, ttl_h=1)
    client.headers["Authorization"] = f"Bearer {token}"


def _image(conn):
    return settings_store.effective_image_settings(conn)


def test_get_runtime_defaults(client):
    resp = client.get("/api/settings/runtime")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    runtime = body["runtime"]
    assert runtime["mode"] == "local"
    assert runtime["placements"] == {"llm": "local", "comfyui": "local"}
    assert runtime["models"] == {
        "llm": "qwen3.6-35b-a3b",
        "image": "flux2_klein",
        "video": "ltxv",
    }
    assert runtime["endpoints"]["local_llm"] == "http://vm.groupabz.com:1234/v1"
    assert runtime["endpoints"]["local_comfyui"] == "http://127.0.0.1:8188"
    assert runtime["endpoints"]["colab_llm"] == ""
    assert runtime["endpoints"]["colab_comfyui"] == ""
    assert runtime["project_url"] == ""
    assert body["catalog"]["models"]["llm"]["qwen3.6-35b-a3b"]["model_alias"] == LLM_ALIAS
    assert body["local_hardware"] == {"gpu": "Test", "vram_mib": 8192}
    assert body["registration"] == {
        "endpoints": None, "hardware": None, "last_seen": None, "models": None,
    }
    assert "sk-catalog-secret" not in resp.text
    assert "api_key" not in resp.text
    assert client.get("/api/settings/llm").json()["base_url"] == "http://vm.groupabz.com:1234/v1"
    assert client.get("/api/settings/llm").json()["model_alias"] == "auto"


def test_partial_update_keeps_the_rest(client):
    resp = client.put("/api/settings/runtime", json={"project_url": "https://play.example"})
    assert resp.status_code == 200, resp.text
    runtime = resp.json()["runtime"]
    assert runtime["project_url"] == "https://play.example"
    assert runtime["mode"] == "local"
    assert runtime["models"]["image"] == "flux2_klein"
    assert runtime["endpoints"]["local_llm"] == "http://vm.groupabz.com:1234/v1"
    assert client.get("/api/settings/llm").json()["temperature"] == 0.85


def test_put_requires_admin(client):
    before = client.get("/api/settings/runtime").json()["runtime"]
    _as_user(client)
    resp = client.put("/api/settings/runtime", json={"project_url": "https://nope.example"})
    assert resp.status_code == 403, resp.text
    client.headers["Authorization"] = f"Bearer {auth_core.make_token('Caio', TEST_AUTH_SECRET, ttl_h=1)}"
    assert client.get("/api/settings/runtime").json()["runtime"] == before


def test_modes_route_the_selected_endpoint_even_when_blank(client):
    drafted = client.put("/api/settings/runtime", json={
        "mode": "local",
        "endpoints": {
            "local_llm": "http://local-llm.example/v1",
            "local_comfyui": "http://local-comfy.example",
            "colab_llm": "",
            "colab_comfyui": "",
        },
    })
    assert drafted.status_code == 200, drafted.text
    assert client.get("/api/settings/llm").json()["base_url"] == "http://local-llm.example/v1"
    assert client.get("/api/settings/image").json()["comfyui_base_url"] == "http://local-comfy.example"

    forced = client.put("/api/settings/runtime", json={
        "mode": "colab",
        "placements": {"llm": "local", "comfyui": "local"},
    })
    assert forced.status_code == 200, forced.text
    runtime = forced.json()["runtime"]
    assert runtime["mode"] == "colab"
    assert runtime["placements"] == {"llm": "colab", "comfyui": "colab"}
    assert client.get("/api/settings/llm").json()["base_url"] == ""
    assert client.get("/api/settings/image").json()["comfyui_base_url"] == ""

    hybrid = client.put("/api/settings/runtime", json={
        "mode": "hybrid",
        "placements": {"llm": "colab", "comfyui": "local"},
        "endpoints": {"colab_llm": "http://colab-llm.example/v1"},
    })
    assert hybrid.status_code == 200, hybrid.text
    assert hybrid.json()["runtime"]["placements"] == {"llm": "colab", "comfyui": "local"}
    assert client.get("/api/settings/llm").json()["base_url"] == "http://colab-llm.example/v1"
    assert client.get("/api/settings/image").json()["comfyui_base_url"] == "http://local-comfy.example"

    rejected = client.put("/api/settings/runtime", json={"mode": "hybrid", "placements": {
        "llm": "local", "comfyui": "local",
    }})
    assert rejected.status_code == 400, rejected.text
    assert client.get("/api/settings/runtime").json()["runtime"]["mode"] == "hybrid"


def test_selected_models_apply_engine_without_sampling_or_resolution(client):
    sampling = client.get("/api/settings/llm").json()
    image = client.get("/api/settings/image").json()
    local_fallback = client.put("/api/settings/runtime", json={
        "models": {"llm": "qwen2.5-3b-instruct-q4_k_m"},
    })
    assert local_fallback.status_code == 400, local_fallback.text

    resp = client.put("/api/settings/runtime", json={
        "mode": "hybrid",
        "placements": {"llm": "colab", "comfyui": "local"},
        "models": {
            "llm": "qwen2.5-3b-instruct-q4_k_m",
            "image": "fineview_qwen",
            "video": "wan",
        },
    })
    assert resp.status_code == 200, resp.text
    assert client.get("/api/settings/llm").json()["model_alias"] == AUX_ALIAS
    assert client.get("/api/settings/llm").json()["temperature"] == sampling["temperature"]
    assert client.get("/api/settings/image").json()["comfyui_width"] == image["comfyui_width"]
    assert client.get("/api/settings/image").json()["comfyui_steps"] == image["comfyui_steps"]
    with db.session() as conn:
        cfg = _image(conn)
    assert cfg["comfyui_engine"] == "qwen"
    assert cfg["comfyui_unet"] == FINEVIEW_UNET
    assert cfg["comfyui_width"] == 1024
    assert cfg["comfyui_steps"] == 8
    video = cfg["comfyui_video"]
    assert video["engine"] == "wan"
    assert video["unet"] == WAN_UNET
    assert video["clip"] == WAN_CLIP
    assert video["vae"] == "wan2.2_vae.safetensors"
    assert int(video["width"]) == 1280
    assert int(video["height"]) == 704
    assert int(video["steps"]) == 20
    assert int(video["cfg"]) == 5
    assert int(video["frames"]) == 49

    back = client.put("/api/settings/runtime", json={
        "mode": "local",
        "models": {
            "llm": "qwen3.6-35b-a3b",
            "image": "flux2_klein",
            "video": "ltxv",
        },
    })
    assert back.status_code == 200, back.text
    with db.session() as conn:
        cfg = _image(conn)
    assert cfg["comfyui_engine"] == "flux2_klein"
    assert cfg["comfyui_flux2"]["unet"] == KLEIN_UNET
    assert int(cfg["comfyui_flux2"]["steps"]) == 4
    assert cfg["comfyui_video"]["engine"] == "ltxv"
    assert cfg["comfyui_video"]["clip"] == LTXV_CLIP
    assert int(cfg["comfyui_video"]["width"]) == 320
    assert int(cfg["comfyui_video"]["steps"]) == 3
    assert client.get("/api/settings/llm").json()["model_alias"] == LLM_ALIAS


def test_unknown_model_does_not_persist(client):
    resp = client.put("/api/settings/runtime", json={"models": {"image": "no-such-model"}})
    assert resp.status_code == 400, resp.text
    assert resp.json()["error"]["code"] == "validation_error"
    assert client.get("/api/settings/runtime").json()["runtime"]["models"]["image"] == "flux2_klein"


def test_runtime_update_invalidates_llm_discovery(client):
    client.app.dependency_overrides[deps.get_http_transport] = \
        lambda: make_llm_transport(model_id="model-a")
    primed = client.put("/api/settings/llm", json={
        "base_url": "http://a.example/v1", "model_alias": "auto",
    })
    assert primed.json()["model_discovered"] == "model-a"
    client.app.dependency_overrides[deps.get_http_transport] = \
        lambda: make_llm_transport(model_id="model-b")
    updated = client.put("/api/settings/runtime", json={"project_url": "https://play.example"})
    assert updated.status_code == 200, updated.text
    assert llm_client.cached_discovery("http://a.example/v1", "auto") is None
    assert client.get("/api/settings/llm").json()["model_discovered"] != "model-a"
    client.app.dependency_overrides.clear()


def test_notebook_auth_and_download(client):
    client.put("/api/settings/llm", json={"api_key": "sk-secret-1234"})
    all_local = client.get("/api/runtime/notebook")
    assert all_local.status_code == 400
    client.put("/api/settings/runtime", json={
        "mode": "hybrid",
        "placements": {"llm": "colab", "comfyui": "local"},
    })
    _as_user(client)
    forbidden = client.get("/api/runtime/notebook")
    assert forbidden.status_code == 403
    client.headers["Authorization"] = f"Bearer {auth_core.make_token('Caio', TEST_AUTH_SECRET, ttl_h=1)}"
    saved = client.headers["Authorization"]
    del client.headers["Authorization"]
    denied = client.get("/api/runtime/notebook")
    assert denied.status_code == 401
    client.headers["Authorization"] = saved
    resp = client.get("/api/runtime/notebook")
    assert resp.status_code == 200, resp.text
    disposition = resp.headers["content-disposition"]
    assert disposition.startswith("attachment;")
    assert "aidungeon-runtime.ipynb" in disposition
    notebook = resp.json()
    assert notebook["nbformat"] == 4
    assert "sk-secret-1234" not in resp.text
    assert "sk-catalog-secret" not in resp.text
    assert "test-secret" not in resp.text


def test_register_requires_admin_and_matching_models(client):
    client.put("/api/settings/runtime", json={
        "mode": "local",
        "endpoints": {
            "local_llm": "http://local-llm.example/v1",
            "local_comfyui": "http://local-comfy.example",
        },
    })
    payload = {
        "endpoints": {"llm": "http://colab-llm.example/v1", "comfyui": "http://colab-comfy.example"},
        "models": {
            "llm": "qwen2.5-3b-instruct-q4_k_m",
            "image": "flux2_klein",
            "video": "ltxv",
        },
        "hardware": {"gpu": "T4", "api_key": "sk-secret-1234", "token": "super-secret-token"},
    }
    _as_user(client)
    forbidden = client.post("/api/runtime/register", json=payload)
    assert forbidden.status_code == 403, forbidden.text
    client.headers["Authorization"] = f"Bearer {auth_core.make_token('Caio', TEST_AUTH_SECRET, ttl_h=1)}"

    mismatch = client.post("/api/runtime/register", json=payload)
    assert mismatch.status_code == 409, mismatch.text
    assert mismatch.json()["error"]["code"] == "model_mismatch"
    state = client.get("/api/settings/runtime").json()
    assert state["registration"]["last_seen"] is None
    assert client.get("/api/settings/llm").json()["base_url"] == "http://local-llm.example/v1"
    assert "sk-secret-1234" not in mismatch.text

    payload["models"]["llm"] = "qwen3.6-35b-a3b"
    kept = client.post("/api/runtime/register", json=payload)
    assert kept.status_code == 200, kept.text
    assert client.get("/api/settings/llm").json()["base_url"] == "http://local-llm.example/v1"
    assert client.get("/api/settings/image").json()["comfyui_base_url"] == "http://local-comfy.example"
    registered = kept.json()
    assert registered["runtime"]["endpoints"]["colab_llm"] == "http://colab-llm.example/v1"
    assert registered["registration"]["endpoints"]["comfyui"] == "http://colab-comfy.example"
    assert registered["registration"]["hardware"] == {"gpu": "T4"}
    assert registered["registration"]["last_seen"]
    assert registered["registration"]["models"]["llm"] == "qwen3.6-35b-a3b"
    assert "sk-secret-1234" not in kept.text
    assert "super-secret-token" not in kept.text
    with db.session() as conn:
        stored = db.get_setting(conn, "runtime.registration.hardware")
    assert "sk-secret-1234" not in stored
    assert "super-secret-token" not in stored

    client.put("/api/settings/runtime", json={
        "mode": "hybrid",
        "placements": {"llm": "colab", "comfyui": "local"},
        "endpoints": {"colab_comfyui": "http://previous-colab-comfy.example"},
    })
    hybrid_reg = client.post("/api/runtime/register", json={
        "endpoints": {"llm": "http://hybrid-colab-llm.example/v1", "comfyui": ""},
        "models": payload["models"],
        "hardware": {"gpu": "L4"},
    })
    assert hybrid_reg.status_code == 200, hybrid_reg.text
    assert hybrid_reg.json()["runtime"]["endpoints"]["colab_comfyui"] == "http://previous-colab-comfy.example"
    assert client.get("/api/settings/llm").json()["base_url"] == "http://hybrid-colab-llm.example/v1"
    assert client.get("/api/settings/image").json()["comfyui_base_url"] == "http://local-comfy.example"

    missing_colab = client.post("/api/runtime/register", json={
        "endpoints": {"llm": "", "comfyui": ""},
        "models": payload["models"],
        "hardware": {"gpu": "L4"},
    })
    assert missing_colab.status_code == 400

    client.put("/api/settings/runtime", json={"mode": "colab"})
    applied = client.post("/api/runtime/register", json=payload)
    assert applied.status_code == 200, applied.text
    assert client.get("/api/settings/llm").json()["base_url"] == "http://colab-llm.example/v1"
    assert client.get("/api/settings/image").json()["comfyui_base_url"] == "http://colab-comfy.example"

def test_runtime_responses_do_not_leak_secrets(client):
    client.put("/api/settings/llm", json={"api_key": "sk-secret-1234"})
    client.put("/api/settings/runtime", json={"project_url": "https://play.example"})
    listing = client.get("/api/settings/runtime")
    assert "sk-secret-1234" not in listing.text
    assert "sk-catalog-secret" not in listing.text
    assert "api_key" not in listing.text
