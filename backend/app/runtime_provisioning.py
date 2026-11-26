"""Catálogo de runtimes, validação e notebook Colab parametrizado.

API pura: sem rede, sem banco e sem segredos. O notebook gerado é
autocontido (nbformat 4). Local é o runtime do projeto; Colab é compute
efêmero. Imagem e vídeo compartilham o placement do ComfyUI.

`local` = llm e comfyui locais. `colab` = os dois no Colab. `hybrid` =
exatamente um no Colab. Não há downgrade de modelo nem de qualidade.
"""

from __future__ import annotations

import hashlib
import inspect
import json
import os
import re
import shutil
import subprocess
import urllib.parse
import urllib.request
from pathlib import Path

REGISTER_PATH = "/api/runtime/register"
COLAB_ROOT = "/content/aidungeon-runtime"
COLAB_BIND_HOST = "0.0.0.0"

_MODES = ("local", "hybrid", "colab")
_PLACEMENTS = ("local", "colab")
_CONFIG_KEYS = ("mode", "placements", "models", "endpoints", "project_url")
_PLACEMENT_KEYS = ("llm", "comfyui")
_MODEL_KEYS = ("llm", "image", "video")
_ENDPOINT_KEYS = (
    "local_llm",
    "local_comfyui",
    "colab_llm",
    "colab_comfyui",
)
_DEFAULT_ENDPOINTS = {
    "local_llm": "http://127.0.0.1:1234/v1",
    "local_comfyui": "http://127.0.0.1:8188",
    "colab_llm": "",
    "colab_comfyui": "",
}
_SECRET_KEYS = {
    "api_key",
    "apikey",
    "token",
    "password",
    "secret",
    "authorization",
    "auth",
    "credential",
    "credentials",
    "tailscale_auth_key",
    "aidungeon_token",
    "civitai_api_key",
}
_URL_FORBIDDEN = set("\\\"'`\n\r\t ")
_SHA_HEX = frozenset("0123456789abcdef")


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _default_catalog_path() -> Path:
    return _repo_root() / "config" / "runtime_models.json"


def _validate_catalog(data: dict) -> None:
    models = data.get("models") if isinstance(data, dict) else None
    if not isinstance(models, dict):
        raise ValueError("catálogo sem models")
    for kind in _MODEL_KEYS:
        group = models.get(kind)
        if not isinstance(group, dict) or not group:
            raise ValueError(f"catálogo sem models.{kind}")
    for model_id, entry in models["llm"].items():
        if not isinstance(entry, dict) or not str(entry.get("model_alias") or "").strip():
            raise ValueError(f"LLM {model_id} sem model_alias")
        _require_artifacts(model_id, entry)
    for model_id, entry in models["image"].items():
        if not isinstance(entry, dict) or not str(entry.get("engine") or "").strip():
            raise ValueError(f"imagem {model_id} sem engine")
        _require_artifacts(model_id, entry)
    for model_id, entry in models["video"].items():
        if not isinstance(entry, dict) or not str(entry.get("engine") or "").strip():
            raise ValueError(f"vídeo {model_id} sem engine")
        if not isinstance(entry.get("config"), dict) or not entry["config"]:
            raise ValueError(f"vídeo {model_id} sem config")
        _require_artifacts(model_id, entry)


def _require_artifacts(model_id: str, entry: dict) -> None:
    artifacts = entry.get("artifacts")
    if not isinstance(artifacts, list) or not artifacts:
        raise ValueError(f"{model_id} sem artefatos")
    for art in artifacts:
        if not isinstance(art, dict):
            raise ValueError(f"{model_id} tem artefato inválido")
        filename = str(art.get("filename") or "").strip()
        dest = str(art.get("dest") or "").strip()
        if not filename or not dest:
            raise ValueError(f"{model_id} tem artefato sem filename/dest")
        pinned = bool(art.get("url")) and bool(art.get("sha256"))
        variables = bool(art.get("url_variable")) and bool(art.get("sha256_variable"))
        if pinned == variables:
            raise ValueError(
                f"{model_id} artefato {filename} precisa de URL+SHA256 "
                "ou de variáveis explícitas, nunca os dois e nunca nenhum"
            )
        sha = art.get("sha256")
        if sha is not None and not _is_sha256(str(sha)):
            raise ValueError(f"{model_id} artefato {filename} tem SHA256 inválido")


def _is_sha256(value: str) -> bool:
    text = value.strip().lower()
    return len(text) == 64 and all(char in _SHA_HEX for char in text)


def load_catalog(path: str | Path | None = None) -> dict:
    """Lê o catálogo JSON. Não baixa nada e não completa hash ausente."""
    catalog_path = Path(path) if path is not None else _default_catalog_path()
    try:
        raw = catalog_path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise ValueError(f"catálogo não encontrado: {catalog_path}") from exc
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"catálogo JSON inválido: {catalog_path}: {exc}") from exc
    _validate_catalog(data)
    return data


def _reject_secrets(value: object, path: str = "config") -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            if str(key).strip().lower() in _SECRET_KEYS:
                raise ValueError(f"{path}.{key} não pode carregar segredo")
            _reject_secrets(item, f"{path}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _reject_secrets(item, f"{path}[{index}]")
    elif isinstance(value, str) and "BEGIN PRIVATE KEY" in value:
        raise ValueError(f"{path} não pode carregar chave privada")


def _http_url(value: object, field: str, *, allow_empty: bool) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field} deve ser string")
    text = value.strip()
    if not text:
        if allow_empty:
            return ""
        raise ValueError(f"{field} vazio")
    if any(char in _URL_FORBIDDEN for char in text):
        raise ValueError(f"{field} contém caracteres não permitidos")
    if not text.startswith(("http://", "https://")):
        raise ValueError(f"{field} deve ser http(s)")
    parsed = urllib.parse.urlsplit(text)
    if parsed.username or parsed.password or "@" in (parsed.netloc or ""):
        raise ValueError(f"{field} não pode embutir credenciais")
    if not parsed.hostname:
        raise ValueError(f"{field} sem host")
    return text.rstrip("/")


def _placements_for_mode(mode: str) -> dict[str, str]:
    if mode == "local":
        return {"llm": "local", "comfyui": "local"}
    if mode == "colab":
        return {"llm": "colab", "comfyui": "colab"}
    raise ValueError(
        "hybrid exige placements com exatamente um serviço em colab"
    )


def _mode_for_placements(placements: dict[str, str]) -> str:
    pair = (placements["llm"], placements["comfyui"])
    if pair == ("local", "local"):
        return "local"
    if pair == ("colab", "colab"):
        return "colab"
    return "hybrid"


def _check_mode(mode: str, placements: dict[str, str]) -> None:
    expected = _mode_for_placements(placements)
    if mode != expected:
        raise ValueError(
            f"mode {mode} não combina com placements "
            f"llm={placements['llm']} comfyui={placements['comfyui']}"
        )


def validate_runtime(
    config: dict,
    catalog: dict | None = None,
    require_colab: bool = False,
) -> dict:
    """Normaliza a config ou levanta ValueError. Não escolhe outro modelo."""
    if not isinstance(config, dict):
        raise ValueError("runtime config precisa ser um objeto")
    _reject_secrets(config)
    unknown = sorted(set(config) - set(_CONFIG_KEYS))
    if unknown:
        raise ValueError(f"chaves de runtime desconhecidas: {unknown}")

    models_raw = config.get("models")
    if not isinstance(models_raw, dict):
        raise ValueError("models ausente")
    if set(models_raw) != set(_MODEL_KEYS):
        raise ValueError("models aceita exatamente llm, image e video")
    models: dict[str, str] = {}
    for key in _MODEL_KEYS:
        value = models_raw[key]
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"models.{key} ausente")
        models[key] = value.strip()

    if "placements" in config and "mode" not in config:
        placements = _parse_placements(config["placements"])
        mode = _mode_for_placements(placements)
    elif "mode" in config and "placements" not in config:
        mode = _parse_mode(config["mode"])
        placements = _placements_for_mode(mode)
    elif "mode" in config and "placements" in config:
        mode = _parse_mode(config["mode"])
        placements = _parse_placements(config["placements"])
        _check_mode(mode, placements)
    else:
        raise ValueError("mode ausente")

    if require_colab and mode == "local":
        raise ValueError("require_colab exige ao menos um placement colab")

    endpoints = _parse_endpoints(config.get("endpoints"))
    project_url = _http_url(
        config.get("project_url", ""),
        "project_url",
        allow_empty=True,
    )
    book = catalog if catalog is not None else load_catalog()
    _validate_catalog(book)
    groups = book["models"]
    for key in _MODEL_KEYS:
        if models[key] not in groups[key]:
            known = ", ".join(sorted(groups[key]))
            raise ValueError(f"models.{key} desconhecido: {models[key]} (válidos: {known})")

    return {
        "mode": mode,
        "placements": placements,
        "models": models,
        "endpoints": endpoints,
        "project_url": project_url,
    }


def _parse_mode(value: object) -> str:
    if not isinstance(value, str) or value.strip() not in _MODES:
        raise ValueError("mode deve ser local, hybrid ou colab")
    return value.strip()


def _parse_placements(value: object) -> dict[str, str]:
    if not isinstance(value, dict):
        raise ValueError("placements precisa ser um objeto")
    if set(value) != set(_PLACEMENT_KEYS):
        raise ValueError(
            "placements aceita somente llm e comfyui; imagem e vídeo compartilham comfyui"
        )
    parsed: dict[str, str] = {}
    for key in _PLACEMENT_KEYS:
        item = value[key]
        if not isinstance(item, str) or item.strip() not in _PLACEMENTS:
            raise ValueError(f"placements.{key} deve ser local ou colab")
        parsed[key] = item.strip()
    return parsed


def _parse_endpoints(value: object) -> dict[str, str]:
    raw = value if value is not None else {}
    if not isinstance(raw, dict):
        raise ValueError("endpoints precisa ser um objeto")
    extra = sorted(set(raw) - set(_ENDPOINT_KEYS))
    if extra:
        raise ValueError(f"endpoints desconhecidos: {extra}")
    parsed: dict[str, str] = {}
    for key in _ENDPOINT_KEYS:
        if key in raw:
            allow_empty = key.startswith("colab_")
            parsed[key] = _http_url(raw[key], f"endpoints.{key}", allow_empty=allow_empty)
        else:
            parsed[key] = _DEFAULT_ENDPOINTS[key]
    return parsed


