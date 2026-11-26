"""Biblioteca dinâmica de modelos.

Busca em CivitAI, CivitAI Red e Hugging Face e transforma o arquivo escolhido
numa entrada do mesmo catálogo do runtime. Daí em diante o notebook do Colab,
o download com SHA256 e o registro tratam esse modelo como qualquer outro.

O jogo só pinta com um motor que tenha grafo no ComfyUI. Cada arquivo entra
por uma família:

- `flux2_klein`, `z_image_turbo`, `qwen`, `ltxv`, `wan`, `h3`: troca o arquivo
  principal de um modelo que o jogo já conhece e mantém encoder e VAE;
- `checkpoint`: arquivo completo (SDXL, Pony, Illustrious, SD 1.5 e os demais
  que o `CheckpointLoaderSimple` do ComfyUI abre), com LoRA;
- `custom`: fluxo do ComfyUI colado pelo usuário, com marcadores;
- `gguf`: narrador, só para o llama-server que sobe no Colab.
"""

from __future__ import annotations

import copy
import os
import posixpath
import re
import shutil
import sqlite3
import threading
import urllib.parse
from pathlib import Path
from typing import Any

import httpx

from app import db, settings_store
from app.errors import GameError

SOURCES = {
    "civitai": "https://civitai.com",
    "civitai_red": "https://civitai.red",
    "huggingface": "https://huggingface.co",
}
KINDS = ("llm", "image", "video", "lora")
FAMILIES = {
    "llm": ("gguf",),
    "image": ("checkpoint", "flux2_klein", "z_image_turbo", "qwen", "custom"),
    "video": ("ltxv", "wan", "h3", "custom"),
    "lora": ("lora",),
}
# Entrada do catálogo fixo que serve de molde: encoder, VAE, serviço e nodes.
_TEMPLATES = {
    ("image", "flux2_klein"): "flux2_klein",
    ("image", "z_image_turbo"): "z_image_turbo",
    ("image", "qwen"): "fineview_qwen",
    ("video", "ltxv"): "ltxv",
    ("video", "wan"): "wan",
    ("video", "h3"): "h3",
    ("llm", "gguf"): "qwen2.5-3b-instruct-q4_k_m",
}
# Arquivo que cada família troca no molde.
_MAIN_ROLE = {
    "flux2_klein": "unet", "z_image_turbo": "unet", "qwen": "unet",
    "ltxv": "checkpoint", "wan": "unet", "h3": "unet", "gguf": "weights",
}
# Família que o CivitAI informa → família do jogo. O resto do tipo Checkpoint
# é arquivo completo; o que não é fica sem motor e só entra por fluxo próprio.
_CIVITAI_FAMILY = {
    "Flux.2 Klein 4B": "flux2_klein",
    "Flux.2 Klein 4B-base": "flux2_klein",
    "ZImageTurbo": "z_image_turbo",
    "Qwen 2.1": "qwen",
    "LTXV": "ltxv",
    "Wan Video 2.2 TI2V-5B": "wan",
}
_CHECKPOINT_BASES = ("SD ", "SDXL", "Pony", "Illustrious", "NoobAI", "PixArt", "AuraFlow")
# Poucos passos e CFG baixo para os destilados; o resto usa o padrão do SDXL.
_FAST_BASES = ("SDXL Lightning", "SDXL Hyper", "SDXL Turbo", "SDXL 1.0 LCM", "SD 1.5 LCM",
               "SD 1.5 Hyper", "SDXL Distilled")
CHECKPOINT_DEFAULTS = {
    "steps": 28, "cfg": 6.0, "sampler": "euler_ancestral", "scheduler": "normal",
    "megapixels": 1.0,
}
_CHECKPOINT_FAST = {"steps": 8, "cfg": 1.5, "sampler": "dpmpp_sde", "scheduler": "karras"}
# Pickle (.ckpt, .pt, .bin) executa código ao carregar: fica de fora.
_WEIGHT_EXT = (".safetensors", ".gguf")
_SAFE_NAME = re.compile(r"[A-Za-z0-9._+-]+")
_SHA256 = re.compile(r"[0-9a-f]{64}")
_FOLDERS = ("checkpoints", "diffusion_models", "text_encoders", "vae", "loras",
            "clip_vision", "controlnet", "upscale_models", "clip_projections")
