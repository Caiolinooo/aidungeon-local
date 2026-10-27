"""Clipe Wan2.2 TI2V: grafo Comfy, fila, cache e o MP4 servido."""

from __future__ import annotations

import json
import struct
import threading
import zlib

import httpx

from app import auth as auth_core
from app import db, deps, events, image_client, jobs
from conftest import TEST_AUTH_SECRET, make_adventure
from test_v3 import PNG_B64, PNG_BYTES, use_tmp_storage

MP4 = b"\x00\x00\x00\x18ftypisom\x00\x00\x00\x00mp41isom"
WAN_UNET = "Wan2.2-TI2V-5B-Q8_0.gguf"
IMAGE_UNET = "fineviewQWEN_qw21TurboFixed_3299367.safetensors"


def _png(width: int, height: int) -> bytes:
    def chunk(tag: bytes, data: bytes) -> bytes:
        crc = zlib.crc32(tag + data) & 0xFFFFFFFF
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", crc)

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    raw = b"".join(b"\x00" + bytes(width * 3) for _ in range(height))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr)
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


def _cfg(tmp_path, **extra) -> dict:
    cfg = {
        "provider": "sdwebui",
        "sdwebui_base_url": "http://127.0.0.1:7861",
        "comfyui_base_url": "http://127.0.0.1:8188",
        "comfyui_unet": IMAGE_UNET,
        "comfyui_clip": "qwen3vl_8b_int8_convrot.safetensors",
        "comfyui_vae": "texture_fix_vae_for_qwen_image_2.1_bf16.safetensors",
        "comfyui_poll_interval_s": 0.2,
        "timeout_seconds": 300,
        "seed": 7,
        "storage_dir": str(tmp_path),
        "comfyui_video": {
            "unet": WAN_UNET,
            "clip": "umt5_xxl_fp8_e4m3fn_scaled.safetensors",
            "vae": "wan2.2_vae.safetensors",
            "frames": 121,
            "fps": 24,
            "steps": 20,
            "cfg": 5,
            "shift": 8,
            "timeout_seconds": 30,
            "width": 1280,
            "height": 704,
            "vertical_width": 704,
            "vertical_height": 1280,
        },
    }
    cfg.update(extra)
    return cfg


def _history(filename: str, *, key: str = "videos", images: bool = False) -> dict:
    if images:
        outputs = {"16": {"images": [{"filename": "frame.png", "subfolder": "",
                                      "type": "output"}]}}
    else:
        outputs = {"12": {key: [{"filename": filename, "subfolder": "",
                                 "type": "output"}]}}
    return {"pid-1": {"status": {"status_str": "success", "completed": True},
                      "outputs": outputs}}


def _transport(capture: dict, *, history: dict | None = None,
               view: bytes = MP4, unload: str | int = 200) -> httpx.MockTransport:
    capture.setdefault("paths", [])

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        capture["paths"].append(path)
        if path == "/prompt":
            capture["graph"] = json.loads(request.content)["prompt"]
            return httpx.Response(200, json={"prompt_id": "pid-1", "node_errors": {}})
        if path == "/history/pid-1":
            body = history if history is not None else _history("aidungeon_0001.mp4")
            if not body:
                body = {"pid-1": {"status": {"status_str": "success"}, "outputs": {}}}
            return httpx.Response(200, json=body)
        if path == "/view":
            capture["view"] = dict(request.url.params)
            return httpx.Response(200, content=view)
        if path == "/upload/image":
            return httpx.Response(200, json={"name": "source.png", "subfolder": "",
                                             "type": "input"})
        if path == "/free":
            return httpx.Response(200, json={"ok": True})
        if path == "/sdapi/v1/unload-checkpoint":
            if unload == "refused":
                raise httpx.ConnectError("Connection refused")
            if isinstance(unload, int) and unload != 200:
                return httpx.Response(unload, text="forge busy")
            return httpx.Response(200, json={})
        if path == "/interrupt":
            return httpx.Response(200, json={})
        if path == "/sdapi/v1/txt2img":
            return httpx.Response(200, json={"images": [PNG_B64]})
        return httpx.Response(404, text=path)

    return httpx.MockTransport(handler)


