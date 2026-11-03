"""Resolução de configuração: config/settings.json (contrato) + overrides do banco.

Precedência para cada campo de `llm`:
    override da tabela `settings` (chaves `llm.*`)  >  config/settings.json

Para `api_key` especificamente:
    override do banco  >  env `LLM_API_KEY`  >  arquivo `.env` (raiz do repo)
    >  `llm.api_key` do settings.json

A `api_key` efetiva NUNCA é logada e só sai da API mascarada (`mask_api_key`).
"""

from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path
from typing import Any

from app.errors import GameError
from app import db

SETTINGS_PATH = db.REPO_ROOT / "config" / "settings.json"

_DEFAULT_COMFY_SIGMAS = (
    1.0, 0.978453, 0.95418, 0.926626, 0.89508,
    0.845148, 0.704534, 0.414568, 0.0,
)

OVERRIDE_KEYS = (
    "llm.base_url", "llm.api_key", "llm.model_alias", "llm.context_size",
    "llm.max_tokens", "llm.streaming", "llm.disable_thinking", "llm.timeout_seconds",
    # sampler (expostos em GET/PUT /api/settings/llm)
    "sampler.temperature", "sampler.top_p", "sampler.min_p",
    "sampler.repetition_penalty", "sampler.presence_penalty", "sampler.frequency_penalty",
    "sampler.dry_multiplier", "sampler.dry_base", "sampler.dry_allowed_length",
    "sampler.dry_penalty_last_n", "sampler.retry_temperature_step", "sampler.max_retry_temperature",
    # imagem (v2; storage_dir é interno — não sai no PUT)
    "image.enabled", "image.provider", "image.sdwebui_base_url",
    "image.sdwebui_steps", "image.sdwebui_width", "image.sdwebui_height",
    "image.comfyui_base_url", "image.comfyui_steps", "image.comfyui_width",
    "image.comfyui_height",
    "image.negative_prompt", "image.openai_base_url", "image.openai_api_key",
    "image.openai_model", "image.openai_size", "image.style_suffix",
    "image.storage_dir", "image.timeout_seconds", "image.seed", "image.variation",
    "image.cfg_scale", "image.sampler_name", "image.scheduler",
    "image.denoising_strength", "image.sd_model_checkpoint",
    # voz (v0.4.0; voices/max_text_chars/stt_max_bytes são internos — não saem no PUT)
    "voice.enabled", "voice.base_url", "voice.api_key", "voice.tts_model",
    "voice.stt_model", "voice.narrator_voice", "voice.timeout_seconds",
    "voice.max_text_chars", "voice.stt_max_bytes",
    "voice.drama3_model", "voice.drama3_enabled",
    # embeddings (v0.8.0 / M2)
    "embeddings.enabled", "embeddings.base_url", "embeddings.model",
    "embeddings.timeout_seconds",
    # runtime portátil (local / hybrid / colab). Segredos não entram aqui.
    "runtime.mode", "runtime.placements.llm", "runtime.placements.comfyui",
    "runtime.models.llm", "runtime.models.image", "runtime.models.video",
    "runtime.endpoints.local_llm", "runtime.endpoints.local_comfyui",
    "runtime.endpoints.colab_llm", "runtime.endpoints.colab_comfyui",
    "runtime.project_url",
    "runtime.registration.endpoints", "runtime.registration.hardware",
    "runtime.registration.last_seen", "runtime.registration.models",
    # identidade aplicada pelo runtime. O formulário grava steps/cfg dentro
    # de image.comfyui_flux2, image.comfyui_zimage e image.comfyui_video.
    "image.comfyui_engine", "image.comfyui_unet", "image.comfyui_clip",
    "image.comfyui_clip_type", "image.comfyui_clip_device", "image.comfyui_vae",
    "image.comfyui_flux2", "image.comfyui_zimage", "image.comfyui_video",
)

_RUNTIME_MODES = ("local", "hybrid", "colab")
_RUNTIME_PLACEMENTS = ("local", "colab")
_RUNTIME_MODEL_KINDS = ("llm", "image", "video")
_RUNTIME_ENDPOINT_KEYS = (
    "local_llm", "local_comfyui", "colab_llm", "colab_comfyui",
)
_FLUX2_IDENTITY = (
    "unet", "clip", "vae", "clip_type", "clip_device", "weight_dtype",
    "max_references", "upscale_method",
)
_FLUX2_SAMPLING = ("steps", "cfg", "sampler", "megapixels")
_FLUX2_MERGE = _FLUX2_IDENTITY + _FLUX2_SAMPLING
_ZIMAGE_IDENTITY = (
    "unet", "clip", "vae", "clip_type", "clip_device", "sampler", "scheduler",
)
_ZIMAGE_MERGE = _ZIMAGE_IDENTITY + ("steps", "cfg", "shift")
_ZIMAGE_FILE_DEFAULTS = {
    "unet": "z-image-turbo-Q4_K_M.gguf",
    "clip": "qwen_3_4b_fp8_mixed.safetensors",
    "clip_type": "lumina2",
    "clip_device": "cpu",
    "vae": "ae.safetensors",
    "steps": 8,
    "cfg": 1.0,
    "shift": 3.0,
    "sampler": "res_multistep",
    "scheduler": "simple",
}
_QWEN_IDENTITY = ("unet", "clip", "vae", "clip_type", "clip_device")
_VIDEO_FIELDS = (
    "engine", "checkpoint", "unet", "clip", "vae",
    "clip_type", "clip_device", "weight_dtype",
    "frames", "fps", "steps", "cfg", "shift", "timeout_seconds",
    "width", "height", "vertical_width", "vertical_height",
)
_VIDEO_API_FIELDS = (
    ("video_steps", "steps"),
    ("video_cfg", "cfg"),
    ("video_width", "width"),
    ("video_height", "height"),
    ("video_frames", "frames"),
    ("video_fps", "fps"),
)
_WAN_ENGINE_PRESET = {
    "engine": "wan",
    "checkpoint": "ltxv-2b-0.9.6-distilled-04-25.safetensors",
    "unet": "Wan2.2-TI2V-5B-Q8_0.gguf",
    "clip": "umt5_xxl_fp8_e4m3fn_scaled.safetensors",
    "vae": "wan2.2_vae.safetensors",
    "steps": 20,
    "cfg": 5,
    "width": 1280,
    "height": 704,
    "vertical_width": 704,
    "vertical_height": 1280,
}
_H3_ENGINE_PRESET = {
    "engine": "h3",
    "width": 768,
    "height": 448,
    "vertical_width": 448,
    "vertical_height": 768,
    "frames": 56,
    "steps": 8,
    "cfg": 1,
    "fps": 24,
}
_QWEN_OVERRIDE_KEYS = {
    "unet": "image.comfyui_unet",
    "clip": "image.comfyui_clip",
    "vae": "image.comfyui_vae",
    "clip_type": "image.comfyui_clip_type",
    "clip_device": "image.comfyui_clip_device",
}
_SECRET_KEYS = frozenset({
    "api_key", "apikey", "openai_api_key", "token", "access_token",
    "refresh_token", "id_token", "authorization", "password", "secret",
    "auth_key", "auth_token", "aidungeon_token", "tailscale_auth_key",
    "password_hash",
})

