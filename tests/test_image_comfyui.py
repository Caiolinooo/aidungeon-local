"""FineView/Qwen 2.1 no ComfyUI: grafos, referências e erros reais da API."""

from __future__ import annotations

import json

import httpx
import pytest

from app import image_client


def _cfg(tmp_path, **extra):
    cfg = {
        "provider": "comfyui",
        "comfyui_base_url": "http://comfy.test:8188",
        "comfyui_unet": "fineviewQWEN_qw21TurboFixed_3299367.safetensors",
        "comfyui_clip": "qwen3vl_8b_int8_convrot.safetensors",
        "comfyui_clip_type": "qwen_image_2_1",
        "comfyui_clip_device": "cpu",
        "comfyui_vae": "texture_fix_vae_for_qwen_image_2.1_bf16.safetensors",
        "comfyui_steps": 8,
        "comfyui_width": 800,
        "comfyui_height": 720,
        "comfyui_sampler": "euler",
        "comfyui_scheduler": "normal",
        "comfyui_shift": 1.31,
        "comfyui_cfg": 1.0,
        "comfyui_poll_interval_s": 0.2,
        "negative_prompt": "bad anatomy",
        "storage_dir": str(tmp_path),
        "timeout_seconds": 5,
    }
    cfg.update(extra)
    return cfg


def _transport(capture: dict, *, fail: bool = False) -> httpx.MockTransport:
    capture.setdefault("uploads", [])

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
                         "16": {"images": [{"filename": "aidungeon_0001.png",
                                            "subfolder": "", "type": "output"}]}}}
            if fail:
                entry["status"]["messages"] = [
                    ["execution_error", {"exception_message": "CUDA out of memory"}]]
            return httpx.Response(200, json={"pid-1": entry})
        if path == "/view":
            capture["view_params"] = dict(request.url.params)
            return httpx.Response(200, content=b"\x89PNG-comfy")
        if path == "/upload/image":
            capture["uploads"].append(request)
            return httpx.Response(200, json={
                "name": f"aidungeon-ref-{len(capture['uploads'])}.png",
                "subfolder": "", "type": "input"})
        return httpx.Response(404)

    return httpx.MockTransport(handler)


def _nodes(graph: dict) -> dict[str, dict]:
    return {node["class_type"]: node for node in graph.values()}


def _of_type(graph: dict, class_type: str) -> list[dict]:
    return [node for node in graph.values() if node["class_type"] == class_type]


def test_comfyui_builds_fineview_graph_and_saves_png(tmp_path):
    capture: dict = {}
    result = image_client.generate_image(
        _cfg(tmp_path), "a knight in the rain", adventure_id="adv-1",
        image_id="img-1", negative_extra="extra limbs",
        transport=_transport(capture))

    assert result["provider"] == "comfyui"
    assert (result["width"], result["height"]) == (800, 720)
    assert (tmp_path / "adv-1" / "img-1.png").read_bytes() == b"\x89PNG-comfy"
    graph = capture["graph"]
    nodes = _nodes(graph)
    texts = [node["inputs"]["text"] for node in _of_type(graph, "CLIPTextEncode")]
    assert texts == ["a knight in the rain", "bad anatomy, extra limbs"]
    assert nodes["UNETLoader"]["inputs"]["unet_name"] == (
        "fineviewQWEN_qw21TurboFixed_3299367.safetensors")
    assert nodes["CLIPLoader"]["inputs"] == {
        "clip_name": "qwen3vl_8b_int8_convrot.safetensors",
        "type": "qwen_image_2_1", "device": "cpu"}
    assert nodes["VAELoader"]["inputs"]["vae_name"] == (
        "texture_fix_vae_for_qwen_image_2.1_bf16.safetensors")
    assert nodes["EmptyImageLatent"]["inputs"]["width"] == 800
    assert nodes["ModelSamplingAuraFlow"]["inputs"]["shift"] == 1.31
    assert nodes["ManualSigmas"]["inputs"]["sigmas"] == [
        1.0, 0.875, 0.75, 0.625, 0.5, 0.375, 0.25, 0.125, 0.0]
    assert nodes["KSamplerSelect"]["inputs"]["sampler_name"] == "euler"
    assert capture["view_params"]["filename"] == "aidungeon_0001.png"


def test_comfyui_uploads_every_identity_reference_into_both_conditionings(tmp_path):
    capture: dict = {}
    image_client.generate_image(
        _cfg(tmp_path), "same person", adventure_id="adv-1", image_id="img-2",
        reference_images=[b"\x89PNG-one", b"\x89PNG-two"],
        transport=_transport(capture))

    assert len(capture["uploads"]) == 2
    graph = capture["graph"]
    assert len(_of_type(graph, "LoadImage")) == 2
    references = _of_type(graph, "ReferenceLatent")
    assert len(references) == 4
    sampler = _nodes(graph)["SamplerCustomAdvanced"]["inputs"]
    assert sampler["latent_image"] == ["32", 0]
    assert sampler["guider"] == ["13", 0]
    assert _nodes(graph)["BasicGuider"]["inputs"]["conditioning"] == ["33", 0]


def test_comfyui_execution_error_is_reported(tmp_path):
    with pytest.raises(image_client.ImageError) as exc:
        image_client.generate_image(
            _cfg(tmp_path), "a tower", adventure_id="adv-1", image_id="img-1",
            transport=_transport({}, fail=True))
    assert "CUDA out of memory" in str(exc.value)


def test_request_key_comfyui_tracks_identity_and_sampler_configuration(tmp_path):
    base = _cfg(tmp_path)
    first = image_client.request_key(base, "a knight", reference_images=[b"one"])
    assert first == image_client.request_key(base, "a knight", reference_images=[b"one"])
    assert first != image_client.request_key(base, "a knight", reference_images=[b"two"])
    assert first != image_client.request_key(
        _cfg(tmp_path, comfyui_unet="outro.safetensors"), "a knight",
        reference_images=[b"one"])
    assert first != image_client.request_key(
        _cfg(tmp_path, comfyui_shift=1.5), "a knight", reference_images=[b"one"])


def test_comfyui_health_and_interrupt_use_native_endpoints(tmp_path):
    paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        if request.url.path == "/system_stats":
            return httpx.Response(200, json={"system": {"comfyui_version": "0.3"}})
        if request.url.path == "/interrupt":
            return httpx.Response(200, json={})
        return httpx.Response(404)

    transport = httpx.MockTransport(handler)
    health = image_client.health(_cfg(tmp_path), transport)
    assert health["status"] == "ok"
    assert health["unet"] == "fineviewQWEN_qw21TurboFixed_3299367.safetensors"
    image_client.interrupt_sdwebui(_cfg(tmp_path), transport)
    assert paths == ["/system_stats", "/interrupt"]