def _store_png(adventure_id: str, tmp_path, raw: bytes = PNG_BYTES, **fields) -> dict:
    path = tmp_path / f"src-{db.new_id()}.png"
    path.write_bytes(raw)
    with db.session() as conn:
        return db.insert_image(
            conn, adventure_id=adventure_id, action_seq=-1, file_path=str(path),
            kind=fields.get("kind", "scene"), prompt="source",
            media_type=fields.get("media_type", "image/png"),
            width=fields.get("width"), height=fields.get("height"),
            duration_ms=fields.get("duration_ms"),
            source_image_id=fields.get("source_image_id", ""),
        )


def test_raster_insert_keeps_png_defaults(tmp_db, tmp_path):
    adv = make_adventure()
    with db.session() as conn:
        columns = {row[1]: row for row in conn.execute("PRAGMA table_info(images)")}
    assert columns["media_type"][4] == "'image/png'"
    assert columns["duration_ms"][4] is None
    assert columns["source_image_id"][4] == "''"
    image = _store_png(adv["id"], tmp_path)
    assert image["media_type"] == "image/png"
    assert image["duration_ms"] is None
    assert image["source_image_id"] == ""


def test_file_video_block_does_not_replace_the_image_unet(tmp_db):
    with db.session() as conn:
        cfg = image_client.effective_settings(conn)
    assert cfg["provider"] == "sdwebui"
    assert cfg["comfyui_unet"] == IMAGE_UNET
    video = cfg["comfyui_video"]
    assert video["unet"] == WAN_UNET
    assert video["clip"] == "umt5_xxl_fp8_e4m3fn_scaled.safetensors"
    assert video["vae"] == "wan2.2_vae.safetensors"
    assert (video["frames"], video["fps"], video["steps"]) == (121, 24, 20)
    assert (video["cfg"], video["shift"], video["timeout_seconds"]) == (5, 8, 1800)
    assert (video["width"], video["height"]) == (1280, 704)
    assert (video["vertical_width"], video["vertical_height"]) == (704, 1280)


def test_wan_graph_uses_video_unet_and_frees_around_the_prompt(tmp_path):
    cfg = _cfg(tmp_path)
    before = (cfg["provider"], cfg["comfyui_unet"], cfg["comfyui_video"]["unet"])
    capture: dict = {}
    result = image_client.generate_clip(
        cfg, "camera pushes in", adventure_id="adv", image_id="clip-1",
        source_png=PNG_BYTES, transport=_transport(capture))
    assert (cfg["provider"], cfg["comfyui_unet"], cfg["comfyui_video"]["unet"]) == before
    assert capture["paths"] == [
        "/free", "/sdapi/v1/unload-checkpoint", "/upload/image", "/prompt",
        "/history/pid-1", "/view", "/free",
    ]
    graph = capture["graph"]
    loaders = [node for node in graph.values() if node["class_type"] == "UnetLoaderGGUF"]
    assert len(loaders) == 1
    assert loaders[0]["inputs"]["unet_name"] == WAN_UNET
    assert all(node["class_type"] != "UNETLoader" for node in graph.values())
    assert IMAGE_UNET not in json.dumps(graph)
    clip = next(node for node in graph.values() if node["class_type"] == "CLIPLoader")
    assert clip["inputs"]["clip_name"] == "umt5_xxl_fp8_e4m3fn_scaled.safetensors"
    assert clip["inputs"]["type"] == "wan"
    latent = next(node for node in graph.values()
                  if node["class_type"] == "Wan22ImageToVideoLatent")
    assert (latent["inputs"]["width"], latent["inputs"]["height"],
            latent["inputs"]["length"]) == (1280, 704, 121)
    sampler = next(node for node in graph.values() if node["class_type"] == "KSampler")
    assert sampler["inputs"]["seed"] == 7
    assert sampler["inputs"]["steps"] == 20
    assert sampler["inputs"]["cfg"] == 5
    assert sampler["inputs"]["sampler_name"] == "uni_pc"
    shift = next(node for node in graph.values()
                 if node["class_type"] == "ModelSamplingSD3")
    assert shift["inputs"]["shift"] == 8
    video = next(node for node in graph.values() if node["class_type"] == "CreateVideo")
    assert video["inputs"]["fps"] == 24
    saved = tmp_path / "adv" / "clip-1.mp4"
    assert saved.read_bytes() == MP4
    assert not saved.with_name(saved.name + ".partial").exists()
    assert result["media_type"] == "video/mp4"
    assert result["duration_ms"] == 5042
    assert (result["width"], result["height"]) == (1280, 704)

    tall: dict = {}
    vertical = image_client.generate_clip(
        cfg, "looks up", adventure_id="adv", image_id="clip-v",
        source_png=_png(8, 16), transport=_transport(tall))
    latent_v = next(node for node in tall["graph"].values()
                    if node["class_type"] == "Wan22ImageToVideoLatent")
    assert (latent_v["inputs"]["width"], latent_v["inputs"]["height"]) == (704, 1280)
    assert (vertical["width"], vertical["height"]) == (704, 1280)


