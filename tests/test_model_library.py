"""Biblioteca dinâmica: busca nos catálogos, entrada no catálogo do runtime,
motores `checkpoint` e `custom`, LoRA, chaves no notebook e download local."""

from __future__ import annotations

import json
import time

import httpx
import pytest

from app import db, deps, image_client, model_library, settings_store
from app import runtime_provisioning as rp

SHA = "ab" * 32


@pytest.fixture()
def admin(client, admin_token):
    client.headers["Authorization"] = f"Bearer {admin_token}"
    return client


def _catalogs(request: httpx.Request) -> httpx.Response:
    host, path = request.url.host, request.url.path
    if host in ("civitai.com", "civitai.red") and path == "/api/v1/models":
        lora = request.url.params.get("types") == "LORA"
        return httpx.Response(200, json={"items": [{
            "id": 7, "name": "Estilo" if lora else "Pony Real", "nsfw": True,
            "type": "LORA" if lora else "Checkpoint",
            "modelVersions": [{
                "id": 70, "name": "v1", "baseModel": "Pony",
                "files": [
                    {"name": "pony real.safetensors", "sizeKB": 1024, "type": "Model",
                     "hashes": {"SHA256": SHA.upper()},
                     "downloadUrl": f"https://{host}/api/download/models/70"},
                    {"name": "treino.ckpt", "sizeKB": 1, "type": "Model",
                     "hashes": {"SHA256": SHA}, "downloadUrl": f"https://{host}/x"},
                ],
            }],
        }]})
    if host == "huggingface.co" and path == "/api/models":
        return httpx.Response(200, json=[{"id": "org/zit", "downloads": 9}])
    if host == "huggingface.co" and path == "/api/models/org/zit/tree/main":
        return httpx.Response(200, json=[
            {"type": "file", "path": "split/zit_fp8.safetensors", "size": 5,
             "lfs": {"oid": SHA, "size": 5}},
            {"type": "file", "path": "README.md", "size": 3},
        ])
    return httpx.Response(404)


def _file(name="pony_real.safetensors", host="civitai.com"):
    return {"name": name, "sha256": SHA, "bytes": 1048576,
            "url": f"https://{host}/api/download/models/70"}


def test_search_reads_the_three_catalogs(admin):
    admin.app.dependency_overrides[deps.get_http_transport] = lambda: httpx.MockTransport(_catalogs)
    for source in ("civitai", "civitai_red"):
        rows = admin.get("/api/models/search", params={
            "source": source, "kind": "image", "q": "pony"}).json()["results"]
        assert rows[0]["family"] == "checkpoint"
        assert rows[0]["base_model"] == "Pony"
        # O pickle (.ckpt) executa código ao abrir: não aparece.
        assert [f["name"] for f in rows[0]["files"]] == ["pony real.safetensors"]
        assert rows[0]["files"][0]["sha256"] == SHA
    hub = admin.get("/api/models/search", params={
        "source": "huggingface", "kind": "image", "q": "zit"}).json()["results"]
    assert hub[0]["id"] == "org/zit" and hub[0]["files"] is None
    files = admin.get("/api/models/files", params={"repo": "org/zit"}).json()["files"]
    assert files == [{
        "name": "zit_fp8.safetensors", "path": "split/zit_fp8.safetensors", "bytes": 5,
        "sha256": SHA, "role": "Model",
        "url": "https://huggingface.co/org/zit/resolve/main/split/zit_fp8.safetensors",
    }]
    assert admin.get("/api/models/search", params={
        "source": "outro", "kind": "image", "q": "pony"}).status_code == 400


def test_only_the_admin_reaches_the_library(client):
    assert client.get("/api/models/library").status_code == 403
    assert client.post("/api/models/library", json={
        "kind": "image", "family": "checkpoint", "file": _file()}).status_code == 403


