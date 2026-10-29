"""Cliente de geração de imagens (v2) — espelha a estrutura de app/llm_client.py.

Providers (settings.image.provider, config efetiva de settings_store):
- `sdwebui`: POST {sdwebui_base_url}{txt2img_path} (A1111/SD.Next/Forge com
  --api) com JSON {prompt, negative_prompt, steps, width, height}; a resposta
  traz `images[0]` em base64 (png).
- `openai_compatible`: POST {openai_base_url}/images/generations com Bearer
  (openai_api_key, pode ser vazia), JSON {model, prompt, size, response_format:
  "b64_json"}; aceita `data[0].b64_json` ou `data[0].url` (neste caso baixa o
  arquivo da URL).
- `comfyui`: POST {comfyui_base_url}/prompt e depois GET /history/{prompt_id}
  e GET /view. `engine` omisso ou `qwen` (default) monta UNETLoader,
  CLIPLoader qwen_image, TextEncodeQwenImage21, ModelSamplingAuraFlow,
  ManualSigmas e SamplerCustom. Referências entram no encoder visual;
  retratos com âncora usam seu latent zero e cenas usam EmptyLatentImage.
  `engine=flux2_klein` é opt-in e lê `comfyui_flux2` (UNET FP8, CLIP
  qwen_3_4b type=flux2, flux2-vae): EmptyFlux2LatentImage e Flux2Scheduler
  no mesmo canvas já múltiplo de 16 (860x820 vira 848x816; Qwen não alinha),
  4 passos, CFG 1, euler. Referências passam por LoadImage,
  ImageScaleToTotalPixels, VAEEncode e ReferenceLatent (teto 4). Não altera
  `comfyui_unet` nem os grafos LTXV/Wan.

O PNG decodificado é salvo em {storage_dir}/{adventure_id}/{image_id}.png
(storage_dir relativo à raiz do repo) e o caminho RELATIVO é devolvido para
ser gravado em images.file_path.

`generate_clip` é image-to-video no mesmo ComfyUI. O default é `ltxv`
(CheckpointLoaderSimple + CLIPLoader `ltxv` + LTXVImgToVideo + SamplerCustom).
`engine=wan` é opt-in e usa o TI2V 5B (template video_wan2_2_5B_ti2v; GGUF
usa UnetLoaderGGUF). Lê `comfyui_video` e não apaga `unet`/`vae` do Wan nem
altera `provider` ou `comfyui_unet`.
Não chama narrador nem voz. O arquivo gravado é um MP4 (ftyp), nunca um PNG
renomeado.

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
from typing import Any, Callable

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
_FLUX2_MAX_REFERENCES = 4
_FLUX2_DEFAULTS = {
    "unet": "flux-2-klein-4b-fp8.safetensors",
    "clip": "qwen_3_4b.safetensors",
    "clip_type": "flux2",
    "clip_device": "default",
    "vae": "flux2-vae.safetensors",
    "weight_dtype": "default",
    "steps": 4,
    "cfg": 1.0,
    "sampler": "euler",
    "megapixels": 0.25,
    "upscale_method": "nearest-exact",
    "resolution_steps": 1,
    "max_references": _FLUX2_MAX_REFERENCES,
}


def _comfy_image_engine(cfg: dict) -> str:
    """`engine` omisso ou vazio é `qwen`. Outro valor só `flux2_klein` segue."""
    engine = str(cfg.get("comfyui_engine") or "qwen").strip().lower() or "qwen"
    if engine not in ("qwen", "flux2_klein"):
        raise ImageError(f"config_error: image.engine desconhecido: {engine!r}")
    return engine


def flux2_settings(cfg: dict) -> dict:
    """Bloco `image.providers.comfyui.flux2`. Não lê o UNET do FineView."""
    raw = cfg.get("comfyui_flux2")
    if not isinstance(raw, dict):
        raw = {}
    merged = dict(_FLUX2_DEFAULTS)
    for key, default in _FLUX2_DEFAULTS.items():
        if key not in raw or raw[key] is None:
            continue
        if isinstance(default, float):
            merged[key] = float(raw[key])
        elif isinstance(default, int):
            merged[key] = int(raw[key])
        else:
            text = str(raw[key]).strip()
            if not text and key not in ("unet", "clip", "vae"):
                continue
            merged[key] = text
    cap = int(merged["max_references"])
    merged["max_references"] = (
        _FLUX2_MAX_REFERENCES if cap < 1 else min(_FLUX2_MAX_REFERENCES, cap))
    return merged


def _flux2_used_references(init_image: bytes | None,
                           reference_images: list[bytes] | None,
                           limit: int) -> list[bytes]:
    """Âncora primeiro, depois as refs. O teto vale para o grafo e para o hash."""
    sequence = ([init_image] if init_image is not None else []) + [
        raw for raw in (reference_images or []) if raw]
    return sequence[:limit]


def _flux2_canvas(width: int | None, height: int | None,
                  cfg: dict) -> tuple[int, int]:
    """Resolve None e 0 pelos defaults do ComfyUI (omissão → 1024).

    Só `flux2_klein` alinha cada lado para baixo ao múltiplo de 16, com piso
    16: 860x820 vira 848x816. Dimensão já válida é identidade. Qwen e o clipe
    não passam por este snap — o clipe nem chama este helper.
    """
    resolved_w = int(width or cfg.get("comfyui_width") or 1024)
    resolved_h = int(height or cfg.get("comfyui_height") or 1024)
    if _comfy_image_engine(cfg) != "flux2_klein":
        return resolved_w, resolved_h
    return (
        max(16, (int(resolved_w) // 16) * 16),
        max(16, (int(resolved_h) // 16) * 16),
    )


def _validate_flux2_canvas(width: int, height: int) -> None:
    if width % 16 or height % 16:
        raise ImageError(
            f"config_error: flux2 exige width e height múltiplos de 16 "
            f"(recebido {width}x{height})")


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


def _comfy_flux2_klein_graph(cfg: dict, prompt: str, image_names: list[str],
                             *, width: int, height: int) -> dict:
    """FLUX.2 Klein 4B distilled. O sampler sempre parte de latent vazio.

    Cada referência é LoadImage → ImageScaleToTotalPixels → VAEEncode →
    ReferenceLatent, em série, no positivo e no negativo. EmptyFlux2LatentImage
    e Flux2Scheduler recebem o mesmo canvas já normalizado.
    """
    width, height = _flux2_canvas(width, height, cfg)
    _validate_flux2_canvas(width, height)
    flux = flux2_settings(cfg)
    seed_value = cfg.get("seed")
    noise_seed = (random.randint(0, 2**31 - 1)
                  if seed_value is None or int(seed_value) < 0 else int(seed_value))
    positive: list = ["4", 0]
    negative: list = ["5", 0]
    graph: dict = {
        "1": {"class_type": "UNETLoader", "inputs": {
            "unet_name": flux["unet"],
            "weight_dtype": flux["weight_dtype"] or "default"}},
        "2": {"class_type": "CLIPLoader", "inputs": {
            "clip_name": flux["clip"],
            "type": flux["clip_type"] or "flux2",
            "device": flux["clip_device"] or "default"}},
        "3": {"class_type": "VAELoader", "inputs": {
            "vae_name": flux["vae"]}},
        "4": {"class_type": "CLIPTextEncode", "inputs": {
            "clip": ["2", 0], "text": prompt}},
        "5": {"class_type": "ConditioningZeroOut", "inputs": {
            "conditioning": ["4", 0]}},
        "6": {"class_type": "EmptyFlux2LatentImage", "inputs": {
            "width": width, "height": height, "batch_size": 1}},
        "7": {"class_type": "Flux2Scheduler", "inputs": {
            "steps": int(flux["steps"]), "width": width, "height": height}},
        "8": {"class_type": "KSamplerSelect", "inputs": {
            "sampler_name": flux["sampler"] or "euler"}},
        "9": {"class_type": "RandomNoise", "inputs": {
            "noise_seed": noise_seed}},
        "10": {"class_type": "CFGGuider", "inputs": {
            "model": ["1", 0], "positive": positive, "negative": negative,
            "cfg": float(flux["cfg"])}},
        "11": {"class_type": "SamplerCustomAdvanced", "inputs": {
            "noise": ["9", 0], "guider": ["10", 0], "sampler": ["8", 0],
            "sigmas": ["7", 0], "latent_image": ["6", 0]}},
        "12": {"class_type": "VAEDecode", "inputs": {
            "samples": ["11", 0], "vae": ["3", 0]}},
        "13": {"class_type": "SaveImage", "inputs": {
            "images": ["12", 0], "filename_prefix": "aidungeon"}},
    }
    for index, image_name in enumerate(image_names):
        base = 20 + index * 5
        load, scale, encode, ref_pos, ref_neg = (
            str(base + offset) for offset in range(5))
        graph[load] = {"class_type": "LoadImage", "inputs": {"image": image_name}}
        graph[scale] = {"class_type": "ImageScaleToTotalPixels", "inputs": {
            "image": [load, 0],
            "upscale_method": flux["upscale_method"] or "nearest-exact",
            "megapixels": float(flux["megapixels"]),
            "resolution_steps": int(flux["resolution_steps"])}}
        graph[encode] = {"class_type": "VAEEncode", "inputs": {
            "pixels": [scale, 0], "vae": ["3", 0]}}
        graph[ref_pos] = {"class_type": "ReferenceLatent", "inputs": {
            "conditioning": positive, "latent": [encode, 0]}}
        graph[ref_neg] = {"class_type": "ReferenceLatent", "inputs": {
            "conditioning": negative, "latent": [encode, 0]}}
        positive = [ref_pos, 0]
        negative = [ref_neg, 0]
    graph["10"]["inputs"]["positive"] = positive
    graph["10"]["inputs"]["negative"] = negative
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


def _comfy_view(client: httpx.Client, base: str, item: dict) -> bytes:
    params = {"filename": str(item["filename"]),
              "type": str(item.get("type") or "output")}
    if item.get("subfolder"):
        params["subfolder"] = str(item["subfolder"])
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
    return _comfy_view(client, base, chosen)


def comfy_poll_interval_s(cfg: dict) -> float:
    """Espera entre leituras do history. Piso 0,2 s; omissão usa 0,25 s."""
    return max(0.2, float(cfg.get("comfyui_poll_interval_s") or 0.25))


def _emit_progress(progress: Callable[[], None] | None) -> None:
    if progress is not None:
        progress()


def _comfy_run(cfg: dict, client: httpx.Client, graph: dict, *,
               timeout: float | None = None,
               fetch=None,
               progress: Callable[[], None] | None = None) -> bytes:
    """Enfileira o grafo, espera a execução e devolve o arquivo de saída.

    `progress`, quando não é None, roda depois do /prompt aceito e depois de
    cada leitura de /history. Exceção do callback propaga e aborta a espera.
    """
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
    _emit_progress(progress)

    if timeout is None:
        timeout = float(cfg.get("timeout_seconds", 300))
    else:
        timeout = float(timeout)
    interval = comfy_poll_interval_s(cfg)
    deadline = time.monotonic() + timeout
    reader = fetch or _comfy_fetch_image
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
        _emit_progress(progress)
        entry = history.get(prompt_id) if isinstance(history, dict) else None
        if entry:
            status = entry.get("status") or {}
            if status.get("status_str") == "error":
                raise ImageError(
                    f"execução falhou no ComfyUI: {_comfy_status_message(status)}")
            outputs = entry.get("outputs") or {}
            if outputs:
                return reader(client, base, outputs)
        if time.monotonic() >= deadline:
            raise ImageError(f"timeout ({timeout:g}s) aguardando o ComfyUI")
        time.sleep(interval)


def _generate_comfyui(cfg: dict, client: httpx.Client, prompt: str,
                      width: int | None = None, height: int | None = None,
                      *, init_image: bytes | None = None,
                      reference_images: list[bytes] | None = None,
                      progress: Callable[[], None] | None = None,
                      ) -> tuple[bytes, int, int]:
    width, height = _flux2_canvas(width, height, cfg)
    engine = _comfy_image_engine(cfg)
    if engine == "flux2_klein":
        _validate_flux2_canvas(width, height)
        flux = flux2_settings(cfg)
        for label in ("unet", "clip", "vae"):
            if not str(flux.get(label) or "").strip():
                raise ImageError(
                    f"config_error: image.providers.comfyui.flux2.{label} vazio")
        references = _flux2_used_references(
            init_image, reference_images, int(flux["max_references"]))
    else:
        references = (
            ([init_image] if init_image is not None else [])
            + list(reference_images or [])
        )[:_MAX_REFERENCES]
    base = _comfy_base(cfg)
    names = [
        _comfy_upload(
            client, base, raw,
            f"aidungeon-{index}-{hashlib.sha256(raw).hexdigest()[:16]}.png",
        )
        for index, raw in enumerate(references)
    ]
    if engine == "flux2_klein":
        graph = _comfy_flux2_klein_graph(
            cfg, prompt, names, width=width, height=height)
    else:
        graph = _comfy_qwen_graph(
            cfg, prompt, names, width=width, height=height,
            edit=init_image is not None)
    content = _comfy_run(cfg, client, graph, progress=progress)
    return content, width, height


def request_key(cfg: dict, prompt: str, *, width: int | None = None,
                height: int | None = None, init_image: bytes | None = None,
                denoising_strength: float | None = None,
                negative_extra: str = "",
                variation: str | int | None = None,
                reference_images: list[bytes] | None = None) -> str:
    """Hash estável do pedido final. Não inclui api_key e nunca vai para prompt.

    Seed e variation só entram quando configurados; assim chaves antigas e o
    comportamento de cache da instalação padrão continuam válidos. `engine`
    omisso ou `qwen` não entra no hash. `flux2_klein` entra, e as referências
    desse engine param no teto de 4, âncora inclusa.
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
        size = list(_flux2_canvas(width, height, cfg))
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
        if _comfy_image_engine(cfg) == "flux2_klein":
            flux = flux2_settings(cfg)
            extra["engine"] = "flux2_klein"
            extra["flux2"] = {
                "unet": flux["unet"],
                "clip": flux["clip"],
                "clip_type": flux["clip_type"],
                "clip_device": flux["clip_device"],
                "vae": flux["vae"],
                "weight_dtype": flux["weight_dtype"],
                "steps": int(flux["steps"]),
                "cfg": float(flux["cfg"]),
                "sampler": flux["sampler"],
                "megapixels": float(flux["megapixels"]),
                "upscale_method": flux["upscale_method"],
                "resolution_steps": int(flux["resolution_steps"]),
            }
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
    klein = provider == "comfyui" and _comfy_image_engine(cfg) == "flux2_klein"
    if klein:
        used = _flux2_used_references(
            init_image, reference_images,
            int(flux2_settings(cfg)["max_references"]))
        payload["init"] = hashlib.sha256(used[0]).hexdigest() if used else ""
        if len(used) > 1:
            payload["refs"] = [hashlib.sha256(raw).hexdigest() for raw in used[1:]]
    else:
        refs = [raw for raw in (reference_images or []) if raw][:_MAX_REFERENCES]
        if refs:
            payload["refs"] = [hashlib.sha256(raw).hexdigest() for raw in refs]
    variation_value = cfg.get("variation", 0) if variation is None else variation
    if variation_value not in (None, "", 0, "0"):
        payload["variation"] = str(variation_value)
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


