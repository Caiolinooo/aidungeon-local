"""FineView/Qwen 2.1 no ComfyUI: grafos, referências e erros reais da API."""

from __future__ import annotations

import json

import httpx
import pytest

from app import db, image_client, jobs


def _cfg(tmp_path, **extra):
    cfg = {
        "provider": "comfyui",
        "comfyui_base_url": "http://comfy.test:8188",
        "comfyui_unet": "fineviewQWEN_qw21TurboFixed_3299367.safetensors",
        "comfyui_clip": "qwen3vl_8b_int8_convrot.safetensors",
        "comfyui_clip_type": "qwen_image",
        "comfyui_clip_device": "cpu",
        "comfyui_vae": "texture_fix_vae_for_qwen_image_2.1_bf16.safetensors",
        "comfyui_steps": 8,
        "comfyui_width": 1024,
        "comfyui_height": 1024,
        "comfyui_sampler": "euler",
        "comfyui_sigmas": [
            1.0, 0.978453, 0.95418, 0.926626, 0.89508,
            0.845148, 0.704534, 0.414568, 0.0,
        ],
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


def test_comfy_poll_defaults_to_a_quarter_second_and_keeps_the_floor(tmp_db):
    assert image_client.comfy_poll_interval_s({}) == 0.25
    assert image_client.comfy_poll_interval_s({"comfyui_poll_interval_s": None}) == 0.25
    assert image_client.comfy_poll_interval_s({"comfyui_poll_interval_s": 0}) == 0.25
    assert image_client.comfy_poll_interval_s({"comfyui_poll_interval_s": 0.05}) == 0.2
    assert image_client.comfy_poll_interval_s({"comfyui_poll_interval_s": 0.25}) == 0.25
    assert image_client.comfy_poll_interval_s({"comfyui_poll_interval_s": 1.5}) == 1.5
    with db.session() as conn:
        assert image_client.effective_settings(conn)["comfyui_poll_interval_s"] == 0.25


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
    assert (result["width"], result["height"]) == (1024, 1024)
    assert (tmp_path / "adv-1" / "img-1.png").read_bytes() == b"\x89PNG-comfy"
    graph = capture["graph"]
    nodes = _nodes(graph)
    encoder = nodes["TextEncodeQwenImage21"]["inputs"]
    assert encoder["prompt"] == "a knight in the rain"
    assert encoder["negative_prompt"] == "bad anatomy, extra limbs"
    assert nodes["UNETLoader"]["inputs"]["unet_name"] == (
        "fineviewQWEN_qw21TurboFixed_3299367.safetensors")
    assert nodes["CLIPLoader"]["inputs"] == {
        "clip_name": "qwen3vl_8b_int8_convrot.safetensors",
        "type": "qwen_image", "device": "cpu"}
    assert nodes["VAELoader"]["inputs"]["vae_name"] == (
        "texture_fix_vae_for_qwen_image_2.1_bf16.safetensors")
    assert nodes["EmptyLatentImage"]["inputs"]["width"] == 1024
    assert nodes["ModelSamplingAuraFlow"]["inputs"]["shift"] == 1.31
    assert nodes["ManualSigmas"]["inputs"]["sigmas"] == (
        "1.0, 0.978453, 0.95418, 0.926626, 0.89508, "
        "0.845148, 0.704534, 0.414568, 0.0")
    assert nodes["KSamplerSelect"]["inputs"]["sampler_name"] == "euler"
    sampler = nodes["SamplerCustom"]["inputs"]
    assert sampler["positive"] == ["4", 0]
    assert sampler["negative"] == ["4", 1]
    assert sampler["cfg"] == 1.0
    assert sampler["add_noise"] is True
    assert capture["view_params"]["filename"] == "aidungeon_0001.png"


def test_comfyui_honors_fixed_seed_and_randomizes_negative_seed(tmp_path, monkeypatch):
    fixed = _cfg(tmp_path, seed=123)
    graph = image_client._comfy_qwen_graph(
        fixed, "same scene", [], width=1024, height=1024)
    assert _nodes(graph)["SamplerCustom"]["inputs"]["noise_seed"] == 123
    other = dict(fixed, seed=124)
    assert image_client.request_key(fixed, "same scene") != image_client.request_key(
        other, "same scene")

    monkeypatch.setattr(image_client.random, "randint", lambda low, high: 456)
    random_cfg = _cfg(tmp_path, seed=-1)
    graph = image_client._comfy_qwen_graph(
        random_cfg, "same scene", [], width=1024, height=1024)
    assert _nodes(graph)["SamplerCustom"]["inputs"]["noise_seed"] == 456


def test_comfyui_uploads_every_identity_reference_into_native_encoder(tmp_path):
    capture: dict = {}
    image_client.generate_image(
        _cfg(tmp_path), "same person", adventure_id="adv-1", image_id="img-2",
        reference_images=[b"\x89PNG-one", b"\x89PNG-two"],
        transport=_transport(capture))

    assert len(capture["uploads"]) == 2
    graph = capture["graph"]
    assert len(_of_type(graph, "LoadImage")) == 2
    encoder = _nodes(graph)["TextEncodeQwenImage21"]["inputs"]
    assert encoder["images"]["image_1"] == ["20", 0]
    assert encoder["images"]["image_2"] == ["21", 0]
    sampler = _nodes(graph)["SamplerCustom"]["inputs"]
    assert sampler["latent_image"] == ["6", 0]


def test_comfyui_portrait_edit_uses_encoder_latent(tmp_path):
    capture: dict = {}
    image_client.generate_image(
        _cfg(tmp_path), "same person", adventure_id="adv-1", image_id="img-3",
        init_image=b"\x89PNG-anchor", transport=_transport(capture))

    nodes = _nodes(capture["graph"])
    assert nodes["SamplerCustom"]["inputs"]["latent_image"] == ["4", 2]
    assert "EmptyLatentImage" not in nodes
    assert nodes["TextEncodeQwenImage21"]["inputs"]["images"]["image_1"] == ["20", 0]


def test_other_providers_reject_visual_references(tmp_path):
    with pytest.raises(image_client.ImageError, match="require ComfyUI Qwen"):
        image_client.generate_image(
            _cfg(tmp_path, provider="sdwebui"), "same person",
            adventure_id="adv-1", image_id="img-4",
            reference_images=[b"\x89PNG-one"])


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
    assert first != image_client.request_key(
        _cfg(tmp_path, comfyui_sigmas=[1.0, 0.0]), "a knight",
        reference_images=[b"one"])


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

def test_jobs_interrupt_running_comfyui_render(client, tmp_path, monkeypatch):
    called: list[str] = []
    monkeypatch.setattr(
        jobs.image_client, "effective_settings", lambda conn: _cfg(tmp_path))
    monkeypatch.setattr(
        jobs.image_client, "interrupt_sdwebui",
        lambda cfg, transport: called.append(cfg["provider"]))

    assert jobs.interrupt_image({"id": "job-1", "type": "see_image"}) is True
    assert called == ["comfyui"]


def _klein(tmp_path, **extra):
    cfg = _cfg(
        tmp_path,
        comfyui_engine="flux2_klein",
        comfyui_flux2={
            "unet": "flux-2-klein-4b-fp8.safetensors",
            "clip": "qwen_3_4b.safetensors",
            "clip_type": "flux2",
            "clip_device": "default",
            "vae": "flux2-vae.safetensors",
            "weight_dtype": "default",
            "steps": 4,
            "cfg": 1.0,
            "sampler": "euler",
            "megapixels": 1.0,
            "upscale_method": "nearest-exact",
            "resolution_steps": 1,
            "max_references": 4,
        },
    )
    cfg.update(extra)
    return cfg


def _refuse_transport(request: httpx.Request) -> httpx.Response:
    raise AssertionError(f"comfy should not be called for {request.url.path}")


def test_qwen_request_key_ignores_flux2_block(tmp_path):
    base = _cfg(tmp_path)
    tagged = _cfg(
        tmp_path,
        comfyui_engine="qwen",
        comfyui_flux2={"unet": "flux-2-klein-4b-fp8.safetensors"},
    )
    prompt = "a knight"
    refs = [b"one"]
    assert image_client.request_key(base, prompt, reference_images=refs) == (
        image_client.request_key(tagged, prompt, reference_images=refs))


def test_flux2_klein_graph_uses_official_canvas_and_scheduler(tmp_path):
    capture: dict = {}
    result = image_client.generate_image(
        _klein(tmp_path), "a knight in the rain", adventure_id="adv-1",
        image_id="klein-1", width=832, height=1216,
        transport=_transport(capture))

    assert (result["width"], result["height"]) == (832, 1216)
    graph = capture["graph"]
    nodes = _nodes(graph)
    assert "TextEncodeQwenImage21" not in nodes
    assert "EmptyLatentImage" not in nodes
    assert "ManualSigmas" not in nodes
    assert "ModelSamplingAuraFlow" not in nodes
    assert "SamplerCustom" not in nodes
    assert "LTXVImgToVideo" not in nodes
    assert "Wan22ImageToVideoLatent" not in nodes
    assert "CheckpointLoaderSimple" not in nodes
    assert "fineviewQWEN_qw21TurboFixed_3299367.safetensors" not in json.dumps(graph)
    assert nodes["UNETLoader"]["inputs"] == {
        "unet_name": "flux-2-klein-4b-fp8.safetensors",
        "weight_dtype": "default",
    }
    assert nodes["CLIPLoader"]["inputs"] == {
        "clip_name": "qwen_3_4b.safetensors",
        "type": "flux2",
        "device": "default",
    }
    assert nodes["VAELoader"]["inputs"]["vae_name"] == "flux2-vae.safetensors"
    assert nodes["CLIPTextEncode"]["inputs"]["text"] == "a knight in the rain"
    assert nodes["ConditioningZeroOut"]["inputs"]["conditioning"] == ["4", 0]
    latent = nodes["EmptyFlux2LatentImage"]["inputs"]
    schedule = nodes["Flux2Scheduler"]["inputs"]
    assert (latent["width"], latent["height"], latent["batch_size"]) == (832, 1216, 1)
    assert (schedule["width"], schedule["height"], schedule["steps"]) == (832, 1216, 4)
    assert nodes["KSamplerSelect"]["inputs"]["sampler_name"] == "euler"
    assert nodes["CFGGuider"]["inputs"]["cfg"] == 1.0
    assert nodes["CFGGuider"]["inputs"]["positive"] == ["4", 0]
    assert nodes["CFGGuider"]["inputs"]["negative"] == ["5", 0]
    sampler = nodes["SamplerCustomAdvanced"]["inputs"]
    assert sampler["latent_image"] == ["6", 0]
    assert sampler["sigmas"] == ["7", 0]
    assert sampler["guider"] == ["10", 0]
    assert sampler["noise"] == ["9", 0]
    assert sampler["sampler"] == ["8", 0]


def test_flux2_klein_chains_references_on_both_conditionings(tmp_path):
    capture: dict = {}
    image_client.generate_image(
        _klein(tmp_path, seed=7), "same person", adventure_id="adv-1",
        image_id="klein-2", width=1024, height=1024,
        init_image=b"\x89PNG-anchor",
        reference_images=[b"\x89PNG-one", b"\x89PNG-two"],
        transport=_transport(capture))

    graph = capture["graph"]
    assert len(capture["uploads"]) == 3
    assert len(_of_type(graph, "LoadImage")) == 3
    assert len(_of_type(graph, "ImageScaleToTotalPixels")) == 3
    assert len(_of_type(graph, "VAEEncode")) == 3
    assert len(_of_type(graph, "ReferenceLatent")) == 6
    assert graph["20"]["inputs"]["image"] == "aidungeon-ref-1.png"
    assert graph["21"]["inputs"]["image"] == ["20", 0]
    assert graph["21"]["inputs"]["upscale_method"] == "nearest-exact"
    assert graph["21"]["inputs"]["megapixels"] == 1.0
    assert graph["21"]["inputs"]["resolution_steps"] == 1
    assert graph["22"]["inputs"] == {"pixels": ["21", 0], "vae": ["3", 0]}
    assert graph["23"]["inputs"] == {
        "conditioning": ["4", 0], "latent": ["22", 0]}
    assert graph["24"]["inputs"] == {
        "conditioning": ["5", 0], "latent": ["22", 0]}
    assert graph["28"]["inputs"]["conditioning"] == ["23", 0]
    assert graph["29"]["inputs"]["conditioning"] == ["24", 0]
    assert graph["33"]["inputs"]["conditioning"] == ["28", 0]
    assert graph["34"]["inputs"]["conditioning"] == ["29", 0]
    guider = graph["10"]["inputs"]
    assert guider["positive"] == ["33", 0]
    assert guider["negative"] == ["34", 0]
    assert graph["11"]["inputs"]["latent_image"] == ["6", 0]
    assert graph["9"]["inputs"]["noise_seed"] == 7
    assert "TextEncodeQwenImage21" not in _nodes(graph)


def test_flux2_reference_cap_is_four_and_matches_the_hash(tmp_path):
    capture: dict = {}
    refs = [b"r1", b"r2", b"r3", b"r4", b"r5"]
    image_client.generate_image(
        _klein(tmp_path), "group", adventure_id="adv-1", image_id="klein-3",
        init_image=b"anchor", reference_images=refs,
        transport=_transport(capture))
    assert len(capture["uploads"]) == 4
    assert len(_of_type(capture["graph"], "LoadImage")) == 4

    cfg = _klein(tmp_path)
    capped = image_client.request_key(
        cfg, "group", init_image=b"anchor", reference_images=refs)
    same_tail = image_client.request_key(
        cfg, "group", init_image=b"anchor",
        reference_images=[b"r1", b"r2", b"r3", b"other"])
    shorter = image_client.request_key(
        cfg, "group", init_image=b"anchor",
        reference_images=[b"r1", b"r2"])
    assert capped == same_tail
    assert capped != shorter
    qwen = image_client.request_key(
        _cfg(tmp_path), "group", init_image=b"anchor", reference_images=refs)
    assert qwen != capped

def test_flux2_request_key_covers_execution_parameters(tmp_path):
    base = _klein(tmp_path)
    changed = _klein(tmp_path)
    changed["comfyui_flux2"]["weight_dtype"] = "fp8_e4m3fn"
    assert image_client.request_key(base, "same") != image_client.request_key(changed, "same")



def test_flux2_canvas_invariant_still_rejects_unaligned_size():
    with pytest.raises(image_client.ImageError, match="múltiplos de 16"):
        image_client._validate_flux2_canvas(860, 820)


@pytest.mark.parametrize(("width", "height"), [
    (1024, 1024),
    (832, 1216),
    (1344, 768),
    (800, 720),
])
def test_flux2_keeps_canvases_already_aligned(tmp_path, width, height):
    capture: dict = {}
    cfg = _klein(tmp_path)
    result = image_client.generate_image(
        cfg, "a knight", adventure_id="adv-1", image_id=f"aligned-{width}",
        width=width, height=height, transport=_transport(capture))

    assert (result["width"], result["height"]) == (width, height)
    nodes = _nodes(capture["graph"])
    latent = nodes["EmptyFlux2LatentImage"]["inputs"]
    schedule = nodes["Flux2Scheduler"]["inputs"]
    assert (latent["width"], latent["height"]) == (width, height)
    assert (schedule["width"], schedule["height"]) == (width, height)
    assert image_client.request_key(cfg, "a knight", width=width, height=height) != (
        image_client.request_key(cfg, "a knight", width=860, height=820))


def test_flux2_snaps_legacy_portrait_anchor_to_848x816(tmp_path):
    capture: dict = {}
    anchor = b"\x89PNG-anchor"
    cfg = _klein(tmp_path)
    result = image_client.generate_image(
        cfg, "the same thief", adventure_id="adv-1", image_id="legacy-portrait",
        width=860, height=820, init_image=anchor, transport=_transport(capture))

    assert (result["width"], result["height"]) == (848, 816)
    assert len(capture["uploads"]) == 1
    nodes = _nodes(capture["graph"])
    latent = nodes["EmptyFlux2LatentImage"]["inputs"]
    schedule = nodes["Flux2Scheduler"]["inputs"]
    assert (latent["width"], latent["height"], latent["batch_size"]) == (848, 816, 1)
    assert (schedule["width"], schedule["height"], schedule["steps"]) == (848, 816, 4)
    assert nodes["SamplerCustomAdvanced"]["inputs"]["latent_image"] == ["6", 0]
    assert "EmptyLatentImage" not in nodes

    assert image_client.request_key(
        cfg, "the same thief", width=860, height=820, init_image=anchor) == (
        image_client.request_key(
            cfg, "the same thief", width=848, height=816, init_image=anchor))
    assert image_client.request_key(
        cfg, "the same thief", width=860, height=820) != (
        image_client.request_key(
            cfg, "the same thief", width=832, height=816))


def test_flux2_resolves_missing_canvas_then_snaps_and_qwen_does_not(tmp_path):
    capture: dict = {}
    flux = _klein(tmp_path, comfyui_width=860, comfyui_height=820)
    result = image_client.generate_image(
        flux, "defaults", adventure_id="adv-1", image_id="flux-defaults",
        width=None, height=0, transport=_transport(capture))
    assert (result["width"], result["height"]) == (848, 816)
    assert (
        _nodes(capture["graph"])["EmptyFlux2LatentImage"]["inputs"]["width"],
        _nodes(capture["graph"])["EmptyFlux2LatentImage"]["inputs"]["height"],
    ) == (848, 816)
    assert image_client.request_key(flux, "defaults", width=0, height=None) == (
        image_client.request_key(flux, "defaults", width=848, height=816))

    omitted = _klein(tmp_path, comfyui_width=0, comfyui_height=None)
    omitted_capture: dict = {}
    omitted_result = image_client.generate_image(
        omitted, "defaults", adventure_id="adv-1", image_id="flux-1024",
        transport=_transport(omitted_capture))
    assert (omitted_result["width"], omitted_result["height"]) == (1024, 1024)
    assert image_client.request_key(omitted, "defaults") == (
        image_client.request_key(omitted, "defaults", width=1024, height=1024))

    qwen_capture: dict = {}
    qwen = _cfg(tmp_path, comfyui_width=860, comfyui_height=820)
    qwen_result = image_client.generate_image(
        qwen, "defaults", adventure_id="adv-1", image_id="qwen-legacy",
        width=860, height=820, transport=_transport(qwen_capture))
    assert (qwen_result["width"], qwen_result["height"]) == (860, 820)
    qwen_nodes = _nodes(qwen_capture["graph"])
    empty = qwen_nodes["EmptyLatentImage"]["inputs"]
    assert (empty["width"], empty["height"]) == (860, 820)
    assert "EmptyFlux2LatentImage" not in qwen_nodes
    assert image_client.request_key(qwen, "defaults", width=860, height=820) != (
        image_client.request_key(qwen, "defaults", width=848, height=816))
    assert image_client.request_key(qwen, "defaults") == (
        image_client.request_key(qwen, "defaults", width=860, height=820))


_MP4 = b"\x00\x00\x00\x18ftypisom\x00\x00\x00\x00isomiso2"


def _png(width: int, height: int) -> bytes:
    return (
        b"\x89PNG\r\n\x1a\n\x00\x00\x00\x0dIHDR"
        + width.to_bytes(4, "big")
        + height.to_bytes(4, "big")
    )


def _polling_transport(capture: dict, *, ready_after: int,
                       motion: bool = False) -> httpx.MockTransport:
    capture.setdefault("polls", 0)

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/prompt":
            capture["graph"] = json.loads(request.content)["prompt"]
            return httpx.Response(200, json={"prompt_id": "pid-1", "number": 1})
        if path == "/history/pid-1":
            capture["polls"] += 1
            if capture["polls"] < ready_after:
                return httpx.Response(200, json={})
            if motion:
                outputs = {"14": {"videos": [{
                    "filename": "aidungeon_0001.mp4",
                    "subfolder": "", "type": "output"}]}}
            else:
                outputs = {"16": {"images": [{
                    "filename": "aidungeon_0001.png",
                    "subfolder": "", "type": "output"}]}}
            return httpx.Response(200, json={"pid-1": {
                "status": {"status_str": "success", "completed": True},
                "outputs": outputs}})
        if path == "/view":
            body = _MP4 if motion else b"\x89PNG-comfy"
            return httpx.Response(200, content=body)
        if path == "/upload/image":
            return httpx.Response(200, json={
                "name": "aidungeon-ref-1.png", "subfolder": "", "type": "input"})
        return httpx.Response(404)

    return httpx.MockTransport(handler)


def test_comfy_progress_runs_after_submit_and_each_poll(tmp_path, monkeypatch):
    monkeypatch.setattr(image_client.time, "sleep", lambda _seconds: None)
    capture: dict = {}
    stamps: list[int] = []

    def progress() -> None:
        stamps.append(capture["polls"])

    image_client.generate_image(
        _cfg(tmp_path), "a knight", adventure_id="adv-1", image_id="progress",
        transport=_polling_transport(capture, ready_after=3),
        progress=progress)

    assert stamps == [0, 1, 2, 3]
    assert capture["polls"] == 3

    clip_capture: dict = {}
    clip_stamps: list[int] = []

    def clip_progress() -> None:
        clip_stamps.append(clip_capture["polls"])

    clip = image_client.generate_clip(
        _cfg(tmp_path, comfyui_video={"width": 860, "height": 820}),
        "camera pushes in", adventure_id="adv-1", image_id="clip-progress",
        source_png=_png(64, 32),
        transport=_polling_transport(clip_capture, ready_after=2, motion=True),
        progress=clip_progress)

    assert clip_stamps == [0, 1, 2]
    assert (clip["width"], clip["height"]) == (860, 820)
    latent = _nodes(clip_capture["graph"])["LTXVImgToVideo"]["inputs"]
    assert (latent["width"], latent["height"]) == (860, 820)


def test_comfy_progress_error_aborts_before_the_next_poll(tmp_path):
    capture: dict = {}

    def progress() -> None:
        raise RuntimeError("stop")

    with pytest.raises(RuntimeError, match="stop"):
        image_client.generate_image(
            _cfg(tmp_path), "a knight", adventure_id="adv-1", image_id="abort",
            transport=_polling_transport(capture, ready_after=1),
            progress=progress)
    assert capture["polls"] == 0


def test_unknown_image_engine_fails(tmp_path):
    with pytest.raises(image_client.ImageError, match="image.engine desconhecido"):
        image_client.generate_image(
            _cfg(tmp_path, comfyui_engine="foo"), "a knight",
            adventure_id="adv-1", image_id="odd",
            transport=httpx.MockTransport(_refuse_transport))


def test_flux2_health_reports_the_klein_unet(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/system_stats":
            return httpx.Response(200, json={"system": {}})
        return httpx.Response(404)

    health = image_client.health(
        _klein(tmp_path), httpx.MockTransport(handler))
    assert health["status"] == "ok"
    assert health["engine"] == "flux2_klein"
    assert health["unet"] == "flux-2-klein-4b-fp8.safetensors"