def test_checkpoint_from_the_search_paints_with_its_lora(admin):
    added = admin.post("/api/models/library", json={
        "kind": "image", "family": "checkpoint", "label": "Pony Real",
        "base_model": "Pony", "file": _file("pony real.safetensors")})
    assert added.status_code == 200, added.text
    model_id = added.json()["id"]
    entry = admin.get("/api/settings/runtime").json()["catalog"]["models"]["image"][model_id]
    assert entry["engine"] == "checkpoint" and entry["user"] is True
    assert entry["artifacts"][0]["dest"] == "models/checkpoints/pony_real.safetensors"
    assert entry["artifacts"][0]["auth_secret"] == "CIVITAI_API_KEY"

    with_lora = admin.post(f"/api/models/library/image/{model_id}/loras", json={
        "file": _file("estilo.safetensors"), "strength": 0.6})
    assert with_lora.json()["id"] == model_id
    admin.patch(f"/api/models/library/image/{model_id}", json={
        "checkpoint": {"steps": 20, "cfg": 5, "sampler": "dpmpp_2m", "scheduler": "karras"}})
    saved = admin.put("/api/settings/runtime", json={"models": {"image": model_id}})
    assert saved.status_code == 200, saved.text

    with db.session() as conn:
        cfg = settings_store.effective_image_settings(conn)
    assert cfg["comfyui_engine"] == "checkpoint"
    graph = image_client._comfy_checkpoint_graph(cfg, "a knight", width=832, height=1216)
    assert graph["1"]["inputs"]["ckpt_name"] == "pony_real.safetensors"
    assert graph["lora1"]["inputs"] == {
        "model": ["1", 0], "clip": ["1", 1], "lora_name": "estilo.safetensors",
        "strength_model": 0.6, "strength_clip": 0.6}
    # Quem lia o modelo e o CLIP do loader passa a ler do LoRA; o VAE não muda.
    assert graph["8"]["inputs"]["model"] == ["lora1", 0]
    assert graph["4"]["inputs"]["clip"] == ["lora1", 1]
    assert graph["9"]["inputs"]["vae"] == ["1", 2]
    assert (graph["8"]["inputs"]["steps"], graph["8"]["inputs"]["sampler_name"]) == (20, "dpmpp_2m")
    # A cena 1920×1080 do jogo cai para a área em que o SDXL foi treinado.
    width, height = image_client._checkpoint_canvas(1920, 1080, 1.0)
    assert width * height <= 1024 * 1024 and width % 8 == 0 and height % 8 == 0

    # Em uso não sai; depois de trocar, sai.
    assert admin.delete(f"/api/models/library/image/{model_id}").status_code == 409
    admin.put("/api/settings/runtime", json={"models": {"image": "flux2_klein"}})
    with db.session() as conn:
        back = settings_store.effective_image_settings(conn)
    assert back["comfyui_engine"] == "flux2_klein" and back["comfyui_loras"] == []
    assert admin.delete(f"/api/models/library/image/{model_id}").status_code == 200


def test_same_file_name_with_other_content_does_not_overwrite(admin):
    first = admin.post("/api/models/library", json={
        "kind": "image", "family": "checkpoint", "label": "Um", "file": _file("model.safetensors")})
    other = {**_file("model.safetensors"), "sha256": "cd" * 32}
    second = admin.post("/api/models/library", json={
        "kind": "image", "family": "checkpoint", "label": "Outro", "file": other})
    catalog = admin.get("/api/settings/runtime").json()["catalog"]["models"]["image"]
    assert first.json()["id"] == "um" and second.json()["id"] == "outro"
    assert catalog["um"]["artifacts"][0]["dest"] == "models/checkpoints/model.safetensors"
    assert catalog["outro"]["artifacts"][0]["dest"] == "models/checkpoints/model-cdcdcdcd.safetensors"
    assert catalog["outro"]["checkpoint"]["checkpoint"] == "model-cdcdcdcd.safetensors"