def test_gifs_fallback_is_saved_as_mp4_and_images_alone_fail(tmp_path):
    cfg = _cfg(tmp_path)
    gifs: dict = {}
    result = image_client.generate_clip(
        cfg, "turns", adventure_id="adv", image_id="from-gif",
        source_png=PNG_BYTES,
        transport=_transport(gifs, history=_history("legacy.mp4", key="gifs")))
    assert gifs["view"]["filename"] == "legacy.mp4"
    assert result["file_path"].endswith("from-gif.mp4")
    assert (tmp_path / "adv" / "from-gif.mp4").read_bytes() == MP4

    both: dict = {}
    image_client.generate_clip(
        cfg, "turns", adventure_id="adv", image_id="prefer-video",
        source_png=PNG_BYTES,
        transport=_transport(both, history={
            "pid-1": {"status": {"status_str": "success"}, "outputs": {
                "12": {
                    "videos": [{"filename": "real.mp4", "type": "output"}],
                    "gifs": [{"filename": "legacy.gif", "type": "output"}],
                },
            }}}))
    assert both["view"]["filename"] == "real.mp4"

    images: dict = {}
    try:
        image_client.generate_clip(
            cfg, "turns", adventure_id="adv", image_id="frames",
            source_png=PNG_BYTES,
            transport=_transport(images, history=_history("frame.png", images=True),
                                 view=b"\x89PNG-not-video"))
    except image_client.ImageError as exc:
        assert "sem vídeo" in exc.message
    else:
        raise AssertionError("history with only images must fail")
    assert "/view" not in images["paths"]
    assert images["paths"][0] == "/free" and images["paths"][-1] == "/free"
    assert not (tmp_path / "adv" / "frames.mp4").exists()
    assert not (tmp_path / "adv" / "frames.mp4.partial").exists()

    partial: dict = {}
    try:
        image_client.generate_clip(
            cfg, "turns", adventure_id="adv", image_id="renamed",
            source_png=PNG_BYTES,
            transport=_transport(partial, view=b"\x89PNG\r\n\x1a\nfake"))
    except image_client.ImageError as exc:
        assert "não é um MP4" in exc.message
    else:
        raise AssertionError("png bytes must not be stored as mp4")
    assert list(tmp_path.rglob("*.partial")) == []
    assert not (tmp_path / "adv" / "renamed.mp4").exists()


def test_clip_cache_tracks_render_settings_but_not_timeout(tmp_path):
    cfg = _cfg(tmp_path)
    base = image_client.clip_request_key(cfg, "  walks   home ", PNG_BYTES)
    assert base == image_client.clip_request_key(cfg, "walks home", PNG_BYTES)

    slower = _cfg(tmp_path)
    slower["comfyui_video"]["timeout_seconds"] = 90
    assert image_client.clip_request_key(slower, "walks home", PNG_BYTES) == base

    refined = _cfg(tmp_path)
    refined["comfyui_video"]["steps"] += 1
    assert image_client.clip_request_key(refined, "walks home", PNG_BYTES) != base