# --------------------------------------------------------- i2v (wan / ltxv)
# Wan: template oficial video_wan2_2_5B_ti2v. GGUF troca só o loader
# (UnetLoaderGGUF). O UNET de imagem não entra no grafo.
# LTXV 0.9.x: grafo nativo Comfy (CheckpointLoaderSimple + CLIPLoader ltxv
# + LTXVImgToVideo + SamplerCustom). Frames 8n+1.
_WAN_NEGATIVE = (
    "色调艳丽，过曝，静态，细节模糊不清，字幕，风格，作品，画作，画面，静止，整体发灰，"
    "最差质量，低质量，JPEG压缩残留，丑陋的，残缺的，多余的手指，画得不好的手部，"
    "画得不好的脸部，畸形的，毁容的，形态畸形的肢体，手指融合，静止不动的画面，"
    "杂乱的背景，三条腿，背景人很多，倒着走"
)
# Negativo do exemplo oficial LTXV 0.9 (ComfyUI_examples/ltxv). CFG 1 no
# distilled quase não usa, mas o CLIPTextEncode negativo continua no grafo.
_LTXV_NEGATIVE = (
    "low quality, worst quality, deformed, distorted, disfigured, "
    "motion smear, motion artifacts, fused fingers, bad anatomy, "
    "weird hand, ugly"
)
_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
_CLIP_CACHE_SKIP = ("timeout_seconds", "note", "auto_scene", "auto_portraits")
_VIDEO_DEFAULTS = {
    # LTXV 2B 0.9.6 distilled é o default (768x448, 8 passos, CFG 1).
    # `engine=wan` é opt-in: unet/vae/shift continuam no bloco.
    "engine": "ltxv",
    "checkpoint": "ltxv-2b-0.9.6-distilled-04-25.safetensors",
    "unet": "Wan2.2-TI2V-5B-Q8_0.gguf",
    "clip": "t5xxl_fp8_e4m3fn_scaled.safetensors",
    "vae": "wan2.2_vae.safetensors",
    "frames": 121,
    "fps": 24.0,
    "steps": 8,
    "cfg": 1.0,
    "shift": 8.0,
    "timeout_seconds": 1800.0,
    "width": 768,
    "height": 448,
    "vertical_width": 448,
    "vertical_height": 768,
    # Só agendam o follow-up. Fora do grafo e da chave de cache.
    "auto_scene": False,
    "auto_portraits": False,
}