def test_family_swap_keeps_the_encoder_and_picks_the_loader(admin):
    added = admin.post("/api/models/library", json={
        "kind": "image", "family": "z_image_turbo",
        "file": _file("zit_fp8.safetensors", "huggingface.co")})
    model_id = added.json()["id"]
    entry = admin.get("/api/settings/runtime").json()["catalog"]["models"]["image"][model_id]
    roles = {art["role"]: art["filename"] for art in entry["artifacts"]}
    assert roles == {"unet": "zit_fp8.safetensors", "clip": "qwen_3_4b_fp8_mixed.safetensors",
                     "vae": "ae.safetensors"}
    assert "auth_secret" not in entry["artifacts"][0]
    admin.put("/api/settings/runtime", json={"models": {"image": model_id}})
    with db.session() as conn:
        cfg = settings_store.effective_image_settings(conn)
    graph = image_client._comfy_zimage_graph(cfg, "a knight", width=1024, height=1024)
    assert graph["1"] == {"class_type": "UNETLoader", "inputs": {
        "unet_name": "zit_fp8.safetensors", "weight_dtype": "default"}}
    # LoRA num modelo do catálogo fixo cria uma cópia ajustável e não mexe no original.
    copy_id = admin.post("/api/models/library/image/flux2_klein/loras", json={
        "file": _file("estilo.safetensors")}).json()["id"]
    assert copy_id != "flux2_klein"
    catalog = admin.get("/api/settings/runtime").json()["catalog"]["models"]["image"]
    assert "loras" not in catalog["flux2_klein"]
    assert catalog[copy_id]["loras"] == [{"name": "estilo.safetensors", "strength": 0.8}]


def test_custom_workflow_fills_the_markers(admin):
    graph = {
        "1": {"class_type": "CheckpointLoaderSimple", "inputs": {"ckpt_name": "x.safetensors"}},
        "2": {"class_type": "CLIPTextEncode", "inputs": {
            "clip": ["1", 1], "text": "masterpiece, {{prompt}}"}},
        "3": {"class_type": "EmptyLatentImage", "inputs": {
            "width": "{{width}}", "height": "{{height}}", "batch_size": 1}},
        "4": {"class_type": "LoadImage", "inputs": {"image": "{{reference_1}}"}},
    }
    body = {"kind": "image", "family": "custom", "label": "Meu fluxo", "graph": graph,
            "files": [{**_file("x.safetensors"), "folder": "checkpoints"}],
            "nodes": [{"git_url": "https://github.com/a/ComfyUI-Extra.git", "git_ref": "abc123"}]}
    model_id = admin.post("/api/models/library", json=body).json()["id"]
    entry = admin.get("/api/settings/runtime").json()["catalog"]["models"]["image"][model_id]
    assert entry["dependencies"][-1] == {
        "name": "ComfyUI-Extra", "git_url": "https://github.com/a/ComfyUI-Extra.git",
        "git_ref": "abc123"}
    admin.put("/api/settings/runtime", json={"models": {"image": model_id}})
    with db.session() as conn:
        cfg = settings_store.effective_image_settings(conn)
    filled = image_client._comfy_custom_graph(cfg, "a knight", ["ref.png"], width=832, height=1216)
    assert filled["2"]["inputs"]["text"] == "masterpiece, a knight"
    assert filled["3"]["inputs"]["width"] == 832 and filled["3"]["inputs"]["height"] == 1216
    assert filled["4"]["inputs"]["image"] == "ref.png"
    assert graph["3"]["inputs"]["width"] == "{{width}}"

    screen_format = admin.post("/api/models/library", json={
        **body, "graph": {"nodes": [], "links": []}})
    assert screen_format.status_code == 400 and "API" in screen_format.json()["error"]["message"]
    no_prompt = admin.post("/api/models/library", json={
        **body, "graph": {"1": {"class_type": "X", "inputs": {}}}})
    assert no_prompt.status_code == 400


