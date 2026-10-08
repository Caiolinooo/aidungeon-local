"""Router de imagens (v2): /see, /images/{id} e /settings/image.

Fluxo do /see (contrato docs/openapi.yaml):
1) prompt_builder: o LLM resume a cena recente (até max_scene_chars, excluindo
   actions type=see) em um prompt de imagem EN;
2) prompt final = image_style da aventura + prompt resumido + style_suffix
   (+ extra_prompt do jogador, se houver);
3) chama o provider efetivo (image_client) e salva o PNG em storage_dir;
4) grava a linha em `images` + action type=see (mesmo seq vinculando);
5) devolve ImageResult.

Mesmo lock por aventura do game_engine → 409 generation_in_progress.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends
from fastapi.responses import FileResponse

from app import db, game_engine, image_client, llm_client, settings_store
from app.deps import get_http_transport
from app.errors import GameError
from app.schemas import ImageSettingsUpdate, SeeRequest

log = logging.getLogger("aidungeon.images")

router = APIRouter()

_PROMPT_BUILDER_SYSTEM = """You condense interactive fiction scenes into image generation prompts.
Given the recent scene, output a single concise English prompt for a text-to-image model:
visual subjects, setting, mood and composition only — no dialogue, no meta text, no quotes.
One or two sentences. Output ONLY the prompt."""


# ------------------------------------------------------------ settings/image
def _image_settings_response(conn) -> dict:
    """Shape ImageSettings do openapi (api_key SEMPRE mascarada)."""
    cfg = settings_store.effective_image_settings(conn)
    return {
        "enabled": cfg["enabled"],
        "provider": cfg["provider"],
        "sdwebui_base_url": cfg["sdwebui_base_url"],
        "sdwebui_steps": cfg["sdwebui_steps"],
        "sdwebui_width": cfg["sdwebui_width"],
        "sdwebui_height": cfg["sdwebui_height"],
        "negative_prompt": cfg["negative_prompt"],
        "openai_base_url": cfg["openai_base_url"],
        "openai_api_key": settings_store.mask_api_key(cfg["openai_api_key"]),
        "openai_model": cfg["openai_model"],
        "openai_size": cfg["openai_size"],
        "style_suffix": cfg["style_suffix"],
    }


@router.get("/settings/image")
def get_image_settings() -> dict:
    with db.session() as conn:
        return _image_settings_response(conn)


@router.put("/settings/image")
def put_image_settings(body: ImageSettingsUpdate) -> dict:
    update = body.model_dump(exclude_unset=True)
    with db.session() as conn:
        settings_store.apply_image_update(conn, update)
        return _image_settings_response(conn)


# -------------------------------------------------------------------- /see
def _build_final_prompt(adv: dict, actions: list[dict], body: SeeRequest,
                        img_cfg: dict, llm_cfg: dict, transport: Any) -> str:
    """LLM resume a cena recente → prompt EN; prefixa image_style, sufixa
    style_suffix e o extra_prompt do jogador."""
    pb = img_cfg["prompt_builder"]
    max_chars = int(pb.get("max_scene_chars", 1200))
    scene_actions = [a for a in actions
                     if a["type"] != "see" and a["seq"] <= body.action_seq]
    scene = "".join(a["text"] for a in scene_actions)[-max_chars:].strip()
    messages = [
        {"role": "system", "content": _PROMPT_BUILDER_SYSTEM},
        {"role": "user", "content": f"Recent scene:\n{scene or '(an empty scene)'}"},
    ]
    summarized = llm_client.chat_completion(
        llm_cfg, messages, temperature=0.4,
        max_tokens=int(pb.get("max_tokens", 120)), transport=transport)
    parts = [adv.get("image_style") or "", summarized,
             img_cfg["style_suffix"], body.extra_prompt.strip()]
    return ", ".join(p for p in parts if p)


@router.post("/adventures/{adventure_id}/see")
def post_see(adventure_id: str, body: SeeRequest,
             transport: Any = Depends(get_http_transport)) -> dict:
    with db.session() as conn:
        adv = db.get_adventure(conn, adventure_id)
        if not adv:
            raise GameError.not_found(f"adventure {adventure_id} not found")
        actions = db.list_actions(conn, adventure_id)
        img_cfg = settings_store.effective_image_settings(conn)
        llm_cfg = settings_store.effective_llm_settings(conn)

    if not game_engine.acquire_generation_lock(adventure_id):
        raise GameError.conflict("generation_in_progress",
                                 "a generation is already running for this adventure")
    try:
        if not img_cfg["enabled"]:
            raise GameError(503, "image_disabled",
                            "image generation is disabled (settings.image.enabled)")

        try:
            prompt = _build_final_prompt(adv, actions, body, img_cfg, llm_cfg,
                                         transport)
        except llm_client.LlmError as exc:
            raise GameError(503, exc.code, exc.message) from exc

        image_id = db.new_id()
        try:
            result = image_client.generate_image(
                img_cfg, prompt, adventure_id=adventure_id, image_id=image_id,
                transport=transport)
        except image_client.ImageError as exc:
            raise GameError(503, exc.code, exc.message) from exc

        with db.session() as conn:
            last_seq = db.max_seq(conn, adventure_id)
            seq = 0 if last_seq is None else last_seq + 1
            action = db.insert_action(conn, adventure_id, seq, "see", prompt)
            db.insert_image(conn, adventure_id=adventure_id, action_seq=seq,
                            prompt=prompt,
                            negative_prompt=img_cfg["negative_prompt"],
                            file_path=result["file_path"],
                            provider=result["provider"],
                            width=result["width"], height=result["height"],
                            image_id=image_id)
            db.touch_adventure(conn, adventure_id)
            action = db.get_action(conn, action["id"])

        log.info("see: imagem %s gerada para aventura %s (%s)",
                 image_id, adventure_id, result["provider"])
        return {
            "id": image_id,
            "url": f"/api/images/{image_id}",
            "prompt": prompt,
            "provider": result["provider"],
            "action": action,
        }
    finally:
        game_engine.release_generation_lock(adventure_id)


# ------------------------------------------------------------- arquivo PNG
@router.get("/images/{image_id}")
def get_image(image_id: str) -> FileResponse:
    with db.session() as conn:
        image = db.get_image(conn, image_id)
    if not image:
        raise GameError.not_found(f"image {image_id} not found")
    path = db.REPO_ROOT / image["file_path"]
    if not path.is_file():
        raise GameError.not_found(f"image file for {image_id} not found")
    return FileResponse(path, media_type="image/png")