def video_settings(cfg: dict) -> dict:
    """Bloco `image.providers.comfyui.video`. Não lê o UNET de imagem.

    `engine` falta ou vem vazio → `ltxv`. `engine=wan` é opt-in e não apaga
    `unet`/`vae`. `auto_scene` e `auto_portraits` só ligam o clipe depois
    do PNG. Default false. Não entram no grafo.
    """
    raw = cfg.get("comfyui_video")
    if not isinstance(raw, dict):
        raw = {}
    merged = dict(_VIDEO_DEFAULTS)
    for key, default in _VIDEO_DEFAULTS.items():
        if key not in raw or raw[key] is None:
            continue
        if isinstance(default, bool):
            merged[key] = bool(raw[key])
        elif isinstance(default, float):
            merged[key] = float(raw[key])
        elif isinstance(default, int):
            merged[key] = int(raw[key])
        else:
            text = str(raw[key])
            if key == "engine":
                text = text.strip().lower() or "ltxv"
            elif key == "checkpoint":
                text = text.strip()
            merged[key] = text
    return merged


def _video_engine(video: dict) -> str:
    return str(video.get("engine") or "ltxv").strip().lower() or "ltxv"


def _png_size(content: bytes) -> tuple[int, int]:
    if len(content) < 24 or not content.startswith(_PNG_MAGIC) or content[12:16] != b"IHDR":
        raise ImageError("source image must be a PNG")
    width = int.from_bytes(content[16:20], "big")
    height = int.from_bytes(content[20:24], "big")
    if width <= 0 or height <= 0:
        raise ImageError("source image must be a PNG")
    return width, height