def test_custom_video_workflow_is_saved_and_selected(admin):
    graph = {
        "1": {"class_type": "CLIPTextEncode", "inputs": {"text": "{{prompt}}"}},
        "2": {"class_type": "LoadImage", "inputs": {"image": "{{reference_1}}"}},
        "3": {"class_type": "LTXVImgToVideo", "inputs": {
            "width": "{{width}}", "height": "{{height}}", "length": "{{frames}}"}},
        "4": {"class_type": "SaveVideo", "inputs": {"fps": "{{fps}}"}},
    }
    body = {"kind": "video", "family": "custom", "label": "Clipe próprio", "graph": graph,
            "files": [{**_file("video.safetensors"), "folder": "checkpoints"}]}
    missing_image = admin.post("/api/models/library", json={
        **body, "graph": {"1": graph["1"]}})
    assert missing_image.status_code == 400
    assert "{{reference_1}}" in missing_image.json()["error"]["message"]

    created = admin.post("/api/models/library", json=body)
    assert created.status_code == 200, created.text
    model_id = created.json()["id"]
    applied = admin.put("/api/settings/runtime", json={"models": {"video": model_id}})
    assert applied.status_code == 200, applied.text
    with db.session() as conn:
        cfg = settings_store.effective_image_settings(conn)
    assert cfg["comfyui_video"]["engine"] == "custom"
    assert cfg["comfyui_video"]["graph"] == graph


@pytest.mark.parametrize("file", [
    {**_file(), "url": "http://civitai.com/api/download/models/70"},
    {**_file(), "url": "https://evil.example/model.safetensors"},
    {**_file(), "url": "https://user:pw@civitai.com/api/download/models/70"},
    {**_file("model.ckpt")},
    {**_file(), "sha256": "zz" * 32},
])
def test_download_source_is_checked_before_it_enters_the_catalog(admin, file):
    resp = admin.post("/api/models/library", json={
        "kind": "image", "family": "checkpoint", "file": file})
    assert resp.status_code == 400, resp.text


def test_notebook_carries_the_saved_keys_and_what_the_engine_needs(admin):
    assert admin.put("/api/models/keys", json={"civitai": "civ-key-123456"}).json()["keys"] == {
        "civitai": True, "huggingface": False}
    model_id = admin.post("/api/models/library", json={
        "kind": "image", "family": "checkpoint", "file": _file()}).json()["id"]
    admin.post(f"/api/models/library/image/{model_id}/loras", json={
        "file": _file("estilo.safetensors")})
    admin.put("/api/settings/runtime", json={
        "mode": "hybrid", "placements": {"llm": "local", "comfyui": "colab"},
        "models": {"image": model_id}})
    resp = admin.get("/api/runtime/notebook", params={"project_url": "https://play.example"})
    assert resp.status_code == 200, resp.text
    cells = {cell["id"]: "".join(cell["source"]) for cell in resp.json()["cells"]}
    assert "CIVITAI_API_KEY = 'civ-key-123456'" in cells["secrets"]
    assert "userdata" not in cells["secrets"]
    assert "models/checkpoints/pony_real.safetensors" in cells["provision"]
    assert "models/loras/estilo.safetensors" in cells["provision"]
    assert "register()" in cells["keepalive"]
    for name, source in cells.items():
        if name != "overview":
            compile(source, name, "exec")
    # A chave apagada some do arquivo seguinte.
    admin.put("/api/models/keys", json={"civitai": ""})
    again = admin.get("/api/runtime/notebook", params={"project_url": "https://play.example"})
    assert "civ-key-123456" not in again.text

    # Z-Image Turbo só pinta texto; com foto de referência o jogo usa o Flux.2 Klein.
    nb = rp.generate_notebook({
        "mode": "hybrid", "placements": {"llm": "local", "comfyui": "colab"},
        "models": {"llm": "qwen3.6-35b-a3b", "image": "z_image_turbo", "video": "ltxv"},
        "endpoints": {"local_llm": "http://a.example/v1", "local_comfyui": "http://b.example",
                      "colab_llm": "", "colab_comfyui": ""},
        "project_url": "https://play.example",
    })
    provision = next("".join(c["source"]) for c in nb["cells"] if c["id"] == "provision")
    assert "z-image-turbo-Q4_K_M.gguf" in provision
    assert "flux-2-klein-4b-fp8.safetensors" in provision
    assert provision.count("models/text_encoders/qwen_3_4b.safetensors") == 1