_PLACEHOLDER = "{{prompt}}"
_HTTP_TIMEOUT = 25.0


# ------------------------------------------------------------------- chaves
def download_keys(conn: sqlite3.Connection) -> dict[str, str]:
    """Chaves salvas no jogo. Vão para o Colab dentro do notebook."""
    out = {}
    for name, key in (("CIVITAI_API_KEY", "downloads.civitai_api_key"),
                      ("HF_TOKEN", "downloads.huggingface_token")):
        value = settings_store._usable_secret(settings_store._get_override(conn, key))
        out[name] = value or ""
    return out


def save_keys(conn: sqlite3.Connection, civitai: str | None, huggingface: str | None) -> None:
    settings_store._apply_key(conn, "downloads.civitai_api_key", civitai)
    settings_store._apply_key(conn, "downloads.huggingface_token", huggingface)


# -------------------------------------------------------------------- busca
def _client(transport: Any) -> httpx.Client:
    return httpx.Client(transport=transport, timeout=_HTTP_TIMEOUT, follow_redirects=True,
                        headers={"User-Agent": "aidungeon-model-library"})


def _get_json(client: httpx.Client, url: str, params: Any, token: str) -> Any:
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    try:
        resp = client.get(url, params=params, headers=headers)
    except httpx.HTTPError as exc:
        raise GameError(502, "catalog_unreachable",
                        f"catálogo fora do ar: {type(exc).__name__}") from exc
    if resp.status_code in (401, 403):
        raise GameError(502, "catalog_auth", "o catálogo recusou a chave salva")
    if resp.status_code != 200:
        raise GameError(502, "catalog_error", f"o catálogo respondeu HTTP {resp.status_code}")
    try:
        return resp.json()
    except ValueError as exc:
        raise GameError(502, "catalog_error", "o catálogo não respondeu JSON") from exc


def family_for(kind: str, base_model: str, model_type: str = "") -> str:
    """Família do jogo para o que o site informou. '' quando não há motor pronto."""
    if kind == "lora":
        return "lora"
    if kind == "llm":
        return "gguf"
    mapped = _CIVITAI_FAMILY.get(base_model, "")
    if mapped:
        return mapped if mapped in FAMILIES[kind] else ""
    if kind == "image" and model_type == "Checkpoint" and base_model.startswith(_CHECKPOINT_BASES):
        return "checkpoint"
    return ""


def _civitai_files(version: dict) -> list[dict]:
    files = []
    for item in version.get("files") or []:
        name = str(item.get("name") or "")
        sha = str((item.get("hashes") or {}).get("SHA256") or "").lower()
        url = str(item.get("downloadUrl") or "")
        if not name.lower().endswith(_WEIGHT_EXT) or not _SHA256.fullmatch(sha) or not url:
            continue
        files.append({
            "name": name,
            "bytes": int(float(item.get("sizeKB") or 0) * 1024),
            "sha256": sha,
            "url": url,
            "role": str(item.get("type") or "Model"),
        })
    return files


def _search_civitai(client: httpx.Client, base: str, source: str, kind: str,
                    query: str, token: str) -> list[dict]:
    types = ["LORA"] if kind == "lora" else ["Checkpoint"]
    data = _get_json(client, f"{base}/api/v1/models", [
        ("query", query), ("limit", "20"), ("nsfw", "true"),
        *[("types", item) for item in types],
    ], token)
    rows = []
    for model in (data.get("items") or []) if isinstance(data, dict) else []:
        for version in (model.get("modelVersions") or [])[:3]:
            files = _civitai_files(version)
            if not files:
                continue
            base_model = str(version.get("baseModel") or "")
            rows.append({
                "source": source,
                "id": f"{model.get('id')}:{version.get('id')}",
                "name": f"{model.get('name')} — {version.get('name')}",
                "base_model": base_model,
                "family": family_for(kind, base_model, str(model.get("type") or "")),
                "nsfw": bool(model.get("nsfw")),
                "page_url": f"{base}/models/{model.get('id')}?modelVersionId={version.get('id')}",
                "files": files,
            })
    return rows