def _clip_frame(video: dict, source: bytes) -> tuple[int, int]:
    width, height = _png_size(source)
    if width >= height:
        return int(video["width"]), int(video["height"])
    return int(video["vertical_width"]), int(video["vertical_height"])


def _is_local_url(url: str) -> bool:
    try:
        host = (httpx.URL(url).host or "").lower().strip("[]")
    except Exception:
        return False
    return host in {"127.0.0.1", "localhost", "::1"}


def _is_mp4(content: bytes) -> bool:
    if len(content) < 12:
        return False
    if content.startswith((_PNG_MAGIC, b"GIF8", b"\xff\xd8\xff", b"RIFF")):
        return False
    return b"ftyp" in content[:32]


def _comfy_free(client: httpx.Client, base: str) -> None:
    """POST /free descarrega modelos do Comfy e devolve a VRAM."""
    try:
        resp = client.post(
            f"{base}/free",
            json={"unload_models": True, "free_memory": True},
            headers={"Accept": "application/json"},
        )
    except httpx.TransportError as exc:
        raise ImageUnreachable(
            f"image provider unreachable: {type(exc).__name__}") from exc
    if resp.status_code in (401, 403):
        raise ImageAuthError(f"auth_error: HTTP {resp.status_code} em free")
    if resp.status_code != 200:
        raise ImageError(
            f"HTTP {resp.status_code} em free (ComfyUI): {resp.text[:200]}")


