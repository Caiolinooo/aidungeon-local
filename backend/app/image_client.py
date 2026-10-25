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
import hashlib
import json
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
# FaceID Plus V2: rosto + CLIP visual para maior fidelidade à referência.
# O modelo e o InsightFace buffalo_l ficam na instalação do Forge Neo.
_REFERENCE_MODULE = "InsightFace+CLIP-H (IPAdapter)"
_REFERENCE_MODEL = "ip-adapter-faceid-plusv2_sdxl"
_REFERENCE_WEIGHT = 0.65
_MAX_REFERENCES = 6


def _reference_unit(image: bytes) -> dict:
    return {
        "enabled": True,
        "module": _REFERENCE_MODULE,
        "model": _REFERENCE_MODEL,
        "weight": _REFERENCE_WEIGHT,
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


def _attach_references(payload: dict, references: list[bytes] | None) -> dict:
    images = [raw for raw in (references or []) if raw][:_MAX_REFERENCES]
    if not images:
        return payload
    payload["alwayson_scripts"] = {
        "controlnet": {"args": [_reference_unit(raw) for raw in images]},
    }
    return payload


def _generate_sdwebui(cfg: dict, client: httpx.Client, prompt: str,
                      width: int | None = None, height: int | None = None,
                      references: list[bytes] | None = None,
                      ) -> tuple[bytes, int, int]:
    width = int(width or cfg["sdwebui_width"])
    height = int(height or cfg["sdwebui_height"])
    payload = _attach_references(
        _apply_checkpoint(cfg, _sd_payload(cfg, prompt, width, height)),
        references,
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
    payload = _attach_references(payload, references)
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
    reference_images trava a foto enviada via ControlNet, sem substituir a cena.
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
        if refs and provider != "sdwebui":
            log.warning("referência rígida ignorada: provider %s não tem ControlNet", provider)
            refs = []
        if init_image is not None:
            if provider != "sdwebui":
                raise ImageError(
                    "cannot keep the same face: this image provider has no img2img")
            content, width, height = _generate_sdwebui_img2img(
                cfg, client, prompt, init_image, width, height,
                float(denoising_strength if denoising_strength is not None
                      else cfg.get("denoising_strength", 0.35)),
                references=refs,
            )
        elif provider == "sdwebui":
            content, width, height = _generate_sdwebui(
                cfg, client, prompt, width, height, references=refs)
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
    """Pede interrupção cooperativa ao WebUI; não gera nem exige corpo JSON."""
    timeout = min(float(cfg.get("timeout_seconds", 300)), 10.0)
    with _make_client(transport, timeout) as client:
        try:
            resp = client.post(f"{_sd_base(cfg)}/sdapi/v1/interrupt", json={},
                               headers={"Accept": "application/json"})
        except httpx.TransportError as exc:
            raise ImageUnreachable(
                f"image provider unreachable: {type(exc).__name__}") from exc
        if resp.status_code in (401, 403):
            raise ImageAuthError(f"auth_error: HTTP {resp.status_code} em interrupt")
        if resp.status_code != 200:
            raise ImageError(f"HTTP {resp.status_code} em interrupt: {resp.text[:200]}")