def _search_huggingface(client: httpx.Client, kind: str, query: str, token: str) -> list[dict]:
    params = [("search", query), ("limit", "20"), ("sort", "downloads"), ("direction", "-1")]
    if kind == "llm":
        params.append(("filter", "gguf"))
    data = _get_json(client, f"{SOURCES['huggingface']}/api/models", params, token)
    rows = []
    for model in data if isinstance(data, list) else []:
        repo = str(model.get("id") or model.get("modelId") or "")
        if not repo:
            continue
        rows.append({
            "source": "huggingface",
            "id": repo,
            "name": repo,
            "base_model": "",
            "family": "gguf" if kind == "llm" else "lora" if kind == "lora" else "",
            "nsfw": False,
            "gated": bool(model.get("gated")),
            "downloads": int(model.get("downloads") or 0),
            "page_url": f"{SOURCES['huggingface']}/{repo}",
            # Arquivos e SHA256 vêm de /files: uma chamada por repositório.
            "files": None,
        })
    return rows


def search(conn: sqlite3.Connection, source: str, kind: str, query: str,
           *, transport: Any = None) -> list[dict]:
    if source not in SOURCES or kind not in KINDS:
        raise GameError.bad_request("validation_error", "fonte ou tipo de modelo desconhecido")
    text = " ".join((query or "").split())
    if len(text) < 2:
        raise GameError.bad_request("validation_error", "digite ao menos 2 letras para buscar")
    keys = download_keys(conn)
    with _client(transport) as client:
        if source == "huggingface":
            return _search_huggingface(client, kind, text, keys["HF_TOKEN"])
        return _search_civitai(client, SOURCES[source], source, kind, text,
                               keys["CIVITAI_API_KEY"])


def huggingface_files(conn: sqlite3.Connection, repo: str, *, transport: Any = None) -> list[dict]:
    """Arquivos de peso do repositório, com o SHA256 que o Hub guarda no LFS."""
    if not re.fullmatch(r"[A-Za-z0-9._-]+/[A-Za-z0-9._-]+", repo or ""):
        raise GameError.bad_request("validation_error", "repositório do Hugging Face inválido")
    token = download_keys(conn)["HF_TOKEN"]
    with _client(transport) as client:
        data = _get_json(client, f"{SOURCES['huggingface']}/api/models/{repo}/tree/main",
                         [("recursive", "true")], token)
    files = []
    for item in data if isinstance(data, list) else []:
        path = str(item.get("path") or "")
        lfs = item.get("lfs") if isinstance(item.get("lfs"), dict) else {}
        sha = str(lfs.get("oid") or "").lower()
        if (item.get("type") != "file" or not path.lower().endswith(_WEIGHT_EXT)
                or not _SHA256.fullmatch(sha)):
            continue
        files.append({
            "name": posixpath.basename(path),
            "path": path,
            "bytes": int(lfs.get("size") or item.get("size") or 0),
            "sha256": sha,
            "url": f"{SOURCES['huggingface']}/{repo}/resolve/main/"
                   + urllib.parse.quote(path),
            "role": "Model",
        })
    return files


# ------------------------------------------------- arquivo → entrada do catálogo
def _checked_file(item: Any) -> dict:
    """Fronteira de confiança: o que entra no catálogo vira download e nome de arquivo."""
    if not isinstance(item, dict):
        raise GameError.bad_request("validation_error", "arquivo do modelo ausente")
    name = posixpath.basename(str(item.get("name") or "").replace("\\", "/")).strip()
    name = re.sub(r"[^A-Za-z0-9._+-]+", "_", name)
    if not _SAFE_NAME.fullmatch(name) or not name.lower().endswith(_WEIGHT_EXT):
        raise GameError.bad_request(
            "validation_error", "o arquivo precisa ser .safetensors ou .gguf")
    url = str(item.get("url") or "").strip()
    parsed = urllib.parse.urlsplit(url)
    host = (parsed.hostname or "").lower()
    allowed = {urllib.parse.urlsplit(base).hostname for base in SOURCES.values()}
    if parsed.scheme != "https" or host not in allowed or parsed.username or parsed.password:
        raise GameError.bad_request(
            "validation_error", "o link precisa ser https do CivitAI ou do Hugging Face")
    sha = str(item.get("sha256") or "").strip().lower()
    if not _SHA256.fullmatch(sha):
        raise GameError.bad_request("validation_error", "arquivo sem SHA256 publicado")
    size = item.get("bytes")
    return {
        "filename": name,
        "url": url,
        "sha256": sha,
        "bytes": int(size) if isinstance(size, (int, float)) and size > 0 else None,
        "auth_secret": "" if host == "huggingface.co" else "CIVITAI_API_KEY",
    }