_file_cache: dict | None = None


# ------------------------------------------------------------ arquivo base
def load_file_settings(force: bool = False) -> dict:
    global _file_cache
    if _file_cache is None or force:
        _file_cache = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
    return _file_cache


def _dotenv_path() -> Path:
    return Path(os.environ.get("AIDUNGEON_ENV_PATH") or (db.REPO_ROOT / ".env"))


def _read_dotenv_key(name: str) -> str | None:
    """Parser mínimo de .env (KEY=VALUE), sem dependência externa."""
    path = _dotenv_path()
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            if key.strip() == name:
                return value.strip().strip('"').strip("'") or None
    except OSError:
        pass
    return None


def resolve_env_value(name: str) -> str | None:
    """Valor de `name` no ambiente ou no .env da raiz (nunca logar o valor)."""
    value = os.environ.get(name)
    if value:
        return value
    return _read_dotenv_key(name)


def resolve_env_api_key() -> str | None:
    """LLM_API_KEY do ambiente ou do .env da raiz (nunca logar o valor)."""
    env_name = load_file_settings().get("llm", {}).get("api_key_env", "LLM_API_KEY")
    return resolve_env_value(env_name)


# -------------------------------------------------------------- overrides
def _get_override(conn: sqlite3.Connection, key: str) -> Any:
    raw = db.get_setting(conn, key)
    if raw is None:
        return None
    try:
        return json.loads(raw)
    except ValueError:
        return raw


def _set_override(conn: sqlite3.Connection, key: str, value: Any) -> None:
    db.set_setting(conn, key, json.dumps(value, ensure_ascii=False))


def _api_key_or_env(conn: sqlite3.Connection, key: str, env_name: str) -> str | None:
    """Override ASCII válido; legados mascarados/não ASCII caem para a env."""
    value = _get_override(conn, key)
    if isinstance(value, str) and value and value.isascii() and value.isprintable():
        return value
    return resolve_env_value(env_name) or None


def _usable_secret(value: Any) -> str | None:
    if isinstance(value, str) and value and value.isascii() and value.isprintable():
        return value
    return None


def _apply_key(conn: sqlite3.Connection, key: str, value: str | None) -> bool:
    """Grava a chave ("" apaga). A chave vira header HTTP: a mascarada que o
    front reenvia ("sk-…1234") e qualquer valor fora de ASCII imprimível são
    ignorados. Devolve se mudou algo."""
    if value is None:
        return False
    if value == "":
        db.delete_setting(conn, key)
        return True
    if _usable_secret(value) is None:
        return False
    _set_override(conn, key, value)
    return True


def apply_llm_update(conn: sqlite3.Connection, update: dict) -> set[str]:
    """Persiste overrides (campos omitidos permanecem). Devolve chaves mudadas.

    `api_key=""` remove a chave (apaga o override); omitir mantém.
    """
    changed: set[str] = set()
    for field, key in (("base_url", "llm.base_url"), ("model_alias", "llm.model_alias"),
                       ("context_size", "llm.context_size"), ("max_tokens", "llm.max_tokens"),
                       ("streaming", "llm.streaming"),
                       ("disable_thinking", "llm.disable_thinking"),
                       ("timeout_seconds", "llm.timeout_seconds"),
                       # sampler: overrides gravados como sampler.* (não llm.*)
                       ("temperature", "sampler.temperature"),
                       ("top_p", "sampler.top_p"),
                       ("min_p", "sampler.min_p"),
                       ("repetition_penalty", "sampler.repetition_penalty"),
                       ("presence_penalty", "sampler.presence_penalty"),
                       ("frequency_penalty", "sampler.frequency_penalty"),
                       ("dry_multiplier", "sampler.dry_multiplier"),
                       ("dry_base", "sampler.dry_base"),
                       ("dry_allowed_length", "sampler.dry_allowed_length"),
                       ("dry_penalty_last_n", "sampler.dry_penalty_last_n"),
                       ("retry_temperature_step", "sampler.retry_temperature_step"),
                       ("max_retry_temperature", "sampler.max_retry_temperature")):
        if field in update and update[field] is not None:
            _set_override(conn, key, update[field])
            changed.add(field)
    if _apply_key(conn, "llm.api_key", update.get("api_key")):
        changed.add("api_key")
    return changed


