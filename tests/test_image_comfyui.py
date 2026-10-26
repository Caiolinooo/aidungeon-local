"""Provider ComfyUI (FLUX.3 Klein): grafo API, polling de /history e edição."""

from __future__ import annotations

import json

import httpx
import pytest

from app import db, image_client, settings_store


def _cfg(tmp_path, **extra):
    cfg = {
        "provider": "comfyui",
        "comfyui_base_url": "http://comfy.test:8188",
        "comfyui_unet": "flux3-image-klein-int8-convrot.safetensors",
        "comfyui_clip": "qwen3vl_4b_int8_convrot.safetensors",
        "comfyui_clip_type": "flux3",
        "comfyui_clip_device": "cpu",
        "comfyui_vae": "flux2-vae.safetensors",
        "comfyui_steps": 15,
        "comfyui_width": 1024,
        "comfyui_height": 1024,
        "comfyui_cfg_scale": 1.0,
        "comfyui_sampler_name": "er_sde",
        "comfyui_scheduler": "simple",
        "comfyui_edit_shift": 2.77259,
        "comfyui_edit_steps": 16,
        "comfyui_edit_sampler": "euler",
        "comfyui_edit_scheduler": "simple",
        "comfyui_poll_interval_s": 0.2,
        "negative_prompt": "bad anatomy",
        "storage_dir": str(tmp_path),
        "timeout_seconds": 5,
    }
    cfg.update(extra)
    return cfg


def _transport(capture: dict, *, fail: bool = False) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/prompt":
            capture["graph"] = json.loads(request.content)["prompt"]
            return httpx.Response(200, json={
                "prompt_id": "pid-1", "number": 1, "node_errors": {}})
        if path == "/history/pid-1":
            status = "error" if fail else "success"
            entry = {"status": {"status_str": status, "completed": True},
                     "outputs": {} if fail else {
                         "9": {"images": [{"filename": "aidungeon_0001.png",
                                           "subfolder": "", "type": "output"}]}}}
            if fail:
                entry["status"]["messages"] = [
                    ["execution_error", {"exception_message": "CUDA out of memory"}]]
            return httpx.Response(200, json={"pid-1": entry})
        if path == "/view":
            capture["view_params"] = dict(request.url.params)
            return httpx.Response(200, content=b"\x89PNG-comfy")
        if path == "/upload/image":
            capture.setdefault("uploads", []).append(request)
            name = f"aidungeon-ref{len(capture['uploads'])}.png"
            return httpx.Response(200, json={
                "name": name, "subfolder": "", "type": "input"})
        if path == "/system_stats":
            return httpx.Response(200, json={
                "system": {"comfyui_version": "0.41.3"},
                "devices": [{"name": "cuda:0"}]})
        return httpx.Response(404)

    return httpx.MockTransport(handler)


def _nodes(graph: dict) -> dict[str, dict]:
    """Primeiro nó de cada tipo, para leitura; use _of_type para os repetidos."""
    nodes: dict[str, dict] = {}
    for node in graph.values():
        nodes.setdefault(node["class_type"], node)
    return nodes


def _of_type(graph: dict, class_type: str) -> list[dict]:
    return [node for node in graph.values() if node["class_type"] == class_type]


def test_comfyui_txt2img_builds_flux3_graph_and_saves_png(tmp_path):
    capture: dict = {}
    result = image_client.generate_image(
        _cfg(tmp_path), "a knight in the rain", adventure_id="adv-1",
        image_id="img-1", transport=_transport(capture))

    assert result["provider"] == "comfyui"
    assert result["width"] == 1024 and result["height"] == 1024
    assert (tmp_path / "adv-1" / "img-1.png").read_bytes() == b"\x89PNG-comfy"

    graph = capture["graph"]
    nodes = _nodes(graph)
    text_nodes = _of_type(graph, "CLIPTextEncode")
    assert [node["inputs"]["text"] for node in text_nodes] == [
        "a knight in the rain", ""]
    assert nodes["UNETLoader"]["inputs"]["unet_name"] == (
        "flux3-image-klein-int8-convrot.safetensors")
    assert nodes["CLIPLoader"]["inputs"]["clip_name"] == (
        "qwen3vl_4b_int8_convrot.safetensors")
    assert nodes["CLIPLoader"]["inputs"]["type"] == "flux3"
    assert nodes["CLIPLoader"]["inputs"]["device"] == "cpu"
    assert nodes["VAELoader"]["inputs"]["vae_name"] == "flux2-vae.safetensors"
    assert nodes["EmptyFlux2LatentImage"]["inputs"]["width"] == 1024
    sampler = nodes["KSampler"]["inputs"]
    assert sampler["steps"] == 15
    assert sampler["cfg"] == 1.0
    assert sampler["sampler_name"] == "er_sde"
    assert sampler["scheduler"] == "simple"
    assert sampler["denoise"] == 1.0
    assert capture["view_params"]["filename"] == "aidungeon_0001.png"


