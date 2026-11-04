"""Catálogo, validação e notebook Colab. Sem rede e sem banco."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

import pytest

from app import runtime_provisioning as rp

ROOT = Path(__file__).resolve().parents[1]
DEPLOY = ROOT / "deploy" / "vm"
FETCH_RE = re.compile(
    r"fetch\s*\\\s*\n\s*'([^']+)'\s*\\\s*\n\s*\"[^\"]+\"\s*\\\s*\n\s*([0-9a-f]{64})"
)


def _config(
    mode="local",
    llm_place="local",
    comfy_place="local",
    llm="qwen3.6-35b-a3b",
    image="flux2_klein",
    video="ltxv",
    project_url="",
    *,
    placements=True,
):
    cfg = {
        "mode": mode,
        "models": {"llm": llm, "image": image, "video": video},
        "endpoints": {
            "local_llm": "http://127.0.0.1:1234/v1",
            "local_comfyui": "http://127.0.0.1:8188",
            "colab_llm": "",
            "colab_comfyui": "",
        },
        "project_url": project_url,
    }
    if placements:
        cfg["placements"] = {"llm": llm_place, "comfyui": comfy_place}
    return cfg


def _text(cell: dict) -> str:
    source = cell["source"]
    return "".join(source) if isinstance(source, list) else source


def _role(nb: dict, role: str) -> str:
    for cell in nb["cells"]:
        if cell.get("metadata", {}).get("aidungeon_role") == role:
            return _text(cell)
    raise AssertionError(role)


def _src(nb: dict) -> str:
    return "\n".join(_text(cell) for cell in nb["cells"])


def _variables(nb: dict) -> dict:
    ns: dict = {}
    exec(_role(nb, "variables"), ns)
    return ns["VARIABLES"]


def _artifacts(catalog: dict):
    for kind in ("llm", "image", "video"):
        for entry in catalog["models"][kind].values():
            yield from entry["artifacts"]


def _big_gpu():
    return {
        "gpu_name": "test",
        "vram_total_mib": 80000,
        "vram_free_mib": 80000,
        "ram_total_mib": 80000,
        "ram_available_mib": 80000,
    }


def test_catalog_groups_and_aliases():
    catalog = rp.load_catalog()
    assert set(catalog["models"]) >= {"llm", "image", "video"}
    for entry in catalog["models"]["llm"].values():
        assert entry["model_alias"]
    assert catalog["models"]["image"]["flux2_klein"]["engine"] == "flux2_klein"
    assert catalog["models"]["image"]["fineview_qwen"]["engine"] == "qwen"
    assert catalog["models"]["video"]["ltxv"]["engine"] == "ltxv"
    assert catalog["models"]["video"]["wan"]["engine"] == "wan"
    assert "comfyanonymous" not in json.dumps(catalog)
    blob = json.dumps(catalog["models"]["llm"])
    assert "Qwen2.5-7B" not in blob
    assert "q5_k_m" not in blob


def test_catalog_matches_installers_and_readme():
    catalog = rp.load_catalog()
    pinned = []
    for name in (
        "install_flux2_klein.sh",
        "install_ltxv_video.sh",
        "install_wan_video.sh",
        "install_z_image_turbo.sh",
        "install_h3_small.sh",
    ):
        pinned.extend(FETCH_RE.findall((DEPLOY / name).read_text(encoding="utf-8")))
    assert len(pinned) == 15
    blob = json.dumps(catalog)
    for url, sha in pinned:
        assert url in blob
        assert sha in blob
    aux = (DEPLOY / "install_qwen_aux.sh").read_text(encoding="utf-8")
    qwen = catalog["models"]["llm"]["qwen2.5-3b-instruct-q4_k_m"]["artifacts"][0]
    assert qwen["url"] in aux
    assert qwen["sha256"] in aux
    assert str(qwen["bytes"]) in aux
    readme = (DEPLOY / "README.md").read_text(encoding="utf-8")
    fine = catalog["models"]["image"]["fineview_qwen"]
    for art in fine["artifacts"]:
        assert art["sha256"] in readme
        assert str(art["bytes"]) in readme
        assert art["url"] in readme
    for art in _artifacts(catalog):
        if art.get("sha256"):
            assert art["sha256"] in aux or art["sha256"] in readme or any(
                art["sha256"] == sha for _url, sha in pinned
            )
            assert art["url"] in aux or art["url"] in readme or any(
                art["url"] == url for url, _sha in pinned
            )
    narrator = catalog["models"]["llm"]["qwen3.6-35b-a3b"]
    for art in narrator["artifacts"]:
        assert art["url"] is None
        assert art["sha256"] is None
        assert art["url_variable"]
        assert art["sha256_variable"]
        assert art["filename"] in readme
    dep = narrator["dependencies"][0]
    assert dep["git_fork"] in readme
    assert dep["git_ref"] in readme
    assert dep["build"] in readme
    assert dep["git_url"] == f"https://github.com/{dep['git_fork']}.git"
    joined = " ".join(narrator["runtime"]["argv_before_model"] + narrator["runtime"]["argv_after_model"])
    for flag in ("-cram", "-ub", "--no-mmap", "-ncmoe", "--kv-unified", "-np"):
        assert flag in joined
    assert "--api-key" not in joined
    assert narrator["runtime"]["argv_before_model"][narrator["runtime"]["argv_before_model"].index("-ub") + 1] == "1024"
    assert narrator["runtime"]["argv_before_model"][narrator["runtime"]["argv_before_model"].index("-np") + 1] == "5"
    assert "14424" in readme
    assert narrator["min_vram_mib"] == 14424
    service = (DEPLOY / "llama-aux.service").read_text(encoding="utf-8")
    assert "MemoryMax=6G" in service
    assert qwen and catalog["models"]["llm"]["qwen2.5-3b-instruct-q4_k_m"]["min_ram_mib"] == 6 * 1024
    assert catalog["models"]["llm"]["qwen2.5-3b-instruct-q4_k_m"]["min_vram_mib"] == 0
    assert "127.0.0.1" in catalog["models"]["llm"]["qwen2.5-3b-instruct-q4_k_m"]["runtime"]["listen_host"]
    gguf = (DEPLOY / "install_wan_video.sh").read_text(encoding="utf-8")
    wan_dep = next(item for item in catalog["models"]["video"]["wan"]["dependencies"] if item["name"] == "ComfyUI-GGUF")
    assert wan_dep["git_ref"] in gguf
    assert wan_dep["git_url"] in gguf
    assert "0.42" in (DEPLOY / "install_flux2_klein.sh").read_text(encoding="utf-8")
    assert catalog["models"]["image"]["flux2_klein"]["dependencies"][0]["min_version"] == "0.42"
    assert "git_ref" not in catalog["models"]["image"]["flux2_klein"]["dependencies"][0]


def test_catalog_quality_matches_settings():
    catalog = rp.load_catalog()
    settings = json.loads((ROOT / "config" / "settings.json").read_text(encoding="utf-8"))
    comfy = settings["image"]["providers"]["comfyui"]
    assert catalog["models"]["image"]["flux2_klein"]["quality"] == comfy["flux2"]
    quality = catalog["models"]["image"]["fineview_qwen"]["quality"]
    for key in (
        "unet", "clip", "clip_type", "clip_device", "vae", "width", "height",
        "steps", "sampler", "cfg", "sigmas", "shift", "poll_interval_s",
    ):
        assert quality[key] == comfy[key]
    video = comfy["video"]
    got = catalog["models"]["video"]["ltxv"]["config"]
    for key, value in video.items():
        if key == "note":
            continue
        assert got[key] == value
    wan = catalog["models"]["video"]["wan"]["config"]
    assert wan["engine"] == "wan"
    assert wan["clip"] == "umt5_xxl_fp8_e4m3fn_scaled.safetensors"
    assert wan["steps"] == 20
    assert wan["cfg"] == 5
    assert wan["width"] == 1280
    assert wan["height"] == 704
    assert wan["vertical_width"] == 704
    assert wan["vertical_height"] == 1280
    for key in (
        "frames", "fps", "shift", "timeout_seconds", "unet", "vae",
        "auto_scene", "auto_portraits", "checkpoint",
    ):
        assert wan[key] == video[key]
    narrator = catalog["models"]["llm"]["qwen3.6-35b-a3b"]["quality"]
    assert narrator["disable_thinking"] is settings["llm"]["disable_thinking"]
    assert narrator["context_size"] == settings["llm"]["context_size"]
    assert narrator["sampler"] == settings["sampler"]
    unet = 7116801326
    vae = 675509688
    clip = 9350798360

    def mib(nbytes: int) -> int:
        return (nbytes + 1048576 - 1) // 1048576

    assert catalog["models"]["image"]["fineview_qwen"]["min_vram_mib"] == mib(unet + vae)
    assert catalog["models"]["image"]["fineview_qwen"]["min_ram_mib"] == mib(clip)
    assert catalog["models"]["image"]["flux2_klein"]["min_vram_mib"] is None
    assert catalog["models"]["video"]["ltxv"]["min_vram_mib"] is None
    assert catalog["models"]["video"]["wan"]["min_vram_mib"] is None


def test_load_catalog_rejects_incomplete(tmp_path):
    path = tmp_path / "bad.json"
    path.write_text(
        json.dumps({
            "models": {
                "llm": {
                    "x": {
                        "model_alias": "x",
                        "artifacts": [{
                            "filename": "x.gguf",
                            "dest": "models/llm/x.gguf",
                            "url": "https://example.invalid/x.gguf",
                            "sha256": "ab" * 32,
                        }],
                    }
                }
            }
        }),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="models.image"):
        rp.load_catalog(path)
    missing = tmp_path / "missing.json"
    with pytest.raises(ValueError, match="não encontrado"):
        rp.load_catalog(missing)


@pytest.mark.parametrize(
    ("mode", "llm_place", "comfy_place"),
    [
        ("local", "local", "local"),
        ("colab", "colab", "colab"),
        ("hybrid", "colab", "local"),
        ("hybrid", "local", "colab"),
    ],
)
def test_validate_modes(mode, llm_place, comfy_place):
    norm = rp.validate_runtime(_config(mode, llm_place, comfy_place, project_url="https://runtime.example"))
    assert norm["mode"] == mode
    assert norm["placements"] == {"llm": llm_place, "comfyui": comfy_place}
    assert norm["project_url"] == "https://runtime.example"


@pytest.mark.parametrize(
    ("mode", "llm_place", "comfy_place"),
    [
        ("hybrid", "local", "local"),
        ("hybrid", "colab", "colab"),
        ("local", "colab", "local"),
        ("colab", "local", "colab"),
    ],
)
def test_validate_rejects_inconsistent_mode(mode, llm_place, comfy_place):
    with pytest.raises(ValueError, match="não combina"):
        rp.validate_runtime(_config(mode, llm_place, comfy_place))


def test_validate_derives_mode_and_defaults():
    cfg = _config("colab", placements=False)
    del cfg["endpoints"]
    norm = rp.validate_runtime(cfg)
    assert norm["mode"] == "colab"
    assert norm["placements"] == {"llm": "colab", "comfyui": "colab"}
    assert norm["endpoints"]["local_llm"] == "http://127.0.0.1:1234/v1"
    assert norm["endpoints"]["local_comfyui"] == "http://127.0.0.1:8188"
    assert norm["endpoints"]["colab_llm"] == ""
    derived = rp.validate_runtime({
        "placements": {"llm": "colab", "comfyui": "local"},
        "models": _config()["models"],
    })
    assert derived["mode"] == "hybrid"
    with pytest.raises(ValueError, match="hybrid exige"):
        rp.validate_runtime(_config("hybrid", placements=False))
    with pytest.raises(ValueError, match="require_colab"):
        rp.validate_runtime(_config(), require_colab=True)


def test_validate_rejects_unknown_models_secrets_and_urls():
    cfg = _config()
    cfg["models"] = {**cfg["models"], "llm": "nao-existe"}
    with pytest.raises(ValueError, match="desconhecido"):
        rp.validate_runtime(cfg)
    cfg = _config()
    cfg["api_key"] = "segredo"
    with pytest.raises(ValueError, match="segredo"):
        rp.validate_runtime(cfg)
    cfg = _config()
    cfg["adventure_id"] = "e17f7abe1731489c9104b502d9584dc9"
    with pytest.raises(ValueError, match="desconhecidas"):
        rp.validate_runtime(cfg)
    cfg = _config()
    cfg["placements"] = {"llm": "local", "comfyui": "local", "image": "colab"}
    with pytest.raises(ValueError, match="compartilham"):
        rp.validate_runtime(cfg)
    cfg = _config()
    cfg["project_url"] = "https://user:pass@runtime.example/app"
    with pytest.raises(ValueError, match="credenciais"):
        rp.validate_runtime(cfg)
    cfg = _config()
    cfg["endpoints"] = {**cfg["endpoints"], "local_llm": "ftp://127.0.0.1/v1"}
    with pytest.raises(ValueError, match="http"):
        rp.validate_runtime(cfg)
    with pytest.raises(ValueError, match="mode deve ser"):
        rp.validate_runtime(_config(mode="Colab"))


def test_hardware_requirements_and_failure_path():
    narrator = _config("hybrid", "colab", "local")
    req = rp.hardware_requirements(narrator)
    assert req == {"min_vram_mib": 14424, "min_ram_mib": 0, "vram_unspecified": []}
    flux = _config("hybrid", "local", "colab")
    req = rp.hardware_requirements(flux)
    assert req["min_vram_mib"] == 0
    assert req["vram_unspecified"] == ["flux2_klein", "ltxv"]
    both = _config(
        "colab", "colab", "colab",
        llm="qwen3.6-35b-a3b", image="fineview_qwen", video="wan",
    )
    req = rp.hardware_requirements(both)
    assert req["min_vram_mib"] == 14424 + 7432
    assert req["vram_unspecified"] == ["wan"]
    assert req["min_ram_mib"] == 8918
    small = _config(
        "hybrid", "colab", "local", llm="qwen2.5-3b-instruct-q4_k_m",
    )
    req = rp.hardware_requirements(small)
    assert req["min_vram_mib"] == 0
    assert req["min_ram_mib"] == 6144
    assert req["vram_unspecified"] == []

    with pytest.raises(ValueError, match="VRAM insuficiente") as exc:
        rp.assert_hardware(
            {"vram_total_mib": 16000, "vram_free_mib": 1000, "ram_available_mib": 64000},
            {"min_vram_mib": 14424, "min_ram_mib": 0, "vram_unspecified": [], "min_vram_override": None},
        )
    assert "Sem downgrade" in str(exc.value)
    with pytest.raises(ValueError, match="VRAM não detectada"):
        rp.assert_hardware(
            {"vram_total_mib": None, "vram_free_mib": None, "ram_available_mib": 64000},
            {"min_vram_mib": 14424, "min_ram_mib": 0, "vram_unspecified": [], "min_vram_override": None},
        )
    with pytest.raises(ValueError, match="VRAM mínima não publicada"):
        rp.assert_hardware(
            _big_gpu(),
            {"min_vram_mib": 0, "min_ram_mib": 0, "vram_unspecified": ["flux2_klein"], "min_vram_override": None},
        )
    with pytest.raises(ValueError, match="abaixo do piso"):
        rp.assert_hardware(
            _big_gpu(),
            {"min_vram_mib": 14424, "min_ram_mib": 0, "vram_unspecified": ["ltxv"], "min_vram_override": 1000},
        )
    with pytest.raises(ValueError, match="RAM insuficiente"):
        rp.assert_hardware(
            {"vram_total_mib": None, "vram_free_mib": None, "ram_available_mib": 1000},
            {"min_vram_mib": 0, "min_ram_mib": 6144, "vram_unspecified": [], "min_vram_override": None},
        )
    rp.assert_hardware(
        {"vram_total_mib": None, "vram_free_mib": None, "ram_available_mib": 8000},
        {"min_vram_mib": 0, "min_ram_mib": 6144, "vram_unspecified": [], "min_vram_override": None},
    )
    rp.assert_hardware(
        {"vram_total_mib": 20000, "vram_free_mib": 20000, "ram_available_mib": 8000},
        {"min_vram_mib": 14424, "min_ram_mib": 0, "vram_unspecified": [], "min_vram_override": None},
    )


def test_fetch_pinned_sha_size_and_retry(tmp_path):
    payload = b"hello-runtime"
    digest = hashlib.sha256(payload).hexdigest()
    dest = tmp_path / "model.gguf"
    calls = {"n": 0}

    def opener(url, part, header):
        calls["n"] += 1
        assert url.startswith("https://")
        assert header is None
        data = b"bad" if calls["n"] == 1 else payload
        Path(part).write_bytes(data)

    rp.fetch_pinned(
        "https://example.invalid/model.gguf",
        str(dest),
        digest,
        retries=3,
        expected_bytes=len(payload),
        opener=opener,
    )
    assert dest.read_bytes() == payload
    assert calls["n"] == 2
    assert not Path(str(dest) + ".part").exists()

    def bad(url, part, header):
        Path(part).write_bytes(b"nope")

    missed = tmp_path / "miss.gguf"
    with pytest.raises(ValueError, match="SHA256 divergente"):
        rp.fetch_pinned(
            "https://example.invalid/miss.gguf",
            str(missed),
            digest,
            retries=1,
            opener=bad,
        )
    assert not missed.exists()
    assert not Path(str(missed) + ".part").exists()

    def wrong_size(url, part, header):
        Path(part).write_bytes(payload)

    with pytest.raises(ValueError, match="tamanho inesperado"):
        rp.fetch_pinned(
            "https://example.invalid/size.gguf",
            str(tmp_path / "size.gguf"),
            digest,
            retries=1,
            expected_bytes=1,
            opener=wrong_size,
        )
    seen = {"called": False}

    def explode(url, part, header):
        seen["called"] = True
        raise AssertionError("não devia baixar")

    with pytest.raises(ValueError, match="URL de download ausente"):
        rp.fetch_pinned("", str(tmp_path / "empty.gguf"), digest, opener=explode)
    assert seen["called"] is False
    rp.fetch_pinned(
        "https://example.invalid/model.gguf",
        str(dest),
        digest,
        retries=1,
        opener=explode,
    )


def test_resolve_narrator_requires_variables():
    catalog = rp.load_catalog()
    spec = catalog["models"]["llm"]["qwen3.6-35b-a3b"]["artifacts"][0]
    with pytest.raises(ValueError, match="sem URL/SHA256") as exc:
        rp.resolve_artifact(spec, {"narrator_gguf_url": "", "narrator_gguf_sha256": ""})
    assert "não será inventado" in str(exc.value)
    url, sha = rp.resolve_artifact(spec, {
        "narrator_gguf_url": "https://example.invalid/narrator.gguf",
        "narrator_gguf_sha256": "ab" * 32,
    })
    assert url.endswith("narrator.gguf")
    assert sha == "ab" * 32


def test_local_hardware_shape():
    hardware = rp.local_hardware()
    assert set(hardware) == {
        "gpu_name", "vram_total_mib", "vram_free_mib", "ram_total_mib", "ram_available_mib",
    }
    if Path("/proc/meminfo").is_file():
        assert hardware["ram_total_mib"] > 0
        assert hardware["ram_available_mib"] > 0


def test_notebook_contract_and_selected_services():
    llm_only = rp.generate_notebook(_config(
        "hybrid", "colab", "local", project_url="https://runtime.example",
    ))
    comfy_only = rp.generate_notebook(_config(
        "hybrid", "local", "colab", project_url="https://runtime.example",
    ))
    both = rp.generate_notebook(_config(
        "colab", "colab", "colab",
        llm="qwen2.5-3b-instruct-q4_k_m",
        image="fineview_qwen",
        video="wan",
        project_url="https://runtime.example",
    ))
    for nb in (llm_only, comfy_only, both):
        assert nb["nbformat"] == 4
        assert nb["cells"][1]["metadata"]["aidungeon_role"] == "variables"
        raw = rp.notebook_bytes(nb)
        assert json.loads(raw) == nb
        src = _src(nb)
        for cell in nb["cells"]:
            if cell["cell_type"] == "code":
                compile(_text(cell), cell["id"], "exec")
        assert ".part" in src
        assert "SHA256 divergente" in src
        assert "retries=3" in src
        assert "tamanho inesperado" in src
        assert "Sem downgrade" in src
        assert "userdata.get(\"AIDUNGEON_TOKEN\")" in src
        assert "userdata.get(\"TAILSCALE_AUTH_KEY\")" in src
        assert "tailscale" in src
        assert rp.REGISTER_PATH in src
        assert "Bearer " in src
        assert not re.search(r'AIDUNGEON_TOKEN\s*=\s*["\']', src)
        assert not re.search(r'TAILSCALE_AUTH_KEY\s*=\s*["\'][^"\']+["\']', src)
        for banned in (
            "sqlite", "aidungeon.db", "INSERT INTO", "BEGIN PRIVATE",
            "LLM_API_KEY", "api_key", "AUTH_PASSWORD", "/home/caio", "--api-key",
        ):
            assert banned not in src
        assert "AIDUNGEON_TOKEN" not in _role(nb, "variables")

    llm_src = _src(llm_only)
    llm_vars = _variables(llm_only)
    assert "llama-server" in llm_src
    assert "main.py" not in llm_src
    assert "flux-2-klein" not in llm_src
    assert "huggingface.co" not in llm_src
    assert "CIVITAI" not in llm_src
    assert llm_vars["narrator_gguf_url"] == ""
    assert llm_vars["narrator_gguf_sha256"] == ""
    assert llm_vars["llama_git_ref"] == "c8064719f"
    assert llm_vars["min_vram_mib"] == 14424
    assert llm_vars["vram_unspecified"] == []
    assert "não será inventado" in llm_src

    comfy_src = _src(comfy_only)
    comfy_vars = _variables(comfy_only)
    assert "llama-server" not in comfy_src
    assert "main.py" in comfy_src
    assert "flux-2-klein-4b-fp8" in comfy_src
    assert "ltxv-2b-0.9.6-distilled" in comfy_src
    assert "Wan2.2" not in comfy_src
    assert "7ba6c0774c233540ef6ea0ea3971acf3cbf403f70bd3672af57eaca00c33578e" not in comfy_src
    assert "CIVITAI" not in comfy_src
    assert "6ea2651e7df66d7585f6ffee804b20e92fb38b8a" not in comfy_src
    assert comfy_vars["comfyui_git_url"] == ""
    assert comfy_vars["comfyui_git_ref"] == ""
    assert comfy_vars["comfy_argv"][comfy_vars["comfy_argv"].index("--listen") + 1] == "0.0.0.0"
    assert "8188" in comfy_vars["comfy_argv"]
    assert "--vram-headroom" in comfy_vars["comfy_argv"]
    assert comfy_vars["vram_unspecified"] == ["flux2_klein", "ltxv"]
    assert "COMFYUI_GIT_REF vazios" in comfy_src

    both_src = _src(both)
    both_vars = _variables(both)
    assert "llama-server" in both_src
    assert "main.py" in both_src
    assert "626b4a6678b86442240e33df819e00132d3ba7dddfe1cdc4fbb18e0a9615c62d" in both_src
    assert "qwen2.5-3b-instruct-q4_k_m.gguf?download=true" in both_src
    assert "CIVITAI_API_KEY" in both_src
    assert "userdata.get(\"CIVITAI_API_KEY\")" in both_src
    assert "6ea2651e7df66d7585f6ffee804b20e92fb38b8a" in both_src
    assert "umt5_xxl_fp8_e4m3fn_scaled" in both_src
    assert "ltxv-2b-0.9.6-distilled-04-25.safetensors?download=true" not in both_src
    assert both_vars["llm_port"] == 1240
    assert both_vars["min_ram_mib"] == 6144 + 8918
    assert "narrator_gguf_url" not in both_vars
    assert "CUDA_VISIBLE_DEVICES" in both_src
    assert '"endpoints"' in _role(both, "register")
    assert '"hardware"' in _role(both, "register")
    assert 'comfy_endpoint = ""' in _role(llm_only, "register")
    assert 'llm_endpoint = ""' in _role(comfy_only, "register")
    local = rp.generate_notebook(_config())
    local_src = _src(local)
    assert local["nbformat"] == 4
    assert local["cells"][1]["metadata"]["aidungeon_role"] == "variables"
    assert _variables(local)["mode"] == "local"
    assert _variables(local)["endpoints"]["colab_llm"] == ""
    assert _variables(local)["endpoints"]["colab_comfyui"] == ""
    assert "Nada foi instalado" in local_src
    for banned in (
        "llama-server", "main.py", "AIDUNGEON_TOKEN", "TAILSCALE",
        "tailscale", "CIVITAI", "huggingface.co", "/api/runtime/register",
    ):
        assert banned not in local_src
    compile(_role(local, "local"), "local", "exec")
    again = rp.generate_notebook(_config(
        "hybrid", "colab", "local", project_url="https://runtime.example",
    ))
    assert rp.notebook_bytes(again) == rp.notebook_bytes(llm_only)


def test_notebook_hardware_and_sha_paths(tmp_path):
    nb = rp.generate_notebook(_config(
        "hybrid", "colab", "local", project_url="https://runtime.example",
    ))
    ns: dict = {}
    exec(_role(nb, "variables"), ns)
    exec(_role(nb, "helpers"), ns)
    ns["detect_hardware"] = lambda: {
        "gpu_name": "tiny",
        "vram_total_mib": 8000,
        "vram_free_mib": 8000,
        "ram_total_mib": 64000,
        "ram_available_mib": 64000,
    }
    with pytest.raises(SystemExit, match="VRAM insuficiente"):
        exec(_role(nb, "hardware"), ns)
    ns["detect_hardware"] = _big_gpu
    exec(_role(nb, "hardware"), ns)

    flux = rp.generate_notebook(_config(
        "hybrid", "local", "colab", project_url="https://runtime.example",
    ))
    flux_ns: dict = {}
    exec(_role(flux, "variables"), flux_ns)
    exec(_role(flux, "helpers"), flux_ns)
    flux_ns["detect_hardware"] = _big_gpu
    with pytest.raises(SystemExit, match="VRAM mínima não publicada"):
        exec(_role(flux, "hardware"), flux_ns)

    digest = hashlib.sha256(b"pinned").hexdigest()
    dest = tmp_path / "weight.gguf"

    def opener(url, part, header):
        Path(part).write_bytes(b"pinned" if header == "Bearer secret" else b"nope")

    with pytest.raises(ValueError, match="SHA256 divergente"):
        ns["fetch_pinned"](
            "https://example.invalid/weight.gguf",
            str(dest),
            digest,
            retries=1,
            opener=opener,
        )
    ns["fetch_pinned"](
        "https://example.invalid/weight.gguf",
        str(dest),
        digest,
        retries=2,
        auth_header="Bearer secret",
        opener=opener,
    )
    assert dest.read_bytes() == b"pinned"


def test_registration_payload_shape():
    payload = rp.registration_payload(
        _config("hybrid", "colab", "local", project_url="https://runtime.example"),
        {"vram_total_mib": 1, "ram_available_mib": 2},
        {"llm": "http://100.64.0.2:1234/v1", "comfyui": ""},
    )
    assert set(payload) == {"endpoints", "models", "hardware"}
    assert payload["endpoints"] == {"llm": "http://100.64.0.2:1234/v1", "comfyui": ""}
    assert set(payload["models"]) == {"llm", "image", "video"}
    assert payload["models"]["llm"] == "qwen3.6-35b-a3b"
    with pytest.raises(ValueError, match="endpoints.llm"):
        rp.registration_payload(
            _config("hybrid", "colab", "local", project_url="https://runtime.example"),
            {"vram_total_mib": 1},
            {"llm": "", "comfyui": ""},
        )
    with pytest.raises(ValueError):
        rp.notebook_bytes({"nbformat": 3, "cells": []})
