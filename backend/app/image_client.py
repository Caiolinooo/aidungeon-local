"""Cliente de geração de imagens (v2) — espelha a estrutura de app/llm_client.py.

Providers (settings.image.provider, config efetiva de settings_store):
- `sdwebui`: POST {sdwebui_base_url}{txt2img_path} (A1111/SD.Next/Forge com
  --api) com JSON {prompt, negative_prompt, steps, width, height}; a resposta
  traz `images[0]` em base64 (png).
- `openai_compatible`: POST {openai_base_url}/images/generations com Bearer
  (openai_api_key, pode ser vazia), JSON {model, prompt, size, response_format:
  "b64_json"}; aceita `data[0].b64_json` ou `data[0].url` (neste caso baixa o
  arquivo da URL).
- `comfyui`: POST {comfyui_base_url}/prompt com UNETLoader, CLIPLoader qwen_image,
  TextEncodeQwenImage21, ModelSamplingAuraFlow, ManualSigmas e SamplerCustom;
  depois GET /history/{prompt_id} e GET /view. Referências entram no encoder
  visual; retratos com âncora usam seu latent zero e cenas usam EmptyLatentImage.

O PNG decodificado é salvo em {storage_dir}/{adventure_id}/{image_id}.png
(storage_dir relativo à raiz do repo) e o caminho RELATIVO é devolvido para
ser gravado em images.file_path.

Erros: falha de conexão/timeout → ImageUnreachable ("unreachable");
401/403 → ImageAuthError ("auth_error"); demais → ImageError. A api_key
NUNCA é escrita em logs/mensagens de erro.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import logging
import random
import time
import uuid
from typing import Any

import httpx

from app import db

log = logging.getLogger("aidungeon.image")


class ImageError(Exception):
    code = "error"

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


class ImageUnreachable(ImageError):
    code = "unreachable"


class ImageAuthError(ImageError):
    code = "auth_error"


# ---------------------------------------------------------------- helpers
def _make_client(transport: httpx.BaseTransport | None, timeout: float) -> httpx.Client:
    t = httpx.Timeout(timeout, connect=min(10.0, timeout))
    kwargs: dict[str, Any] = {"timeout": t, "trust_env": False}
    if transport is not None:
        kwargs["transport"] = transport
    return httpx.Client(**kwargs)


def _post_json(client: httpx.Client, url: str, payload: dict,
               headers: dict[str, str], what: str) -> dict:
    """POST JSON com mapeamento de erros do contrato; devolve o corpo JSON."""
    try:
        resp = client.post(url, json=payload, headers=headers)
    except httpx.TransportError as exc:
        raise ImageUnreachable(f"image provider unreachable: {type(exc).__name__}") from exc
    if resp.status_code in (401, 403):
        raise ImageAuthError(f"auth_error: HTTP {resp.status_code} em {what}")
    if resp.status_code != 200:
        raise ImageError(f"HTTP {resp.status_code} em {what}: {resp.text[:200]}")
    try:
        data = resp.json()
    except ValueError as exc:
        raise ImageError(f"resposta de {what} não é JSON") from exc
    if not isinstance(data, dict):
        raise ImageError(f"resposta de {what} inválida (não é objeto JSON)")
    return data

def _get_json(client: httpx.Client, url: str,
              headers: dict[str, str], what: str) -> dict:
    """GET JSON para health checks; nunca inicia uma geração."""
    try:
        resp = client.get(url, headers=headers)
    except httpx.TransportError as exc:
        raise ImageUnreachable(f"image provider unreachable: {type(exc).__name__}") from exc
    if resp.status_code in (401, 403):
        raise ImageAuthError(f"auth_error: HTTP {resp.status_code} em {what}")
    if resp.status_code != 200:
        raise ImageError(f"HTTP {resp.status_code} em {what}")
    try:
        data = resp.json()
    except ValueError as exc:
        raise ImageError(f"resposta de {what} não é JSON") from exc
    if not isinstance(data, dict):
        raise ImageError(f"resposta de {what} inválida (não é objeto JSON)")
    return data


def _download(client: httpx.Client, url: str, headers: dict[str, str],
              configured_url: str) -> bytes:
    """Baixa uma imagem sem levar a credencial do provider a outra origem."""
    configured = httpx.URL(configured_url)
    configured_origin = (configured.scheme, configured.host, configured.port)
    current_url = url
    for redirect_count in range(6):
        request = client.build_request("GET", current_url, headers=headers)
        origin = (request.url.scheme, request.url.host, request.url.port)
        if origin != configured_origin and "Authorization" in request.headers:
            del request.headers["Authorization"]
        try:
            resp = client.send(request, follow_redirects=False)
        except httpx.TransportError as exc:
            raise ImageUnreachable(
                f"image provider unreachable: {type(exc).__name__}") from exc
        location = resp.headers.get("location")
        if not resp.is_redirect or not location:
            break
        if redirect_count == 5:
            raise ImageError("excesso de redirects ao baixar imagem")
        current_url = str(resp.url.join(location))
    if resp.status_code in (401, 403):
        raise ImageAuthError(f"auth_error: HTTP {resp.status_code} ao baixar imagem")
    if resp.status_code != 200:
        raise ImageError(f"HTTP {resp.status_code} ao baixar imagem: {resp.text[:200]}")
    return resp.content


def _decode_b64(raw: str, what: str) -> bytes:
    """Base64 → bytes, tolerando prefixo data-url ('data:image/png;base64,')."""
    if "," in raw and raw.lstrip().startswith("data:"):
        raw = raw.split(",", 1)[1]
    try:
        return base64.b64decode(raw, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ImageError(f"{what}: base64 inválido na resposta do provider") from exc


def _save_png(cfg: dict, adventure_id: str, image_id: str, content: bytes) -> str:
    """Grava o PNG em {storage_dir}/{adventure_id}/{image_id}.png (relativo ao
    repo) e devolve o caminho relativo (posix) para images.file_path."""
    directory = db.REPO_ROOT / cfg["storage_dir"] / adventure_id
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{image_id}.png"
    path.write_bytes(content)
    try:
        return path.relative_to(db.REPO_ROOT).as_posix()
    except ValueError:
        # storage_dir fora do repo (ex.: override em testes) — caminho absoluto
        return str(path)


# ---------------------------------------------------------------- providers
# Anatomia e enquadramento, em todo txt2img e img2img. O negative do
# settings cobre estilo; isto cobre a mão a mais e a segunda cabeça.
_STANDARD_NEGATIVE = (
    "bad anatomy, bad hands, extra fingers, missing fingers, fused fingers, "
    "extra limbs, extra arms, extra legs, malformed hands, deformed face, "
    "duplicate person, two heads, extra head, cropped, out of frame, "
    "jpeg artifacts, blurry, lowres, plastic skin, doll"
)


def _merged_negative(cfg: dict) -> str:
    seen: list[str] = []
    folded = set()
    for part in ((cfg.get("negative_prompt") or ""), _STANDARD_NEGATIVE):
        for bit in part.split(","):
            bit = " ".join(bit.split())
            key = bit.casefold()
            if bit and key not in folded:
                folded.add(key)
                seen.append(bit)
    return ", ".join(seen)


def _sd_payload(cfg: dict, prompt: str, width: int, height: int) -> dict:
    # DPM++ 2M Karras e CFG 5 servem o epiCRealism XL (pede CFG 3-6).
    # O checkpoint é travado por _apply_checkpoint em volta deste payload.
    payload = {
        "prompt": prompt,
        "negative_prompt": _merged_negative(cfg),
        "steps": cfg["sdwebui_steps"],
        "width": width,
        "height": height,
        "sampler_name": cfg.get("sampler_name", "DPM++ 2M"),
        "scheduler": cfg.get("scheduler", "Karras"),
        "cfg_scale": float(cfg.get("cfg_scale", 5)),
    }
    if cfg.get("seed") is not None:
        payload["seed"] = int(cfg["seed"])
    return payload


def _apply_checkpoint(cfg: dict, payload: dict) -> dict:
    """Trava o checkpoint sem safety checker. Sem nome, usa o que o WebUI já carregou."""
    checkpoint = " ".join(str(cfg.get("sd_model_checkpoint") or "").split())
    if not checkpoint:
        return payload
    payload["override_settings"] = {"sd_model_checkpoint": checkpoint}
    payload["override_settings_restore_afterwards"] = False
    return payload


def _sd_base(cfg: dict) -> str:
    base = cfg["sdwebui_base_url"].rstrip("/")
    if not base:
        raise ImageError("config_error: image.providers.sdwebui.base_url vazio")
    return base


def _first_image(data: dict, what: str) -> bytes:
    images = data.get("images")
    if not isinstance(images, list) or not images:
        raise ImageError(f"resposta de {what} sem lista 'images'")
    return _decode_b64(str(images[0]), what)
# InstantID: preserva melhor a identidade facial que o FaceID genérico.
# O modelo e o antelopev2 ficam na instalação do Forge Neo.
_REFERENCE_MODULE = "InsightFace (InstantID)"
_REFERENCE_MODEL = "ip-adapter-instantid"
_MULTI_REFERENCE_MODEL = "ip-adapter-instantid"
_REFERENCE_WEIGHT = 1.0
_MAX_REFERENCES = 6


def _reference_unit(image: bytes, *, mask: bytes | None = None,
                    weight: float | None = None,
                    model: str | None = None) -> dict:
    unit = {
        "enabled": True,
        "module": _REFERENCE_MODULE,
        "model": model or _REFERENCE_MODEL,
        "weight": float(weight if weight is not None else _REFERENCE_WEIGHT),
        "image": base64.b64encode(image).decode("ascii"),
        "resize_mode": "Crop and Resize",
        "processor_res": -1,
        "threshold_a": -1,
        "threshold_b": -1,
        "guidance_start": 0.0,
        "guidance_end": 1.0,
        "pixel_perfect": False,
        "control_mode": "ControlNet is more important",
    }
    if mask is not None:
        unit["mask_image"] = base64.b64encode(mask).decode("ascii")
    return unit


def _region_mask(width: int, height: int, index: int, count: int) -> bytes:
    """Máscara PNG da região atribuída ao NPC."""
    import io
    from PIL import Image, ImageDraw, ImageFilter
    width, height = int(width), int(height)
    if count <= 2:
        x0 = width * index // count
        x1 = width * (index + 1) // count
        y0, y1 = 0, height
    else:
        col, row = index % 2, index // 2
        x0, x1 = width * col // 2, width * (col + 1) // 2
        y0, y1 = height * row // 2, height * (row + 1) // 2
    x0, x1 = max(0, x0), min(width, x1)
    y0, y1 = max(0, y0), min(height, y1)
    image = Image.new("L", (width, height), 0)
    draw = ImageDraw.Draw(image)
    draw.rectangle((x0, y0, max(x0, x1 - 1), max(y0, y1 - 1)), fill=255)
    image = image.filter(ImageFilter.GaussianBlur(radius=max(8, width // 40)))
    output = io.BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


def _attach_references(payload: dict, references: list[bytes] | None,
                       *, width: int, height: int) -> dict:
    images = [raw for raw in (references or []) if raw][:_MAX_REFERENCES]
    if not images:
        return payload
    units = []
    for index, raw in enumerate(images):
        mask = None
        if len(images) > 1:
            mask = _region_mask(width, height, index, len(images))
        units.append(_reference_unit(
            raw, mask=mask,
            weight=_REFERENCE_WEIGHT if len(images) == 1 else 0.55,
            model=_REFERENCE_MODEL if len(images) == 1 else _MULTI_REFERENCE_MODEL,
        ))
    payload["alwayson_scripts"] = {"controlnet": {"args": units}}
    return payload


def _generate_sdwebui(cfg: dict, client: httpx.Client, prompt: str,
                      width: int | None = None, height: int | None = None,
                      references: list[bytes] | None = None,
                      ) -> tuple[bytes, int, int]:
    width = int(width or cfg["sdwebui_width"])
    height = int(height or cfg["sdwebui_height"])
    payload = _attach_references(
        _apply_checkpoint(cfg, _sd_payload(cfg, prompt, width, height)),
        references, width=width, height=height,
    )
    data = _post_json(client, f"{_sd_base(cfg)}{cfg['txt2img_path']}",
                      payload, {"Accept": "application/json"}, "txt2img")
    return _first_image(data, "txt2img"), width, height


def _generate_sdwebui_img2img(cfg: dict, client: httpx.Client, prompt: str,
                              init_image: bytes, width: int | None,
                              height: int | None,
                              denoising_strength: float,
                              references: list[bytes] | None = None,
                              ) -> tuple[bytes, int, int]:
    """Pinta a partir de um PNG já salvo. denoising baixo mantém o mesmo rosto."""
    width = int(width or cfg["sdwebui_width"])
    height = int(height or cfg["sdwebui_height"])
    payload = _apply_checkpoint(cfg, _sd_payload(cfg, prompt, width, height))
    payload["init_images"] = [base64.b64encode(init_image).decode("ascii")]
    payload["denoising_strength"] = denoising_strength
    payload = _attach_references(payload, references, width=width, height=height)
    path = cfg.get("img2img_path") or "/sdapi/v1/img2img"
    data = _post_json(client, f"{_sd_base(cfg)}{path}", payload,
                      {"Accept": "application/json"}, "img2img")
    return _first_image(data, "img2img"), width, height


def _generate_openai(cfg: dict, client: httpx.Client, prompt: str) -> tuple[bytes, int | None, int | None]:
    base = cfg["openai_base_url"].rstrip("/")
    if not base:
        raise ImageError("config_error: image.providers.openai_compatible.base_url vazio")
    headers = {"Accept": "application/json"}
    if cfg.get("openai_api_key"):
        headers["Authorization"] = f"Bearer {cfg['openai_api_key']}"
    payload = {
        "model": cfg["openai_model"],
        "prompt": prompt,
        "size": cfg["openai_size"],
        "response_format": "b64_json",
    }
    data = _post_json(client, f"{base}/images/generations", payload, headers,
                      "images/generations")
    items = data.get("data")
    if not isinstance(items, list) or not items or not isinstance(items[0], dict):
        raise ImageError("resposta de images/generations sem lista 'data'")
    first = items[0]
    if first.get("b64_json"):
        content = _decode_b64(str(first["b64_json"]), "images/generations")
    elif first.get("url"):
        content = _download(client, str(first["url"]), headers, base)
    else:
        raise ImageError("resposta de images/generations sem b64_json nem url")
    width = height = None
    try:  # size "WxH" → dimensões (best-effort; None se formato inesperado)
        w, h = str(cfg["openai_size"]).lower().split("x", 1)
        width, height = int(w), int(h)
    except (ValueError, AttributeError):
        pass
    return content, width, height


# ---------------------------------------------------------------- comfyui
def _comfy_base(cfg: dict) -> str:
    base = str(cfg.get("comfyui_base_url") or "").rstrip("/")
    if not base:
        raise ImageError("config_error: image.providers.comfyui.base_url vazio")
    return base


_COMFY_SIGMAS = (
    1.0, 0.978453, 0.95418, 0.926626, 0.89508,
    0.845148, 0.704534, 0.414568, 0.0,
)


def _comfy_qwen_graph(cfg: dict, prompt: str, image_names: list[str],
                      *, width: int, height: int, edit: bool = False) -> dict:
    """FineView/Qwen 2.1 com referências no condicionamento nativo."""
    seed_value = cfg.get("seed")
    noise_seed = (random.randint(0, 2**31 - 1)
                  if seed_value is None or int(seed_value) < 0 else int(seed_value))
    graph = {
        "1": {"class_type": "UNETLoader", "inputs": {
            "unet_name": cfg.get("comfyui_unet") or "",
            "weight_dtype": "default"}},
        "2": {"class_type": "CLIPLoader", "inputs": {
            "clip_name": cfg.get("comfyui_clip") or "",
            "type": cfg.get("comfyui_clip_type") or "qwen_image",
            "device": cfg.get("comfyui_clip_device") or "default"}},
        "3": {"class_type": "VAELoader", "inputs": {
            "vae_name": cfg.get("comfyui_vae") or ""}},
        "4": {"class_type": "TextEncodeQwenImage21", "inputs": {
            "clip": ["2", 0], "prompt": prompt,
            "negative_prompt": cfg.get("negative_prompt") or "",
            "vae": ["3", 0], "resolution": max(width, height)}},
        "7": {"class_type": "ModelSamplingAuraFlow", "inputs": {
            "model": ["1", 0], "shift": float(cfg.get("comfyui_shift") or 1.31)}},
        "10": {"class_type": "ManualSigmas", "inputs": {
            "sigmas": ", ".join(str(value) for value in (
                cfg.get("comfyui_sigmas") or _COMFY_SIGMAS))}},
        "12": {"class_type": "KSamplerSelect", "inputs": {
            "sampler_name": cfg.get("comfyui_sampler") or "euler"}},
        "14": {"class_type": "SamplerCustom", "inputs": {
            "model": ["7", 0], "cfg": float(cfg.get("comfyui_cfg") or 1.0),
            "positive": ["4", 0], "negative": ["4", 1],
            "latent_image": ["4", 2] if edit else ["6", 0],
            "add_noise": True, "noise_seed": noise_seed,
            "sampler": ["12", 0], "sigmas": ["10", 0]}},
        "15": {"class_type": "VAEDecode", "inputs": {
            "samples": ["14", 0], "vae": ["3", 0]}},
        "16": {"class_type": "SaveImage", "inputs": {
            "images": ["15", 0], "filename_prefix": "aidungeon"}},
    }
    if not edit:
        graph["6"] = {"class_type": "EmptyLatentImage", "inputs": {
            "width": width, "height": height, "batch_size": 1}}
    for index, image_name in enumerate(image_names):
        node_id = str(20 + index)
        graph[node_id] = {"class_type": "LoadImage", "inputs": {"image": image_name}}
        graph["4"]["inputs"].setdefault("images", {})[
            f"image_{index + 1}"] = [node_id, 0]
    return graph


def _comfy_upload(client: httpx.Client, base: str, content: bytes,
                  filename: str) -> str:
    """Sobe o PNG de referência para /upload/image; devolve o nome no input."""
    try:
        resp = client.post(
            f"{base}/upload/image",
            files={"image": (filename, content, "image/png")},
            data={"overwrite": "true", "type": "input"},
            headers={"Accept": "application/json"},
        )
    except httpx.TransportError as exc:
        raise ImageUnreachable(
            f"image provider unreachable: {type(exc).__name__}") from exc
    if resp.status_code in (401, 403):
        raise ImageAuthError(f"auth_error: HTTP {resp.status_code} em upload/image")
    if resp.status_code != 200:
        raise ImageError(
            f"HTTP {resp.status_code} em upload/image (ComfyUI): {resp.text[:200]}")
    try:
        data = resp.json()
    except ValueError as exc:
        raise ImageError("resposta de upload/image não é JSON") from exc
    if not isinstance(data, dict):
        raise ImageError("resposta de upload/image inválida (não é objeto JSON)")
    name = str(data.get("name") or filename)
    subfolder = str(data.get("subfolder") or "").strip("/")
    return f"{subfolder}/{name}" if subfolder else name


def _comfy_status_message(status: dict) -> str:
    for item in reversed(status.get("messages") or []):
        if isinstance(item, list) and len(item) > 1 and isinstance(item[1], dict):
            detail = item[1].get("exception_message") or item[1].get("message")
            if detail:
                return str(detail)[:200]
    return "erro sem detalhe"


def _comfy_fetch_image(client: httpx.Client, base: str, outputs: dict) -> bytes:
    images: list[dict] = []
    for output in outputs.values():
        if not isinstance(output, dict):
            continue
        for item in output.get("images") or []:
            if isinstance(item, dict) and item.get("filename"):
                images.append(item)
    if not images:
        raise ImageError("histórico do ComfyUI sem imagens de saída")
    chosen = next((item for item in images if item.get("type") == "output"),
                  images[0])
    params = {"filename": str(chosen["filename"]),
              "type": str(chosen.get("type") or "output")}
    if chosen.get("subfolder"):
        params["subfolder"] = str(chosen["subfolder"])
    try:
        resp = client.get(f"{base}/view", params=params)
    except httpx.TransportError as exc:
        raise ImageUnreachable(
            f"image provider unreachable: {type(exc).__name__}") from exc
    if resp.status_code in (401, 403):
        raise ImageAuthError(f"auth_error: HTTP {resp.status_code} em view")
    if resp.status_code != 200:
        raise ImageError(f"HTTP {resp.status_code} em view (ComfyUI): {resp.text[:200]}")
    return resp.content


def _comfy_run(cfg: dict, client: httpx.Client, graph: dict) -> bytes:
    """Enfileira o grafo, espera a execução e devolve o PNG."""
    base = _comfy_base(cfg)
    submitted = _post_json(
        client, f"{base}/prompt",
        {"prompt": graph, "client_id": uuid.uuid4().hex},
        {"Accept": "application/json"}, "prompt (ComfyUI)")
    prompt_id = str(submitted.get("prompt_id") or "")
    if not prompt_id:
        raise ImageError("resposta de /prompt sem prompt_id")
    if submitted.get("node_errors"):
        raise ImageError(
            f"workflow recusado pelo ComfyUI: {str(submitted['node_errors'])[:200]}")

    timeout = float(cfg.get("timeout_seconds", 300))
    interval = max(0.2, float(cfg.get("comfyui_poll_interval_s") or 1.0))
    deadline = time.monotonic() + timeout
    while True:
        try:
            resp = client.get(f"{base}/history/{prompt_id}")
        except httpx.TransportError as exc:
            raise ImageUnreachable(
                f"image provider unreachable: {type(exc).__name__}") from exc
        if resp.status_code in (401, 403):
            raise ImageAuthError(f"auth_error: HTTP {resp.status_code} em history")
        if resp.status_code != 200:
            raise ImageError(
                f"HTTP {resp.status_code} em history (ComfyUI): {resp.text[:200]}")
        try:
            history = resp.json()
        except ValueError as exc:
            raise ImageError("resposta de history não é JSON") from exc
        entry = history.get(prompt_id) if isinstance(history, dict) else None
        if entry:
            status = entry.get("status") or {}
            if status.get("status_str") == "error":
                raise ImageError(
                    f"execução falhou no ComfyUI: {_comfy_status_message(status)}")
            outputs = entry.get("outputs") or {}
            if outputs:
                return _comfy_fetch_image(client, base, outputs)
        if time.monotonic() >= deadline:
            raise ImageError(f"timeout ({timeout:g}s) aguardando o ComfyUI")
        time.sleep(interval)


def _generate_comfyui(cfg: dict, client: httpx.Client, prompt: str,
                      width: int | None = None, height: int | None = None,
                      *, init_image: bytes | None = None,
                      reference_images: list[bytes] | None = None,
                      ) -> tuple[bytes, int, int]:
    width = int(width or cfg.get("comfyui_width") or 1024)
    height = int(height or cfg.get("comfyui_height") or 1024)
    base = _comfy_base(cfg)
    references = ([init_image] if init_image is not None else []) + list(reference_images or [])
    names = [
        _comfy_upload(
            client, base, raw,
            f"aidungeon-{index}-{hashlib.sha256(raw).hexdigest()[:16]}.png",
        )
        for index, raw in enumerate(references[:_MAX_REFERENCES])
    ]
    content = _comfy_run(cfg, client, _comfy_qwen_graph(
        cfg, prompt, names, width=width, height=height, edit=init_image is not None))
    return content, width, height


def request_key(cfg: dict, prompt: str, *, width: int | None = None,
                height: int | None = None, init_image: bytes | None = None,
                denoising_strength: float | None = None,
                negative_extra: str = "",
                variation: str | int | None = None,
                reference_images: list[bytes] | None = None) -> str:
    """Hash estável do pedido final. Não inclui api_key e nunca vai para prompt.

    Seed e variation só entram quando configurados; assim chaves antigas e o
    comportamento de cache da instalação padrão continuam válidos.
    """
    provider = cfg.get("provider") or "sdwebui"
    negative = _merged_negative({
        **cfg,
        "negative_prompt": ", ".join(
            part for part in ((cfg.get("negative_prompt") or ""), negative_extra) if part
        ),
    })
    if provider == "sdwebui":
        model = cfg.get("sd_model_checkpoint") or ""
        size = [int(width or cfg.get("sdwebui_width") or 0),
                int(height or cfg.get("sdwebui_height") or 0)]
        extra = {
            "steps": cfg.get("sdwebui_steps"),
            "sampler": cfg.get("sampler_name", "DPM++ 2M"),
            "scheduler": cfg.get("scheduler", "Karras"),
            "cfg_scale": float(cfg.get("cfg_scale", 5)),
            "denoise": (
                round(float(denoising_strength if denoising_strength is not None
                            else cfg.get("denoising_strength", 0.35)), 4)
                if init_image is not None or denoising_strength is not None else None
            ),
        }
        if cfg.get("seed") is not None:
            extra["seed"] = int(cfg["seed"])
    elif provider == "comfyui":
        model = cfg.get("comfyui_unet") or ""
        size = [int(width or cfg.get("comfyui_width") or 0),
                int(height or cfg.get("comfyui_height") or 0)]
        extra = {
            "steps": cfg.get("comfyui_steps"),
            "sampler": cfg.get("comfyui_sampler") or "euler",
            "cfg": float(cfg.get("comfyui_cfg") or 1.0),
            "clip": cfg.get("comfyui_clip") or "",
            "clip_type": cfg.get("comfyui_clip_type") or "qwen_image",
            "clip_device": cfg.get("comfyui_clip_device") or "default",
            "vae": cfg.get("comfyui_vae") or "",
            "shift": float(cfg.get("comfyui_shift") or 1.31),
            "sigmas": list(cfg.get("comfyui_sigmas") or _COMFY_SIGMAS),
        }
        if cfg.get("seed") is not None:
            extra["seed"] = int(cfg["seed"])
    else:
        model = cfg.get("openai_model") or ""
        size = cfg.get("openai_size") or ""
        extra = {}
    payload = {
        "v": 1,
        "provider": provider,
        "model": model,
        "prompt": prompt,
        "negative": negative,
        "size": size,
        "extra": extra,
        "init": hashlib.sha256(init_image).hexdigest() if init_image is not None else "",
    }
    refs = [raw for raw in (reference_images or []) if raw][:_MAX_REFERENCES]
    if refs:
        payload["refs"] = [hashlib.sha256(raw).hexdigest() for raw in refs]
    variation_value = cfg.get("variation", 0) if variation is None else variation
    if variation_value not in (None, "", 0, "0"):
        payload["variation"] = str(variation_value)
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


# ------------------------------------------------------------------ público
def generate_image(cfg: dict, prompt: str, *, adventure_id: str, image_id: str,
                   transport: httpx.BaseTransport | None = None,
                   width: int | None = None, height: int | None = None,
                   init_image: bytes | None = None,
                   denoising_strength: float | None = None,
                   negative_extra: str = "",
                   reference_images: list[bytes] | None = None) -> dict:
    """Gera a imagem no provider efetivo, salva o PNG e devolve metadados
    {file_path (relativo), width, height, provider}. Levanta ImageError subclasses.

    init_image pede img2img (mesmo rosto). Sem endpoint img2img o pedido falha
    em vez de inventar outro personagem em txt2img.
    reference_images entram no condicionamento visual nativo do Qwen.
    """
    provider = cfg.get("provider") or "sdwebui"
    timeout = float(cfg.get("timeout_seconds", 300))
    if negative_extra:
        # Cópia rasa: o cfg efetivo é reusado no pedido seguinte.
        cfg = dict(cfg)
        cfg["negative_prompt"] = ", ".join(
            part for part in (cfg.get("negative_prompt") or "", negative_extra) if part
        )
    with _make_client(transport, timeout) as client:
        refs = [raw for raw in (reference_images or []) if raw][:_MAX_REFERENCES]
        if refs and provider != "comfyui":
            raise ImageError("config_error: visual references require ComfyUI Qwen")
        denoise = float(denoising_strength if denoising_strength is not None else 0.35)
        if provider == "comfyui":
            content, width, height = _generate_comfyui(
                cfg, client, prompt, width, height,
                init_image=init_image, reference_images=refs)
        elif init_image is not None:
            if provider == "sdwebui":
                content, width, height = _generate_sdwebui_img2img(
                    cfg, client, prompt, init_image, width, height, denoise, refs)
            else:
                raise ImageError(
                    "cannot keep the same face: this image provider has no img2img")
        elif provider == "sdwebui":
            content, width, height = _generate_sdwebui(
                cfg, client, prompt, width, height, refs)
        elif provider == "openai_compatible":
            content, width, height = _generate_openai(cfg, client, prompt)
        else:
            raise ImageError(f"config_error: provider de imagem desconhecido: {provider!r}")
    file_path = _save_png(cfg, adventure_id, image_id, content)
    return {"file_path": file_path, "width": width, "height": height,
            "provider": provider}


def effective_settings(conn) -> dict:
    """Config efetiva: defaults do arquivo seguidos dos overrides persistidos."""
    from app import settings_store

    return settings_store.effective_image_settings(conn)


def health(cfg: dict, transport: httpx.BaseTransport | None = None) -> dict:
    """Sonda não gerativa do provider. O resultado é sempre serializável."""
    provider = cfg.get("provider") or "sdwebui"
    result = {"enabled": bool(cfg.get("enabled", True)),
              "provider": provider, "status": "ok"}
    if not result["enabled"]:
        result["status"] = "disabled"
        result["detail"] = "image generation is disabled"
        return result
    try:
        timeout = min(float(cfg.get("timeout_seconds", 300)), 10.0)
        with _make_client(transport, timeout) as client:
            if provider == "sdwebui":
                data = _get_json(client, f"{_sd_base(cfg)}/sdapi/v1/options",
                                 {"Accept": "application/json"}, "options")
                result["checkpoint"] = str(data.get("sd_model_checkpoint") or "") or None
            elif provider == "comfyui":
                base = str(cfg.get("comfyui_base_url") or "").rstrip("/")
                if not base:
                    raise ImageError(
                        "config_error: image.providers.comfyui.base_url vazio")
                _get_json(client, f"{base}/system_stats",
                          {"Accept": "application/json"}, "system_stats")
                result["unet"] = cfg.get("comfyui_unet") or None
            elif provider == "openai_compatible":
                base = str(cfg.get("openai_base_url") or "").rstrip("/")
                if not base:
                    raise ImageError(
                        "config_error: image.providers.openai_compatible.base_url vazio")
                headers = {"Accept": "application/json"}
                if cfg.get("openai_api_key"):
                    headers["Authorization"] = f"Bearer {cfg['openai_api_key']}"
                data = _get_json(client, f"{base}/models", headers, "models")
                models = data.get("data")
                if not isinstance(models, list):
                    raise ImageError("resposta de models sem lista 'data'")
                ids = {str(item.get("id")) for item in models if isinstance(item, dict)}
                result["model_count"] = len(ids)
                if cfg.get("openai_model"):
                    result["model_available"] = cfg["openai_model"] in ids
                if not models:
                    result["status"] = "degraded"
                    result["detail"] = "servidor respondeu, mas nenhum modelo está disponível"
                elif not result.get("model_available", True):
                    result["status"] = "degraded"
                    result["detail"] = (
                        f"modelo '{cfg['openai_model']}' não consta em /models")
            else:
                raise ImageError(f"config_error: provider de imagem desconhecido: {provider!r}")
    except ImageError as exc:
        result["status"] = exc.code
        result["detail"] = exc.message
    except Exception as exc:
        log.warning("image health: falha inesperada (%s)", type(exc).__name__)
        result["status"] = "error"
        result["detail"] = "image health check failed"
    return result


def interrupt_sdwebui(cfg: dict, transport: httpx.BaseTransport | None = None) -> None:
    """Pede interrupção cooperativa ao provider; não gera nem exige corpo JSON."""
    timeout = min(float(cfg.get("timeout_seconds", 300)), 10.0)
    if cfg.get("provider") == "comfyui":
        url = f"{str(cfg.get('comfyui_base_url') or '').rstrip('/')}/interrupt"
    else:
        url = f"{_sd_base(cfg)}/sdapi/v1/interrupt"
    with _make_client(transport, timeout) as client:
        try:
            resp = client.post(url, json={}, headers={"Accept": "application/json"})
        except httpx.TransportError as exc:
            raise ImageUnreachable(
                f"image provider unreachable: {type(exc).__name__}") from exc
        if resp.status_code in (401, 403):
            raise ImageAuthError(f"auth_error: HTTP {resp.status_code} em interrupt")
        if resp.status_code != 200:
            raise ImageError(f"HTTP {resp.status_code} em interrupt: {resp.text[:200]}")