def _artifact(role: str, folder: str, checked: dict, catalog: dict | None = None) -> dict:
    # "model.safetensors" e "v6.safetensors" existem aos montes. Mesmo destino
    # com outro conteúdo ganha um pedaço do SHA no nome, senão um apaga o outro.
    name = checked["filename"]
    taken = {
        art.get("dest"): art.get("sha256")
        for group in ((catalog or {}).get("models") or {}).values()
        for entry in group.values() for art in entry.get("artifacts") or []
    }
    if taken.get(f"{folder}/{name}") not in (None, checked["sha256"]):
        stem, dot, ext = name.rpartition(".")
        name = f"{stem}-{checked['sha256'][:8]}{dot}{ext}"
        checked["filename"] = name
    art = {
        "role": role,
        "filename": name,
        "dest": f"{folder}/{name}",
        "url": checked["url"],
        "sha256": checked["sha256"],
    }
    if checked["bytes"]:
        art["bytes"] = checked["bytes"]
    if checked["auth_secret"]:
        art["auth_secret"] = checked["auth_secret"]
    return art


def _slug(text: str) -> str:
    stem = re.sub(r"\.(safetensors|gguf)$", "", text, flags=re.IGNORECASE)
    return re.sub(r"[^a-z0-9]+", "-", stem.lower()).strip("-")[:60] or "modelo"


def _free_id(catalog: dict, kind: str, base: str) -> str:
    taken = catalog["models"].get(kind) or {}
    candidate, number = base, 2
    while candidate in taken:
        candidate = f"{base}-{number}"
        number += 1
    return candidate


def check_graph(graph: Any, kind: str = "image") -> dict:
    """Fluxo próprio no formato API do ComfyUI, com o marcador do prompt."""
    if not isinstance(graph, dict) or not graph:
        raise GameError.bad_request("validation_error", "cole o fluxo em JSON")
    if "nodes" in graph and "links" in graph:
        raise GameError.bad_request(
            "validation_error",
            "este é o formato de tela do ComfyUI. Exporte com Arquivo → Exportar (API)")
    for node_id, node in graph.items():
        if (not isinstance(node, dict) or not isinstance(node.get("class_type"), str)
                or not isinstance(node.get("inputs"), dict)):
            raise GameError.bad_request(
                "validation_error", f"nó {node_id} sem class_type ou inputs")
    import json
    if _PLACEHOLDER not in json.dumps(graph):
        raise GameError.bad_request(
            "validation_error", "o fluxo precisa ter {{prompt}} no texto positivo")
    if kind == "video" and not any(
            node["inputs"].get("image") == "{{reference_1}}" for node in graph.values()):
        raise GameError.bad_request("validation_error", "use {{reference_1}} na entrada da imagem do clipe")
    return graph


