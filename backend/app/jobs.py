"""Pipeline assíncrono de assets (v0.3.0) — worker asyncio sobre a tabela jobs.

Fluxo: rotas enfileiram (db.insert_job); o worker (único por processo) faz
claim do mais antigo e executa num thread (SQLite é síncrono). Hoje existe um
tipo: `character_portrait` — LLM expande a descrição do NPC em prompt EN de
retrato (se o card não tiver portrait_prompt), gera a imagem no provider
efetivo (mesmo do "See") e vincula ao card (images kind='portrait').

O worker é desligável por env (AIDUNGEON_JOBS_WORKER=0) para testes
determinísticos — a suíte chama `process_pending()` diretamente.
"""

from __future__ import annotations

import asyncio
import logging
import os
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


def worker_enabled() -> bool:
    return os.environ.get("AIDUNGEON_JOBS_WORKER", "1") != "0"


# ------------------------------------------------------------- enfileirar
def enqueue_portrait(conn, adventure_id: str, card_id: str) -> dict:
    """Enfileira geração de retrato do card (dedupe: reaproveita job ativo)."""
    active = db.find_active_job(conn, "character_portrait", card_id)
    if active:
        return active
    return db.insert_job(conn, adventure_id=adventure_id,
                         type="character_portrait", ref_id=card_id)


def enqueue_all_portraits(conn, adventure_id: str) -> list[dict]:
    """Um job por card do tipo character (pipeline 'preparar tudo' da v0.3.0)."""
    jobs = []
    for card in db.list_cards(conn, adventure_id):
        if card["type"] == "character":
            jobs.append(enqueue_portrait(conn, adventure_id, card["id"]))
    return jobs


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


def _execute_portrait(job: dict, transport: Any) -> dict:
    """Executa um job character_portrait; devolve o result (ou levanta)."""
    with db.session() as conn:
        adv = db.get_adventure(conn, job["adventure_id"])
        card = db.get_card(conn, job["adventure_id"], job["ref_id"])
        if not adv:
            raise GameError(404, "not_found", f"adventure {job['adventure_id']} not found")
        if not card:
            raise GameError(404, "not_found", f"card {job['ref_id']} not found")
        img_cfg = settings_store.effective_image_settings(conn)
        if not img_cfg["enabled"]:
            raise GameError(503, "image_generation_disabled",
                            "Image generation is disabled in settings")
        llm_cfg = settings_store.effective_llm_settings(conn)
        prompt = _build_portrait_prompt(conn, adv, card, llm_cfg, transport)
        final = " ".join(p for p in (adv.get("image_style") or "",
                                     prompt,
                                     img_cfg["style_suffix"] or "") if p).strip()
        image_id = db.new_id()
        meta = image_client.generate_image(img_cfg, final, adventure_id=adv["id"],
                                           image_id=image_id, transport=transport)
        image = db.insert_image(
            conn, adventure_id=adv["id"], action_seq=-1,
            file_path=meta["file_path"], prompt=final,
            negative_prompt=img_cfg["negative_prompt"],
            provider=meta["provider"], width=meta["width"], height=meta["height"],
            image_id=image_id, kind="portrait", ref_id=card["id"],
        )
        db.set_card_portrait(conn, card["id"], image["id"])
        return {"image": db.image_ref(image), "card_id": card["id"],
                "prompt": prompt}


def _run_job(job: dict, transport: Any) -> None:
    """Executa um job claimed e grava done/error (canceled não se aplica: o
    claim já o tirou de pending)."""
    try:
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
