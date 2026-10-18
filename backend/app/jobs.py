"""Pipeline assíncrono de assets (v0.3.0) — worker asyncio sobre a tabela jobs.

Fluxo: rotas enfileiram (`enqueue`, contrato C5); o worker (único por
processo) faz claim do de menor prioridade e, nela, do mais antigo, e executa
num thread (SQLite é síncrono). Prioridades: 0 See, 1 retrato de NPC
presente, 2 fundo de cena, 3 retratos em lote. Com narrativa em curso só o
See (0) roda. Job novo com a mesma `supersede_key` cancela os pendentes
antigos. Cada mudança de estado sai no bus (`job_queued`, `job_progress`).
Tipos:
- `character_portrait` — LLM expande a descrição do NPC em prompt EN de
  retrato (se o card não tiver portrait_prompt), gera a imagem no provider
  efetivo (mesmo do "See") e vincula ao card (images kind='portrait').
  A primeira foto vira a âncora (`portrait_anchor_id`). Geração automática
  não substitui um retrato que já existe. Pedido explícito de regenerar
  usa img2img a partir dessa âncora, para o rosto continuar o mesmo.
- `scene_backdrop` — pinta o fundo do lugar atual (payload.prompt já pronto,
  sem LLM) em images kind='backdrop'. Wide 1344×768 (bucket SDXL).
- `see_image` — See assíncrono (C6). Nasce em `preparing`: o prompt (LLM,
  role image_prompt) roda numa thread e só então o job entra na fila do SD,
  para não prender o worker. Termina em `see_ready` ou `see_failed`.

Aventura apagada no meio do render: o PNG é descartado e o job termina
cancelado. O worker é desligável por env (AIDUNGEON_JOBS_WORKER=0) para
testes determinísticos — a suíte chama `process_pending()` diretamente.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sqlite3
import threading
from pathlib import Path
from typing import Any, Callable

from app import db, events, game_engine, image_client, llm_client, npc_assets, settings_store
from app.errors import GameError
from app.image_client import ImageError
from app.llm_client import LlmError

log = logging.getLogger("aidungeon.jobs")

PRIORITY_SEE = 0
PRIORITY_PRESENT_PORTRAIT = 1
PRIORITY_BACKDROP = 2
PRIORITY_BULK_PORTRAIT = 3

_wakeup = threading.Event()

_PORTRAIT_SYSTEM = """You write image generation prompts for character portraits.
Given the character description and the story context, output a single concise English
prompt for a text-to-image model: physical appearance, clothing, expression and
portrait framing (bust/upper body, neutral background). Visual traits only —
no names as text, no dialogue, no meta text, no quotes. One or two sentences.
Output ONLY the prompt."""

# img2img a partir da âncora. Baixo o bastante para o rosto não virar outra pessoa.
PORTRAIT_DENOISE = 0.35

_BACKDROP_NEGATIVE = "person, people, human, face, portrait, character, crowd, figure"
# Quando o card tem rosto, a cena PODE ter essa pessoa — e só ela, com essa cara.
_CAST_NEGATIVE = "extra person, crowd, duplicate face, wrong hair, different outfit, costume change"
# Cena nua: a roupa do card não é negativa. "costume change" vestia de novo.
_CAST_NEGATIVE_EXPLICIT = "extra person, crowd, duplicate face, wrong hair"


def worker_enabled() -> bool:
    return os.environ.get("AIDUNGEON_JOBS_WORKER", "1") != "0"


def wake() -> None:
    """Acorda o worker antes do intervalo (job novo pronto para rodar)."""
    _wakeup.set()


# ---------------------------------------------------------------- eventos
def _progress(job: dict) -> None:
    events.publish(job["adventure_id"], "job_progress",
                   {"job_id": job["id"], "type": job["type"], "status": job["status"]})


def _finish(job: dict, status: str, *, result: dict | None = None,
            error: str | None = None) -> None:
    with db.session() as conn:
        db.finish_job(conn, job["id"], status, result=result, error=error)
    _progress({**job, "status": status})


# ------------------------------------------------------------- enfileirar
def enqueue(conn, type_: str, adventure_id: str, ref_id: str | None = None,
            payload: dict | None = None, priority: int = PRIORITY_BACKDROP,
            supersede_key: str | None = None) -> dict:
    """Job novo (C5). Com `supersede_key`, cancela os pendentes antigos com a
    mesma chave. Os eventos saem antes do commit de quem chamou."""
    job = db.insert_job(conn, adventure_id=adventure_id, type=type_, ref_id=ref_id or "",
                        payload=payload, priority=priority,
                        supersede_key=supersede_key or "")
    superseded = db.supersede_jobs(conn, supersede_key, job["id"]) if supersede_key else []
    events.publish(adventure_id, "job_queued",
                   {"job_id": job["id"], "type": type_, "ref_id": job["ref_id"]})
    for old in superseded:
        _progress(old)
    wake()
    return job


def enqueue_portrait(conn, adventure_id: str, card_id: str,
                     *, replace: bool = False,
                     priority: int = PRIORITY_PRESENT_PORTRAIT) -> dict | None:
    """Enfileira retrato do card (dedupe: reaproveita job ativo, que passa à
    frente se o pedido novo for mais urgente).

    Sem replace, um card que já tem foto oficial não entra na fila — a cena
    e o "gerar todos" não trocam o rosto. replace=True é o botão de regenerar:
    o job leva o id da âncora para img2img.
    """
    card = db.get_card(conn, adventure_id, card_id)
    if card and card.get("portrait_image_id") and not replace:
        return None
    active = db.find_active_job(conn, "character_portrait", card_id)
    if active:
        return db.raise_job_priority(conn, active["id"], priority)
    payload: dict = {}
    if replace:
        payload["replace"] = True
        anchor = ""
        if card:
            anchor = card.get("portrait_anchor_id") or card.get("portrait_image_id") or ""
        if anchor:
            payload["init_image_id"] = anchor
    return enqueue(conn, "character_portrait", adventure_id, card_id, payload,
                   priority=priority)


def enqueue_all_portraits(conn, adventure_id: str) -> list[dict]:
    """Um job por NPC que ainda não tem retrato. Quem já tem foto fica como está."""
    queued = []
    for card in db.list_cards(conn, adventure_id):
        if card["type"] != "character" or card.get("portrait_image_id"):
            continue
        job = enqueue_portrait(conn, adventure_id, card["id"],
                               priority=PRIORITY_BULK_PORTRAIT)
        if job:
            queued.append(job)
    return queued


def start_see(adventure_id: str, payload: dict, plan: Callable[[], dict]) -> dict:
    """See assíncrono (C6): grava o job em `preparing` e devolve na hora; `plan`
    (o prompt, via LLM) roda numa thread e completa o payload com `prompt`,
    `negative_extra`, `negative_prompt` e `caption`."""
    with db.session() as conn:
        job = db.insert_job(conn, adventure_id=adventure_id, type="see_image",
                            payload=payload, priority=PRIORITY_SEE, status="preparing")
    events.publish(adventure_id, "job_queued",
                   {"job_id": job["id"], "type": "see_image", "ref_id": ""})
    threading.Thread(target=_prepare_see, args=(job, plan), name="see-prompt",
                     daemon=True).start()
    return job


def _prepare_see(job: dict, plan: Callable[[], dict]) -> None:
    try:
        ready = {**job["payload"], **plan()}
    except (GameError, LlmError) as exc:
        _fail_see(job, exc.code, exc.message)
        return
    except Exception:
        log.exception("see %s: prompt falhou", job["id"])
        _fail_see(job, "generation_failed", "could not build the image prompt")
        return
    with db.session() as conn:
        promoted = db.promote_job(conn, job["id"], ready)
    if promoted is None:
        return
    _progress(promoted)
    wake()


def _fail_see(job: dict, code: str, message: str) -> None:
    with db.session() as conn:
        current = db.get_job(conn, job["id"])
    if current is not None and current["status"] == "preparing":
        _failed(job, code, message)


def cancel(conn, job_id: str) -> dict | None:
    """Cancela um job que ainda não roda e avisa no bus; None se já rodava."""
    job = db.cancel_job(conn, job_id)
    if job is not None:
        _progress(job)
    return job


# --------------------------------------------------------------- execução
class AdventureGone(GameError):
    """A aventura foi apagada enquanto a imagem era pintada."""

    def __init__(self, adventure_id: str) -> None:
        super().__init__(404, "not_found", f"adventure {adventure_id} not found")


def _raise_if_gone(adventure_id: str, file_path: str) -> None:
    """Depois de um IntegrityError ao gravar: se a aventura sumiu, o PNG vai
    embora e sobe AdventureGone; senão quem chamou relança o erro."""
    with db.session() as conn:
        if db.get_adventure(conn, adventure_id) is not None:
            return
    (db.REPO_ROOT / file_path).unlink(missing_ok=True)
    raise AdventureGone(adventure_id)


def _build_portrait_prompt(adv: dict, card: dict, llm_cfg: dict,
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
    prompt = llm_client.chat_completion(llm_cfg, messages, role="image_prompt",
                                        temperature=0.5, max_tokens=160,
                                        transport=transport).strip()
    if not prompt:
        raise GameError(503, "generation_failed",
                        "LLM returned an empty portrait prompt; try again")
    with db.session() as conn:
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
    """Executa um job character_portrait; devolve o result (ou levanta).

    LLM e render rodam fora de transação: o banco não fica travado para a
    narrativa enquanto a GPU pinta."""
    payload = job.get("payload") or {}
    replace = bool(payload.get("replace"))
    anchor_id = str(payload.get("init_image_id") or "")
    anchor = None
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
        if anchor_id:
            anchor = db.get_image(conn, anchor_id)
            if not anchor:
                raise GameError(503, "generation_failed",
                                "canonical portrait image is missing")
    prompt = _build_portrait_prompt(adv, card, llm_cfg, transport)
    final = " ".join(p for p in (adv.get("image_style") or "",
                                 prompt,
                                 img_cfg["style_suffix"] or "") if p).strip()
    look = npc_assets.sheet_context(card.get("sheet") or "")
    if look:
        final = f"{final}. Mandatory appearance, do not contradict: {look}"
    init_bytes = None
    init_width = init_height = None
    if anchor:
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
    try:
        with db.session() as conn:
            image = db.insert_image(
                conn, adventure_id=adv["id"], action_seq=-1,
                file_path=meta["file_path"], prompt=final,
                negative_prompt=img_cfg["negative_prompt"],
                provider=meta["provider"], width=meta["width"], height=meta["height"],
                image_id=image_id, kind="portrait", ref_id=card["id"],
            )
            db.set_card_portrait(conn, card["id"], image["id"],
                                 anchor_id=anchor_id or image["id"])
    except sqlite3.IntegrityError:
        _raise_if_gone(adv["id"], meta["file_path"])
        raise
    return {"image": db.image_ref(image), "card_id": card["id"],
            "prompt": prompt}


# Fundo de cena: bucket SDXL 7:4. A faixa corta o centro; 896×512 ficava mole.
_BACKDROP_WIDTH = 1344
_BACKDROP_HEIGHT = 768


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
    payload = job.get("payload") or {}
    if payload.get("explicit"):
        ban = _CAST_NEGATIVE_EXPLICIT
    elif payload.get("include_people"):
        ban = _CAST_NEGATIVE
    else:
        ban = _BACKDROP_NEGATIVE
    negative = ", ".join(
        part for part in (img_cfg["negative_prompt"], ban) if part
    )
    meta = image_client.generate_image(
        img_cfg, prompt, adventure_id=job["adventure_id"],
        image_id=image_id, transport=transport,
        width=_BACKDROP_WIDTH, height=_BACKDROP_HEIGHT,
        negative_extra=ban,
    )
    try:
        with db.session() as conn:
            image = db.insert_image(
                conn, adventure_id=job["adventure_id"], action_seq=-1,
                file_path=meta["file_path"], prompt=prompt,
                negative_prompt=negative,
                provider=meta["provider"], width=meta["width"], height=meta["height"],
                image_id=image_id, kind="backdrop", ref_id=job["ref_id"] or "",
            )
    except sqlite3.IntegrityError:
        _raise_if_gone(job["adventure_id"], meta["file_path"])
        raise
    return {"image": db.image_ref(image), "ref_id": job["ref_id"]}


def store_see(adventure_id: str, caption: str, meta: dict, *, image_id: str,
              prompt: str, negative_prompt: str) -> dict:
    """Grava a action `see` no fim da aventura e a imagem no mesmo seq (rota
    síncrona e job). Devolve o ImageResult."""
    try:
        with game_engine.appending(adventure_id) as conn:
            action = game_engine.append_action(conn, adventure_id, "see", caption)
            db.insert_image(conn, adventure_id=adventure_id, action_seq=action["seq"],
                            prompt=prompt, negative_prompt=negative_prompt,
                            file_path=meta["file_path"], provider=meta["provider"],
                            width=meta["width"], height=meta["height"],
                            image_id=image_id)
            action = db.get_action(conn, action["id"])
    except sqlite3.IntegrityError:
        _raise_if_gone(adventure_id, meta["file_path"])
        raise
    return {"id": image_id, "url": f"/api/images/{image_id}", "prompt": prompt,
            "provider": meta["provider"], "action": action}


def _execute_see(job: dict, transport: Any) -> dict:
    """Executa um job see_image (prompt já no payload); devolve {"image": ImageResult}."""
    payload = job.get("payload") or {}
    prompt = str(payload.get("prompt") or "").strip()
    if not prompt:
        raise GameError(500, "generation_failed", "see job has no prompt")
    with db.session() as conn:
        img_cfg = settings_store.effective_image_settings(conn)
    if not img_cfg["enabled"]:
        raise GameError(503, "image_disabled",
                        "image generation is disabled (settings.image.enabled)")
    image_id = db.new_id()
    meta = image_client.generate_image(
        img_cfg, prompt, adventure_id=job["adventure_id"], image_id=image_id,
        transport=transport, negative_extra=str(payload.get("negative_extra") or ""))
    image = store_see(job["adventure_id"], str(payload.get("caption") or ""), meta,
                      image_id=image_id, prompt=prompt,
                      negative_prompt=str(payload.get("negative_prompt") or ""))
    return {"image": image}


_EXECUTORS: dict[str, Callable[[dict, Any], dict]] = {
    "character_portrait": _execute_portrait,
    "scene_backdrop": _execute_backdrop,
    "see_image": _execute_see,
}


def _failed(job: dict, code: str, message: str) -> None:
    _finish(job, "error", result={"code": code}, error=message)
    if job["type"] == "see_image":
        events.publish(job["adventure_id"], "see_failed",
                       {"job_id": job["id"], "code": code, "message": message})


def _announce(job: dict, result: dict) -> None:
    """Evento do tipo, depois do `job_progress` done."""
    data = {"job_id": job["id"], "image": result["image"]}
    if job["type"] == "character_portrait":
        events.publish(job["adventure_id"], "portrait_ready",
                       {**data, "card_id": result["card_id"]})
    elif job["type"] == "scene_backdrop":
        events.publish(job["adventure_id"], "backdrop_ready",
                       {**data, "ref_id": job["ref_id"]})
    elif job["type"] == "see_image":
        events.publish(job["adventure_id"], "see_ready", data)


def _run_job(job: dict, transport: Any) -> None:
    """Executa um job claimed e grava done/error. Aventura apagada no meio
    termina canceled, sem evento (o canal dela já foi fechado)."""
    _progress(job)
    try:
        executor = _EXECUTORS.get(job["type"])
        if executor is None:
            raise GameError(500, "generation_failed", f"unknown job type {job['type']}")
        result = executor(job, transport)
    except AdventureGone:
        log.info("job %s (%s): aventura apagada no meio, PNG descartado",
                 job["id"], job["type"])
        with db.session() as conn:
            db.finish_job(conn, job["id"], "canceled", error="adventure deleted")
    except (GameError, ImageError, LlmError) as exc:
        message = getattr(exc, "message", str(exc))
        log.warning("job %s (%s) falhou: %s", job["id"], job["type"], message)
        _failed(job, getattr(exc, "code", "generation_failed"), message)
    except Exception:
        log.exception("job %s (%s) erro inesperado", job["id"], job["type"])
        _failed(job, "generation_failed", "unexpected internal error")
    else:
        log.info("job %s (%s) concluído", job["id"], job["type"])
        _finish(job, "done", result=result)
        _announce(job, result)


def process_pending(transport: Any = None, *, should_pause=None) -> int:
    """Drena a fila em ordem de prioridade (worker e, diretamente, testes).

    should_pause, quando passado, é consultado antes de cada claim: enquanto
    devolver verdade (narrativa em curso), só o See sai da fila e o resto
    espera em pending (a GPU fica com o LLM).
    """
    processed = 0
    while True:
        paused = should_pause is not None and should_pause()
        with db.session() as conn:
            job = db.claim_next_job(conn, max_priority=PRIORITY_SEE if paused else None)
        if not job:
            return processed
        _run_job(job, transport)
        processed += 1


def recover_interrupted() -> tuple[int, int]:
    """Jobs que o processo anterior deixou no meio (db.requeue_interrupted_jobs)."""
    with db.session() as conn:
        requeued, failed = db.requeue_interrupted_jobs(conn)
    if requeued or failed:
        log.info("jobs: %d de volta à fila, %d See interrompidos", requeued, failed)
    return requeued, failed


# ----------------------------------------------------------------- worker
async def worker_loop(interval: float = 1.5) -> None:
    """Loop infinito: claim+execução em thread para não bloquear o event loop.
    `wake()` encurta a espera quando entra job novo."""
    log.info("jobs worker iniciado (intervalo %.1fs)", interval)
    while True:
        try:
            await asyncio.to_thread(
                process_pending,
                should_pause=game_engine.is_any_generation_active,
            )
        except Exception:
            log.exception("jobs worker: erro no ciclo")
        await asyncio.to_thread(_wakeup.wait, interval)
        _wakeup.clear()


def start_worker(app) -> None:
    """Chamado no lifespan; no-op se AIDUNGEON_JOBS_WORKER=0."""
    if not worker_enabled():
        log.info("jobs worker desligado (AIDUNGEON_JOBS_WORKER=0)")
        return
    recover_interrupted()
    app.state.job_worker = asyncio.create_task(worker_loop())


def stop_worker(app) -> None:
    task = getattr(app.state, "job_worker", None)
    if task:
        task.cancel()
        wake()