def _free_comfy(base: str, transport: httpx.BaseTransport | None) -> None:
    with _make_client(transport, 10) as client:
        _comfy_free(client, base)


def _free_comfy_quiet(base: str, transport: httpx.BaseTransport | None) -> None:
    try:
        _free_comfy(base, transport)
    except ImageError as exc:
        log.warning("comfy free: %s", exc.message)
    except Exception as exc:
        log.warning("comfy free: %s", type(exc).__name__)


def _unload_local_forge(cfg: dict, transport: httpx.BaseTransport | None) -> None:
    """Forge local solta o SDXL antes do clipe. Conexão recusada: Forge parado.
    HTTP de erro aborta antes do /prompt. Provider que não é sdwebui, ou URL
    que não é localhost, não é chamado."""
    if (cfg.get("provider") or "sdwebui") != "sdwebui":
        return
    base_url = str(cfg.get("sdwebui_base_url") or "")
    if not base_url or not _is_local_url(base_url):
        return
    url = f"{base_url.rstrip('/')}/sdapi/v1/unload-checkpoint"
    with _make_client(transport, 10) as client:
        try:
            resp = client.post(url, json={}, headers={"Accept": "application/json"})
        except httpx.ConnectError:
            return
        except httpx.TransportError as exc:
            if "refused" in str(exc).lower():
                return
            raise ImageUnreachable(
                f"image provider unreachable: {type(exc).__name__}") from exc
        if resp.status_code in (401, 403):
            raise ImageAuthError(
                f"auth_error: HTTP {resp.status_code} em unload-checkpoint")
        if resp.status_code != 200:
            raise ImageError(
                f"HTTP {resp.status_code} em unload-checkpoint: {resp.text[:200]}")