def hardware_requirements(config: dict, catalog: dict | None = None) -> dict:
    """Piso de VRAM/RAM só dos serviços que este runtime coloca no Colab.

    Imagem e vídeo não rodam juntos: entra o maior piso conhecido. Piso
    ausente (null no catálogo) vai para `vram_unspecified` e o notebook
    recusa seguir sem `min_vram_mib_override`.
    """
    norm = validate_runtime(config, catalog, require_colab=False)
    book = catalog if catalog is not None else load_catalog()
    vram = 0
    ram = 0
    unspecified: list[str] = []
    if norm["placements"]["llm"] == "colab":
        entry = book["models"]["llm"][norm["models"]["llm"]]
        part = entry.get("min_vram_mib")
        if part is None:
            unspecified.append(norm["models"]["llm"])
        else:
            vram += int(part)
        ram += int(entry.get("min_ram_mib") or 0)
    if norm["placements"]["comfyui"] == "colab":
        image_id = norm["models"]["image"]
        video_id = norm["models"]["video"]
        image = book["models"]["image"][image_id]
        video = book["models"]["video"][video_id]
        floors: list[int] = []
        for model_id, entry in ((image_id, image), (video_id, video)):
            part = entry.get("min_vram_mib")
            if part is None:
                unspecified.append(model_id)
            else:
                floors.append(int(part))
            ram += int(entry.get("min_ram_mib") or 0)
        if floors:
            vram += max(floors)
    return {
        "min_vram_mib": vram,
        "min_ram_mib": ram,
        "vram_unspecified": unspecified,
    }


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _download_url(url: str, part: Path, auth_header: str | None) -> None:
    headers = {"User-Agent": "aidungeon-runtime-provisioning"}
    if auth_header:
        headers["Authorization"] = auth_header
    request = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(request, timeout=120) as response, part.open("wb") as handle:
        shutil.copyfileobj(response, handle, length=1024 * 1024)


def fetch_pinned(
    url: str,
    dest: str,
    sha256: str,
    retries: int = 3,
    expected_bytes: int | None = None,
    auth_header: str | None = None,
    *,
    opener=None,
) -> None:
    """Baixa para `dest + '.part'`, confere tamanho e SHA256, só então troca o nome.

    Falha de hash ou de rede repete até `retries`. Não imprime a URL.
    """
    if not isinstance(url, str) or not url.startswith(("https://", "http://")):
        raise ValueError(f"URL de download ausente ou inválida para {dest}")
    sha = str(sha256 or "").strip().lower()
    if len(sha) != 64 or any(char not in "0123456789abcdef" for char in sha):
        raise ValueError(f"SHA256 ausente ou inválido para {dest}")
    if int(retries) < 1:
        raise ValueError("retries precisa ser >= 1")
    destination = Path(dest)
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.is_file() and sha256_file(destination) == sha:
        if expected_bytes is None or destination.stat().st_size == int(expected_bytes):
            return
    part = Path(str(destination) + ".part")
    write = opener or _download_url
    attempts = int(retries)
    for attempt in range(1, attempts + 1):
        try:
            if part.exists():
                part.unlink()
            write(url, part, auth_header)
            if expected_bytes is not None and part.stat().st_size != int(expected_bytes):
                got = part.stat().st_size
                part.unlink(missing_ok=True)
                raise ValueError(
                    f"tamanho inesperado: {got} bytes (esperado {expected_bytes})"
                )
            actual = sha256_file(part)
            if actual != sha:
                part.unlink(missing_ok=True)
                raise ValueError(
                    f"SHA256 divergente em {destination.name}: obtido {actual} esperado {sha}"
                )
            os.replace(part, destination)
            return
        except ValueError:
            if attempt >= attempts:
                raise
        except Exception as exc:
            if part.exists():
                part.unlink()
            if attempt >= attempts:
                raise ValueError(
                    f"download falhou após {attempts} tentativas: "
                    f"{destination.name}: {exc.__class__.__name__}"
                ) from exc