def test_comfyui_negative_node_is_empty_because_cfg_is_one(tmp_path):
    capture: dict = {}
    image_client.generate_image(
        _cfg(tmp_path), "a forest", adventure_id="adv-1", image_id="img-1",
        transport=_transport(capture), negative_extra="extra limbs")

    texts = [node["inputs"]["text"] for node in capture["graph"].values()
             if node["class_type"] == "CLIPTextEncode"]
    assert texts == ["a forest", ""]


def test_comfyui_img2img_uploads_reference_and_uses_edit_graph(tmp_path):
    capture: dict = {}
    result = image_client.generate_image(
        _cfg(tmp_path, comfyui_width=800, comfyui_height=720),
        "same person, keep the same face", adventure_id="adv-1",
        image_id="img-2", transport=_transport(capture),
        width=800, height=720, init_image=b"\x89PNG-ref",
        denoising_strength=0.4,
        reference_images=[b"\x89PNG-ref2"])

    assert result["width"] == 800 and result["height"] == 720
    uploads = capture["uploads"]
    assert len(uploads) == 2
    assert all(u.headers["content-type"].startswith("multipart/form-data")
               for u in uploads)
    assert b"\x89PNG-ref" in uploads[0].content

    graph = capture["graph"]
    nodes = _nodes(graph)
    loads = _of_type(graph, "LoadImage")
    assert [node["inputs"]["image"] for node in loads] == [
        "aidungeon-ref1.png", "aidungeon-ref2.png"]
    scale = nodes["ImageScale"]["inputs"]
    assert (scale["width"], scale["height"]) == (800, 720)
    # Edição do Klein: referências no condicionamento, denoise 1.
    assert nodes["KSampler"]["inputs"]["denoise"] == 1.0
    assert nodes["KSampler"]["inputs"]["steps"] == 16
    assert nodes["KSampler"]["inputs"]["sampler_name"] == "euler"
    assert len(_of_type(graph, "ReferenceLatent")) == 4
    assert nodes["ModelSamplingFlux"]["inputs"]["max_shift"] == 2.77259
    assert "EmptyFlux2LatentImage" not in nodes


def test_comfyui_execution_error_is_reported(tmp_path):
    with pytest.raises(image_client.ImageError) as exc:
        image_client.generate_image(
            _cfg(tmp_path), "a tower", adventure_id="adv-1", image_id="img-1",
            transport=_transport({}, fail=True))
    assert "CUDA out of memory" in str(exc.value)


def test_comfyui_health_probes_system_stats(tmp_path):
    result = image_client.health(_cfg(tmp_path), transport=_transport({}))
    assert result["status"] == "ok"
    assert result["comfyui_version"] == "0.41.3"
    assert result["device_count"] == 1


def test_request_key_comfyui_tracks_model_steps_and_denoise(tmp_path):
    base = _cfg(tmp_path)
    first = image_client.request_key(base, "a knight", width=1024, height=1024)
    assert first == image_client.request_key(base, "a knight", width=1024, height=1024)
    assert first != image_client.request_key(
        _cfg(tmp_path, comfyui_unet="outro.safetensors"), "a knight",
        width=1024, height=1024)
    assert first != image_client.request_key(
        _cfg(tmp_path, comfyui_steps=30), "a knight", width=1024, height=1024)
    assert first != image_client.request_key(
        base, "a knight", width=1024, height=1024, denoising_strength=0.4)


def test_image_comfyui_settings_roundtrip(client, admin_token):
    client.headers["Authorization"] = f"Bearer {admin_token}"
    resp = client.put("/api/settings/image", json={
        "provider": "comfyui",
        "comfyui_base_url": "http://127.0.0.1:8188",
        "comfyui_steps": 12,
        "comfyui_width": 800,
        "comfyui_height": 720,
        "comfyui_cfg_scale": 1.0,
    })
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["provider"] == "comfyui"
    assert data["comfyui_steps"] == 12
    assert (data["comfyui_width"], data["comfyui_height"]) == (800, 720)

    with db.session() as conn:
        cfg = settings_store.effective_image_settings(conn)
    # Modelo/VAE/CLIP/sampler de edição vêm só do settings.json (internos).
    assert cfg["comfyui_unet"] == "flux3-image-klein-int8-convrot.safetensors"
    assert cfg["comfyui_clip"] == "qwen3vl_4b_int8_convrot.safetensors"
    assert cfg["comfyui_clip_type"] == "flux3"
    assert cfg["comfyui_vae"] == "flux2-vae.safetensors"
    assert cfg["comfyui_edit_sampler"] == "euler"
    assert cfg["comfyui_edit_shift"] == 2.77259
