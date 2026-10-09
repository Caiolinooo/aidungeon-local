"""Pipeline assíncrono de assets (v0.3.0) — worker asyncio sobre a tabela jobs.

Fluxo: rotas enfileiram (db.insert_job); o worker (único por processo) faz
claim do mais antigo e executa num thread (SQLite é síncrono). Hoje existe um
tipos:
- `character_portrait` — LLM expande a descrição do NPC em prompt EN de
  retrato (se o card não tiver portrait_prompt), gera a imagem no provider
  efetivo (mesmo do "See") e vincula ao card (images kind='portrait').
  A primeira foto vira a âncora (`portrait_anchor_id`). Geração automática
  não substitui um retrato que já existe. Pedido explícito de regenerar
  usa img2img a partir dessa âncora, para o rosto continuar o mesmo.
- `scene_backdrop` — pinta o fundo do lugar atual (payload.prompt já pronto,
  sem LLM) em images kind='backdrop'. Wide 896×512.

O worker é desligável por env (AIDUNGEON_JOBS_WORKER=0) para testes
determinísticos — a suíte chama `process_pending()` diretamente.
"""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path
from typing import Any

from app import db, image_client, llm_client, npc_assets, settings_store
from app.errors import GameError
from app.image_client import ImageError
from app.llm_client import LlmError

log = logging.getLogger("aidungeon.jobs")

_PORTRAIT_SYSTEM = """You write image generation prompts for character portraits.
Given the character description and the story context, output a single concise English
prompt for a text-to-image model: physical appearance, clothing, expression and
portrait framing (bust/upper body, neutral background). Visual traits only —
no names as text, no dialogue, no meta text, no quotes. One or two sentences.
Output ONLY the prompt."""

# img2img a partir da âncora. Baixo o bastante para o rosto não virar outra pessoa.
PORTRAIT_DENOISE = 0.35

_BACKDROP_NEGATIVE = "person, people, human, face, portrait, character, crowd, figure"


def worker_enabled() -> bool:
    return os.environ.get("AIDUNGEON_JOBS_WORKER", "1") != "0"


# ------------------------------------------------------------- enfileirar
def enqueue_portrait(conn, adventure_id: str, card_id: str,
                     *, replace: bool = False) -> dict | None:
    """Enfileira retrato do card (dedupe: reaproveita job ativo).

    Sem replace, um card que já tem foto oficial não entra na fila — a cena
    e o "gerar todos" não trocam o rosto. replace=True é o botão de regenerar:
    o job leva o id da âncora para img2img.
    """
    card = db.get_card(conn, adventure_id, card_id)
    if card and card.get("portrait_image_id") and not replace:
        return None
    active = db.find_active_job(conn, "character_portrait", card_id)
    if active:
        return active
    payload: dict = {}
    if replace:
        payload["replace"] = True
        anchor = ""
        if card:
            anchor = card.get("portrait_anchor_id") or card.get("portrait_image_id") or ""
        if anchor:
            payload["init_image_id"] = anchor
    return db.insert_job(conn, adventure_id=adventure_id,
                         type="character_portrait", ref_id=card_id, payload=payload)


def enqueue_all_portraits(conn, adventure_id: str) -> list[dict]:
    """Um job por NPC que ainda não tem retrato. Quem já tem foto fica como está."""
    queued = []
    for card in db.list_cards(conn, adventure_id):
        if card["type"] != "character" or card.get("portrait_image_id"):
            continue
        job = enqueue_portrait(conn, adventure_id, card["id"])
        if job:
            queued.append(job)
    return queued