def build_entry(catalog: dict, kind: str, family: str, body: dict) -> tuple[str, dict]:
    """Entrada de catálogo para o arquivo escolhido. Não grava nada."""
    if kind not in ("llm", "image", "video") or family not in FAMILIES[kind]:
        raise GameError.bad_request("validation_error", "família de modelo desconhecida")
    models = catalog["models"]
    label = " ".join(str(body.get("label") or "").split())[:120]
    page = str(body.get("page_url") or "")[:300]
    if family == "custom":
        files = body.get("files")
        if not isinstance(files, list) or not files:
            raise GameError.bad_request(
                "validation_error", "o fluxo próprio precisa de ao menos um arquivo de modelo")
        artifacts = []
        for item in files:
            folder = str((item or {}).get("folder") or "")
            if folder not in _FOLDERS:
                raise GameError.bad_request(
                    "validation_error", f"pasta de modelo desconhecida: {folder}")
            artifacts.append(_artifact(
                folder.rstrip("s"), f"models/{folder}", _checked_file(item), catalog))
        template = models["image"][_TEMPLATES[("image", "flux2_klein")]]
        nodes = []
        for node in body.get("nodes") or []:
            url = str((node or {}).get("git_url") or "").strip()
            ref = str((node or {}).get("git_ref") or "").strip()
            name = posixpath.basename(urllib.parse.urlsplit(url).path).removesuffix(".git")
            if (not url.startswith("https://github.com/") or not _SAFE_NAME.fullmatch(name)
                    or not re.fullmatch(r"[A-Za-z0-9._/-]{1,80}", ref)):
                raise GameError.bad_request(
                    "validation_error", "node precisa de link https do GitHub e de um commit ou tag")
            nodes.append({"name": name, "git_url": url, "git_ref": ref})
        entry = {
            "engine": "custom",
            "artifacts": artifacts,
            "dependencies": [template["dependencies"][0], *nodes],
            "service": copy.deepcopy(template["service"]),
            "custom": {"graph": check_graph(body.get("graph"), kind)},
        }
        base_id = _slug(label or artifacts[0]["filename"])
    else:
        checked = _checked_file(body.get("file"))
        base_id = _slug(label or checked["filename"])
        if family == "checkpoint":
            template = models["image"][_TEMPLATES[("image", "flux2_klein")]]
            sampling = dict(CHECKPOINT_DEFAULTS)
            if str(body.get("base_model") or "").startswith(_FAST_BASES):
                sampling.update(_CHECKPOINT_FAST)
            entry = {
                "engine": "checkpoint",
                "artifacts": [_artifact("checkpoint", "models/checkpoints", checked, catalog)],
                "dependencies": [template["dependencies"][0]],
                "service": copy.deepcopy(template["service"]),
                "checkpoint": {"checkpoint": checked["filename"], **sampling},
            }
        else:
            entry = copy.deepcopy(models[kind][_TEMPLATES[(kind, family)]])
            role = _MAIN_ROLE[family]
            old = next(art for art in entry["artifacts"] if art.get("role") == role)
            new = _artifact(role, posixpath.dirname(old["dest"]), checked, catalog)
            entry["artifacts"] = [
                new if art is old else art for art in entry["artifacts"]
                # O projetor de visão é do GGUF do molde, não do que foi escolhido.
                if art.get("role") != "mmproj"
            ]
            for block in ("flux2", "zimage", "quality", "video", "config"):
                if isinstance(entry.get(block), dict) and role in entry[block]:
                    entry[block][role] = checked["filename"]
            if kind == "llm":
                entry["model_alias"] = checked["filename"]
                # Só o llama-server do Colab troca de arquivo; o da VM é do serviço dela.
                entry["role"] = "fallback"
    entry["label"] = label or base_id
    entry["user"] = True
    entry["source"] = page or "biblioteca do usuário"
    entry["min_vram_mib"] = None
    entry["min_ram_mib"] = int(entry.get("min_ram_mib") or 0)
    entry["min_vram_note"] = "Modelo escolhido na busca: VRAM mínima não medida."
    entry.pop("vram_budget", None)
    return _free_id(catalog, kind, base_id), entry


def add_model(conn: sqlite3.Connection, kind: str, family: str, body: dict) -> str:
    catalog = settings_store.runtime_catalog(conn)
    model_id, entry = build_entry(catalog, kind, family, body)
    settings_store.save_user_model(conn, kind, model_id, entry)
    return model_id