def test_local_install_downloads_only_what_is_missing(admin, tmp_path, monkeypatch):
    root = tmp_path / "ComfyUI"
    (root / "models").mkdir(parents=True)
    monkeypatch.setenv("COMFYUI_ROOT", str(root))
    model_id = admin.post("/api/models/library", json={
        "kind": "image", "family": "checkpoint", "file": _file()}).json()["id"]
    fetched = []

    def fake_fetch(url, dest, sha256, retries=3, expected_bytes=None, auth_header=None):
        fetched.append((url, dest, auth_header))
        target = tmp_path / "ComfyUI" / "models" / "checkpoints"
        target.mkdir(parents=True, exist_ok=True)
        (target / "pony_real.safetensors").write_bytes(b"x" * int(expected_bytes))

    monkeypatch.setattr(rp, "fetch_pinned", fake_fetch)
    admin.put("/api/models/keys", json={"civitai": "civ-key-123456"})
    assert admin.get("/api/models/library").json()["comfyui_root"] == str(root)
    started = admin.post("/api/models/install", json={"kind": "image", "id": model_id})
    assert started.status_code == 200, started.text
    for _ in range(100):
        state = admin.get("/api/models/install").json()
        if state["state"] != "running":
            break
        time.sleep(0.02)
    assert state == {"state": "done", "model": model_id, "total": 1}
    assert fetched == [("https://civitai.com/api/download/models/70",
                        str(root / "models/checkpoints/pony_real.safetensors"),
                        "Bearer civ-key-123456")]
    # Já está no disco com o tamanho certo: não baixa de novo.
    again = admin.post("/api/models/install", json={"kind": "image", "id": model_id}).json()
    assert again["state"] == "done" and again["total"] == 0
    assert len(fetched) == 1
    assert admin.post("/api/models/install", json={
        "kind": "image", "id": "nao-existe"}).status_code == 404


def test_reference_photo_goes_through_flux2_and_changing_the_model_picks_comfyui(admin):
    admin.put("/api/settings/image", json={"provider": "openai_compatible"})
    model_id = admin.post("/api/models/library", json={
        "kind": "image", "family": "checkpoint", "file": _file()}).json()["id"]
    # Salvar sem trocar de modelo não mexe no provedor; trocar, sim.
    admin.put("/api/settings/runtime", json={"endpoints": {"local_comfyui": "http://c.example"}})
    assert admin.get("/api/settings/image").json()["provider"] == "openai_compatible"
    admin.put("/api/settings/runtime", json={"models": {"image": model_id}})
    assert admin.get("/api/settings/image").json()["provider"] == "comfyui"

    with db.session() as conn:
        cfg = settings_store.effective_image_settings(conn)
    assert image_client.reference_capable_cfg(cfg)["comfyui_engine"] == "checkpoint"
    assert image_client.reference_capable_cfg(
        cfg, reference_images=[b"png"])["comfyui_engine"] == "flux2_klein"

    plain = {**cfg, "comfyui_engine": "custom", "comfyui_custom": {"graph": {
        "1": {"class_type": "X", "inputs": {"text": "{{prompt}}"}}}}}
    assert image_client.reference_capable_cfg(
        plain, reference_images=[b"png"])["comfyui_engine"] == "flux2_klein"
    reads_photo = {**plain, "comfyui_custom": {"graph": {
        "1": {"class_type": "X", "inputs": {"text": "{{prompt}}"}},
        "2": {"class_type": "LoadImage", "inputs": {"image": "{{reference_1}}"}},
        "3": {"class_type": "LoadImage", "inputs": {"image": "{{reference_2}}"}}}}}
    assert image_client.reference_capable_cfg(
        reads_photo, reference_images=[b"png"])["comfyui_engine"] == "custom"
    # Dois marcadores e uma foto: o segundo repete a única que veio.
    filled = image_client._comfy_custom_graph(
        reads_photo, "a knight", ["a.png"], width=832, height=1216)
    assert filled["3"]["inputs"]["image"] == "a.png"

    # O notebook leva o Flux.2 Klein junto com o checkpoint.
    admin.put("/api/settings/runtime", json={
        "mode": "hybrid", "placements": {"llm": "local", "comfyui": "colab"}})
    resp = admin.get("/api/runtime/notebook", params={"project_url": "https://play.example"})
    provision = next("".join(c["source"]) for c in resp.json()["cells"] if c["id"] == "provision")
    assert "flux-2-klein-4b-fp8.safetensors" in provision