# --------------------------------------------------------------- execução
def _build_portrait_prompt(conn, adv: dict, card: dict, llm_cfg: dict,
                           transport: Any) -> str:
    """Usa portrait_prompt do card se houver; senão o LLM escreve um a partir
    da descrição (entry) + contexto da aventura, e o prompt fica persistido."""
    if card["portrait_prompt"]:
        return card["portrait_prompt"]
    user = (
        f"Story: {adv['title']} — {adv['description'][:600]}\n"
        f"Character: {card['title']}\n"
        f"Description: {card['entry'][:1200]}"
    )
    # v0.5.0: ficha estruturada enriquece o prompt (aparência/corpo/roupa).
    sheet_ctx = npc_assets.sheet_context(card["sheet"])
    if sheet_ctx:
        user += f"\nCharacter sheet:\n{sheet_ctx}"
    messages = [{"role": "system", "content": _PORTRAIT_SYSTEM},
                {"role": "user", "content": user}]
    prompt = llm_client.chat_completion(llm_cfg, messages, temperature=0.5,
                                        max_tokens=160, transport=transport).strip()
    if not prompt:
        raise GameError(503, "generation_failed",
                        "LLM returned an empty portrait prompt; try again")
    db.update_card(conn, adv["id"], card["id"], {"portrait_prompt": prompt})
    return prompt


def _portrait_bytes(image: dict) -> bytes:
    """PNG da foto já gravada (caminho relativo ao repo ou absoluto)."""
    raw = image.get("file_path") or ""
    path = Path(raw)
    if not path.is_absolute():
        path = db.REPO_ROOT / path
    if not path.is_file():
        raise GameError(503, "generation_failed", "canonical portrait file is missing")
    return path.read_bytes()


def _execute_portrait(job: dict, transport: Any) -> dict:
    """Executa um job character_portrait; devolve o result (ou levanta)."""
    payload = job.get("payload") or {}
    replace = bool(payload.get("replace"))
    with db.session() as conn:
        adv = db.get_adventure(conn, job["adventure_id"])
        card = db.get_card(conn, job["adventure_id"], job["ref_id"])
        if not adv:
            raise GameError(404, "not_found", f"adventure {job['adventure_id']} not found")
        if not card:
            raise GameError(404, "not_found", f"card {job['ref_id']} not found")
        if card.get("portrait_image_id") and not replace:
            existing = db.get_image(conn, card["portrait_image_id"])
            if not existing:
                raise GameError(503, "generation_failed",
                                "portrait is already assigned but the image row is missing")
            return {"image": db.image_ref(existing), "card_id": card["id"],
                    "prompt": card.get("portrait_prompt") or "", "kept": True}
        img_cfg = settings_store.effective_image_settings(conn)
        if not img_cfg["enabled"]:
            raise GameError(503, "image_generation_disabled",
                            "Image generation is disabled in settings")
        llm_cfg = settings_store.effective_llm_settings(conn)
        prompt = _build_portrait_prompt(conn, adv, card, llm_cfg, transport)
        final = " ".join(p for p in (adv.get("image_style") or "",
                                     prompt,
                                     img_cfg["style_suffix"] or "") if p).strip()
        init_bytes = None
        init_width = init_height = None
        anchor_id = str(payload.get("init_image_id") or "")
        if anchor_id:
            anchor = db.get_image(conn, anchor_id)
            if not anchor:
                raise GameError(503, "generation_failed",
                                "canonical portrait image is missing")
            init_bytes = _portrait_bytes(anchor)
            init_width, init_height = anchor.get("width"), anchor.get("height")
            final = f"{final}, same person, keep the same face".strip()
        image_id = db.new_id()
        meta = image_client.generate_image(
            img_cfg, final, adventure_id=adv["id"],
            image_id=image_id, transport=transport,
            width=init_width, height=init_height,
            init_image=init_bytes,
            denoising_strength=PORTRAIT_DENOISE if init_bytes else None,
        )
        image = db.insert_image(
            conn, adventure_id=adv["id"], action_seq=-1,
            file_path=meta["file_path"], prompt=final,
            negative_prompt=img_cfg["negative_prompt"],
            provider=meta["provider"], width=meta["width"], height=meta["height"],
            image_id=image_id, kind="portrait", ref_id=card["id"],
        )
        if anchor_id:
            db.set_card_portrait(conn, card["id"], image["id"], anchor_id=anchor_id)
        else:
            db.set_card_portrait(conn, card["id"], image["id"], anchor_id=image["id"])
        return {"image": db.image_ref(image), "card_id": card["id"],
                "prompt": prompt}