def test_forge_unload_errors_and_video_timeout(tmp_path):
    cfg = _cfg(tmp_path)
    refused: dict = {}
    image_client.generate_clip(
        cfg, "walks", adventure_id="adv", image_id="refused",
        source_png=PNG_BYTES, transport=_transport(refused, unload="refused"))
    assert refused["paths"].index("/sdapi/v1/unload-checkpoint") < refused["paths"].index("/prompt")

    http_error: dict = {}
    try:
        image_client.generate_clip(
            cfg, "walks", adventure_id="adv", image_id="busy",
            source_png=PNG_BYTES, transport=_transport(http_error, unload=500))
    except image_client.ImageError as exc:
        assert "unload-checkpoint" in exc.message
    else:
        raise AssertionError("forge HTTP error must abort")
    assert "/prompt" not in http_error["paths"]
    assert "/upload/image" not in http_error["paths"]
    assert http_error["paths"][0] == "/free" and http_error["paths"][-1] == "/free"

    remote = _cfg(tmp_path, sdwebui_base_url="http://forge.example:7861")
    far: dict = {}
    image_client.generate_clip(
        remote, "walks", adventure_id="adv", image_id="remote",
        source_png=PNG_BYTES, transport=_transport(far))
    assert "/sdapi/v1/unload-checkpoint" not in far["paths"]
    assert "/prompt" in far["paths"]

    comfy = _cfg(tmp_path, provider="comfyui")
    native: dict = {}
    image_client.generate_clip(
        comfy, "walks", adventure_id="adv", image_id="native",
        source_png=PNG_BYTES, transport=_transport(native))
    assert "/sdapi/v1/unload-checkpoint" not in native["paths"]
    assert comfy["provider"] == "comfyui"
    assert comfy["comfyui_unet"] == IMAGE_UNET

    empty: dict = {}
    try:
        image_client.generate_clip(
            cfg, "walks", adventure_id="adv", image_id="empty",
            source_png=b"", transport=_transport(empty))
    except image_client.ImageError as exc:
        assert "required" in exc.message
    else:
        raise AssertionError("missing png must fail before comfy")
    assert empty["paths"] == []

    slow = _cfg(tmp_path)
    slow["comfyui_video"] = {**slow["comfyui_video"], "timeout_seconds": 0}
    timed: dict = {}
    try:
        image_client.generate_clip(
            slow, "walks", adventure_id="adv", image_id="slow",
            source_png=PNG_BYTES,
            transport=_transport(timed, history={}))
    except image_client.ImageError as exc:
        assert "timeout (0s)" in exc.message
        assert "300" not in exc.message
    else:
        raise AssertionError("empty history must use the video timeout")


def _comfy_and_sd(order: list[str], release: threading.Event | None = None,
                  entered: threading.Event | None = None,
                  interrupts: list | None = None) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        order.append(path)
        if path == "/sdapi/v1/txt2img":
            return httpx.Response(200, json={"images": [PNG_B64]})
        if path == "/free" or path == "/sdapi/v1/unload-checkpoint":
            return httpx.Response(200, json={})
        if path == "/upload/image":
            return httpx.Response(200, json={"name": "source.png", "type": "input"})
        if path == "/prompt":
            if entered is not None and release is not None:
                entered.set()
                release.wait(5)
            return httpx.Response(200, json={"prompt_id": "pid-clip", "node_errors": {}})
        if path == "/interrupt":
            if interrupts is not None:
                interrupts.append(path)
            if release is not None:
                release.set()
            return httpx.Response(200, json={})
        if path.startswith("/history/"):
            return httpx.Response(200, json={"pid-clip": {
                "status": {"status_str": "success", "completed": True},
                "outputs": {"12": {"videos": [
                    {"filename": "out.mp4", "subfolder": "", "type": "output"}]}},
            }})
        if path == "/view":
            return httpx.Response(200, content=MP4)
        return httpx.Response(404, text=path)

    return httpx.MockTransport(handler)


def test_cancel_does_not_persist_the_mp4(client, tmp_path):
    use_tmp_storage(tmp_path)
    adv = make_adventure()
    source = _store_png(adv["id"], tmp_path)
    with db.session() as conn:
        job = jobs.enqueue_scene_clip(conn, adv["id"], source["id"], "the door opens")
    entered, release = threading.Event(), threading.Event()
    interrupts: list[str] = []
    order: list[str] = []
    transport = _comfy_and_sd(order, release, entered, interrupts)
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
        kinds = []
        while (event := sub.get(0.05)) is not None:
            kinds.append(event.type)
    assert interrupts == ["/interrupt"]
    assert "/free" in order
    assert "clip_ready" not in kinds
    with db.session() as conn:
        assert db.get_job(conn, job["id"])["status"] == "canceled"
        assert conn.execute(
            "SELECT COUNT(*) FROM images WHERE kind='clip'").fetchone()[0] == 0
    assert list(tmp_path.rglob("*.mp4")) == []
    assert list(tmp_path.rglob("*.partial")) == []