def _owned(conn: sqlite3.Connection, kind: str, model_id: str) -> tuple[str, dict]:
    """Entrada editável. A do catálogo fixo é copiada para a biblioteca primeiro."""
    mine = settings_store.user_models(conn).get(kind, {})
    if model_id in mine:
        return model_id, mine[model_id]
    catalog = settings_store.runtime_catalog(conn)
    entry = (catalog["models"].get(kind) or {}).get(model_id)
    if not isinstance(entry, dict):
        raise GameError.not_found("modelo não encontrado")
    clone = copy.deepcopy(entry)
    clone["user"] = True
    clone["label"] = f"{entry.get('label') or model_id} (ajustado)"
    return _free_id(catalog, kind, f"{model_id}-ajustado"), clone


def _reapply(conn: sqlite3.Connection, kind: str, edited: str, target: str) -> None:
    """Edição do modelo em uso vale na hora. Se a edição criou uma cópia do
    catálogo fixo, é a cópia que passa a ser o modelo em uso."""
    if settings_store.effective_runtime_settings(conn)["models"].get(kind) != edited:
        return
    if target != edited:
        settings_store._set_override(conn, f"runtime.models.{kind}", target)
    settings_store._apply_selected_models(
        conn, settings_store.effective_runtime_settings(conn),
        settings_store.runtime_catalog(conn))


def add_lora(conn: sqlite3.Connection, model_id: str, body: dict) -> str:
    """Prende um LoRA ao modelo de imagem. Devolve o id da entrada que ficou com ele."""
    target, entry = _owned(conn, "image", model_id)
    if entry.get("engine") == "custom":
        raise GameError.bad_request(
            "validation_error", "no fluxo próprio o LoRA vai dentro do próprio fluxo")
    checked = _checked_file(body.get("file"))
    try:
        strength = max(-4.0, min(4.0, float(body.get("strength", 0.8))))
    except (TypeError, ValueError):
        strength = 0.8
    loras = [item for item in entry.get("loras") or [] if item.get("name") != checked["filename"]]
    loras.append({"name": checked["filename"], "strength": strength})
    entry["loras"] = loras
    entry["artifacts"] = [
        art for art in entry["artifacts"] if art.get("filename") != checked["filename"]
    ] + [_artifact("lora", "models/loras", checked, settings_store.runtime_catalog(conn))]
    settings_store.save_user_model(conn, "image", target, entry)
    _reapply(conn, "image", model_id, target)
    return target


def update_model(conn: sqlite3.Connection, kind: str, model_id: str, body: dict) -> str:
    """Nome, força ou remoção de LoRA, amostragem do checkpoint e o fluxo próprio."""
    target, entry = _owned(conn, kind, model_id)
    if body.get("label"):
        entry["label"] = " ".join(str(body["label"]).split())[:120]
    if isinstance(body.get("loras"), list):
        kept = []
        known = {item.get("name") for item in entry.get("loras") or []}
        for item in body["loras"]:
            name = str((item or {}).get("name") or "")
            if name not in known:
                continue
            kept.append({"name": name,
                         "strength": max(-4.0, min(4.0, float(item.get("strength", 0.8))))})
        gone = known - {item["name"] for item in kept}
        entry["loras"] = kept
        entry["artifacts"] = [
            art for art in entry["artifacts"]
            if not (art.get("role") == "lora" and art.get("filename") in gone)
        ]
    if isinstance(body.get("checkpoint"), dict) and entry.get("engine") == "checkpoint":
        block = entry["checkpoint"]
        raw = body["checkpoint"]
        for key, cast, low, high in (("steps", int, 1, 150), ("cfg", float, 0.0, 30.0),
                                     ("megapixels", float, 0.0, 8.0)):
            if raw.get(key) is not None:
                block[key] = max(low, min(high, cast(raw[key])))
        for key in ("sampler", "scheduler"):
            text = str(raw.get(key) or "").strip()
            if re.fullmatch(r"[a-z0-9_+]{1,40}", text):
                block[key] = text
    if body.get("graph") is not None and entry.get("engine") == "custom":
        entry["custom"] = {"graph": check_graph(body["graph"], kind)}
    settings_store.save_user_model(conn, kind, target, entry)
    _reapply(conn, kind, model_id, target)
    return target