def _wan_ti2v_graph(video: dict, prompt: str, image_name: str, *,
                    width: int, height: int, seed: int) -> dict:
    """API prompt do Wan2.2 TI2V 5B. O loader segue a extensão do arquivo."""
    unet_name = str(video.get("unet") or "")
    if unet_name.lower().endswith(".gguf"):
        loader = "UnetLoaderGGUF"
        unet_inputs: dict[str, Any] = {"unet_name": unet_name}
    else:
        loader = "UNETLoader"
        unet_inputs = {"unet_name": unet_name, "weight_dtype": "default"}
    return {
        "1": {"class_type": loader, "inputs": unet_inputs},
        "2": {"class_type": "CLIPLoader", "inputs": {
            "clip_name": str(video.get("clip") or ""),
            "type": "wan",
            "device": "default"}},
        "3": {"class_type": "VAELoader", "inputs": {
            "vae_name": str(video.get("vae") or "")}},
        "4": {"class_type": "ModelSamplingSD3", "inputs": {
            "model": ["1", 0], "shift": float(video["shift"])}},
        "5": {"class_type": "CLIPTextEncode", "inputs": {
            "clip": ["2", 0], "text": prompt}},
        "6": {"class_type": "CLIPTextEncode", "inputs": {
            "clip": ["2", 0], "text": _WAN_NEGATIVE}},
        "7": {"class_type": "LoadImage", "inputs": {"image": image_name}},
        "8": {"class_type": "Wan22ImageToVideoLatent", "inputs": {
            "vae": ["3", 0], "start_image": ["7", 0],
            "width": int(width), "height": int(height),
            "length": int(video["frames"]), "batch_size": 1}},
        "9": {"class_type": "KSampler", "inputs": {
            "model": ["4", 0], "positive": ["5", 0], "negative": ["6", 0],
            "latent_image": ["8", 0], "seed": int(seed),
            "steps": int(video["steps"]), "cfg": float(video["cfg"]),
            "sampler_name": "uni_pc", "scheduler": "simple", "denoise": 1.0}},
        "10": {"class_type": "VAEDecode", "inputs": {
            "samples": ["9", 0], "vae": ["3", 0]}},
        "11": {"class_type": "CreateVideo", "inputs": {
            "images": ["10", 0], "fps": float(video["fps"])}},
        "12": {"class_type": "SaveVideo", "inputs": {
            "video": ["11", 0], "filename_prefix": "video/aidungeon",
            "format": "mp4", "codec": "auto"}},
    }


def _ltxv_i2v_graph(video: dict, prompt: str, image_name: str, *,
                    width: int, height: int, seed: int) -> dict:
    """API prompt nativo do LTXV 0.9.x. Checkpoint traz MODEL+VAE; o CLIP
    é o loader `ltxv`. O UNET/VAE do Wan não entra no grafo."""
    return {
        "1": {"class_type": "CheckpointLoaderSimple", "inputs": {
            "ckpt_name": str(video.get("checkpoint") or "")}},
        "2": {"class_type": "CLIPLoader", "inputs": {
            "clip_name": str(video.get("clip") or ""),
            "type": "ltxv",
            "device": "default"}},
        "3": {"class_type": "CLIPTextEncode", "inputs": {
            "clip": ["2", 0], "text": prompt}},
        "4": {"class_type": "CLIPTextEncode", "inputs": {
            "clip": ["2", 0], "text": _LTXV_NEGATIVE}},
        "5": {"class_type": "LoadImage", "inputs": {"image": image_name}},
        "6": {"class_type": "LTXVPreprocess", "inputs": {
            "image": ["5", 0], "img_compression": 40}},
        "7": {"class_type": "LTXVImgToVideo", "inputs": {
            "positive": ["3", 0], "negative": ["4", 0],
            "vae": ["1", 2], "image": ["6", 0],
            "width": int(width), "height": int(height),
            "length": int(video["frames"]), "batch_size": 1,
            "strength": 1.0}},
        "8": {"class_type": "LTXVConditioning", "inputs": {
            "positive": ["7", 0], "negative": ["7", 1],
            "frame_rate": float(video["fps"])}},
        "9": {"class_type": "LTXVScheduler", "inputs": {
            "steps": int(video["steps"]),
            "max_shift": 2.05, "base_shift": 0.95,
            "stretch": True, "terminal": 0.1,
            "latent": ["7", 2]}},
        "10": {"class_type": "KSamplerSelect", "inputs": {
            "sampler_name": "euler"}},
        "11": {"class_type": "SamplerCustom", "inputs": {
            "model": ["1", 0], "add_noise": True, "noise_seed": int(seed),
            "cfg": float(video["cfg"]),
            "positive": ["8", 0], "negative": ["8", 1],
            "sampler": ["10", 0], "sigmas": ["9", 0],
            "latent_image": ["7", 2]}},
        "12": {"class_type": "VAEDecode", "inputs": {
            "samples": ["11", 0], "vae": ["1", 2]}},
        "13": {"class_type": "CreateVideo", "inputs": {
            "images": ["12", 0], "fps": float(video["fps"])}},
        "14": {"class_type": "SaveVideo", "inputs": {
            "video": ["13", 0], "filename_prefix": "video/aidungeon",
            "format": "mp4", "codec": "h264"}},
    }