def test_narrative_holds_the_clip_behind_see_and_backdrop(client, tmp_path):
    use_tmp_storage(tmp_path)
    adv = make_adventure()
    source = _store_png(adv["id"], tmp_path)
    order: list[str] = []
    transport = _comfy_and_sd(order)
    with db.session() as conn:
        backdrop = jobs.enqueue(conn, "scene_backdrop", adv["id"], "room",
                                {"prompt": "empty room"})
        clip = jobs.enqueue_scene_clip(conn, adv["id"], source["id"], "curtain moves")
        see = db.insert_job(
            conn, adventure_id=adv["id"], type="see_image",
            priority=jobs.PRIORITY_SEE,
            payload={"prompt": "a lantern", "caption": "A lantern."})
    assert clip["priority"] == jobs.PRIORITY_SCENE_CLIP
    assert jobs.PRIORITY_SCENE_CLIP > jobs.PRIORITY_SEE

    assert jobs.process_pending(transport, should_pause=lambda: True) == 1
    with db.session() as conn:
        assert db.get_job(conn, see["id"])["status"] == "done"
        assert db.get_job(conn, backdrop["id"])["status"] == "pending"
        assert db.get_job(conn, clip["id"])["status"] == "pending"
    assert "/prompt" not in order
    before_queue = len(order)

    with events.subscribe(adv["id"]) as sub:
        assert jobs.process_pending(transport) == 2
        ready = []
        while (event := sub.get(0.05)) is not None:
            if event.type == "clip_ready":
                ready.append(event)
    with db.session() as conn:
        assert db.get_job(conn, backdrop["id"])["status"] == "done"
        assert db.get_job(conn, clip["id"])["status"] == "done"
    rest = order[before_queue:]
    assert "/sdapi/v1/txt2img" in rest
    assert rest.index("/sdapi/v1/txt2img") < rest.index("/prompt")
    assert len(ready) == 1
    data = ready[0].data
    assert data["adventure_id"] == adv["id"]
    assert data["job_id"] == clip["id"]
    assert data["source_image_id"] == source["id"]
    assert data["image"]["media_type"] == "video/mp4"
    assert data["image"]["duration_ms"] == 5042
    assert data["image"]["url"] == f"/api/images/{data['image']['id']}"


def test_cache_reuses_the_mp4_and_same_source_supersedes(client, tmp_path):
    use_tmp_storage(tmp_path)
    adv = make_adventure()
    source = _store_png(adv["id"], tmp_path)
    other = _store_png(adv["id"], tmp_path, _png(4, 8))
    order: list[str] = []
    transport = _comfy_and_sd(order)
    with db.session() as conn:
        first = jobs.enqueue_scene_clip(conn, adv["id"], source["id"], "nods")
        second = jobs.enqueue_scene_clip(conn, adv["id"], source["id"], "shakes their head")
        third = jobs.enqueue_scene_clip(conn, adv["id"], other["id"], "nods")
    with db.session() as conn:
        assert db.get_job(conn, first["id"])["status"] == "canceled"
        assert db.get_job(conn, first["id"])["error"] == "superseded"
        assert db.get_job(conn, second["id"])["status"] == "pending"
        assert db.get_job(conn, third["id"])["status"] == "pending"
        assert second["supersede_key"] == f"clip:{adv['id']}:{source['id']}"
        assert third["supersede_key"] != second["supersede_key"]

    assert jobs.process_pending(transport) == 2
    prompts = order.count("/prompt")
    assert prompts == 2
    with db.session() as conn:
        again = jobs.enqueue_scene_clip(conn, adv["id"], source["id"], "shakes their head")
    assert jobs.process_pending(transport) == 1
    assert order.count("/prompt") == prompts
    with db.session() as conn:
        done = db.get_job(conn, again["id"])
        assert done["status"] == "done"
        assert done["result"]["image"]["media_type"] == "video/mp4"
        rows = conn.execute(
            "SELECT id, file_path, reused_from FROM images WHERE kind='clip' AND ref_id=?",
            (source["id"],),
        ).fetchall()
    assert len(rows) == 2
    reused = [row for row in rows if row["reused_from"]]
    original = [row for row in rows if not row["reused_from"]]
    assert len(reused) == 1 and len(original) == 1
    assert reused[0]["reused_from"] == original[0]["id"]
    assert original[0]["file_path"] == reused[0]["file_path"]
    assert original[0]["file_path"].endswith(".mp4")