def delete_model(conn: sqlite3.Connection, kind: str, model_id: str) -> None:
    if model_id not in settings_store.user_models(conn).get(kind, {}):
        raise GameError.bad_request(
            "validation_error", "só modelos adicionados pela busca podem ser removidos")
    selected = settings_store.effective_runtime_settings(conn)["models"].get(kind)
    if selected == model_id:
        raise GameError.conflict("model_in_use", "troque o modelo em uso antes de remover este")
    settings_store.delete_user_model(conn, kind, model_id)


# --------------------------------------------------- instalação na própria VM
_install_lock = threading.Lock()
_install: dict = {"state": "idle"}


def comfy_root(conn: sqlite3.Connection) -> Path | None:
    """Pasta do ComfyUI desta máquina. Sem ela não há onde baixar."""
    raw = (settings_store._get_override(conn, "runtime.comfyui_root")
           or os.environ.get("COMFYUI_ROOT") or "")
    candidates = [Path(str(raw)).expanduser()] if raw else [Path.home() / "ComfyUI"]
    for path in candidates:
        if (path / "models").is_dir():
            return path
    return None


def missing_files(root: Path, entry: dict) -> list[dict]:
    missing = []
    for art in entry.get("artifacts") or []:
        if not art.get("url") or not art.get("sha256"):
            continue
        dest = root / art["dest"]
        size = art.get("bytes")
        if not dest.is_file() or (size and dest.stat().st_size != int(size)):
            missing.append(art)
    return missing


def install_status() -> dict:
    with _install_lock:
        status = dict(_install)
    part = status.pop("_part", None)
    if status.get("state") == "running" and part:
        try:
            status["file_bytes_done"] = Path(part).stat().st_size
        except OSError:
            status["file_bytes_done"] = 0
    return status


def _run_install(root: Path, model: str, files: list[dict], keys: dict) -> None:
    from app import runtime_provisioning
    try:
        for index, art in enumerate(files):
            dest = root / art["dest"]
            with _install_lock:
                _install.update({"file": art["filename"], "done": index,
                                 "file_bytes": art.get("bytes"), "_part": f"{dest}.part"})
            secret = keys.get(art.get("auth_secret") or "") or ""
            if not secret and "huggingface.co" in art["url"]:
                secret = keys.get("HF_TOKEN") or ""
            runtime_provisioning.fetch_pinned(
                art["url"], str(dest), art["sha256"], retries=3,
                expected_bytes=art.get("bytes"),
                auth_header=f"Bearer {secret}" if secret else None)
        with _install_lock:
            _install.clear()
            _install.update({"state": "done", "model": model, "total": len(files)})
    except Exception as exc:  # a thread não tem quem receba a exceção
        with _install_lock:
            _install.clear()
            _install.update({"state": "error", "model": model, "error": str(exc)[:300]})


def start_install(conn: sqlite3.Connection, kind: str, model_id: str) -> dict:
    """Baixa para o ComfyUI desta máquina o que falta do modelo. Um por vez."""
    if kind not in ("image", "video"):
        raise GameError.bad_request(
            "validation_error", "só imagem e vídeo são baixados para o ComfyUI desta máquina")
    root = comfy_root(conn)
    if root is None:
        raise GameError.bad_request(
            "comfyui_root_missing",
            "pasta do ComfyUI não encontrada nesta máquina; defina COMFYUI_ROOT")
    entry = (settings_store.runtime_catalog(conn)["models"].get(kind) or {}).get(model_id)
    if not isinstance(entry, dict):
        raise GameError.not_found("modelo não encontrado")
    files = missing_files(root, entry)
    need = sum(int(art.get("bytes") or 0) for art in files)
    if need and shutil.disk_usage(root).free < need + 2 * 1024 ** 3:
        raise GameError.bad_request("disk_full", "não há espaço em disco para este modelo")
    with _install_lock:
        if _install.get("state") == "running":
            raise GameError.conflict("install_running", "já há um download em andamento")
        _install.clear()
        _install.update({"state": "running" if files else "done", "model": model_id,
                         "total": len(files), "done": 0})
    if files:
        threading.Thread(target=_run_install, daemon=True,
                         args=(root, model_id, files, download_keys(conn))).start()
    return install_status()
