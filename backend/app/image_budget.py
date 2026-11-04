"""Orçamento de um preset de imagem ou vídeo contra o buraco da L4.

O narrador fica carregado. `spill_mib` é o número de planejamento da UI:
quanto do preset passa de `HOLE_MIB`. A VRAM livre de verdade continua
em `routes.health._memory` e só entra em `fits_vram`.
"""

from __future__ import annotations

import json

from app import db

# 23034 - 14472 - 1426
HOLE_MIB = 7136

_QWEN_UNET_BYTES = 7116801326
_QWEN_VAE_BYTES = 675509688
_QWEN_CLIP_BYTES = 9350798360
_ZIMAGE_UNET_BYTES = 3850130496
_ZIMAGE_VAE_BYTES = 335304388
_ZIMAGE_CLIP_BYTES = 5631994051
_H3_UNET_BYTES = 8063029344
_H3_VAE_BYTES = 2811065184
_H3_CLIP_BYTES = 4829126213
_H3_PROJECTION_BYTES = 503423800

_catalog_cache: dict | None = None


def h3_length(frames: int) -> int:
    """Sobe para a grade 17k+5 do H3. 121 vira 124. 56 fica 56. 48 vira 56."""
    count = max(5, int(frames))
    return count + (5 - count % 17) % 17


def _mib(nbytes: int) -> int:
    return (int(nbytes) + 1048575) // 1048576


def _catalog() -> dict:
    global _catalog_cache
    if _catalog_cache is None:
        path = db.REPO_ROOT / "config" / "runtime_models.json"
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            loaded = {}
        _catalog_cache = loaded if isinstance(loaded, dict) else {}
    return _catalog_cache


def _artifact_bytes(kind: str, engine: str, role: str) -> int | None:
    models = _catalog().get("models")
    bucket = models.get(kind) if isinstance(models, dict) else None
    entry = bucket.get(engine) if isinstance(bucket, dict) else None
    artifacts = entry.get("artifacts") if isinstance(entry, dict) else None
    if not isinstance(artifacts, list):
        return None
    for item in artifacts:
        if not isinstance(item, dict) or item.get("role") != role:
            continue
        value = item.get("bytes")
        if isinstance(value, bool) or not isinstance(value, int):
            return None
        return value
    return None


def _known_mib(kind: str, engine: str, roles: tuple[str, ...]) -> tuple[int, bool]:
    """Soma os bytes só quando cada arquivo do papel tem tamanho. Um null zera."""
    total = 0
    for role in roles:
        value = _artifact_bytes(kind, engine, role)
        if value is None:
            return 0, False
        total += value
    return _mib(total), True


def preset_budget(kind: str, engine: str, *, width: int, height: int,
                  frames: int = 1) -> dict:
    """MiB de peso, ativação, VRAM, RAM e spill. Engine desconhecido é ValueError.

    Passos e CFG não entram na conta. `weights_known` é false quando o
    arquivo de peso no catálogo está com `bytes` null.
    """
    kind_name = str(kind or "").strip().lower()
    engine_name = str(engine or "").strip().lower()
    width = int(width)
    height = int(height)
    frames = int(frames)
    if kind_name == "image" and engine_name == "qwen":
        weights = _mib(_QWEN_UNET_BYTES + _QWEN_VAE_BYTES)
        ram = _mib(_QWEN_CLIP_BYTES)
        activation = round(2048 * width * height / (1024 * 1024))
        known = True
    elif kind_name == "image" and engine_name == "flux2_klein":
        weights, known = _known_mib("image", "flux2_klein", ("unet",))
        ram = 0
        activation = round(1536 * width * height / (1344 * 768))
    elif kind_name == "image" and engine_name == "z_image_turbo":
        weights = _mib(_ZIMAGE_UNET_BYTES + _ZIMAGE_VAE_BYTES)
        ram = _mib(_ZIMAGE_CLIP_BYTES)
        activation = round(2048 * width * height / (1024 * 1024))
        known = True
    elif kind_name == "video" and engine_name == "ltxv":
        weights, known = _known_mib("video", "ltxv", ("checkpoint",))
        ram = 0
        activation = round(
            2048 * width * height / (768 * 448) * max(frames, 1) / 121)
    elif kind_name == "video" and engine_name == "wan":
        weights, known = _known_mib("video", "wan", ("unet", "clip", "vae"))
        ram = 0
        activation = round(
            2048 * width * height / (1280 * 704) * max(frames, 1) / 121)
    elif kind_name == "video" and engine_name == "h3":
        weights, known = _known_mib(
            "video", "h3", ("unet", "vae", "projection"))
        if not known:
            weights = _mib(
                _H3_UNET_BYTES + _H3_VAE_BYTES + _H3_PROJECTION_BYTES)
        clip_bytes = _artifact_bytes("video", "h3", "clip")
        ram = _mib(clip_bytes if clip_bytes is not None else _H3_CLIP_BYTES)
        activation = round(
            3072 * width * height / (768 * 448) * h3_length(frames) / 56)
    else:
        raise ValueError(f"engine desconhecido: {kind_name}:{engine_name}")
    vram = weights + activation
    return {
        "weights_vram_mib": weights,
        "activation_mib": activation,
        "vram_mib": vram,
        "ram_mib": ram,
        "weights_known": known,
        "spill_mib": 0 if vram <= HOLE_MIB else vram - HOLE_MIB,
    }