def _history_motion(outputs: dict) -> dict:
    """SaveVideo publica em `images`; versões antigas usam `videos`/`gifs`."""
    videos: list[dict] = []
    gifs: list[dict] = []
    native: list[dict] = []
    for output in outputs.values():
        if not isinstance(output, dict):
            continue
        for item in output.get("videos") or []:
            if isinstance(item, dict) and item.get("filename"):
                videos.append(item)
        for item in output.get("gifs") or []:
            if isinstance(item, dict) and item.get("filename"):
                gifs.append(item)
        for item in output.get("images") or []:
            if (isinstance(item, dict)
                    and str(item.get("filename") or "").lower().endswith(".mp4")):
                native.append(item)
    pool = videos or gifs or native
    if not pool:
        raise ImageError("histórico do ComfyUI sem vídeo (videos/gifs/images MP4)")
    return next((item for item in pool if item.get("type") == "output"), pool[0])


def _comfy_fetch_video(client: httpx.Client, base: str, outputs: dict) -> bytes:
    return _comfy_view(client, base, _history_motion(outputs))


def _save_mp4(cfg: dict, adventure_id: str, image_id: str, content: bytes) -> str:
    """Grava um MP4 real. PNG, GIF ou arquivo pela metade não ficam no disco."""
    directory = db.REPO_ROOT / cfg["storage_dir"] / adventure_id
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{image_id}.mp4"
    partial = path.with_name(path.name + ".partial")
    try:
        partial.write_bytes(content)
        if not _is_mp4(content):
            raise ImageError("saída do ComfyUI não é um MP4")
        partial.replace(path)
    except Exception:
        partial.unlink(missing_ok=True)
        path.unlink(missing_ok=True)
        raise
    try:
        return path.relative_to(db.REPO_ROOT).as_posix()
    except ValueError:
        return str(path)