def detect_hardware() -> dict:
    """Lê nvidia-smi e /proc/meminfo. Ausência de GPU vira null, sem exceção."""
    result = {
        "gpu_name": None,
        "vram_total_mib": None,
        "vram_free_mib": None,
        "ram_total_mib": None,
        "ram_available_mib": None,
    }
    try:
        proc = subprocess.run(
            [
                "nvidia-smi",
                "--query-gpu=name,memory.total,memory.free",
                "--format=csv,noheader,nounits",
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
        if proc.returncode == 0 and proc.stdout.strip():
            name, total, free = [part.strip() for part in proc.stdout.splitlines()[0].split(",")]
            result["gpu_name"] = name
            result["vram_total_mib"] = int(float(total))
            result["vram_free_mib"] = int(float(free))
    except (OSError, ValueError, subprocess.TimeoutExpired):
        pass
    meminfo = Path("/proc/meminfo")
    if meminfo.is_file():
        info: dict[str, int] = {}
        for line in meminfo.read_text(encoding="utf-8").splitlines():
            if ":" not in line:
                continue
            key, value = line.split(":", 1)
            parts = value.strip().split()
            if parts and parts[0].isdigit():
                info[key] = int(parts[0])
        if "MemTotal" in info:
            result["ram_total_mib"] = info["MemTotal"] // 1024
        if "MemAvailable" in info:
            result["ram_available_mib"] = info["MemAvailable"] // 1024
    return result


def local_hardware() -> dict:
    """Hardware da máquina que está gerando ou servindo o projeto."""
    return detect_hardware()


def assert_hardware(hardware: dict, requirements: dict) -> None:
    """Recusa VRAM/RAM abaixo do piso. Não escolhe modelo menor."""
    if not isinstance(hardware, dict) or not isinstance(requirements, dict):
        raise ValueError("hardware e requirements precisam ser objetos")
    unspecified = [str(item) for item in (requirements.get("vram_unspecified") or [])]
    known = int(requirements.get("min_vram_mib") or 0)
    override = requirements.get("min_vram_override")
    if unspecified:
        if override is None or override == "":
            names = ", ".join(unspecified)
            raise ValueError(
                f"VRAM mínima não publicada para {names}. "
                "Informe MIN_VRAM_MIB. Sem downgrade."
            )
        override_mib = int(override)
        if override_mib <= 0 or override_mib < known:
            raise ValueError(
                f"MIN_VRAM_MIB {override_mib} MiB está abaixo do piso conhecido "
                f"{known} MiB. Sem downgrade."
            )
        need = override_mib
    elif override not in (None, ""):
        override_mib = int(override)
        if override_mib < known:
            raise ValueError(
                f"MIN_VRAM_MIB {override_mib} MiB está abaixo do piso conhecido "
                f"{known} MiB. Sem downgrade."
            )
        need = override_mib
    else:
        need = known
    total = hardware.get("vram_total_mib")
    free = hardware.get("vram_free_mib")
    if need > 0:
        if total is None:
            raise ValueError("VRAM não detectada. Sem downgrade.")
        if int(total) < need or (free is not None and int(free) < need):
            raise ValueError(
                f"VRAM insuficiente: total {total} MiB, livre {free} MiB, "
                f"exigido {need} MiB. Sem downgrade."
            )
    ram_need = int(requirements.get("min_ram_mib") or 0)
    ram = hardware.get("ram_available_mib")
    if ram_need > 0 and (ram is None or int(ram) < ram_need):
        raise ValueError(
            f"RAM insuficiente: disponível {ram} MiB, exigido {ram_need} MiB. Sem downgrade."
        )


def resolve_artifact(spec: dict, variables: dict) -> tuple[str, str]:
    """URL e SHA256 pinados, ou as variáveis do notebook. Nunca inventa hash."""
    url = str(spec.get("url") or "").strip()
    sha = str(spec.get("sha256") or "").strip().lower()
    if not url:
        url = str(variables.get(spec.get("url_variable") or "") or "").strip()
    if not sha:
        sha = str(variables.get(spec.get("sha256_variable") or "") or "").strip().lower()
    if not url or not sha:
        filename = spec.get("filename") or "artefato"
        url_var = spec.get("url_variable") or "URL"
        sha_var = spec.get("sha256_variable") or "SHA256"
        raise ValueError(
            f"Artefato {filename} sem URL/SHA256 verificados. "
            f"Preencha {url_var} e {sha_var}. "
            "O repositório não publica esse hash; ele não será inventado."
        )
    return url, sha


def _bind_argv(argv: list[str]) -> list[str]:
    return [COLAB_BIND_HOST if token == "{listen_host}" else token for token in argv]


def _selected_artifacts(norm: dict, catalog: dict) -> list[dict]:
    items: list[dict] = []
    if norm["placements"]["llm"] == "colab":
        entry = catalog["models"]["llm"][norm["models"]["llm"]]
        for art in entry["artifacts"]:
            items.append({**art, "dest": f"{COLAB_ROOT}/{art['dest']}"})
    if norm["placements"]["comfyui"] == "colab":
        for kind in ("image", "video"):
            entry = catalog["models"][kind][norm["models"][kind]]
            for art in entry["artifacts"]:
                items.append({**art, "dest": f"{COLAB_ROOT}/ComfyUI/{art['dest']}"})
    return items


def _variables(norm: dict, catalog: dict, requirements: dict) -> dict:
    variables: dict = {
        "mode": norm["mode"],
        "placements": dict(norm["placements"]),
        "models": dict(norm["models"]),
        "project_url": norm["project_url"],
        "endpoints": dict(norm["endpoints"]),
        "min_vram_mib": requirements["min_vram_mib"],
        "min_ram_mib": requirements["min_ram_mib"],
        "vram_unspecified": list(requirements["vram_unspecified"]),
        "min_vram_mib_override": None,
    }
    if norm["mode"] != "local":
        variables["register_path"] = REGISTER_PATH
        variables["tailscale_hostname"] = "aidungeon-colab"
    if norm["placements"]["llm"] == "colab":
        entry = catalog["models"]["llm"][norm["models"]["llm"]]
        runtime = entry["runtime"]
        variables["llm_port"] = int(runtime["port"])
        variables["llm_health_path"] = runtime["health_path"]
        variables["llm_cuda"] = bool(runtime["cuda"])
        variables["llm_env"] = dict(runtime.get("env") or {})
        dependency = entry["dependencies"][0]
        variables["llama_git_url"] = dependency["git_url"]
        variables["llama_git_ref"] = dependency["git_ref"]
        variables["llama_build"] = dependency["build"]
        for art in entry["artifacts"]:
            if art.get("url_variable"):
                variables[art["url_variable"]] = ""
                variables[art["sha256_variable"]] = ""
    if norm["placements"]["comfyui"] == "colab":
        image = catalog["models"]["image"][norm["models"]["image"]]
        video = catalog["models"]["video"][norm["models"]["video"]]
        if int(image["service"]["port"]) != int(video["service"]["port"]):
            raise ValueError("imagem e vídeo não compartilham a porta do ComfyUI")
        service = image["service"]
        variables["comfy_port"] = int(service["port"])
        variables["comfy_health_path"] = service["health_path"]
        variables["comfy_env"] = dict(service.get("env") or {})
        variables["comfy_argv"] = _bind_argv(list(service["argv"]))
        variables["comfyui_git_url"] = ""
        variables["comfyui_git_ref"] = ""
        variables["comfyui_min_version"] = service["min_version"]
        for dep in video.get("dependencies") or []:
            if dep.get("name") == "ComfyUI-GGUF":
                variables["gguf_node_url"] = dep["git_url"]
                variables["gguf_node_ref"] = dep["git_ref"]
    return variables


def _overview(norm: dict, artifacts: list[dict]) -> str:
    selected = []
    if norm["placements"]["llm"] == "colab":
        selected.append(f"llama-server ({norm['models']['llm']})")
    if norm["placements"]["comfyui"] == "colab":
        selected.append(
            f"ComfyUI ({norm['models']['image']} + {norm['models']['video']})"
        )
    if not selected:
        return (
            "# AI Dungeon — runtime local\n\n"
            "Nenhum serviço está no Colab. Este notebook não instala modelo, "
            "não abre banco e não grava segredo. llm e comfyui continuam nos endpoints locais.\n"
        )
    services = " e ".join(selected)
    secret_names = ["AIDUNGEON_TOKEN", "TAILSCALE_AUTH_KEY"]
    for art in artifacts:
        name = art.get("auth_secret")
        if name and name not in secret_names:
            secret_names.append(name)
    secrets = ", ".join(f"`{name}`" for name in secret_names)
    return (
        "# AI Dungeon — runtime Colab\n\n"
        "Compute efêmero. Não baixa aventura, não abre banco e não grava segredo "
        "neste arquivo. Rode as células em ordem.\n\n"
        f"Serviços neste notebook: {services}.\n\n"
        f"Segredos só no painel do Colab: {secrets} (`AIDUNGEON_TOKEN` deve ser token da conta admin). "
        "VRAM ou RAM abaixo do piso aborta. Não há troca de modelo, passos ou resolução.\n"
    )


def _variables_cell(variables: dict) -> str:
    notes = [
        "# VARIABLES — edite o JSON antes de executar.",
        "# min_vram_mib_override: null, ou MiB inteiro se vram_unspecified não for vazio.",
    ]
    if "narrator_gguf_url" in variables:
        notes.append(
            "# narrator_gguf_url, narrator_gguf_sha256, narrator_mmproj_url e "
            "narrator_mmproj_sha256 são obrigatórios: o repositório não publica esse hash."
        )
    if "comfyui_git_url" in variables:
        notes.append(
            "# comfyui_git_url e comfyui_git_ref são obrigatórios. "
            "Os instaladores só fixam ComfyUI >= 0.42, sem commit."
        )
    blob = json.dumps(variables, ensure_ascii=False, indent=2)
    return (
        "\n".join(notes)
        + "\nimport json\nVARIABLES = json.loads(r\"\"\"\n"
        + blob
        + "\n\"\"\")\n"
    )


def _secrets_cell(variables: dict, artifacts: list[dict]) -> str:
    lines = [
        "from google.colab import userdata",
        "def _optional_secret(loader):",
        "    try:",
        "        return loader() or \"\"",
        "    except Exception:",
        "        return \"\"",
        '# AIDUNGEON_TOKEN precisa ser de uma sessão admin (POST /api/runtime/register exige admin).',
        'AIDUNGEON_TOKEN = _optional_secret(lambda: userdata.get("AIDUNGEON_TOKEN"))',
        'TAILSCALE_AUTH_KEY = _optional_secret(lambda: userdata.get("TAILSCALE_AUTH_KEY"))',
    ]
    secrets = []
    for art in artifacts:
        name = art.get("auth_secret")
        if name and name not in secrets:
            secrets.append(name)
    for name in secrets:
        lines.append(f'{name} = _optional_secret(lambda: userdata.get("{name}"))')
    lines.append("print(\"segredos carregados do painel do Colab\")")
    return "\n".join(lines) + "\n"


def _helper_source() -> str:
    functions = (
        sha256_file,
        _download_url,
        fetch_pinned,
        detect_hardware,
        assert_hardware,
        resolve_artifact,
    )
    parts = [
        "import hashlib",
        "import os",
        "import shutil",
        "import subprocess",
        "import urllib.request",
        "from pathlib import Path",
        "",
    ]
    parts.extend(inspect.getsource(fn).rstrip() for fn in functions)
    return "\n\n".join(parts) + "\n"


def _hardware_cell() -> str:
    return (
        "requirements = {\n"
        '    "min_vram_mib": int(VARIABLES["min_vram_mib"]),\n'
        '    "min_ram_mib": int(VARIABLES["min_ram_mib"]),\n'
        '    "vram_unspecified": list(VARIABLES["vram_unspecified"]),\n'
        '    "min_vram_override": VARIABLES["min_vram_mib_override"],\n'
        "}\n"
        "hardware = detect_hardware()\n"
        "try:\n"
        "    assert_hardware(hardware, requirements)\n"
        "except ValueError as exc:\n"
        "    raise SystemExit(str(exc)) from None\n"
        "print(\"hardware ok\", hardware)\n"
    )


def _tailscale_cell() -> str:
    return (
        "import subprocess\n"
        "if not TAILSCALE_AUTH_KEY:\n"
        "    raise SystemExit(\n"
        "        \"Secret TAILSCALE_AUTH_KEY ausente. Sem ele não há conectividade privada "
        "e o notebook não continua.\"\n"
        "    )\n"
        "subprocess.check_call([\n"
        "    \"bash\", \"-lc\",\n"
        "    \"command -v tailscale >/dev/null 2>&1 || curl -fsSL https://tailscale.com/install.sh | sh\",\n"
        "])\n"
        "subprocess.check_call([\n"
        "    \"tailscale\", \"up\",\n"
        "    \"--auth-key\", TAILSCALE_AUTH_KEY,\n"
        "    \"--hostname\", VARIABLES[\"tailscale_hostname\"],\n"
        "])\n"
        "TAILSCALE_IP = subprocess.check_output(\n"
        "    [\"tailscale\", \"ip\", \"-4\"], text=True\n"
        ").strip().splitlines()[0]\n"
        "print(\"tailscale\", TAILSCALE_IP)\n"
    )


def _llama_command(entry: dict, artifacts: list[dict]) -> list[str]:
    weights_paths = [art["dest"] for art in artifacts if art.get("role") == "weights"]
    if len(weights_paths) != 1:
        raise ValueError("LLM no Colab precisa de exatamente um artefato role=weights")
    weights = weights_paths[0]
    mmproj = next((art["dest"] for art in artifacts if art.get("role") == "mmproj"), None)
    runtime = entry["runtime"]
    before = _bind_argv(list(runtime["argv_before_model"]))
    after = _bind_argv(list(runtime["argv_after_model"]))
    command = ["{binary}", *before, "-m", weights]
    if mmproj:
        command.extend(["--mmproj", mmproj])
    command.extend(after)
    return command


def _provision_cell(norm: dict, catalog: dict, artifacts: list[dict]) -> str:
    lines = [
        "import json",
        "import os",
        "import subprocess",
        "import sys",
        "from pathlib import Path",
        f"ROOT = Path({COLAB_ROOT!r})",
        "ROOT.mkdir(parents=True, exist_ok=True)",
        "ARTIFACTS = json.loads(r\"\"\"",
        json.dumps(artifacts, ensure_ascii=False, indent=2),
        "\"\"\")",
        "for spec in ARTIFACTS:",
        "    try:",
        "        resolve_artifact(spec, VARIABLES)",
        "    except ValueError as exc:",
        "        raise SystemExit(str(exc)) from None",
    ]
    if norm["placements"]["comfyui"] == "colab":
        lines.extend([
            "if not VARIABLES[\"comfyui_git_url\"] or not VARIABLES[\"comfyui_git_ref\"]:",
            "    raise SystemExit(",
            "        \"COMFYUI_GIT_URL e COMFYUI_GIT_REF vazios. \"",
            "        \"Os instaladores só exigem ComfyUI >= \" + str(VARIABLES[\"comfyui_min_version\"]) + ",
            "        \", sem commit pinado. Informe o ref; o notebook não escolhe uma versão mais fraca.\"",
            "    )",
        ])
    if norm["placements"]["llm"] == "colab":
        lines.extend(_llama_install_lines(catalog["models"]["llm"][norm["models"]["llm"]]))
    if norm["placements"]["comfyui"] == "colab":
        video = catalog["models"]["video"][norm["models"]["video"]]
        lines.extend(_comfy_install_lines(video))
    lines.extend([
        "for spec in ARTIFACTS:",
        "    url, sha = resolve_artifact(spec, VARIABLES)",
        "    header = None",
        "    secret_name = spec.get(\"auth_secret\")",
        "    if secret_name:",
        "        secret = globals().get(secret_name) or \"\"",
        "        if not secret:",
        "            raise SystemExit(",
        "                spec[\"filename\"] + \" exige o secret \" + secret_name +",
        "                \" do Colab. A chave não vai no notebook.\"",
        "            )",
        "        header = \"Bearer \" + secret",
        "    print(\"baixando\", spec[\"filename\"], flush=True)",
        "    try:",
        "        fetch_pinned(",
        "            url, spec[\"dest\"], sha, retries=3,",
        "            expected_bytes=spec.get(\"bytes\"), auth_header=header,",
        "        )",
        "    except ValueError as exc:",
        "        raise SystemExit(str(exc)) from None",
    ])
    if norm["placements"]["llm"] == "colab":
        entry = catalog["models"]["llm"][norm["models"]["llm"]]
        command = _llama_command(entry, [
            art for art in artifacts if art["dest"].startswith(f"{COLAB_ROOT}/models/llm/")
        ])
        lines.extend(_start_llama_lines(entry, command))
    if norm["placements"]["comfyui"] == "colab":
        lines.extend(_start_comfy_lines())
    return "\n".join(lines) + "\n"


def _llama_install_lines(entry: dict) -> list[str]:
    cuda = "ON" if entry["runtime"]["cuda"] else "OFF"
    return [
        "subprocess.check_call([\"apt-get\", \"update\"])",
        "subprocess.check_call([\"apt-get\", \"install\", \"-y\", \"build-essential\", \"cmake\", \"git\", \"curl\"])",
        "llama_src = ROOT / \"llama.cpp\"",
        "if not (llama_src / \".git\").is_dir():",
        "    subprocess.check_call([\"git\", \"clone\", VARIABLES[\"llama_git_url\"], str(llama_src)])",
        "checkout = subprocess.run([\"git\", \"checkout\", VARIABLES[\"llama_git_ref\"]], cwd=llama_src)",
        "if checkout.returncode != 0:",
        "    raise SystemExit(",
        "        \"git checkout \" + VARIABLES[\"llama_git_ref\"] + \" falhou. Pin documentado: \"",
        "        + VARIABLES[\"llama_build\"] + \". Sem trocar de build.\"",
        "    )",
        "subprocess.check_call([",
        "    \"cmake\", \"-S\", str(llama_src), \"-B\", str(llama_src / \"build\"),",
        f"    \"-DGGML_CUDA={cuda}\",",
        "])",
        "subprocess.check_call([",
        "    \"cmake\", \"--build\", str(llama_src / \"build\"), \"--target\", \"llama-server\",",
        "    \"-j\", str(os.cpu_count() or 2),",
        "])",
        "binary = llama_src / \"build\" / \"bin\" / \"llama-server\"",
        "if not binary.is_file():",
        "    raise SystemExit(\"llama-server não apareceu em \" + str(binary) + \" após o build pinado\")",
    ]


def _comfy_install_lines(video_entry: dict) -> list[str]:
    lines = [
        "subprocess.check_call([\"apt-get\", \"update\"])",
        "subprocess.check_call([\"apt-get\", \"install\", \"-y\", \"git\", \"curl\"])",
        "comfy_src = ROOT / \"ComfyUI\"",
        "if not (comfy_src / \".git\").is_dir():",
        "    subprocess.check_call([\"git\", \"clone\", VARIABLES[\"comfyui_git_url\"], str(comfy_src)])",
        "checkout = subprocess.run([\"git\", \"checkout\", VARIABLES[\"comfyui_git_ref\"]], cwd=comfy_src)",
        "if checkout.returncode != 0:",
        "    raise SystemExit(",
        "        \"git checkout do ComfyUI falhou no ref informado. Sem fallback de versão.\"",
        "    )",
        "subprocess.check_call([sys.executable, \"-m\", \"pip\", \"install\", \"-r\", \"requirements.txt\"], cwd=comfy_src)",
    ]
    for dep in video_entry.get("dependencies") or []:
        if dep.get("name") != "ComfyUI-GGUF":
            continue
        lines.extend([
            "node = comfy_src / \"custom_nodes\" / \"ComfyUI-GGUF\"",
            "if not (node / \".git\").is_dir():",
            "    subprocess.check_call([\"git\", \"clone\", VARIABLES[\"gguf_node_url\"], str(node)])",
            "subprocess.check_call([\"git\", \"fetch\", \"origin\", VARIABLES[\"gguf_node_ref\"]], cwd=node)",
            "checkout = subprocess.run([\"git\", \"checkout\", VARIABLES[\"gguf_node_ref\"]], cwd=node)",
            "if checkout.returncode != 0:",
            "    raise SystemExit(\"checkout do ComfyUI-GGUF falhou no commit pinado\")",
            "subprocess.check_call([sys.executable, \"-m\", \"pip\", \"install\", \"-r\", \"requirements.txt\"], cwd=node)",
        ])
    return lines


def _start_llama_lines(entry: dict, command: list[str]) -> list[str]:
    rendered = json.dumps(command, ensure_ascii=False)
    env_items = entry["runtime"].get("env") or {}
    env_literal = json.dumps(env_items, ensure_ascii=False)
    return [
        f"llama_cmd = json.loads({rendered!r})",
        "llama_cmd[0] = str(binary)",
        "log_dir = ROOT / \"logs\"",
        "log_dir.mkdir(parents=True, exist_ok=True)",
        "llama_log = (log_dir / \"llama-server.log\").open(\"ab\")",
        "llama_env = os.environ.copy()",
        f"llama_env.update(json.loads({env_literal!r}))",
        "subprocess.Popen(llama_cmd, env=llama_env, stdout=llama_log, stderr=llama_log, start_new_session=True)",
        "print(\"llama-server iniciado\", llama_cmd[0], \"porta\", VARIABLES[\"llm_port\"])",
    ]


def _start_comfy_lines() -> list[str]:
    return [
        "comfy_cmd = [sys.executable, \"main.py\", *VARIABLES[\"comfy_argv\"]]",
        "log_dir = ROOT / \"logs\"",
        "log_dir.mkdir(parents=True, exist_ok=True)",
        "comfy_log = (log_dir / \"comfyui.log\").open(\"ab\")",
        "comfy_env = os.environ.copy()",
        "comfy_env.update(VARIABLES[\"comfy_env\"])",
        "subprocess.Popen(comfy_cmd, cwd=str(ROOT / \"ComfyUI\"), env=comfy_env, stdout=comfy_log, stderr=comfy_log, start_new_session=True)",
        "print(\"ComfyUI iniciado na porta\", VARIABLES[\"comfy_port\"])",
    ]


def _health_cell(norm: dict) -> str:
    lines = [
        "import time",
        "import urllib.request",
        "def wait_http(url, attempts=60, pause=5):",
        "    last = None",
        "    for _ in range(attempts):",
        "        try:",
        "            with urllib.request.urlopen(url, timeout=5) as resp:",
        "                if 200 <= resp.status < 300:",
        "                    return",
        "                last = resp.status",
        "        except Exception as exc:",
        "            last = exc.__class__.__name__",
        "        time.sleep(pause)",
        "    raise SystemExit(\"health check falhou: \" + url + \" (\" + str(last) + \")\")",
    ]
    if norm["placements"]["llm"] == "colab":
        lines.append(
            "wait_http(\"http://127.0.0.1:\" + str(VARIABLES[\"llm_port\"]) + VARIABLES[\"llm_health_path\"])"
        )
    if norm["placements"]["comfyui"] == "colab":
        lines.append(
            "wait_http(\"http://127.0.0.1:\" + str(VARIABLES[\"comfy_port\"]) + VARIABLES[\"comfy_health_path\"])"
        )
    lines.append("print(\"health ok\")")
    return "\n".join(lines) + "\n"


def _register_cell(norm: dict) -> str:
    llm_local = norm["placements"]["llm"] != "colab"
    comfy_local = norm["placements"]["comfyui"] != "colab"
    llm_expr = (
        '""'
        if llm_local
        else '"http://" + TAILSCALE_IP + ":" + str(VARIABLES["llm_port"]) + "/v1"'
    )
    comfy_expr = (
        '""'
        if comfy_local
        else '"http://" + TAILSCALE_IP + ":" + str(VARIABLES["comfy_port"])'
    )
    return (
        "import json\n"
        "import urllib.error\n"
        "import urllib.request\n"
        "if not VARIABLES[\"project_url\"]:\n"
        "    print(\"registro omitido: project_url vazio\")\n"
        "elif not AIDUNGEON_TOKEN:\n"
        "    print(\"registro omitido: secret AIDUNGEON_TOKEN vazio\")\n"
        "else:\n"
        f"    llm_endpoint = {llm_expr}\n"
        f"    comfy_endpoint = {comfy_expr}\n"
        "    hardware = detect_hardware()\n"
        "    payload = {\n"
        "        \"endpoints\": {\"llm\": llm_endpoint, \"comfyui\": comfy_endpoint},\n"
        "        \"models\": {\n"
        "            \"llm\": VARIABLES[\"models\"][\"llm\"],\n"
        "            \"image\": VARIABLES[\"models\"][\"image\"],\n"
        "            \"video\": VARIABLES[\"models\"][\"video\"],\n"
        "        },\n"
        "        \"hardware\": hardware,\n"
        "    }\n"
        "    target = VARIABLES[\"project_url\"].rstrip(\"/\")\n"
        "    suffix = VARIABLES[\"register_path\"]\n"
        "    if not target.endswith(suffix):\n"
        "        target = target + suffix\n"
        "    request = urllib.request.Request(\n"
        "        target,\n"
        "        data=json.dumps(payload).encode(\"utf-8\"),\n"
        "        headers={\n"
        "            \"Authorization\": \"Bearer \" + AIDUNGEON_TOKEN,\n"
        "            \"Content-Type\": \"application/json\",\n"
        "            \"Accept\": \"application/json\",\n"
        "        },\n"
        "        method=\"POST\",\n"
        "    )\n"
        "    try:\n"
        "        with urllib.request.urlopen(request, timeout=30) as resp:\n"
        "            if resp.status not in (200, 201, 204):\n"
        "                raise SystemExit(\"registro recusado: HTTP \" + str(resp.status))\n"
        "    except urllib.error.HTTPError as exc:\n"
        "        hint = \" (AIDUNGEON_TOKEN precisa ser da conta admin)\" if exc.code == 403 else \"\"\n"
        "        raise SystemExit(\"registro recusado: HTTP \" + str(exc.code) + hint) from None\n"
        "    print(\"registro ok\")\n"
    )


def _code_cell(role: str, source: str) -> dict:
    if not source.endswith("\n"):
        source += "\n"
    return {
        "id": role,
        "cell_type": "code",
        "execution_count": None,
        "metadata": {"aidungeon_role": role},
        "outputs": [],
        "source": source.splitlines(keepends=True),
    }


def _markdown_cell(role: str, source: str) -> dict:
    if not source.endswith("\n"):
        source += "\n"
    return {
        "id": role,
        "cell_type": "markdown",
        "metadata": {"aidungeon_role": role},
        "source": source.splitlines(keepends=True),
    }


def _local_notice_cell() -> str:
    return (
        "if VARIABLES[\"mode\"] != \"local\":\n"
        "    raise SystemExit(\"notebook local inconsistente\")\n"
        "print(\"mode local: llm e comfyui ficam no runtime do projeto. Nada foi instalado.\")\n"
    )


def generate_notebook(config: dict, catalog: dict | None = None) -> dict:
    """Notebook nbformat 4. Modo local não instala serviço; Colab sobe só o placement colab."""
    book = catalog if catalog is not None else load_catalog()
    norm = validate_runtime(config, book, require_colab=False)
    requirements = hardware_requirements(norm, book)
    variables = _variables(norm, book, requirements)
    artifacts = _selected_artifacts(norm, book)
    if norm["mode"] == "local":
        cells = [
            _markdown_cell("overview", _overview(norm, artifacts)),
            _code_cell("variables", _variables_cell(variables)),
            _code_cell("local", _local_notice_cell()),
        ]
    else:
        cells = [
            _markdown_cell("overview", _overview(norm, artifacts)),
            _code_cell("variables", _variables_cell(variables)),
            _code_cell("secrets", _secrets_cell(variables, artifacts)),
            _code_cell("helpers", _helper_source()),
            _code_cell("hardware", _hardware_cell()),
            _code_cell("tailscale", _tailscale_cell()),
            _code_cell("provision", _provision_cell(norm, book, artifacts)),
            _code_cell("health", _health_cell(norm)),
            _code_cell("register", _register_cell(norm)),
        ]
    return {
        "nbformat": 4,
        "nbformat_minor": 5,
        "metadata": {
            "kernelspec": {
                "display_name": "Python 3",
                "language": "python",
                "name": "python3",
            },
            "language_info": {"name": "python", "pygments_lexer": "ipython3"},
            "aidungeon_runtime": {
                "mode": norm["mode"],
                "placements": norm["placements"],
                "models": norm["models"],
            },
        },
        "cells": cells,
    }


COLAB_APP_GIT_URL = "https://github.com/Caiolinooo/aidungeon-local.git"
COLAB_APP_GIT_REF = "main"
COLAB_APP_ROOT = "/content/aidungeon-local"
COLAB_COMFY_GIT_URL = "https://github.com/comfyanonymous/ComfyUI.git"
COLAB_COMFY_GIT_REF = "v0.46.0"
COLAB_NODE_TARBALL = (
    "https://nodejs.org/dist/v20.18.1/node-v20.18.1-linux-x64.tar.xz"
)
COLAB_PLAY_LLM = "qwen2.5-3b-instruct-q4_k_m"
COLAB_PLAY_IMAGE = "flux2_klein"


def _pinned_artifact(catalog: dict, group: str, model_id: str, role: str) -> dict:
    """Artefato do catálogo com URL e SHA256. Sem os dois, o Colab não baixa."""
    try:
        artifacts = catalog["models"][group][model_id]["artifacts"]
    except (KeyError, TypeError) as exc:
        raise ValueError(f"catálogo sem {group}.{model_id}") from exc
    for art in artifacts:
        if art.get("role") != role:
            continue
        if not art.get("url") or not art.get("sha256"):
            raise ValueError(f"{group}.{model_id}.{role} sem URL ou SHA256 publicados")
        return art
    raise ValueError(f"catálogo sem artefato {role} em {group}.{model_id}")


def _llama_pin(catalog: dict) -> dict:
    deps = catalog["models"]["llm"][COLAB_PLAY_LLM].get("dependencies") or []
    if not deps or not deps[0].get("git_url") or not deps[0].get("git_ref"):
        raise ValueError("catálogo do narrador Colab sem git do llama.cpp")
    return deps[0]


def _download_host(url: str) -> str:
    host = (urllib.parse.urlparse(url).hostname or "").lower()
    if host in {"huggingface.co", "hf.co"} or host.endswith(".huggingface.co"):
        return "huggingface"
    if (
        host in {"civitai.com", "redcivit.ai", "redcivit.com"}
        or host.endswith(".civitai.com")
        or host.endswith(".redcivit.ai")
        or host.endswith(".redcivit.com")
    ):
        return "civitai"
    return "link"


_EXTRA_COLAB_PRESETS: dict[str, dict[str, dict]] = {
    "llm": {
        "qwen2.5-7b-instruct-q4_k_m": {
            "model_alias": "Qwen2.5-7B-Instruct (GGUF Q4_K_M · Hugging Face)",
            "artifacts": [
                {
                    "role": "weights",
                    "filename": "qwen2.5-7b-instruct-q4_k_m.gguf",
                    "dest": "models/llm/qwen2.5-7b-instruct-q4_k_m.gguf",
                    "url": "https://huggingface.co/bartowski/Qwen2.5-7B-Instruct-GGUF/resolve/main/Qwen2.5-7B-Instruct-Q4_K_M.gguf?download=true",
                    "sha256": "",
                    "bytes": None,
                }
            ],
        },
        "llama-3.1-8b-lexi-uncensored-v2-q4_k_m": {
            "model_alias": "Llama-3.1-8B-Lexi-Uncensored-V2 (GGUF Q4_K_M · Hugging Face)",
            "artifacts": [
                {
                    "role": "weights",
                    "filename": "Llama-3.1-8B-Lexi-Uncensored_V2_Q4.gguf",
                    "dest": "models/llm/Llama-3.1-8B-Lexi-Uncensored_V2_Q4.gguf",
                    "url": "https://huggingface.co/Orenguteng/Llama-3.1-8B-Lexi-Uncensored-V2-GGUF/resolve/main/Llama-3.1-8B-Lexi-Uncensored_V2_Q4.gguf?download=true",
                    "sha256": "",
                    "bytes": None,
                }
            ],
        },
    },
}


def _merged_colab_section(book: dict, group: str) -> dict:
    base = dict(((book.get("models") or {}).get(group)) or {})
    for model_id, entry in (_EXTRA_COLAB_PRESETS.get(group) or {}).items():
        if model_id not in base:
            base[model_id] = entry
    return base


def colab_model_choices(catalog: dict | None = None) -> list[dict]:
    """Modelos do catálogo e presets extras prontos para o Colab baixar."""
    book = catalog if catalog is not None else load_catalog()
    choices = []
    for group in ("llm", "image", "video"):
        section = _merged_colab_section(book, group)
        for model_id, entry in section.items():
            files = []
            hosts = set()
            needs_key = False
            for art in entry.get("artifacts") or []:
                url = str(art.get("url") or "")
                sha = str(art.get("sha256") or "")
                is_extra = model_id in (_EXTRA_COLAB_PRESETS.get(group) or {})
                if not url or (not sha and not is_extra):
                    continue
                hosts.add(_download_host(url))
                if art.get("auth_secret"):
                    needs_key = True
                files.append(art.get("filename") or "")
            if not files:
                continue
            if hosts == {"civitai"}:
                source = "civitai"
            elif "civitai" in hosts:
                source = "mixed"
            else:
                source = "huggingface"
            choices.append({
                "id": f"{group}:{model_id}",
                "group": group,
                "model_id": model_id,
                "label": str(entry.get("model_alias") or model_id),
                "source": source,
                "engine": str(entry.get("engine") or ""),
                "needs_civitai_key": needs_key,
                "files": [name for name in files if name],
            })
    return choices


def _safe_filename(name: str) -> str:
    cleaned = Path(str(name or "")).name.strip()
    if not cleaned or cleaned in {".", ".."} or "/" in cleaned or "\\" in cleaned:
        raise ValueError("nome de arquivo inválido")
    if not re.fullmatch(r"[A-Za-z0-9._+-]+", cleaned):
        raise ValueError("o nome do arquivo só pode ter letras, números, ponto, _ e -")
    return cleaned


def _infer_filename_from_url(parsed: urllib.parse.SplitResult, role: str) -> str:
    candidate = Path(urllib.parse.unquote(parsed.path or "")).name.strip()
    if candidate and re.fullmatch(r"[A-Za-z0-9._+-]+", candidate) and "." in candidate:
        return candidate
    ext = ".gguf" if role == "llm" else ".safetensors"
    slug = re.sub(r"[^A-Za-z0-9._+-]+", "-", candidate or f"custom-{role}").strip("-") or f"custom-{role}"
    if not slug.endswith((".gguf", ".safetensors", ".ckpt", ".pt", ".bin")):
        slug = f"{slug}{ext}"
    return _safe_filename(slug)


def _manual_download(item: dict) -> dict:
    if not isinstance(item, dict):
        raise ValueError("modelo manual inválido")
    url = str(item.get("url") or "").strip()
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != "https" or not parsed.hostname:
        raise ValueError("o link do modelo precisa ser https")
    role = str(item.get("role") or "").strip()
    if role not in {"llm", "unet", "clip", "vae", "checkpoint", "video"}:
        raise ValueError("o tipo do link manual precisa ser llm, unet, clip, vae, checkpoint ou video")
    raw_name = str(item.get("filename") or "").strip()
    filename = _safe_filename(raw_name) if raw_name else _infer_filename_from_url(parsed, role)
    sha = str(item.get("sha256") or "").strip().lower()
    if sha and not re.fullmatch(r"[0-9a-f]{64}", sha):
        raise ValueError("SHA256 do link manual precisa ter 64 caracteres hexadecimais")
    folders = {
        "llm": f"{COLAB_APP_ROOT}/models/llm/{filename}",
        "unet": f"{COLAB_APP_ROOT}/ComfyUI/models/diffusion_models/{filename}",
        "video": f"{COLAB_APP_ROOT}/ComfyUI/models/diffusion_models/{filename}",
        "clip": f"{COLAB_APP_ROOT}/ComfyUI/models/text_encoders/{filename}",
        "vae": f"{COLAB_APP_ROOT}/ComfyUI/models/vae/{filename}",
        "checkpoint": f"{COLAB_APP_ROOT}/ComfyUI/models/checkpoints/{filename}",
    }
    secret = "CIVITAI_API_KEY" if _download_host(url) == "civitai" else ""
    return {
        "filename": filename,
        "url": url,
        "sha256": sha,
        "bytes": None,
        "dest": folders[role],
        "auth_secret": secret,
        "role": "weights" if role == "llm" else role,
        "group": "llm" if role == "llm" else "manual",
    }


def resolve_colab_downloads(catalog: dict, models: list[str] | None,
                            manual: list[dict] | None) -> tuple[list[dict], dict]:
    """Monta a lista de download e o que o jogo liga.

    `models` None usa o narrador 3B e o FLUX.2 Klein. Lista vazia sem link
    manual não gera notebook.
    """
    book = catalog
    selected = (
        [f"llm:{COLAB_PLAY_LLM}", f"image:{COLAB_PLAY_IMAGE}"]
        if models is None else list(models)
    )
    downloads: list[dict] = []
    image_engine = ""
    video_engine = ""
    image_files: dict[str, str] = {}
    video_files: dict[str, str] = {}
    llm_filename = ""
    for raw_id in selected:
        text = str(raw_id or "").strip()
        if ":" not in text:
            raise ValueError(f"modelo desconhecido: {text}")
        group, model_id = text.split(":", 1)
        if group not in {"llm", "image", "video"}:
            raise ValueError(f"modelo desconhecido: {text}")
        section = _merged_colab_section(book, group)
        if model_id not in section:
            raise ValueError(f"modelo desconhecido: {text}")
        entry = section[model_id]
        is_extra = model_id in (_EXTRA_COLAB_PRESETS.get(group) or {})
        if group == "image" and not image_engine:
            image_engine = str(entry.get("engine") or "")
        if group == "video" and not video_engine:
            video_engine = str(entry.get("engine") or model_id)
        for art in entry.get("artifacts") or []:
            url = str(art.get("url") or "")
            sha = str(art.get("sha256") or "")
            if not url or (not sha and not is_extra):
                continue
            filename = _safe_filename(art.get("filename") or Path(urllib.parse.urlparse(url).path).name)
            role = str(art.get("role") or "")
            if group == "llm":
                dest = f"{COLAB_APP_ROOT}/models/llm/{filename}"
                if role == "weights" and not llm_filename:
                    llm_filename = filename
            else:
                rel = str(art.get("dest") or f"models/{filename}")
                if rel.startswith("/") or ".." in Path(rel).parts:
                    raise ValueError(f"destino inválido em {text}")
                dest = f"{COLAB_APP_ROOT}/ComfyUI/{rel}"
                bucket = image_files if group == "image" else video_files
                if role in {"unet", "clip", "vae", "checkpoint"} and role not in bucket:
                    bucket[role] = filename
            downloads.append({
                "filename": filename,
                "url": url,
                "sha256": sha,
                "bytes": art.get("bytes"),
                "dest": dest,
                "auth_secret": str(art.get("auth_secret") or ""),
                "role": role,
                "group": group,
            })
    for item in manual or []:
        spec = _manual_download(item)
        downloads.append(spec)
        if spec["role"] == "weights" and not llm_filename:
            llm_filename = spec["filename"]
        elif spec["role"] in {"unet", "clip", "vae", "checkpoint"} and spec["role"] not in image_files:
            image_files[spec["role"]] = spec["filename"]
            if not image_engine and spec["role"] == "unet":
                image_engine = "flux2_klein"
    if not downloads:
        raise ValueError("escolha ao menos um modelo do catálogo ou um link https")
    if not llm_filename:
        raise ValueError("escolha um narrador (GGUF) ou cole o link de um modelo de texto")
    needs_gguf_node = any(
        "/ComfyUI/" in item["dest"] and str(item["filename"]).lower().endswith(".gguf")
        for item in downloads
    )
    profile = {
        "llm_filename": llm_filename,
        "start_comfy": any("/ComfyUI/" in item["dest"] for item in downloads),
        "needs_gguf_node": needs_gguf_node,
        "image_engine": image_engine,
        "image_files": image_files,
        "video_engine": video_engine,
        "video_files": video_files,
        "needs_civitai_key": any(item.get("auth_secret") == "CIVITAI_API_KEY" for item in downloads),
    }
    return downloads, profile


def generate_colab_project(catalog: dict | None = None, *,
                           git_url: str = COLAB_APP_GIT_URL,
                           git_ref: str = COLAB_APP_GIT_REF,
                           models: list[str] | None = None,
                           manual: list[dict] | None = None) -> dict:
    """Notebook que abre no Colab e sobe o jogo com os modelos escolhidos.

    Sem `models`, baixa o Qwen2.5-3B e o FLUX.2 Klein. Com lista, usa o
    catálogo (Hugging Face ou CivitAI) e os links manuais. A senha de login
    nasce na execução e não entra no arquivo. Voz e embeddings ficam desligados.
    """
    book = catalog if catalog is not None else load_catalog()
    llama = _llama_pin(book)
    downloads, profile = resolve_colab_downloads(book, models, manual)
    names = ", ".join(item["filename"] for item in downloads)
    overview = (
        "# AI Dungeon — projeto Colab (End-to-End · 1 Clique)\n\n"
        "Abra este arquivo no Colab, escolha uma GPU (T4 ou melhor) em "
        "Ambiente de execução → Alterar o tipo de ambiente de execução, "
        "e clique em **Executar tudo** (Run all). Nenhum segredo ou variável manual é exigido.\n\n"
        f"O notebook clona `{git_url}` em `{git_ref}`, compila o llama-server "
        f"({llama['git_ref']}) e baixa: {names}.\n\n"
        "No fim da execução ele imprime um link público `https://...trycloudflare.com/?aidl_token=...` "
        "que já entra logado em 1 clique (além do usuário `Caio` e senha gerada na hora).\n"
    )
    if profile["needs_civitai_key"]:
        overview += (
            "\nOpcional: para links privados do CivitAI, você pode definir `CIVITAI_API_KEY` no painel "
            "Secrets do Colab (se não definir, o notebook tenta o download direto sem falhar).\n"
        )
    cells = [
        _markdown_cell("overview", overview),
        _code_cell("clone", _colab_clone_cell(git_url, git_ref)),
        _code_cell("frontend", _colab_frontend_cell()),
    ]
    if profile["start_comfy"]:
        cells.append(_code_cell("comfy", _colab_comfy_cell(needs_gguf_node=profile["needs_gguf_node"])))
    if profile["needs_civitai_key"]:
        cells.append(_code_cell("civitai", _colab_civitai_cell()))
    cells.extend([
        _code_cell("weights", _colab_weights_cell(downloads)),
        _code_cell("llama", _colab_llama_cell(llama, profile["llm_filename"])),
        _code_cell("serve", _colab_serve_cell(profile)),
        _code_cell("tunnel", _colab_tunnel_cell()),
    ])
    return {
        "nbformat": 4,
        "nbformat_minor": 5,
        "metadata": {
            "accelerator": "GPU",
            "colab": {"provenance": [], "gpuType": "T4"},
            "kernelspec": {
                "display_name": "Python 3",
                "language": "python",
                "name": "python3",
            },
            "language_info": {"name": "python", "pygments_lexer": "ipython3"},
            "aidungeon_colab_project": {
                "git_url": git_url,
                "git_ref": git_ref,
                "llm_filename": profile["llm_filename"],
                "image_engine": profile["image_engine"],
                "video_engine": profile["video_engine"],
                "files": [item["filename"] for item in downloads],
                "comfy_ref": COLAB_COMFY_GIT_REF,
            },
        },
        "cells": cells,
    }


def _colab_clone_cell(git_url: str, git_ref: str) -> str:
    return (
        "import subprocess\n"
        "import sys\n"
        "from pathlib import Path\n"
        f"REPO = Path({COLAB_APP_ROOT!r})\n"
        f"GIT_URL = {git_url!r}\n"
        f"GIT_REF = {git_ref!r}\n"
        "if not (REPO / \".git\").is_dir():\n"
        "    subprocess.check_call([\"git\", \"clone\", GIT_URL, str(REPO)])\n"
        "subprocess.check_call([\"git\", \"fetch\", \"origin\", GIT_REF], cwd=REPO)\n"
        "subprocess.check_call([\"git\", \"checkout\", \"-f\", GIT_REF], cwd=REPO)\n"
        "subprocess.check_call([\"git\", \"reset\", \"--hard\", f\"origin/{GIT_REF}\"], cwd=REPO)\n"
        "subprocess.check_call([sys.executable, \"-m\", \"pip\", \"install\", \"-q\", \"-r\", \"backend/requirements.txt\"], cwd=REPO)\n"
        "print(\"repositório\", GIT_REF)\n"
    )


def _colab_frontend_cell() -> str:
    return (
        "import os\n"
        "import subprocess\n"
        "import tarfile\n"
        "import urllib.request\n"
        "from pathlib import Path\n"
        f"REPO = Path({COLAB_APP_ROOT!r})\n"
        f"NODE_URL = {COLAB_NODE_TARBALL!r}\n"
        "archive = Path(\"/tmp/node-colab.tar.xz\")\n"
        "if not list(Path(\"/usr/local/lib\").glob(\"node-v*-linux-x64\")):\n"
        "    urllib.request.urlretrieve(NODE_URL, archive)\n"
        "    with tarfile.open(archive) as bundle:\n"
        "        bundle.extractall(\"/usr/local/lib\")\n"
        "extracted = next(Path(\"/usr/local/lib\").glob(\"node-v*-linux-x64\"))\n"
        "for name in (\"node\", \"npm\", \"npx\"):\n"
        "    dest = Path(\"/usr/local/bin\") / name\n"
        "    if dest.exists() or dest.is_symlink():\n"
        "        dest.unlink()\n"
        "    dest.symlink_to(extracted / \"bin\" / name)\n"
        "env = os.environ.copy()\n"
        "env[\"PATH\"] = str(extracted / \"bin\") + \":/usr/local/bin:\" + env.get(\"PATH\", \"\")\n"
        "subprocess.check_call([\"npm\", \"ci\"], cwd=REPO / \"frontend\", env=env)\n"
        "subprocess.check_call([\"npm\", \"run\", \"build\"], cwd=REPO / \"frontend\", env=env)\n"
        "print(\"frontend\", REPO / \"frontend\" / \"dist\")\n"
    )


def _colab_weights_cell(downloads: list[dict]) -> str:
    blob = json.dumps(downloads, ensure_ascii=False, indent=2)
    return (
        "import hashlib\n"
        "import json\n"
        "import time\n"
        "import urllib.request\n"
        "from pathlib import Path\n"
        "DOWNLOADS = json.loads(r\"\"\"\n"
        + blob
        + "\n\"\"\")\n"
        "def _file_sha256(path):\n"
        "    h = hashlib.sha256()\n"
        "    with path.open(\"rb\") as f:\n"
        "        while True:\n"
        "            chunk = f.read(1024 * 1024)\n"
        "            if not chunk:\n"
        "                break\n"
        "            h.update(chunk)\n"
        "    return h.hexdigest()\n"
        "def fetch_one(spec):\n"
        "    dest = Path(spec[\"dest\"])\n"
        "    dest.parent.mkdir(parents=True, exist_ok=True)\n"
        "    expected_sha = (spec.get(\"sha256\") or \"\").strip().lower()\n"
        "    expected = spec.get(\"bytes\")\n"
        "    if dest.is_file() and dest.stat().st_size > 0:\n"
        "        if (not expected or dest.stat().st_size == int(expected)) and (not expected_sha or _file_sha256(dest) == expected_sha):\n"
        "            print(\"já baixado\", spec[\"filename\"], flush=True)\n"
        "            return\n"
        "    part = dest.with_suffix(dest.suffix + \".part\")\n"
        "    last = None\n"
        "    for attempt in range(3):\n"
        "        try:\n"
        "            headers = {\"User-Agent\": \"aidungeon-colab\"}\n"
        "            secret_name = spec.get(\"auth_secret\") or \"\"\n"
        "            if secret_name:\n"
        "                key = globals().get(secret_name) or \"\"\n"
        "                if key:\n"
        "                    headers[\"Authorization\"] = \"Bearer \" + key\n"
        "            request = urllib.request.Request(spec[\"url\"], headers=headers)\n"
        "            hasher = hashlib.sha256()\n"
        "            with urllib.request.urlopen(request, timeout=180) as resp, part.open(\"wb\") as out:\n"
        "                while True:\n"
        "                    chunk = resp.read(1024 * 1024)\n"
        "                    if not chunk:\n"
        "                        break\n"
        "                    out.write(chunk)\n"
        "                    hasher.update(chunk)\n"
        "            digest = hasher.hexdigest()\n"
        "            if expected_sha and digest != expected_sha:\n"
        "                raise ValueError(\"SHA256 divergente em \" + spec[\"filename\"])\n"
        "            if expected and part.stat().st_size != int(expected):\n"
        "                raise ValueError(\"tamanho inesperado em \" + spec[\"filename\"])\n"
        "            part.replace(dest)\n"
        "            print(\"ok\", spec[\"filename\"], flush=True)\n"
        "            return\n"
        "        except Exception as exc:\n"
        "            last = exc\n"
        "            time.sleep(2 * (attempt + 1))\n"
        "    raise SystemExit(str(last))\n"
        "for spec in DOWNLOADS:\n"
        "    fetch_one(spec)\n"
    )


def _colab_civitai_cell() -> str:
    return (
        "from google.colab import userdata\n"
        "try:\n"
        "    CIVITAI_API_KEY = userdata.get(\"CIVITAI_API_KEY\") or \"\"\n"
        "except Exception:\n"
        "    CIVITAI_API_KEY = \"\"\n"
        "print(\"chave CivitAI carregada\" if CIVITAI_API_KEY else \"sem CIVITAI_API_KEY no painel; tentando download direto\")\n"
    )


def _colab_llama_cell(llama: dict, llm_filename: str) -> str:
    return (
        "import os\n"
        "import subprocess\n"
        "from pathlib import Path\n"
        f"REPO = Path({COLAB_APP_ROOT!r})\n"
        f"LLAMA_GIT = {llama['git_url']!r}\n"
        f"LLAMA_REF = {llama['git_ref']!r}\n"
        "src = REPO / \"colab\" / \"llama.cpp\"\n"
        "binary = src / \"build\" / \"bin\" / \"llama-server\"\n"
        "if not binary.is_file():\n"
        "    if not (src / \".git\").is_dir():\n"
        "        src.parent.mkdir(parents=True, exist_ok=True)\n"
        "        subprocess.check_call([\"git\", \"clone\", LLAMA_GIT, str(src)])\n"
        "    checkout = subprocess.run([\"git\", \"checkout\", LLAMA_REF], cwd=src)\n"
        "    if checkout.returncode != 0:\n"
        "        subprocess.check_call([\"git\", \"fetch\", \"--all\"], cwd=src)\n"
        "        checkout = subprocess.run([\"git\", \"checkout\", LLAMA_REF], cwd=src)\n"
        "    if checkout.returncode != 0:\n"
        "        raise SystemExit(\"git checkout do llama.cpp falhou em \" + LLAMA_REF)\n"
        "    subprocess.check_call([\"apt-get\", \"update\"])\n"
        "    subprocess.check_call([\"apt-get\", \"install\", \"-y\", \"build-essential\", \"cmake\", \"git\"])\n"
        "    subprocess.check_call([\n"
        "        \"cmake\", \"-S\", str(src), \"-B\", str(src / \"build\"),\n"
        "        \"-DGGML_CUDA=ON\", \"-DCMAKE_CUDA_ARCHITECTURES=native\",\n"
        "    ])\n"
        "    subprocess.check_call([\"cmake\", \"--build\", str(src / \"build\"), \"--target\", \"llama-server\", \"-j\", str(os.cpu_count() or 2)])\n"
        "if not binary.is_file():\n"
        "    raise SystemExit(\"llama-server não foi gerado\")\n"
        f"weights = REPO / \"models\" / \"llm\" / {llm_filename!r}\n"
        "if not weights.is_file():\n"
        "    raise SystemExit(\"peso do narrador ausente: \" + weights.name)\n"
        "log = (REPO / \"colab\")\n"
        "log.mkdir(parents=True, exist_ok=True)\n"
        "handle = (log / \"llama-server.log\").open(\"ab\")\n"
        "subprocess.run([\"pkill\", \"-f\", \"llama-server.*--port 1234\"], check=False)\n"
        "subprocess.Popen([\n"
        "    str(binary), \"--host\", \"127.0.0.1\", \"--port\", \"1234\",\n"
        "    \"-m\", str(weights), \"-ngl\", \"99\", \"-c\", \"8192\", \"-np\", \"1\",\n"
        "    \"-t\", \"4\", \"-b\", \"512\", \"-ub\", \"512\",\n"
        "], stdout=handle, stderr=handle, start_new_session=True)\n"
        "print(\"llama-server\", weights.name)\n"
    )


def _colab_comfy_cell(*, needs_gguf_node: bool = False) -> str:
    gguf_lines = ""
    if needs_gguf_node:
        gguf_lines = (
            "gguf_node = src / \"custom_nodes\" / \"ComfyUI-GGUF\"\n"
            "if not (gguf_node / \".git\").is_dir():\n"
            "    subprocess.check_call([\"git\", \"clone\", \"https://github.com/city96/ComfyUI-GGUF.git\", str(gguf_node)])\n"
            "subprocess.check_call([sys.executable, \"-m\", \"pip\", \"install\", \"-q\", \"-r\", \"requirements.txt\"], cwd=gguf_node)\n"
        )
    return (
        "import subprocess\n"
        "import sys\n"
        "from pathlib import Path\n"
        f"REPO = Path({COLAB_APP_ROOT!r})\n"
        f"COMFY_GIT = {COLAB_COMFY_GIT_URL!r}\n"
        f"COMFY_REF = {COLAB_COMFY_GIT_REF!r}\n"
        "src = REPO / \"ComfyUI\"\n"
        "if not (src / \".git\").is_dir():\n"
        "    subprocess.check_call([\"git\", \"clone\", COMFY_GIT, str(src)])\n"
        "subprocess.check_call([\"git\", \"fetch\", \"origin\", \"tag\", COMFY_REF, \"--no-tags\"], cwd=src)\n"
        "checkout = subprocess.run([\"git\", \"checkout\", COMFY_REF], cwd=src)\n"
        "if checkout.returncode != 0:\n"
        "    raise SystemExit(\"git checkout do ComfyUI falhou em \" + COMFY_REF)\n"
        "subprocess.check_call([sys.executable, \"-m\", \"pip\", \"install\", \"-q\", \"-r\", \"requirements.txt\"], cwd=src)\n"
        + gguf_lines
        + "import torch\n"
        "if not torch.cuda.is_available():\n"
        "    raise SystemExit(\"PyTorch ficou sem CUDA depois do ComfyUI. Troque o ambiente para GPU e execute de novo.\")\n"
        "(REPO / \"colab\").mkdir(parents=True, exist_ok=True)\n"
        "log = (REPO / \"colab\" / \"comfyui.log\").open(\"ab\")\n"
        "subprocess.run([\"pkill\", \"-f\", \"main.py.*--port 8188\"], check=False)\n"
        "subprocess.Popen([\n"
        "    sys.executable, \"main.py\", \"--listen\", \"127.0.0.1\", \"--port\", \"8188\",\n"
        "], cwd=src, stdout=log, stderr=log, start_new_session=True)\n"
        "print(\"ComfyUI\", COMFY_REF)\n"
    )


def _colab_serve_cell(profile: dict) -> str:
    picked = {
        "start_comfy": bool(profile["start_comfy"]),
        "image_engine": profile["image_engine"],
        "image_files": profile["image_files"],
        "video_engine": profile["video_engine"],
        "video_files": profile["video_files"],
    }
    blob = json.dumps(picked, ensure_ascii=False)
    return (
        "import hashlib\n"
        "import json\n"
        "import os\n"
        "import secrets\n"
        "import subprocess\n"
        "import sys\n"
        "import time\n"
        "import urllib.error\n"
        "import urllib.request\n"
        "from pathlib import Path\n"
        f"REPO = Path({COLAB_APP_ROOT!r})\n"
        "password = secrets.token_urlsafe(12)\n"
        "auth_secret = secrets.token_hex(32)\n"
        "env_path = REPO / \".env\"\n"
        "env_path.write_text(\n"
        "    \"AUTH_USER=Caio\\n\"\n"
        "    + \"AUTH_PASSWORD_HASH=\" + hashlib.sha256(password.encode(\"utf-8\")).hexdigest() + \"\\n\"\n"
        "    + \"AUTH_SECRET=\" + auth_secret + \"\\n\"\n"
        "    + \"LLM_API_KEY=\\n\",\n"
        "    encoding=\"utf-8\",\n"
        ")\n"
        "settings_path = REPO / \"config\" / \"settings.json\"\n"
        "settings = json.loads(settings_path.read_text(encoding=\"utf-8\"))\n"
        "settings[\"llm\"][\"base_url\"] = \"http://127.0.0.1:1234/v1\"\n"
        "settings[\"llm\"][\"context_size\"] = 8192\n"
        "settings[\"llm\"][\"model_alias\"] = \"auto\"\n"
        "settings[\"context\"][\"context_size\"] = 8192\n"
        "settings[\"context\"][\"budgets\"][\"recent_window\"] = 2048\n"
        "settings[\"embeddings\"][\"enabled\"] = False\n"
        "settings[\"voice\"][\"enabled\"] = False\n"
        f"PROFILE = json.loads({blob!r})\n"
        "comfy = settings[\"image\"][\"providers\"][\"comfyui\"]\n"
        "if PROFILE[\"start_comfy\"]:\n"
        "    comfy[\"base_url\"] = \"http://127.0.0.1:8188\"\n"
        "    engine = PROFILE[\"image_engine\"]\n"
        "    if engine:\n"
        "        comfy[\"engine\"] = engine\n"
        "    block = comfy\n"
        "    if engine == \"flux2_klein\":\n"
        "        block = comfy[\"flux2\"]\n"
        "    elif engine == \"z_image_turbo\":\n"
        "        block = comfy[\"zimage\"]\n"
        "    for role in (\"unet\", \"clip\", \"vae\"):\n"
        "        filename = PROFILE[\"image_files\"].get(role) or \"\"\n"
        "        if filename:\n"
        "            block[role] = filename\n"
        "    if not engine and not any(PROFILE[\"image_files\"].values()):\n"
        "        settings[\"image\"][\"enabled\"] = False\n"
        "    if PROFILE[\"video_engine\"]:\n"
        "        comfy[\"video\"][\"engine\"] = PROFILE[\"video_engine\"]\n"
        "    for role in (\"unet\", \"clip\", \"vae\", \"checkpoint\"):\n"
        "        filename = PROFILE[\"video_files\"].get(role) or \"\"\n"
        "        if filename:\n"
        "            comfy[\"video\"][role] = filename\n"
        "else:\n"
        "    settings[\"image\"][\"enabled\"] = False\n"
        "settings_path.write_text(json.dumps(settings, ensure_ascii=False, indent=2) + \"\\n\", encoding=\"utf-8\")\n"
        "def wait_http(url):\n"
        "    last = None\n"
        "    for _ in range(60):\n"
        "        try:\n"
        "            with urllib.request.urlopen(url, timeout=5) as resp:\n"
        "                if resp.status < 500:\n"
        "                    return\n"
        "                last = resp.status\n"
        "        except urllib.error.HTTPError as exc:\n"
        "            if exc.code < 500:\n"
        "                return\n"
        "            last = exc.code\n"
        "        except Exception as exc:\n"
        "            last = exc.__class__.__name__\n"
        "        time.sleep(5)\n"
        "    raise SystemExit(\"health check falhou: \" + url + \" (\" + str(last) + \")\")\n"
        "print(\"login Caio\")\n"
        "print(\"senha\", password)\n"
        "wait_http(\"http://127.0.0.1:1234/health\")\n"
        "if PROFILE[\"start_comfy\"]:\n"
        "    wait_http(\"http://127.0.0.1:8188/system_stats\")\n"
        "app_env = os.environ.copy()\n"
        "app_env[\"AIDUNGEON_ENV_PATH\"] = str(env_path)\n"
        "(REPO / \"colab\").mkdir(parents=True, exist_ok=True)\n"
        "log = (REPO / \"colab\" / \"app.log\").open(\"ab\")\n"
        "subprocess.run([\"pkill\", \"-f\", \"uvicorn.*app.main:app\"], check=False)\n"
        "subprocess.Popen(\n"
        "    [sys.executable, \"-m\", \"uvicorn\", \"app.main:app\", \"--host\", \"0.0.0.0\", \"--port\", \"8000\"],\n"
        "    cwd=REPO / \"backend\", env=app_env, stdout=log, stderr=log, start_new_session=True,\n"
        ")\n"
        "wait_http(\"http://127.0.0.1:8000/api/health/llm\")\n"
        "print(\"jogo em http://127.0.0.1:8000\")\n"
    )


def _colab_tunnel_cell() -> str:
    return (
        "import base64\n"
        "import hashlib\n"
        "import hmac\n"
        "import pathlib\n"
        "import re\n"
        "import subprocess\n"
        "import time\n"
        "import urllib.request\n"
        "binary = pathlib.Path(\"/tmp/cloudflared\")\n"
        "if not binary.is_file():\n"
        "    urllib.request.urlretrieve(\n"
        "        \"https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64\",\n"
        "        binary,\n"
        "    )\n"
        "    binary.chmod(0o755)\n"
        "subprocess.run([\"pkill\", \"-f\", \"cloudflared.*127.0.0.1:8000\"], check=False)\n"
        "log_path = pathlib.Path(\"/tmp/cloudflared.log\")\n"
        "log = log_path.open(\"wb\")\n"
        "subprocess.Popen(\n"
        "    [str(binary), \"tunnel\", \"--url\", \"http://127.0.0.1:8000\", \"--no-autoupdate\"],\n"
        "    stdout=log, stderr=subprocess.STDOUT, start_new_session=True,\n"
        ")\n"
        "url = \"\"\n"
        "for _ in range(40):\n"
        "    text = log_path.read_text(encoding=\"utf-8\", errors=\"replace\") if log_path.exists() else \"\"\n"
        "    found = re.search(r\"https://[-0-9a-z]+\\.trycloudflare\\.com\", text)\n"
        "    if found:\n"
        "        url = found.group(0)\n"
        "        break\n"
        "    time.sleep(1)\n"
        "if not url:\n"
        "    raise SystemExit(\"o túnel não publicou uma URL\")\n"
        "env_file = pathlib.Path(\"/content/aidungeon-local/.env\")\n"
        "secret_val = \"\"\n"
        "if env_file.is_file():\n"
        "    for line in env_file.read_text(encoding=\"utf-8\").splitlines():\n"
        "        if line.startswith(\"AUTH_SECRET=\"):\n"
        "            secret_val = line.split(\"=\", 1)[1].strip()\n"
        "if secret_val:\n"
        "    exp = int(time.time()) + 168 * 3600\n"
        "    u_b64 = base64.urlsafe_b64encode(b\"Caio\").rstrip(b\"=\").decode(\"ascii\")\n"
        "    e_b64 = base64.urlsafe_b64encode(str(exp).encode(\"ascii\")).rstrip(b\"=\").decode(\"ascii\")\n"
        "    sig = hmac.new(secret_val.encode(\"utf-8\"), f\"Caio.{exp}\".encode(\"utf-8\"), hashlib.sha256).hexdigest()\n"
        "    print(\"abra (1-clique já logado):\", f\"{url}/?aidl_token={u_b64}.{e_b64}.{sig}\")\n"
        "print(\"abra\", url)\n"
    )


def notebook_bytes(notebook: dict) -> bytes:
    """Serializa um notebook nbformat 4. Não altera o objeto."""
    if not isinstance(notebook, dict) or notebook.get("nbformat") != 4:
        raise ValueError("notebook precisa ser nbformat 4")
    cells = notebook.get("cells")
    if not isinstance(cells, list) or not cells:
        raise ValueError("notebook sem células")
    return (json.dumps(notebook, ensure_ascii=False, indent=1) + "\n").encode("utf-8")


def registration_payload(config: dict, hardware: dict, endpoints: dict) -> dict:
    """Corpo do POST {project_url}/api/runtime/register. Sem segredo."""
    norm = validate_runtime(config, require_colab=False)
    if set(endpoints) != {"llm", "comfyui"}:
        raise ValueError("endpoints do registro aceita exatamente llm e comfyui")
    cleaned_ep: dict[str, str] = {}
    for key, value in endpoints.items():
        if not isinstance(value, str):
            raise ValueError(f"endpoints.{key} precisa ser string")
        stripped = value.strip()
        if norm["placements"][key] == "colab" and not stripped:
            raise ValueError(f"endpoints.{key} do registro vazio")
        cleaned_ep[key] = stripped
    if not isinstance(hardware, dict):
        raise ValueError("hardware do registro precisa ser um objeto")
    _reject_secrets(hardware)
    _reject_secrets(cleaned_ep)
    return {
        "endpoints": cleaned_ep,
        "models": {
            "llm": norm["models"]["llm"],
            "image": norm["models"]["image"],
            "video": norm["models"]["video"],
        },
        "hardware": dict(hardware),
    }