def test_clip_route_auth_ownership_raster_and_download(client, tmp_path):
    adv = make_adventure()
    source = _store_png(adv["id"], tmp_path)
    png = client.get(f"/api/images/{source['id']}")
    assert png.status_code == 200
    assert png.headers["content-type"].startswith("image/png")
    assert "attachment" not in png.headers.get("content-disposition", "")

    mp4_path = tmp_path / "clip.mp4"
    mp4_path.write_bytes(MP4)
    with db.session() as conn:
        movie = db.insert_image(
            conn, adventure_id=adv["id"], action_seq=-1, file_path=str(mp4_path),
            kind="clip", media_type="video/mp4", duration_ms=5042,
            source_image_id=source["id"], width=1280, height=704,
        )
    inline = client.get(f"/api/images/{movie['id']}")
    assert inline.status_code == 200
    assert inline.headers["content-type"].startswith("video/mp4")
    assert inline.content == MP4
    assert "attachment" not in inline.headers.get("content-disposition", "")
    downloaded = client.get(f"/api/images/{movie['id']}", params={"download": 1})
    disposition = downloaded.headers["content-disposition"]
    assert disposition.startswith("attachment;")
    assert mp4_path.name in disposition

    anon = client.post(
        f"/api/adventures/{adv['id']}/clips",
        json={"image_id": source["id"], "prompt": "waves"},
        headers={"Authorization": ""},
    )
    assert anon.status_code == 401

    foreign = make_adventure(owner="alice")
    foreign_png = _store_png(foreign["id"], tmp_path)
    hidden = client.post(
        f"/api/adventures/{foreign['id']}/clips",
        json={"image_id": foreign_png["id"], "prompt": "waves"},
    )
    assert hidden.status_code == 404

    missing = client.post(
        f"/api/adventures/{adv['id']}/clips",
        json={"image_id": "no-such-image", "prompt": "waves"},
    )
    assert missing.status_code == 404
    video_source = client.post(
        f"/api/adventures/{adv['id']}/clips",
        json={"image_id": movie["id"], "prompt": "waves"},
    )
    assert video_source.status_code == 404
    other = make_adventure()
    stolen = client.post(
        f"/api/adventures/{other['id']}/clips",
        json={"image_id": source["id"], "prompt": "waves"},
    )
    assert stolen.status_code == 404
    broken = _store_png(adv["id"], tmp_path, b"not-a-png")
    assert client.post(
        f"/api/adventures/{adv['id']}/clips",
        json={"image_id": broken["id"], "prompt": "waves"},
    ).status_code == 404
    assert client.post(
        f"/api/adventures/{adv['id']}/clips",
        json={"image_id": source["id"], "prompt": "   "},
    ).status_code == 400

    created = client.post(
        f"/api/adventures/{adv['id']}/clips",
        json={"image_id": source["id"], "prompt": "waves"},
    )
    assert created.status_code == 202, created.text
    with db.session() as conn:
        job = db.get_job(conn, created.json()["job_id"])
    assert job["type"] == "scene_clip"
    assert job["status"] == "pending"
    assert job["priority"] == jobs.PRIORITY_SCENE_CLIP
    assert job["ref_id"] == source["id"]

    alice = auth_core.make_token("alice", TEST_AUTH_SECRET, ttl_h=1)
    owned = client.post(
        f"/api/adventures/{foreign['id']}/clips",
        json={"image_id": foreign_png["id"], "prompt": "waves"},
        headers={"Authorization": f"Bearer {alice}"},
    )
    assert owned.status_code == 202, owned.text