# Fundo de cena: widescreen, lugar sem pessoas (os retratos entram por cima).
_BACKDROP_WIDTH = 896
_BACKDROP_HEIGHT = 512


def _execute_backdrop(job: dict, transport: Any) -> dict:
    """Executa um job scene_backdrop; devolve o result (ou levanta)."""
    prompt = str((job.get("payload") or {}).get("prompt") or "").strip()
    if not prompt:
        raise GameError(500, "generation_failed", "scene backdrop job has no prompt")
    with db.session() as conn:
        img_cfg = settings_store.effective_image_settings(conn)
        if not img_cfg["enabled"]:
            raise GameError(503, "image_generation_disabled",
                            "Image generation is disabled in settings")
        image_id = db.new_id()
        negative = ", ".join(
            part for part in (img_cfg["negative_prompt"], _BACKDROP_NEGATIVE) if part
        )
        meta = image_client.generate_image(
            img_cfg, prompt, adventure_id=job["adventure_id"],
            image_id=image_id, transport=transport,
            width=_BACKDROP_WIDTH, height=_BACKDROP_HEIGHT,
        )
        image = db.insert_image(
            conn, adventure_id=job["adventure_id"], action_seq=-1,
            file_path=meta["file_path"], prompt=prompt,
            negative_prompt=negative,
            provider=meta["provider"], width=meta["width"], height=meta["height"],
            image_id=image_id, kind="backdrop", ref_id=job["ref_id"] or "",
        )
        return {"image": db.image_ref(image), "ref_id": job["ref_id"]}


def _run_job(job: dict, transport: Any) -> None:
    """Executa um job claimed e grava done/error (canceled não se aplica: o
    claim já o tirou de pending)."""
    try:
        if job["type"] == "scene_backdrop":
            result = _execute_backdrop(job, transport)
        else:
            result = _execute_portrait(job, transport)
    except (GameError, ImageError, LlmError) as exc:
        message = getattr(exc, "message", str(exc))
        log.warning("job %s (%s) falhou: %s", job["id"], job["type"], message)
        with db.session() as conn:
            db.finish_job(conn, job["id"], "error", error=message)
    except Exception:
        log.exception("job %s (%s) erro inesperado", job["id"], job["type"])
        with db.session() as conn:
            db.finish_job(conn, job["id"], "error", error="unexpected internal error")
    else:
        log.info("job %s (%s) concluído", job["id"], job["type"])
        with db.session() as conn:
            db.finish_job(conn, job["id"], "done", result=result)


def process_pending(transport: Any = None) -> int:
    """Drena a fila em ordem (chamado pelo worker e, diretamente, nos testes)."""
    processed = 0
    while True:
        with db.session() as conn:
            job = db.claim_next_job(conn)
        if not job:
            return processed
        _run_job(job, transport)
        processed += 1


# ----------------------------------------------------------------- worker
async def worker_loop(interval: float = 1.5) -> None:
    """Loop infinito: claim+execução em thread para não bloquear o event loop."""
    log.info("jobs worker iniciado (intervalo %.1fs)", interval)
    while True:
        try:
            await asyncio.to_thread(process_pending)
        except Exception:
            log.exception("jobs worker: erro no ciclo")
        await asyncio.sleep(interval)


def start_worker(app) -> None:
    """Chamado no lifespan; no-op se AIDUNGEON_JOBS_WORKER=0."""
    if not worker_enabled():
        log.info("jobs worker desligado (AIDUNGEON_JOBS_WORKER=0)")
        return
    app.state.job_worker = asyncio.create_task(worker_loop())


def stop_worker(app) -> None:
    task = getattr(app.state, "job_worker", None)
    if task:
        task.cancel()