def clip_request_key(cfg: dict, prompt: str, source: bytes) -> str:
    """Engine, checkpoint e os parâmetros visuais entram na chave.

    Timeout, nota e flags de agendamento ficam de fora. Engine omitido é
    `ltxv`. `engine=wan` e outro checkpoint não compartilham essa chave.
    """
    video = video_settings(cfg)
    payload = {
        "v": 2,
        "kind": "scene_clip",
        "video": {
            key: value for key, value in video.items()
            if key not in _CLIP_CACHE_SKIP
        },
        "prompt": " ".join(str(prompt or "").split()),
        "source": hashlib.sha256(source).hexdigest() if source else "",
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _clip_seed(cfg: dict) -> int:
    seed_value = cfg.get("seed")
    if seed_value is None or int(seed_value) < 0:
        return random.randint(0, 2**31 - 1)
    return int(seed_value)


def generate_clip(cfg: dict, prompt: str, *, adventure_id: str, image_id: str,
                  source_png: bytes,
                  transport: httpx.BaseTransport | None = None,
                  progress: Callable[[], None] | None = None) -> dict:
    """Image-to-video no ComfyUI. Exige o PNG de origem, grava `.mp4` e
    devolve file_path, width, height, provider, media_type e duration_ms.

    Sem `engine`, ou com `ltxv`, monta o grafo nativo e exige checkpoint.
    `engine=wan` é opt-in (TI2V); unet/vae do Wan ficam no bloco. Não muda `cfg`.

    Provider de imagem `sdwebui`: /free, unload do Forge local, upload,
    /prompt, /history, /view, /free — o SDXL precisa da VRAM. Provider
    `comfyui`: sem /free. O Comfy troca o modelo e o clipe seguinte reusa
    o que ficou carregado.
    """
    prompt = " ".join(str(prompt or "").split())
    if not prompt:
        raise ImageError("clip prompt is required")
    if not source_png:
        raise ImageError("source image is required")
    if not source_png.startswith(_PNG_MAGIC):
        raise ImageError("source image must be a PNG")
    video = video_settings(cfg)
    frames = int(video["frames"])
    fps = float(video["fps"])
    if frames <= 0 or fps <= 0:
        raise ImageError("config_error: video frames/fps inválidos")
    engine = _video_engine(video)
    if engine == "wan":
        if not str(video.get("unet") or "").strip():
            raise ImageError("config_error: image.providers.comfyui.video.unet vazio")
    elif engine == "ltxv":
        if not str(video.get("checkpoint") or "").strip():
            raise ImageError(
                "config_error: image.providers.comfyui.video.checkpoint vazio")
        if not str(video.get("clip") or "").strip():
            raise ImageError("config_error: image.providers.comfyui.video.clip vazio")
        if frames < 9 or (frames - 1) % 8 != 0:
            raise ImageError(
                "config_error: video.frames do LTXV deve ser 8n+1 (mínimo 9)")
    else:
        raise ImageError(f"config_error: video.engine desconhecido: {engine!r}")
    base = _comfy_base(cfg)
    frame_w, frame_h = _clip_frame(video, source_png)
    timeout = float(video["timeout_seconds"])
    client_timeout = timeout if timeout >= 1 else 30.0
    # Só o Forge (sdwebui) divide a placa com o Comfy. Imagem no próprio
    # ComfyUI troca de modelo sem unload forçado entre clipes.
    release_for_forge = (cfg.get("provider") or "sdwebui") != "comfyui"
    try:
        if release_for_forge:
            _free_comfy(base, transport)
        _unload_local_forge(cfg, transport)
        with _make_client(transport, client_timeout) as client:
            name = _comfy_upload(
                client, base, source_png,
                f"aidungeon-clip-{hashlib.sha256(source_png).hexdigest()[:16]}.png",
            )
            if engine == "ltxv":
                graph = _ltxv_i2v_graph(
                    video, prompt, name, width=frame_w, height=frame_h,
                    seed=_clip_seed(cfg))
            else:
                graph = _wan_ti2v_graph(
                    video, prompt, name, width=frame_w, height=frame_h,
                    seed=_clip_seed(cfg))
            content = _comfy_run(
                cfg, client, graph, timeout=timeout, fetch=_comfy_fetch_video,
                progress=progress)
        file_path = _save_mp4(cfg, adventure_id, image_id, content)
    finally:
        if release_for_forge:
            _free_comfy_quiet(base, transport)
    return {
        "file_path": file_path,
        "width": frame_w,
        "height": frame_h,
        "provider": "comfyui",
        "media_type": "video/mp4",
        "duration_ms": int(round(frames * 1000 / fps)),
    }


def interrupt_comfy(cfg: dict, transport: httpx.BaseTransport | None = None) -> None:
    """Cancela o prompt de vídeo e solta a VRAM (/interrupt e /free)."""
    base = _comfy_base(cfg)
    interrupt_error: ImageError | None = None
    with _make_client(transport, 10) as client:
        try:
            resp = client.post(
                f"{base}/interrupt", json={},
                headers={"Accept": "application/json"})
        except httpx.TransportError as exc:
            interrupt_error = ImageUnreachable(
                f"image provider unreachable: {type(exc).__name__}")
            resp = None
        if interrupt_error is None and resp is not None:
            if resp.status_code in (401, 403):
                interrupt_error = ImageAuthError(
                    f"auth_error: HTTP {resp.status_code} em interrupt")
            elif resp.status_code != 200:
                interrupt_error = ImageError(
                    f"HTTP {resp.status_code} em interrupt: {resp.text[:200]}")
        try:
            _comfy_free(client, base)
        except ImageError:
            if interrupt_error is None:
                raise
    if interrupt_error is not None:
        raise interrupt_error


# ------------------------------------------------------------------ público
def generate_image(cfg: dict, prompt: str, *, adventure_id: str, image_id: str,
                   transport: httpx.BaseTransport | None = None,
                   width: int | None = None, height: int | None = None,
                   init_image: bytes | None = None,
                   denoising_strength: float | None = None,
                   negative_extra: str = "",
                   reference_images: list[bytes] | None = None,
                   progress: Callable[[], None] | None = None) -> dict:
    """Gera a imagem no provider efetivo, salva o PNG e devolve metadados
    {file_path (relativo), width, height, provider}. Levanta ImageError subclasses.

    init_image pede img2img (mesmo rosto). Sem endpoint img2img o pedido falha
    em vez de inventar outro personagem em txt2img.
    reference_images entram no condicionamento visual nativo do Qwen.
    Com engine flux2_klein viram ReferenceLatent, no máximo 4, âncora inclusa.
    Nesse engine, width/height do retorno são o canvas efetivo (múltiplo de 16).
    `progress`, se não for None, roda no provider comfyui após o submit e a
    cada poll. Nos outros providers o argumento é aceito e ignorado.
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
                init_image=init_image, reference_images=refs,
                progress=progress)
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
                engine = _comfy_image_engine(cfg)
                _get_json(client, f"{base}/system_stats",
                          {"Accept": "application/json"}, "system_stats")
                if engine == "flux2_klein":
                    result["unet"] = flux2_settings(cfg)["unet"] or None
                else:
                    result["unet"] = cfg.get("comfyui_unet") or None
                result["engine"] = engine
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