# ----------------------------------------------------------- config efetiva
def _override_slot_value(value: Any) -> int | None:
    """Slot inteiro >= 0. Ausente ou ilegível não vira 0."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        slot = value
    elif isinstance(value, float) and value.is_integer():
        slot = int(value)
    elif isinstance(value, str):
        text = value.strip()
        if not text or not text.lstrip("-").isdigit():
            return None
        slot = int(text)
    else:
        return None
    if slot < 0:
        return None
    return slot


def _normalize_role_overrides(raw: Any) -> dict:
    """`scheduler.role_overrides` estável. Papel sem campo útil sai fora."""
    if not isinstance(raw, dict):
        return {}
    out: dict = {}
    for role, spec in raw.items():
        if not isinstance(role, str) or not role.strip() or not isinstance(spec, dict):
            continue
        item: dict = {}
        for key in ("base_url", "model_alias"):
            value = spec.get(key)
            if isinstance(value, str) and value.strip():
                item[key] = value.strip()
        secret_raw = spec.get("api_key")
        if isinstance(secret_raw, str):
            secret = _usable_secret(secret_raw.strip())
            if secret:
                item["api_key"] = secret
        if "slot" in spec:
            slot = _override_slot_value(spec.get("slot"))
            if slot is not None:
                item["slot"] = slot
        if item:
            out[role.strip()] = item
    return out


def effective_llm_settings(conn: sqlite3.Connection) -> dict:
    """Config efetiva do servidor LLM (arquivo + overrides + env/.env).

    `scheduler.role_overrides` vem só do arquivo (padrão {}). Sem a chave,
    base_url, modelo e slot dos papéis permanecem os do servidor principal.
    """
    cfg = load_file_settings()
    llm = cfg.get("llm", {})
    sampler = cfg.get("sampler", {})

    def ov(key: str, default: Any) -> Any:
        value = _get_override(conn, key)
        return default if value is None else value

    api_key = _api_key_or_env(conn, "llm.api_key", llm.get("api_key_env", "LLM_API_KEY"))
    if not api_key:
        api_key = llm.get("api_key") or None

    effective = {
        "provider": llm.get("provider", "llamacpp"),
        "base_url": ov("llm.base_url", llm.get("base_url", "")),
        "api_key": api_key or None,
        "model_alias": ov("llm.model_alias", llm.get("model_alias", "auto")),
        "context_size": int(ov("llm.context_size", llm.get("context_size", 8192))),
        "max_tokens": int(ov("llm.max_tokens", sampler.get("max_tokens", 300))),
        "streaming": bool(ov("llm.streaming", llm.get("streaming", True))),
        "disable_thinking": bool(ov("llm.disable_thinking", llm.get("disable_thinking", True))),
        "timeout_seconds": int(ov("llm.timeout_seconds", llm.get("timeout_seconds", 180))),
        # sampler efetivo (override sampler.* > settings.json sampler)
        "temperature": float(ov("sampler.temperature", sampler.get("temperature", 0.85))),
        "top_p": float(ov("sampler.top_p", sampler.get("top_p", 1.0))),
        "min_p": float(ov("sampler.min_p", sampler.get("min_p", 0.08))),
        "repetition_penalty": float(ov("sampler.repetition_penalty",
                                       sampler.get("repetition_penalty", 1.02))),
        "presence_penalty": float(ov("sampler.presence_penalty", sampler.get("presence_penalty", 0.15))),
        "frequency_penalty": float(ov("sampler.frequency_penalty", sampler.get("frequency_penalty", 0.10))),
        "dry_multiplier": float(ov("sampler.dry_multiplier", sampler.get("dry_multiplier", 0.8))),
        "dry_base": float(ov("sampler.dry_base", sampler.get("dry_base", 1.75))),
        "dry_allowed_length": int(ov("sampler.dry_allowed_length", sampler.get("dry_allowed_length", 2))),
        "dry_penalty_last_n": int(ov("sampler.dry_penalty_last_n", sampler.get("dry_penalty_last_n", -1))),
        "retry_temperature_step": float(ov("sampler.retry_temperature_step", sampler.get("retry_temperature_step", 0.05))),
        "max_retry_temperature": float(ov("sampler.max_retry_temperature", sampler.get("max_retry_temperature", 1.15))),
    }
    # Clamp on read: VM DB may store 262144, which exceeds per-slot n_ctx.
    # Never write the stored override back down.
    cap = int(llm.get("context_size_limits", {}).get("max", 106496))
    effective["context_size"] = min(effective["context_size"], cap)
    scheduler = cfg.get("scheduler", {}) or {}
    effective["role_timeouts"] = dict(scheduler.get("timeouts", {}) or {})
    effective["aux_cache_prompt"] = bool(scheduler.get("aux_cache_prompt", False))
    effective["scheduler"] = _scheduler_settings(scheduler)
    return effective


def _scheduler_settings(raw: dict) -> dict:
    """Bloco `scheduler` para o llm_client, com os caminhos absolutos
    (relativos à raiz do repo)."""
    out = {key: value for key, value in raw.items() if key not in ("timeouts", "note")}
    for key in ("stall_hint_path", "watchdog_status_path"):
        raw_path = str(raw.get(key) or "").strip()
        if raw_path:
            path = Path(raw_path)
            out[key] = str(path if path.is_absolute() else db.REPO_ROOT / path)
    out["role_overrides"] = _normalize_role_overrides(raw.get("role_overrides"))
    return out


def mask_api_key(key: str | None) -> str | None:
    """'sk-secret-1234' -> 'sk-…1234' (formato do exemplo do openapi.yaml)."""
    if not key:
        return None
    if len(key) <= 7:
        return "…" + key[-4:]
    return key[:3] + "…" + key[-4:]


# ------------------------------------------------------- config de imagem (v2)
def effective_image_settings(conn: sqlite3.Connection) -> dict:
    """Config efetiva de geração de imagens (arquivo + overrides + env/.env).

    As chaves públicas seguem o schema ImageSettings do openapi.yaml; as
    internas (storage_dir, timeout_seconds, txt2img_path, prompt_builder)
    são consumidas pelo image_client/rotas e não saem na API.

    Precedência da openai_api_key: override do banco > env/.env nomeada por
    image.providers.openai_compatible.api_key_env > None.
    """
    cfg = load_file_settings()
    image = cfg.get("image", {})
    sdwebui = image.get("providers", {}).get("sdwebui", {})
    comfyui = image.get("providers", {}).get("comfyui", {})
    openai = image.get("providers", {}).get("openai_compatible", {})
    prompt_builder = image.get("prompt_builder", {})

    def ov(key: str, default: Any) -> Any:
        value = _get_override(conn, key)
        return default if value is None else value

    api_key = _api_key_or_env(
        conn, "image.openai_api_key", openai.get("api_key_env", "IMAGE_API_KEY"))
    comfyui_steps = int(ov("image.comfyui_steps", comfyui.get("steps", 8)))
    comfyui_sigmas = [float(value) for value in comfyui.get(
        "sigmas", _DEFAULT_COMFY_SIGMAS)]
    if len(comfyui_sigmas) != comfyui_steps + 1:
        raise ValueError("comfyui.sigmas deve conter comfyui.steps + 1 valores")

    return {
        # ---- shape público (ImageSettings) ----
        "enabled": bool(ov("image.enabled", image.get("enabled", True))),
        "provider": ov("image.provider", image.get("provider", "sdwebui")),
        "sdwebui_base_url": ov("image.sdwebui_base_url", sdwebui.get("base_url", "")),
        "sdwebui_steps": int(ov("image.sdwebui_steps", sdwebui.get("steps", 20))),
        "sdwebui_width": int(ov("image.sdwebui_width", sdwebui.get("width", 512))),
        "sdwebui_height": int(ov("image.sdwebui_height", sdwebui.get("height", 512))),
        "comfyui_base_url": ov("image.comfyui_base_url", comfyui.get("base_url", "")),
        "comfyui_steps": comfyui_steps,
        "comfyui_width": int(ov("image.comfyui_width", comfyui.get("width", 1024))),
        "comfyui_height": int(ov("image.comfyui_height", comfyui.get("height", 1024))),
        "negative_prompt": ov("image.negative_prompt", sdwebui.get("negative_prompt", "")),
        "openai_base_url": ov("image.openai_base_url", openai.get("base_url", "")),
        "openai_api_key": api_key,
        "openai_model": ov("image.openai_model", openai.get("model", "")),
        "openai_size": ov("image.openai_size", openai.get("size", "512x512")),
        "style_suffix": ov("image.style_suffix", prompt_builder.get("style_suffix", "")),
        "seed": ov("image.seed", image.get("seed", sdwebui.get("seed"))),
        "variation": int(ov("image.variation", image.get("variation", 0))),
        "cfg_scale": float(ov("image.cfg_scale", sdwebui.get("cfg_scale", 5))),
        "sampler_name": ov("image.sampler_name", sdwebui.get("sampler_name", "DPM++ 2M")),
        "scheduler": ov("image.scheduler", sdwebui.get("scheduler", "Karras")),
        "denoising_strength": float(ov(
            "image.denoising_strength", sdwebui.get("denoising_strength", 0.35))),
        # ---- internos ----
        "storage_dir": ov("image.storage_dir", image.get("storage_dir", "data/images")),
        "timeout_seconds": int(ov("image.timeout_seconds",
                                  image.get("timeout_seconds", 300))),
        "txt2img_path": sdwebui.get("txt2img_path", "/sdapi/v1/txt2img"),
        "img2img_path": sdwebui.get("img2img_path", "/sdapi/v1/img2img"),
        "sd_model_checkpoint": ov(
            "image.sd_model_checkpoint", sdwebui.get("sd_model_checkpoint", "")),
        # Sampler do arquivo. Pesos/engine aceitam override interno do runtime.
        "comfyui_sampler": comfyui.get("sampler", "euler"),
        "comfyui_cfg": float(comfyui.get("cfg", 1.0)),
        "comfyui_sigmas": comfyui_sigmas,
        "comfyui_shift": float(comfyui.get("shift", 1.31)),
        "comfyui_poll_interval_s": float(comfyui.get("poll_interval_s", 0.25)),
        # Still opt-in. engine omisso é qwen e não entra no request_key.
        # flux2 não participa da validação len(sigmas) == steps + 1.
        # Overrides de runtime só trocam a identidade (engine e pesos).
        "comfyui_unet": ov("image.comfyui_unet", comfyui.get("unet", "")),
        "comfyui_clip": ov("image.comfyui_clip", comfyui.get("clip", "")),
        "comfyui_clip_type": ov("image.comfyui_clip_type",
                                comfyui.get("clip_type", "qwen_image")),
        "comfyui_clip_device": ov("image.comfyui_clip_device",
                                  comfyui.get("clip_device", "default")),
        "comfyui_vae": ov("image.comfyui_vae", comfyui.get("vae", "")),
        "comfyui_engine": _effective_engine(
            ov("image.comfyui_engine", None), comfyui.get("engine")),
        "comfyui_flux2": _merge_identity(
            dict(comfyui.get("flux2") or {}),
            _get_override(conn, "image.comfyui_flux2"),
            _FLUX2_MERGE),
        "comfyui_zimage": _merge_identity(
            _zimage_file_block(comfyui),
            _get_override(conn, "image.comfyui_zimage"),
            _ZIMAGE_MERGE),
        # Clipe: LTXV no arquivo; Wan é video.engine=wan. Não entra no
        # ImageSettings público e não substitui provider/unet da imagem.
        # O override aplica o preset completo do engine de vídeo (ex.: Wan
        # 20 steps / CFG 5 / 1280×704); auto_scene/auto_portraits ficam no arquivo.
        "comfyui_video": _merge_identity(
            dict(comfyui.get("video") or {}),
            _get_override(conn, "image.comfyui_video"),
            _VIDEO_FIELDS),
        "prompt_builder": dict(prompt_builder),
    }


def apply_image_update(conn: sqlite3.Connection, update: dict) -> set[str]:
    """Persiste overrides de imagem (campos omitidos permanecem).

    `openai_api_key=""` remove o override (cai para a env); omitir mantém.
    """
    changed: set[str] = set()
    if update.get("comfyui_steps") is not None:
        sigmas = load_file_settings().get("image", {}).get(
            "providers", {}).get("comfyui", {}).get("sigmas", _DEFAULT_COMFY_SIGMAS)
        if len(sigmas) != int(update["comfyui_steps"]) + 1:
            raise GameError.bad_request(
                "validation_error", "comfyui_steps deve ser igual a len(sigmas) - 1")
    for field, key in (("enabled", "image.enabled"), ("provider", "image.provider"),
                       ("sdwebui_base_url", "image.sdwebui_base_url"),
                       ("sdwebui_steps", "image.sdwebui_steps"),
                       ("sdwebui_width", "image.sdwebui_width"),
                       ("sdwebui_height", "image.sdwebui_height"),
                       ("comfyui_base_url", "image.comfyui_base_url"),
                       ("comfyui_steps", "image.comfyui_steps"),
                       ("comfyui_width", "image.comfyui_width"),
                       ("comfyui_height", "image.comfyui_height"),
                       ("negative_prompt", "image.negative_prompt"),
                       ("openai_base_url", "image.openai_base_url"),
                       ("openai_model", "image.openai_model"),
                       ("openai_size", "image.openai_size"),
                       ("style_suffix", "image.style_suffix"),
                       ("variation", "image.variation"),
                       ("cfg_scale", "image.cfg_scale"),
                       ("sampler_name", "image.sampler_name"),
                       ("scheduler", "image.scheduler"),
                       ("denoising_strength", "image.denoising_strength"),
                       ("timeout_seconds", "image.timeout_seconds"),
                       ("sd_model_checkpoint", "image.sd_model_checkpoint")):
        if field in update and update[field] is not None:
            _set_override(conn, key, update[field])
            changed.add(field)
    if "seed" in update:
        if update["seed"] is None:
            db.delete_setting(conn, "image.seed")
        else:
            _set_override(conn, "image.seed", update["seed"])
        changed.add("seed")
    if _apply_key(conn, "image.openai_api_key", update.get("openai_api_key")):
        changed.add("openai_api_key")
    if update.get("comfyui_engine"):
        _set_override(
            conn, "image.comfyui_engine",
            str(update["comfyui_engine"]).strip().lower())
        changed.add("comfyui_engine")
    if _apply_sampling_block(conn, "image.comfyui_flux2", update, (
            ("flux2_steps", "steps"), ("flux2_cfg", "cfg"),
            ("flux2_sampler", "sampler"), ("flux2_megapixels", "megapixels"))):
        changed.update(key for key in (
            "flux2_steps", "flux2_cfg", "flux2_sampler", "flux2_megapixels")
            if key in update and update[key] is not None)
    if _apply_sampling_block(conn, "image.comfyui_zimage", update, (
            ("zimage_steps", "steps"), ("zimage_cfg", "cfg"),
            ("zimage_shift", "shift"))):
        changed.update(key for key in ("zimage_steps", "zimage_cfg", "zimage_shift")
                       if key in update and update[key] is not None)
    if _apply_video_choice(conn, update):
        changed.update(key for key in (
            "video_engine", "video_steps", "video_cfg", "video_width",
            "video_height", "video_frames", "video_fps")
            if key in update and update[key] is not None)
    return changed


def _apply_sampling_block(conn: sqlite3.Connection, key: str, update: dict,
                          mapping: tuple[tuple[str, str], ...]) -> bool:
    """Grava steps/cfg no override do bloco sem apagar a identidade já salva."""
    if not any(src in update and update[src] is not None for src, _dest in mapping):
        return False
    current = _get_override(conn, key)
    merged = dict(current) if isinstance(current, dict) else {}
    for src, dest in mapping:
        if src in update and update[src] is not None:
            merged[dest] = update[src]
    _set_override(conn, key, merged)
    return True


def _file_video_block() -> dict:
    comfy = (load_file_settings().get("image", {}).get("providers", {})
             .get("comfyui", {}))
    raw = comfy.get("video") if isinstance(comfy, dict) else None
    return dict(raw) if isinstance(raw, dict) else {}


def _explicit_video_fields(update: dict) -> dict:
    picked: dict = {}
    for src, dest in _VIDEO_API_FIELDS:
        if src in update and update[src] is not None:
            picked[dest] = update[src]
    return picked


def _apply_video_choice(conn: sqlite3.Connection, update: dict) -> bool:
    """Troca o engine de vídeo e preenche o preset só nos campos omitidos.

    Leitura não regrava. Engine que já é `h3` não recebe de novo o quadro 56.
    """
    names = ("video_engine",) + tuple(src for src, _dest in _VIDEO_API_FIELDS)
    if not any(key in update and update[key] is not None for key in names):
        return False
    current = dict(effective_image_settings(conn)["comfyui_video"])
    explicit = _explicit_video_fields(update)
    current_engine = str(current.get("engine") or "ltxv").strip().lower() or "ltxv"
    requested = str(update.get("video_engine") or "").strip().lower()
    if requested and requested != current_engine:
        if requested == "wan":
            base = dict(current)
            base.update(_WAN_ENGINE_PRESET)
        elif requested == "h3":
            base = dict(current)
            for key, value in _H3_ENGINE_PRESET.items():
                if key not in explicit:
                    base[key] = value
        elif requested == "ltxv":
            base = _file_video_block()
            base["engine"] = "ltxv"
        else:
            base = dict(current)
        base.update(explicit)
        base["engine"] = requested
    else:
        base = current
        base.update(explicit)
        if requested:
            base["engine"] = requested
    _set_override(conn, "image.comfyui_video", _identity_from(base, _VIDEO_FIELDS))
    return True


# --------------------------------------------------------- config de voz (v4)
def effective_voice_settings(conn: sqlite3.Connection) -> dict:
    """Config efetiva de voz TTS/STT (arquivo + overrides + env/.env).

    As chaves públicas seguem o schema VoiceSettings do openapi.yaml; as
    internas (max_text_chars, stt_max_bytes) são consumidas pelas rotas e
    não saem no PUT (mas saem no GET, para a UI validar antes de enviar).

    Precedência da api_key: override do banco > env/.env nomeada por
    voice.api_key_env (default VOICE_API_KEY) > None.
    """
    cfg = load_file_settings()
    voice = cfg.get("voice", {})

    def ov(key: str, default: Any) -> Any:
        value = _get_override(conn, key)
        return default if value is None else value

    api_key = _api_key_or_env(conn, "voice.api_key", voice.get("api_key_env", "VOICE_API_KEY"))

    return {
        "enabled": bool(ov("voice.enabled", voice.get("enabled", True))),
        "base_url": ov("voice.base_url", voice.get("base_url", "")),
        "api_key": api_key,
        "tts_model": ov("voice.tts_model", voice.get("tts_model", "")),
        "stt_model": ov("voice.stt_model", voice.get("stt_model", "")),
        "narrator_voice": ov("voice.narrator_voice", voice.get("narrator_voice", "M1")),
        "voices": list(voice.get("voices", [])),
        "timeout_seconds": int(ov("voice.timeout_seconds",
                                  voice.get("timeout_seconds", 60))),
        "max_text_chars": int(ov("voice.max_text_chars",
                                 voice.get("max_text_chars", 4096))),
        "stt_max_bytes": int(ov("voice.stt_max_bytes",
                                voice.get("stt_max_bytes", 10485760))),
        "drama3_model": ov("voice.drama3_model", voice.get("drama3_model", "drama-3")),
        "drama3_enabled": bool(ov("voice.drama3_enabled",
                                  voice.get("drama3_enabled", False))),
    }


def apply_voice_update(conn: sqlite3.Connection, update: dict) -> set[str]:
    """Persiste overrides de voz (campos omitidos permanecem).

    `api_key=""` remove o override (cai para a env); omitir mantém.
    """
    changed: set[str] = set()
    for field, key in (("enabled", "voice.enabled"), ("base_url", "voice.base_url"),
                       ("tts_model", "voice.tts_model"), ("stt_model", "voice.stt_model"),
                       ("narrator_voice", "voice.narrator_voice"),
                       ("timeout_seconds", "voice.timeout_seconds")):
        if field in update and update[field] is not None:
            _set_override(conn, key, update[field])
            changed.add(field)
    if _apply_key(conn, "voice.api_key", update.get("api_key")):
        changed.add("api_key")
    return changed


# ---------------------------------------------------------------- embeddings
def effective_embeddings_settings(conn: sqlite3.Connection | None = None) -> dict:
    """Config efetiva do cliente de embeddings (arquivo + overrides SQLite)."""
    cfg = load_file_settings()
    emb = cfg.get("embeddings", {})

    def ov(key: str, default: Any) -> Any:
        if conn is None:
            return default
        value = _get_override(conn, key)
        return default if value is None else value

    return {
        "enabled": bool(ov("embeddings.enabled", emb.get("enabled", True))),
        "base_url": str(ov("embeddings.base_url", emb.get("base_url", "http://127.0.0.1:8091/v1"))),
        "model": str(ov("embeddings.model", emb.get("model", "Qwen3-Embedding-0.6B"))),
        "timeout_seconds": int(ov("embeddings.timeout_seconds", emb.get("timeout_seconds", 30))),
    }


def apply_embeddings_update(conn: sqlite3.Connection, update: dict) -> set[str]:
    """Persiste overrides de embeddings."""
    changed: set[str] = set()
    for field, key in (
        ("enabled", "embeddings.enabled"),
        ("base_url", "embeddings.base_url"),
        ("model", "embeddings.model"),
        ("timeout_seconds", "embeddings.timeout_seconds"),
    ):
        if field in update and update[field] is not None:
            _set_override(conn, key, update[field])
            changed.add(field)
    return changed


def _effective_engine(override: Any, file_value: Any) -> str:
    """Engine do arquivo, trocado só quando o runtime gravou um override."""
    raw = file_value if override is None else override
    return str(raw or "qwen").strip().lower() or "qwen"


def _zimage_file_block(comfyui: dict) -> dict:
    """Defaults do Z-Image mais o bloco do arquivo. O engine ativo não muda."""
    base = dict(_ZIMAGE_FILE_DEFAULTS)
    raw = comfyui.get("zimage") if isinstance(comfyui, dict) else None
    if isinstance(raw, dict):
        for key, value in raw.items():
            if key in _ZIMAGE_MERGE and value is not None:
                base[key] = value
    return base


def _merge_identity(base: dict, overlay: Any, allowed: tuple[str, ...]) -> dict:
    """Completa o bloco do arquivo com as chaves permitidas do override."""
    merged = dict(base)
    if not isinstance(overlay, dict):
        return merged
    for key in allowed:
        if key not in overlay or overlay[key] is None:
            continue
        merged[key] = overlay[key]
    return merged


def _identity_from(source: Any, allowed: tuple[str, ...]) -> dict:
    if not isinstance(source, dict):
        return {}
    picked: dict = {}
    for key in allowed:
        if key not in source or source[key] is None:
            continue
        value = source[key]
        if isinstance(value, str):
            value = value.strip()
            if not value:
                continue
            if key == "engine":
                value = value.lower()
        picked[key] = value
    return picked


def _first_dict(entry: dict, keys: tuple[str, ...]) -> dict:
    """Pesos do catálogo: bloco nomeado ou o próprio registro."""
    for key in keys:
        nested = entry.get(key)
        if isinstance(nested, dict):
            return nested
    return entry


def _provisioning():
    from app import runtime_provisioning
    return runtime_provisioning


def _invalidate_llm_discovery() -> None:
    from app import llm_client
    llm_client.invalidate_discovery_cache()


def _runtime_file() -> dict:
    raw = load_file_settings().get("runtime")
    return raw if isinstance(raw, dict) else {}


def _runtime_defaults() -> dict:
    file_rt = _runtime_file()
    placements = file_rt.get("placements") if isinstance(file_rt.get("placements"), dict) else {}
    models = file_rt.get("models") if isinstance(file_rt.get("models"), dict) else {}
    endpoints = file_rt.get("endpoints") if isinstance(file_rt.get("endpoints"), dict) else {}
    mode = file_rt.get("mode") if file_rt.get("mode") in _RUNTIME_MODES else "local"

    def place(name: str) -> str:
        value = placements.get(name)
        return value if value in _RUNTIME_PLACEMENTS else "local"

    return {
        "mode": mode,
        "placements": {"llm": place("llm"), "comfyui": place("comfyui")},
        "models": {kind: str(models.get(kind) or "") for kind in _RUNTIME_MODEL_KINDS},
        "endpoints": {key: str(endpoints.get(key) or "") for key in _RUNTIME_ENDPOINT_KEYS},
        "project_url": str(file_rt.get("project_url") or ""),
    }


def _active_placements(config: dict) -> dict[str, str]:
    """local e colab mandam nos dois serviços. hybrid usa a escolha de cada um."""
    mode = config.get("mode")
    placements = config.get("placements") if isinstance(config.get("placements"), dict) else {}
    if mode == "local":
        return {"llm": "local", "comfyui": "local"}
    if mode == "colab":
        return {"llm": "colab", "comfyui": "colab"}
    llm = placements.get("llm")
    comfy = placements.get("comfyui")
    if (llm not in _RUNTIME_PLACEMENTS or comfy not in _RUNTIME_PLACEMENTS
            or llm == comfy):
        raise GameError.bad_request(
            "validation_error",
            "hybrid requires one local placement and one colab placement")
    return {"llm": llm, "comfyui": comfy}


def _public_runtime(raw: dict, fallback: dict) -> dict:
    """Só o contrato público. Descarta nota, hash, URL de download e segredo."""
    mode = raw.get("mode", fallback["mode"])
    if mode not in _RUNTIME_MODES:
        raise GameError.bad_request("validation_error", "mode must be local, hybrid, or colab")
    raw_placements = raw.get("placements") if isinstance(raw.get("placements"), dict) else {}
    raw_models = raw.get("models") if isinstance(raw.get("models"), dict) else {}
    raw_endpoints = raw.get("endpoints") if isinstance(raw.get("endpoints"), dict) else {}
    placements = {
        "llm": raw_placements.get("llm", fallback["placements"]["llm"]),
        "comfyui": raw_placements.get("comfyui", fallback["placements"]["comfyui"]),
    }
    models = {
        kind: str(raw_models.get(kind) or fallback["models"][kind] or "")
        for kind in _RUNTIME_MODEL_KINDS
    }
    endpoints = {}
    for key in _RUNTIME_ENDPOINT_KEYS:
        value = raw_endpoints.get(key, fallback["endpoints"][key])
        endpoints[key] = "" if value is None else str(value).strip()
    project_url = raw.get("project_url", fallback["project_url"])
    config = {
        "mode": mode,
        "placements": placements,
        "models": models,
        "endpoints": endpoints,
        "project_url": "" if project_url is None else str(project_url).strip(),
    }
    config["placements"] = _active_placements(config)
    return config


def effective_runtime_settings(conn: sqlite3.Connection) -> dict:
    """Runtime efetivo: `config/settings.json` + overrides `runtime.*`."""
    base = _runtime_defaults()

    def leaf(key: str, default: Any) -> Any:
        value = _get_override(conn, key)
        return default if value is None else value

    config = {
        "mode": leaf("runtime.mode", base["mode"]),
        "placements": {
            "llm": leaf("runtime.placements.llm", base["placements"]["llm"]),
            "comfyui": leaf("runtime.placements.comfyui", base["placements"]["comfyui"]),
        },
        "models": {
            kind: str(leaf(f"runtime.models.{kind}", base["models"][kind]) or "")
            for kind in _RUNTIME_MODEL_KINDS
        },
        "endpoints": {
            key: "" if (value := leaf(f"runtime.endpoints.{key}", base["endpoints"][key])) is None
            else str(value)
            for key in _RUNTIME_ENDPOINT_KEYS
        },
        "project_url": str(leaf("runtime.project_url", base["project_url"]) or ""),
    }
    if config["mode"] not in _RUNTIME_MODES:
        config["mode"] = base["mode"]
    for name in ("llm", "comfyui"):
        if config["placements"][name] not in _RUNTIME_PLACEMENTS:
            config["placements"][name] = base["placements"][name]
    return config


def runtime_registration(conn: sqlite3.Connection) -> dict:
    """Estado da última inscrição do Colab. Ausente vira null, sem segredo."""
    endpoints = _get_override(conn, "runtime.registration.endpoints")
    hardware = _get_override(conn, "runtime.registration.hardware")
    last_seen = _get_override(conn, "runtime.registration.last_seen")
    models = _get_override(conn, "runtime.registration.models")
    return {
        "endpoints": endpoints if isinstance(endpoints, dict) else None,
        "hardware": hardware if isinstance(hardware, dict) else None,
        "last_seen": last_seen if isinstance(last_seen, str) and last_seen else None,
        "models": models if isinstance(models, dict) else None,
    }


def _persist_runtime(conn: sqlite3.Connection, config: dict) -> None:
    _set_override(conn, "runtime.mode", config["mode"])
    _set_override(conn, "runtime.placements.llm", config["placements"]["llm"])
    _set_override(conn, "runtime.placements.comfyui", config["placements"]["comfyui"])
    for kind in _RUNTIME_MODEL_KINDS:
        _set_override(conn, f"runtime.models.{kind}", config["models"][kind])
    for key in _RUNTIME_ENDPOINT_KEYS:
        _set_override(conn, f"runtime.endpoints.{key}", config["endpoints"][key])
    _set_override(conn, "runtime.project_url", config["project_url"])


def _endpoint_value(endpoints: dict, key: str) -> str:
    value = endpoints.get(key, "")
    return "" if value is None else str(value).strip()


def _apply_routing(conn: sqlite3.Connection, config: dict) -> None:
    """A URL escolhida entra no llm e no Comfy mesmo quando vem vazia.

    Vazio não cai de volta no outro lado: o health do serviço selecionado fecha.
    """
    placements = _active_placements(config)
    endpoints = config["endpoints"]
    llm_key = "local_llm" if placements["llm"] == "local" else "colab_llm"
    comfy_key = "local_comfyui" if placements["comfyui"] == "local" else "colab_comfyui"
    _set_override(conn, "llm.base_url", _endpoint_value(endpoints, llm_key))
    _set_override(conn, "image.comfyui_base_url", _endpoint_value(endpoints, comfy_key))


def _catalog_models(catalog: dict, kind: str) -> dict:
    models = catalog.get("models") if isinstance(catalog, dict) else None
    bucket = models.get(kind) if isinstance(models, dict) else None
    return bucket if isinstance(bucket, dict) else {}


def _require_catalog_entry(catalog: dict, kind: str, model_id: str) -> dict:
    entry = _catalog_models(catalog, kind).get(model_id)
    if not isinstance(entry, dict):
        raise GameError.bad_request(
            "validation_error", f"unknown {kind} model: {model_id}")
    return entry


def _apply_selected_models(conn: sqlite3.Connection, config: dict, catalog: dict) -> None:
    """Aponta alias, engine e pesos; para vídeo aplica também o preset do engine."""
    llm_entry = _require_catalog_entry(catalog, "llm", config["models"]["llm"])
    image_entry = _require_catalog_entry(catalog, "image", config["models"]["image"])
    video_entry = _require_catalog_entry(catalog, "video", config["models"]["video"])
    placements = _active_placements(config)
    if placements["llm"] == "local" and str(llm_entry.get("role") or "") == "fallback":
        raise GameError.bad_request(
            "validation_error",
            "fallback llm model requires colab placement or dedicated endpoint")
    alias = str(llm_entry.get("model_alias") or "").strip()
    if not alias:
        raise GameError.bad_request(
            "validation_error", "catalog llm entry is missing model_alias")
    _set_override(conn, "llm.model_alias", alias)

    engine = str(image_entry.get("engine") or "").strip().lower()
    if not engine:
        raise GameError.bad_request(
            "validation_error", "catalog image entry is missing engine")
    _set_override(conn, "image.comfyui_engine", engine)
    image_src = _first_dict(image_entry, ("flux2", "quality"))
    if engine == "qwen":
        db.delete_setting(conn, "image.comfyui_flux2")
        db.delete_setting(conn, "image.comfyui_zimage")
        identity = _identity_from(image_src, _QWEN_IDENTITY)
        for field, key in _QWEN_OVERRIDE_KEYS.items():
            if field in identity:
                _set_override(conn, key, identity[field])
            else:
                db.delete_setting(conn, key)
    elif engine == "z_image_turbo":
        for key in _QWEN_OVERRIDE_KEYS.values():
            db.delete_setting(conn, key)
        db.delete_setting(conn, "image.comfyui_flux2")
        z_src = _first_dict(image_entry, ("zimage", "quality"))
        overlay = _identity_from(z_src, _ZIMAGE_IDENTITY)
        if overlay:
            _set_override(conn, "image.comfyui_zimage", overlay)
        else:
            db.delete_setting(conn, "image.comfyui_zimage")
    else:
        for key in _QWEN_OVERRIDE_KEYS.values():
            db.delete_setting(conn, key)
        db.delete_setting(conn, "image.comfyui_zimage")
        overlay = _identity_from(image_src, _FLUX2_IDENTITY)
        if overlay:
            _set_override(conn, "image.comfyui_flux2", overlay)
        else:
            db.delete_setting(conn, "image.comfyui_flux2")

    video_src = _first_dict(video_entry, ("video", "config"))
    video_overlay = _identity_from(video_src, _VIDEO_FIELDS)
    video_engine = str(
        video_entry.get("engine") or video_overlay.get("engine") or "").strip().lower()
    if not video_engine:
        raise GameError.bad_request(
            "validation_error", "catalog video entry is missing engine")
    video_overlay["engine"] = video_engine
    _set_override(conn, "image.comfyui_video", video_overlay)


def _merge_runtime_update(current: dict, update: dict) -> dict:
    merged = {
        "mode": current["mode"],
        "placements": dict(current["placements"]),
        "models": dict(current["models"]),
        "endpoints": dict(current["endpoints"]),
        "project_url": current["project_url"],
    }
    if update.get("mode"):
        merged["mode"] = update["mode"]
    for section in ("placements", "models", "endpoints"):
        patch = update.get(section)
        if not isinstance(patch, dict):
            continue
        for key, value in patch.items():
            if value is None or key not in merged[section]:
                continue
            merged[section][key] = value
    if "project_url" in update and update["project_url"] is not None:
        merged["project_url"] = update["project_url"]
    merged["placements"] = _active_placements(merged)
    return merged


def apply_runtime_update(conn: sqlite3.Connection, update: dict) -> dict:
    """Valida, grava `runtime.*` e torna endpoint e modelo efetivos na hora.

    Endpoint Colab em branco pode ficar salvo com o modo local (rascunho).
    Quando o modo ou o placement escolhe Colab, a URL efetiva é essa string,
    inclusive vazia — não permanece a URL local.
    """
    current = effective_runtime_settings(conn)
    merged = _merge_runtime_update(current, update or {})
    prov = _provisioning()
    catalog = strip_secrets(prov.load_catalog())
    try:
        normalized = prov.validate_runtime(merged, catalog, require_colab=False)
    except ValueError as exc:
        raise GameError.bad_request("validation_error", str(exc)) from exc
    if not isinstance(normalized, dict):
        raise GameError.bad_request("validation_error", "runtime config was rejected")
    config = _public_runtime(normalized, merged)
    _persist_runtime(conn, config)
    _apply_routing(conn, config)
    _apply_selected_models(conn, config, catalog)
    _invalidate_llm_discovery()
    return effective_runtime_settings(conn)


def apply_runtime_registration(conn: sqlite3.Connection, report: dict) -> dict:
    """Confere os modelos, grava o Colab e aplica a URL só no placement Colab."""
    current = effective_runtime_settings(conn)
    reported = report.get("models") if isinstance(report.get("models"), dict) else {}
    selected = current["models"]
    if any(str(reported.get(kind) or "") != str(selected.get(kind) or "")
           for kind in _RUNTIME_MODEL_KINDS):
        raise GameError.conflict(
            "model_mismatch",
            "reported models do not match the selected runtime models")
    incoming = report.get("endpoints") if isinstance(report.get("endpoints"), dict) else {}
    colab_llm = "" if incoming.get("llm") is None else str(incoming.get("llm")).strip()
    colab_comfy = "" if incoming.get("comfyui") is None else str(incoming.get("comfyui")).strip()
    hardware = scrub_public(report.get("hardware") if isinstance(report.get("hardware"), dict) else {}, conn)
    if not isinstance(hardware, dict):
        hardware = {}
    saved_models = {kind: selected[kind] for kind in _RUNTIME_MODEL_KINDS}
    placements = _active_placements(current)
    if placements["llm"] == "colab" and not colab_llm:
        raise GameError.bad_request(
            "validation_error", "colab llm endpoint is required when llm placement is colab")
    if placements["comfyui"] == "colab" and not colab_comfy:
        raise GameError.bad_request(
            "validation_error", "colab comfyui endpoint is required when comfyui placement is colab")
    _set_override(conn, "runtime.registration.endpoints",
                  {"llm": colab_llm, "comfyui": colab_comfy})
    _set_override(conn, "runtime.registration.hardware", hardware)
    _set_override(conn, "runtime.registration.last_seen", db.utcnow())
    _set_override(conn, "runtime.registration.models", saved_models)
    if colab_llm:
        _set_override(conn, "runtime.endpoints.colab_llm", colab_llm)
    if colab_comfy:
        _set_override(conn, "runtime.endpoints.colab_comfyui", colab_comfy)
    if placements["llm"] == "colab":
        _set_override(conn, "llm.base_url", colab_llm)
    if placements["comfyui"] == "colab":
        _set_override(conn, "image.comfyui_base_url", colab_comfy)
    _invalidate_llm_discovery()
    return effective_runtime_settings(conn)


def strip_secrets(value: Any) -> Any:
    """Tira chaves de credencial. Não mexe em nome de modelo nem em URL."""
    if isinstance(value, dict):
        return {
            str(key): strip_secrets(item)
            for key, item in value.items()
            if str(key).strip().lower() not in _SECRET_KEYS
        }
    if isinstance(value, list):
        return [strip_secrets(item) for item in value]
    return value


def secret_values(conn: sqlite3.Connection) -> set[str]:
    """Segredos já configurados. Não entra valor curto, para não apagar texto comum."""
    found: set[str] = set()
    llm_key = effective_llm_settings(conn).get("api_key")
    image_key = effective_image_settings(conn).get("openai_api_key")
    voice_key = effective_voice_settings(conn).get("api_key")
    for value in (llm_key, image_key, voice_key):
        if isinstance(value, str) and len(value) >= 8:
            found.add(value)
    for name in ("AUTH_SECRET", "LLM_API_KEY", "IMAGE_API_KEY", "VOICE_API_KEY",
                 "AUTH_PASSWORD_HASH"):
        env_value = os.environ.get(name)
        if isinstance(env_value, str) and len(env_value) >= 8:
            found.add(env_value)
    return found


def scrub_public(value: Any, conn: sqlite3.Connection | None = None) -> Any:
    """Resposta e notebook saem sem chave de credencial e sem o valor dela."""
    cleaned = strip_secrets(value)
    secrets = secret_values(conn) if conn is not None else set()
    return _redact_values(cleaned, secrets)


def scrub_bytes(payload: bytes, conn: sqlite3.Connection) -> bytes:
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError:
        return payload
    for secret in secret_values(conn):
        text = text.replace(secret, "…")
    return text.encode("utf-8")


def _redact_values(value: Any, secrets: set[str]) -> Any:
    if isinstance(value, str):
        redacted = value
        for secret in secrets:
            if secret and secret in redacted:
                redacted = redacted.replace(secret, "…")
        return redacted
    if isinstance(value, dict):
        return {key: _redact_values(item, secrets) for key, item in value.items()}
    if isinstance(value, list):
        return [_redact_values(item, secrets) for item in value]
    return value


# ---------------------------------------------------- config p/ game engine
def runtime_config(conn: sqlite3.Connection) -> dict:
    """Tudo que o context_builder/game_engine precisam (budgets, sampler, stops)."""
    cfg = load_file_settings()
    llm = effective_llm_settings(conn)
    sampler = dict(cfg.get("sampler", {}))
    sampler["max_tokens"] = llm["max_tokens"]
    sampler["temperature"] = llm["temperature"]
    sampler["top_p"] = llm["top_p"]
    sampler["min_p"] = llm["min_p"]
    sampler["repetition_penalty"] = llm["repetition_penalty"]
    sampler["presence_penalty"] = llm["presence_penalty"]
    sampler["frequency_penalty"] = llm["frequency_penalty"]
    sampler["dry_multiplier"] = llm["dry_multiplier"]
    sampler["dry_base"] = llm["dry_base"]
    sampler["dry_allowed_length"] = llm["dry_allowed_length"]
    sampler["dry_penalty_last_n"] = llm["dry_penalty_last_n"]
    sampler["retry_temperature_step"] = llm["retry_temperature_step"]
    sampler["max_retry_temperature"] = llm["max_retry_temperature"]
    return {
        "context_size": llm["context_size"],
        "budgets": dict(cfg.get("context", {}).get("budgets", {})),
        "dynamic_scaling": dict(cfg.get("context", {}).get("dynamic_scaling", {})),
        "stop_sequences": list(cfg.get("stop_sequences", ["\n> You", "\n> you"])),
        "sampler": sampler,
        "memory_bank": dict(cfg.get("memory_bank", {})),
        "embeddings": effective_embeddings_settings(conn),
        "story_cards": dict(cfg.get("story_cards", {})),
        "llm": llm,
    }


def get_stop_sequences() -> list[str]:
    return list(load_file_settings().get("stop_sequences", ["\n> You", "\n> you"]))
