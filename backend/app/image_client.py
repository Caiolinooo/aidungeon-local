"""Cliente de geração de imagens (v2) — espelha a estrutura de app/llm_client.py.

Providers (settings.image.provider, config efetiva de settings_store):
- `sdwebui`: POST {sdwebui_base_url}{txt2img_path} (A1111/SD.Next/Forge com
  --api) com JSON {prompt, negative_prompt, steps, width, height}; a resposta
  traz `images[0]` em base64 (png).
- `openai_compatible`: POST {openai_base_url}/images/generations com Bearer
  (openai_api_key, pode ser vazia), JSON {model, prompt, size, response_format:
  "b64_json"}; aceita `data[0].b64_json` ou `data[0].url` (neste caso baixa o
  arquivo da URL).

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
import logging
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


def _download(client: httpx.Client, url: str, headers: dict[str, str]) -> bytes:
    """Baixa uma imagem por URL (ramo data[0].url do openai_compatible)."""
    try:
        resp = client.get(url, headers=headers)
    except httpx.TransportError as exc:
        raise ImageUnreachable(f"image provider unreachable: {type(exc).__name__}") from exc
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
def _sd_payload(cfg: dict, prompt: str, width: int, height: int) -> dict:
    # Fixos (não-config): DPM++ 2M Karras + CFG 5 cobrem bem SD1.5 e SDXL
    # (Juggernaut/realistas pedem CFG 3-6); o default do servidor (Euler a,
    # CFG 7, salvo em ui-config) deixa de depender de estado da UI.
    return {
        "prompt": prompt,
        "negative_prompt": cfg["negative_prompt"],
        "steps": cfg["sdwebui_steps"],
        "width": width,
        "height": height,
        "sampler_name": "DPM++ 2M",
        "scheduler": "Karras",
        "cfg_scale": 5,
    }


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


def _generate_sdwebui(cfg: dict, client: httpx.Client, prompt: str,
                      width: int | None = None, height: int | None = None,
                      ) -> tuple[bytes, int, int]:
    width = int(width or cfg["sdwebui_width"])
    height = int(height or cfg["sdwebui_height"])
    data = _post_json(client, f"{_sd_base(cfg)}{cfg['txt2img_path']}",
                      _sd_payload(cfg, prompt, width, height),
                      {"Accept": "application/json"}, "txt2img")
    return _first_image(data, "txt2img"), width, height


def _generate_sdwebui_img2img(cfg: dict, client: httpx.Client, prompt: str,
                              init_image: bytes, width: int | None,
                              height: int | None,
                              denoising_strength: float) -> tuple[bytes, int, int]:
    """Pinta a partir de um PNG já salvo. denoising baixo mantém o mesmo rosto."""
    width = int(width or cfg["sdwebui_width"])
    height = int(height or cfg["sdwebui_height"])
    payload = _sd_payload(cfg, prompt, width, height)
    payload["init_images"] = [base64.b64encode(init_image).decode("ascii")]
    payload["denoising_strength"] = denoising_strength
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
        content = _download(client, str(first["url"]), headers)
    else:
        raise ImageError("resposta de images/generations sem b64_json nem url")
    width = height = None
    try:  # size "WxH" → dimensões (best-effort; None se formato inesperado)
        w, h = str(cfg["openai_size"]).lower().split("x", 1)
        width, height = int(w), int(h)
    except (ValueError, AttributeError):
        pass
    return content, width, height


# ------------------------------------------------------------------ público
def generate_image(cfg: dict, prompt: str, *, adventure_id: str, image_id: str,
                   transport: httpx.BaseTransport | None = None,
                   width: int | None = None, height: int | None = None,
                   init_image: bytes | None = None,
                   denoising_strength: float | None = None) -> dict:
    """Gera a imagem no provider efetivo, salva o PNG e devolve metadados
    {file_path (relativo), width, height, provider}. Levanta ImageError subclasses.

    init_image pede img2img (mesmo rosto). Sem endpoint img2img o pedido falha
    em vez de inventar outro personagem em txt2img.
    """
    provider = cfg.get("provider") or "sdwebui"
    timeout = float(cfg.get("timeout_seconds", 300))
    with _make_client(transport, timeout) as client:
        if init_image is not None:
            if provider != "sdwebui":
                raise ImageError(
                    "cannot keep the same face: this image provider has no img2img")
            content, width, height = _generate_sdwebui_img2img(
                cfg, client, prompt, init_image, width, height,
                float(denoising_strength if denoising_strength is not None else 0.35),
            )
        elif provider == "sdwebui":
            content, width, height = _generate_sdwebui(cfg, client, prompt, width, height)
        elif provider == "openai_compatible":
            content, width, height = _generate_openai(cfg, client, prompt)
        else:
            raise ImageError(f"config_error: provider de imagem desconhecido: {provider!r}")
    file_path = _save_png(cfg, adventure_id, image_id, content)
    return {"file_path": file_path, "width": width, "height": height,
            "provider": provider}
