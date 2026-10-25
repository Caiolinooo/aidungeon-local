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
  usa img2img a partir dessa âncora, para o rosto fictício continuar o mesmo.
  Pessoa real com arquivo de referência ignora essa âncora e parte da foto
  (`likeness.portrait_init`, denoise `REFERENCE_DENOISE`).
  CUDA out of memory não reenfileira o mesmo card na hora: a UI pede a cena
  de novo a cada poucos segundos, e cada erro virava outro txt2img.
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
import json
import logging
import os
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from app import (db, events, game_engine, image_client, likeness, llm_client,
                    npc_assets, settings_store)
from app.errors import GameError
from app.image_client import ImageError
from app.llm_client import LlmError

log = logging.getLogger("aidungeon.jobs")

PRIORITY_SEE = 0
PRIORITY_PRESENT_PORTRAIT = 1
PRIORITY_BACKDROP = 2
PRIORITY_BULK_PORTRAIT = 3
PRIORITY_LIFE_TICK = 4
PRIORITY_CARD_ASSETS = 5

# A cena aberta pede retrato de novo assim que o job anterior termina em erro.
# Sem esta espera, um CUDA OOM vira outro txt2img a cada poucos segundos.
CUDA_OOM_BACKOFF_S = 300
CUDA_OOM_RETRY_CAP = 3

_wakeup = threading.Event()

_PORTRAIT_SYSTEM = """You write image generation prompts for character portraits.
Given the character description and the story context, output a single concise English
prompt for a text-to-image model: physical appearance, clothing, expression and
portrait framing (bust/upper body, neutral background). Visual traits only —
no names as text, no dialogue, no meta text, no quotes. One or two sentences.
Output ONLY the prompt."""

# img2img a partir da âncora. Baixo o bastante para o rosto não virar outra pessoa.
PORTRAIT_DENOISE = 0.35
PORTRAIT_VARIANT_DENOISE = 0.5

_BACKDROP_NEGATIVE = "person, people, human, face, portrait, character, crowd, figure"
# Cena vestida: o fundo recusa nudez. Sem isso a pintura trocava a roupa do
# card e a fada saía nua no painel enquanto o See mostrava o vestido.
_NUDITY_BAN = "nude, naked, undressed, topless, bottomless, nudity"
# Quando o card tem rosto, a cena PODE ter essa pessoa — e só ela, com essa cara.
_CAST_NEGATIVE = (
    "extra person, crowd, duplicate face, wrong hair, different outfit, costume change, "
    + npc_assets.IDENTITY_NEGATIVE
    + ", " + _NUDITY_BAN
)
# Cena nua: a roupa do card não é negativa. "costume change" vestia de novo.
_CAST_NEGATIVE_EXPLICIT = (
    "extra person, crowd, duplicate face, wrong hair, "
    + npc_assets.IDENTITY_NEGATIVE
)


def worker_enabled() -> bool:
    return os.environ.get("AIDUNGEON_JOBS_WORKER", "1") != "0"


def wake() -> None:
    """Acorda o worker antes do intervalo (job novo pronto para rodar)."""
    _wakeup.set()


# ---------------------------------------------------------------- eventos
def _progress(job: dict) -> None:
    events.publish(job["adventure_id"], "job_progress",
                   {"job_id": job["id"], "type": job["type"], "status": job["status"]},
                   entity_id=job["id"], correlation_id=job["id"])


def _finish(job: dict, status: str, *, result: dict | None = None,
            error: str | None = None) -> bool:
    with db.session() as conn:
        finished = db.finish_job(conn, job["id"], status, result=result, error=error)
        if finished:
            events.record(
                conn, job["adventure_id"], "job_progress",
                {"job_id": job["id"], "type": job["type"], "status": status},
                entity_id=job["id"], correlation_id=job["id"],
                idempotency_key=f"job:{job['id']}:{status}",
            )
    return finished

# ------------------------------------------------------------- enfileirar
def enqueue(conn, type_: str, adventure_id: str, ref_id: str | None = None,
            payload: dict | None = None, priority: int = PRIORITY_BACKDROP,
            supersede_key: str | None = None) -> dict:
    """Job novo (C5). Fila e eventos saem no commit de quem chamou."""
    job = db.insert_job(conn, adventure_id=adventure_id, type=type_, ref_id=ref_id or "",
                        payload=payload, priority=priority,
                        supersede_key=supersede_key or "")
    superseded = db.supersede_jobs(conn, supersede_key, job["id"]) if supersede_key else []
    events.record(
        conn, adventure_id, "job_queued",
        {"job_id": job["id"], "type": type_, "ref_id": job["ref_id"]},
        entity_id=job["id"], correlation_id=job["id"],
        idempotency_key=f"job:{job['id']}:queued",
    )
    for old in superseded:
        events.record(
            conn, adventure_id, "job_progress",
            {"job_id": old["id"], "type": old["type"], "status": old["status"]},
            entity_id=old["id"], correlation_id=job["id"],
            idempotency_key=f"job:{old['id']}:{old['status']}",
        )
    wake()
    return job


def is_cuda_oom(message: str | None) -> bool:
    """True quando o provider recusou por falta de VRAM."""
    text = (message or "").lower()
    return "out of memory" in text or "outofmemory" in text


def _utc_stamp(value: str | None) -> datetime | None:
    if not value:
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _recent_jobs(conn, type_: str, ref_id: str, limit: int) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM jobs WHERE type=? AND ref_id=?"
        " ORDER BY created_at DESC, rowid DESC LIMIT ?",
        (type_, ref_id, int(limit)),
    ).fetchall()
    return [db.job_to_dict(row) for row in rows]


def cuda_oom_hold(conn, type_: str, ref_id: str, *, explicit: bool = False,
                  now: datetime | None = None) -> str | None:
    """Motivo para não enfileirar de novo, ou None se o alvo pode tentar.

    O último CUDA OOM segura o alvo por CUDA_OOM_BACKOFF_S. Sem pedido
    explícito, CUDA_OOM_RETRY_CAP erros seguidos encerram a tentativa
    automática. Um job que não é esse erro zera a sequência.
    """
    recent = _recent_jobs(conn, type_, ref_id, CUDA_OOM_RETRY_CAP)
    streak = 0
    for job in recent:
        if job["status"] == "error" and is_cuda_oom(job.get("error")):
            streak += 1
            continue
        break
    if streak == 0:
        return None
    newest = recent[0]
    moment = now or datetime.now(timezone.utc)
    finished = _utc_stamp(newest.get("finished_at") or newest.get("created_at"))
    if finished is not None:
        remaining = CUDA_OOM_BACKOFF_S - (moment - finished).total_seconds()
        if remaining > 0:
            return f"CUDA out of memory; retry waits {max(1, int(remaining))}s"
    if not explicit and streak >= CUDA_OOM_RETRY_CAP:
        return (
            "CUDA out of memory; automatic retries capped at "
            f"{CUDA_OOM_RETRY_CAP}"
        )
    return None


def enqueue_portrait(conn, adventure_id: str, card_id: str,
                     *, replace: bool = False,
                     priority: int = PRIORITY_PRESENT_PORTRAIT,
                     force_reference: bool = False,
                     variant_key: str = "") -> dict | None:
    """Enfileira retrato do card (dedupe: reaproveita job ativo, que passa à
    frente se o pedido novo for mais urgente).

    Sem replace, um card que já tem foto oficial não entra na fila — a cena
    e o "gerar todos" não trocam o rosto. replace=True é o botão de regenerar:
    o job leva o id da âncora para img2img. CUDA OOM recente segura os dois;
    o botão, passado o prazo, ainda tenta. A cena, no teto, para.
    """
    card = db.get_card(conn, adventure_id, card_id)
    if card and card.get("portrait_image_id") and not replace and not variant_key:
        return None
    active = db.find_active_job(conn, "character_portrait", card_id)
    if active:
        active_key = str((active.get("payload") or {}).get("state_key") or "")
        if variant_key and active_key and active_key != variant_key \
                and active.get("status") in ("preparing", "pending"):
            db.cancel_job(conn, active["id"])
        else:
            return db.raise_job_priority(conn, active["id"], priority)
    held = cuda_oom_hold(conn, "character_portrait", card_id, explicit=replace)
    if held:
        if replace:
            raise GameError(429, "cuda_oom_backoff", held)
        return None
    payload: dict = {}
    if variant_key:
        payload["replace"] = True
        payload["state_key"] = variant_key
        anchor = ""
        if card:
            anchor = card.get("portrait_anchor_id") or card.get("portrait_image_id") or ""
        if anchor:
            payload["init_image_id"] = anchor
    elif replace or force_reference:
        payload["replace"] = True
        anchor = ""
        if card and not force_reference:
            anchor = card.get("portrait_anchor_id") or card.get("portrait_image_id") or ""
        if anchor:
            payload["init_image_id"] = anchor
    if force_reference:
        payload["force_reference"] = True
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


def enqueue_card_assets(conn, adventure_id: str, card_id: str) -> dict | None:
    """Enfileira a ficha viva de um NPC (os campos em branco sao preenchidos
    pelo LLM em background). Card do jogador nao entra: a ficha dele e do
    jogador, nao do mundo."""
    card = db.get_card(conn, adventure_id, card_id)
    if not card or card.get("player_username"):
        return None
    sheet = npc_assets.parse_sheet(card.get("sheet"))
    if not npc_assets.missing_sheet_fields(sheet):
        return None
    active = db.find_active_job(conn, "card_assets", card_id)
    if active:
        return active
    return enqueue(conn, "card_assets", adventure_id, card_id,
                   priority=PRIORITY_CARD_ASSETS)


def enqueue_incomplete_sheets(conn, adventure_id: str) -> list[dict]:
    """Um job por NPC cuja ficha ainda tem campo em branco."""
    queued = []
    for card in db.list_cards(conn, adventure_id):
        if card["type"] != "character" or card.get("player_username"):
            continue
        if db.find_active_job(conn, "card_assets", card["id"]):
            continue
        job = enqueue_card_assets(conn, adventure_id, card["id"])
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
        events.record(
            conn, adventure_id, "job_queued",
            {"job_id": job["id"], "type": "see_image", "ref_id": ""},
            entity_id=job["id"], correlation_id=job["id"],
            idempotency_key=f"job:{job['id']}:queued",
        )
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
    """Cancela um job ativo e grava seu evento no mesmo commit."""
    job = db.cancel_job(conn, job_id)
    if job is not None:
        events.record(
            conn, job["adventure_id"], "job_progress",
            {"job_id": job["id"], "type": job["type"], "status": job["status"]},
            entity_id=job["id"], correlation_id=job["id"],
            idempotency_key=f"job:{job['id']}:{job['status']}",
        )
    return job


def interrupt_image(job: dict, transport: Any = None) -> bool:
    """Melhor esforço para interromper o render SD de um job running cancelado."""
    if job["type"] not in ("character_portrait", "scene_backdrop", "see_image"):
        return False
    try:
        with db.session() as conn:
            cfg = image_client.effective_settings(conn)
        if (cfg.get("provider") or "sdwebui") != "sdwebui":
            return False
        image_client.interrupt_sdwebui(cfg, transport)
    except ImageError as exc:
        log.warning("job %s: interrupt do WebUI falhou: %s", job["id"], exc.message)
        return False
    except Exception as exc:
        log.warning("job %s: interrupt do WebUI indisponível (%s)",
                    job["id"], type(exc).__name__)
        return False
    return True


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

def _discard_png(file_path: str) -> None:
    """Remove um render que perdeu a corrida para cancelamento."""
    (db.REPO_ROOT / file_path).unlink(missing_ok=True)


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
    force_reference = bool(payload.get("force_reference"))
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
        img_cfg = image_client.effective_settings(conn)
        if not img_cfg["enabled"]:
            raise GameError(503, "image_generation_disabled",
                            "Image generation is disabled in settings")
        llm_cfg = settings_store.effective_llm_settings(conn)
        if anchor_id:
            anchor = db.get_image(conn, anchor_id)
            if not anchor:
                raise GameError(503, "generation_failed",
                                "canonical portrait image is missing")
    state_key = str(payload.get("state_key") or "")
    if state_key:
        prompt = npc_assets.portrait_variant_prompt(card)
    else:
        prompt = _build_portrait_prompt(adv, card, llm_cfg, transport)
    final = " ".join(p for p in (adv.get("image_style") or "",
                                 prompt,
                                 img_cfg["style_suffix"] or "") if p).strip()
    prefix = likeness.real_person_prefix(card)
    if prefix:
        final = f"{prefix}, {final}" if final else prefix
    look = npc_assets.sheet_context(card.get("sheet") or "")
    if look:
        if state_key:
            final = f"{final}. Current look, follow the story: {look}"
        else:
            final = f"{final}. Mandatory appearance, do not contradict: {look}"
    init_width = init_height = None
    anchor_bytes = None
    if anchor:
        anchor_bytes = _portrait_bytes(anchor)
        init_width, init_height = anchor.get("width"), anchor.get("height")
        final = f"{final}, same person, keep the same face".strip()
    init_bytes, denoise = likeness.portrait_init(
        card, None if force_reference else anchor_bytes,
        anchor_denoise=PORTRAIT_VARIANT_DENOISE if state_key else PORTRAIT_DENOISE,
        force_reference=force_reference,
    )
    if init_bytes and not anchor:
        final = f"{final}, same person, keep the same face".strip()
    cache_key = state_key or image_client.request_key(
        img_cfg, final, width=init_width, height=init_height,
        init_image=init_bytes, denoising_strength=denoise,
    )
    with db.session() as conn:
        cached = db.find_cached_image(
            conn, adv["id"], kind="portrait", ref_id=card["id"],
            cache_key=cache_key,
        )
        if cached:
            current = db.get_card(conn, adv["id"], card["id"])
            desired = str(npc_assets.parse_sheet(
                (current or {}).get("sheet") or "").get("portrait_state_key") or "")
            cached_stale = bool(state_key and desired and desired != state_key)
            if not cached_stale:
                db.set_card_portrait(conn, card["id"], cached["id"],
                                     anchor_id=anchor_id or cached["id"],
                                     job_id=job["id"])
                _set_portrait_state(conn, adv["id"], card["id"], state_key, "")
            return {"image": db.image_ref(cached), "card_id": card["id"],
                    "prompt": prompt, "reused": True, "stale": cached_stale}
    image_id = db.new_id()
    stale = False
    stored_key = state_key
    if not stored_key:
        stored_key = npc_assets.portrait_state_key({
            **card,
            "portrait_image_id": image_id,
            "portrait_anchor_id": anchor_id or image_id,
        })
    meta = image_client.generate_image(
        img_cfg, final, adventure_id=adv["id"],
        image_id=image_id, transport=transport,
        width=init_width, height=init_height,
        init_image=init_bytes,
        denoising_strength=denoise,
    )
    try:
        with db.session() as conn:
            image = db.insert_image(
                conn, adventure_id=adv["id"], action_seq=-1,
                file_path=meta["file_path"], prompt=final,
                negative_prompt=img_cfg["negative_prompt"],
                provider=meta["provider"], width=meta["width"], height=meta["height"],
                image_id=image_id, kind="portrait", ref_id=card["id"],
                cache_key=stored_key, job_id=job["id"],
            )
            current = db.get_card(conn, adv["id"], card["id"])
            desired = str(npc_assets.parse_sheet(
                (current or {}).get("sheet") or "").get("portrait_state_key") or "")
            stale = bool(state_key and desired and desired != state_key)
            if not stale:
                db.set_card_portrait(conn, card["id"], image["id"],
                                     anchor_id=anchor_id or image["id"],
                                     job_id=job["id"])
                _set_portrait_state(conn, adv["id"], card["id"], stored_key, "")
    except db.JobCanceled:
        _discard_png(meta["file_path"])
        raise
    except sqlite3.IntegrityError:
        _raise_if_gone(adv["id"], meta["file_path"])
        raise
    return {"image": db.image_ref(image), "card_id": card["id"],
            "prompt": prompt, "stale": stale}


def _set_portrait_state(conn, adventure_id: str, card_id: str, state_key: str,
                        job_id: str) -> None:
    """Grava a chave desejada do retrato sem tocar no resto da ficha."""
    card = db.get_card(conn, adventure_id, card_id)
    if not card:
        return
    sheet = npc_assets.parse_sheet(card.get("sheet") or "")
    if state_key:
        sheet["portrait_state_key"] = state_key
    if "portrait_job_id" in sheet or job_id:
        sheet["portrait_job_id"] = job_id
    db.update_card(conn, adventure_id, card_id, {
        "sheet": json.dumps(sheet, ensure_ascii=False),
    })


def _execute_card_assets(job: dict, transport: Any) -> dict:
    """Preenche os campos em branco da ficha viva de um NPC (papel `assets`).

    A chamada ao LLM roda fora de transação: o banco não fica travado para a
    narrativa enquanto o modelo escreve. Só os campos pedidos são gravados —
    o que a ficha já tinha não é reescrito."""
    adventure_id = job["adventure_id"]
    card_id = job["ref_id"]
    with db.session() as conn:
        adv = db.get_adventure(conn, adventure_id)
        card = db.get_card(conn, adventure_id, card_id)
        if not adv:
            raise GameError(404, "not_found", f"adventure {adventure_id} not found")
        if not card:
            raise GameError(404, "not_found", f"card {card_id} not found")
        llm_cfg = settings_store.effective_llm_settings(conn)
        sheet = npc_assets.parse_sheet(card.get("sheet"))
        title = card.get("title") or ""
        entry = card.get("entry") or ""
        card_type = card.get("type") or ""
    wanted = [key for key in npc_assets.missing_sheet_fields(sheet)
              if key in npc_assets.LLM_SHEET_FIELDS]
    filled: list[str] = []
    if wanted:
        story = f"Story: {adv.get('title') or ''} — {(adv.get('description') or '')[:600]}"
        extra = npc_assets.fill_missing_fields(
            llm_cfg, title=title, entry=entry, sheet=sheet, missing=wanted,
            transport=transport, role="assets", story=story, card_type=card_type,
        )
        merged = npc_assets.merge_filled_fields(sheet, extra, wanted)
        filled = [key for key in wanted if merged.get(key) != sheet.get(key)]
        sheet = merged
    complete = npc_assets.ensure_sheet_complete(sheet, entry=entry, card=card)
    remaining = npc_assets.missing_sheet_fields(complete)
    with db.session() as conn:
        db.update_card(conn, adventure_id, card_id,
                       {"sheet": json.dumps(complete, ensure_ascii=False)})
    log.info("jobs: ficha de %s preenchida (%s)", card_id, ", ".join(filled) or "nada")
    return {"card_id": card_id, "filled": filled, "remaining": remaining}


# Fundo de cena: bucket SDXL 7:4. A faixa corta o centro; 896×512 ficava mole.
_BACKDROP_WIDTH = 1344
_BACKDROP_HEIGHT = 768


def backdrop_negative(img_cfg: dict, *, include_people: bool, explicit: bool) -> str:
    if explicit:
        ban = _CAST_NEGATIVE_EXPLICIT
    elif include_people:
        ban = _CAST_NEGATIVE
    else:
        ban = _BACKDROP_NEGATIVE
    return ", ".join(part for part in (img_cfg["negative_prompt"], ban) if part)


def backdrop_cache_key(img_cfg: dict, prompt: str, *, include_people: bool,
                       explicit: bool) -> str:
    ban = ""
    if explicit:
        ban = _CAST_NEGATIVE_EXPLICIT
    elif include_people:
        ban = _CAST_NEGATIVE
    else:
        ban = _BACKDROP_NEGATIVE
    return image_client.request_key(
        img_cfg, prompt, width=_BACKDROP_WIDTH, height=_BACKDROP_HEIGHT,
        negative_extra=ban,
    )


def _execute_backdrop(job: dict, transport: Any) -> dict:
    """Executa um job scene_backdrop; devolve o result (ou levanta)."""
    prompt = str((job.get("payload") or {}).get("prompt") or "").strip()
    if not prompt:
        raise GameError(500, "generation_failed", "scene backdrop job has no prompt")
    payload = job.get("payload") or {}
    with db.session() as conn:
        img_cfg = image_client.effective_settings(conn)
    if not img_cfg["enabled"]:
        raise GameError(503, "image_generation_disabled",
                        "Image generation is disabled in settings")
    ban = ""
    if payload.get("explicit"):
        ban = _CAST_NEGATIVE_EXPLICIT
    elif payload.get("include_people"):
        ban = _CAST_NEGATIVE
    else:
        ban = _BACKDROP_NEGATIVE
    negative = ", ".join(
        part for part in (img_cfg["negative_prompt"], ban) if part
    )
    cache_key = image_client.request_key(
        img_cfg, prompt, width=_BACKDROP_WIDTH, height=_BACKDROP_HEIGHT,
        negative_extra=ban,
    )
    with db.session() as conn:
        cached = db.find_cached_image(
            conn, job["adventure_id"], kind="backdrop",
            ref_id=job["ref_id"] or "", cache_key=cache_key,
        )
        if cached:
            return {"image": db.image_ref(cached), "ref_id": job["ref_id"],
                    "reused": True}
    image_id = db.new_id()
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
                cache_key=cache_key, job_id=job["id"],
            )
    except db.JobCanceled:
        _discard_png(meta["file_path"])
        raise
    except sqlite3.IntegrityError:
        _raise_if_gone(job["adventure_id"], meta["file_path"])
        raise
    return {"image": db.image_ref(image), "ref_id": job["ref_id"]}


def store_see(adventure_id: str, caption: str, meta: dict, *, image_id: str,
              prompt: str, negative_prompt: str, cache_key: str = "",
              reused_from: str = "", job_id: str | None = None) -> dict:
    """Grava a action `see` no fim da aventura e a imagem no mesmo seq (rota
    síncrona e job). Devolve o ImageResult."""
    try:
        with game_engine.appending(adventure_id) as conn:
            action = game_engine.append_action(conn, adventure_id, "see", caption)
            db.insert_image(conn, adventure_id=adventure_id, action_seq=action["seq"],
                            prompt=prompt, negative_prompt=negative_prompt,
                            file_path=meta["file_path"], provider=meta["provider"],
                            width=meta["width"], height=meta["height"],
                            image_id=image_id, cache_key=cache_key,
                            reused_from=reused_from, job_id=job_id)
            action = db.get_action(conn, action["id"])
    except sqlite3.IntegrityError:
        _raise_if_gone(adventure_id, meta["file_path"])
        raise
    return {"id": image_id, "url": f"/api/images/{image_id}", "prompt": prompt,
            "provider": meta["provider"], "action": action}


def enqueue_day_tick(conn, adventure_id: str, *, reason: str = "auto") -> dict:
    """Um dia fora de cena. Não roda o LLM aqui: o worker chama tick_day."""
    active = db.find_active_job(conn, "npc_day_tick", adventure_id)
    if active:
        return active
    return enqueue(
        conn, "npc_day_tick", adventure_id, adventure_id,
        {"reason": reason}, priority=PRIORITY_LIFE_TICK,
    )


def _execute_day_tick(job: dict, transport: Any) -> dict:
    from app import npc_life
    return npc_life.tick_day(job["adventure_id"], transport)


def _execute_see(job: dict, transport: Any) -> dict:
    """Executa um job see_image (prompt já no payload); devolve {"image": ImageResult}."""
    payload = job.get("payload") or {}
    prompt = str(payload.get("prompt") or "").strip()
    if not prompt:
        raise GameError(500, "generation_failed", "see job has no prompt")
    with db.session() as conn:
        img_cfg = image_client.effective_settings(conn)
    if not img_cfg["enabled"]:
        raise GameError(503, "image_disabled",
                        "image generation is disabled (settings.image.enabled)")
    negative_extra = str(payload.get("negative_extra") or "")
    cache_key = str(payload.get("cache_key") or "") or image_client.request_key(
        img_cfg, prompt, negative_extra=negative_extra,
        variation=payload.get("variation"))
    with db.session() as conn:
        cached = db.find_cached_image(
            conn, job["adventure_id"], kind="scene", ref_id="",
            cache_key=cache_key,
        )
    if cached:
        meta = {
            "file_path": cached["file_path"], "provider": cached["provider"],
            "width": cached["width"], "height": cached["height"],
        }
        image = store_see(
            job["adventure_id"], str(payload.get("caption") or ""), meta,
            image_id=db.new_id(), prompt=prompt,
            negative_prompt=str(payload.get("negative_prompt") or ""),
            cache_key=cache_key, reused_from=cached["id"], job_id=job["id"],
        )
        return {"image": image, "reused": True}
    image_id = db.new_id()
    meta = image_client.generate_image(
        img_cfg, prompt, adventure_id=job["adventure_id"], image_id=image_id,
        transport=transport, negative_extra=negative_extra)
    try:
        image = store_see(job["adventure_id"], str(payload.get("caption") or ""), meta,
                          image_id=image_id, prompt=prompt,
                          negative_prompt=str(payload.get("negative_prompt") or ""),
                          cache_key=cache_key, job_id=job["id"])
    except db.JobCanceled:
        _discard_png(meta["file_path"])
        raise
    return {"image": image}


_EXECUTORS: dict[str, Callable[[dict, Any], dict]] = {
    "character_portrait": _execute_portrait,
    "scene_backdrop": _execute_backdrop,
    "see_image": _execute_see,
    "npc_day_tick": _execute_day_tick,
    "card_assets": _execute_card_assets,
}


def _failed(job: dict, code: str, message: str) -> None:
    finished = _finish(job, "error", result={"code": code}, error=message)
    if finished and job["type"] == "see_image":
        events.publish(job["adventure_id"], "see_failed",
                       {"job_id": job["id"], "code": code, "message": message})


def _announce(job: dict, result: dict) -> None:
    """Evento do tipo, depois do `job_progress` done.

    Só os jobs de imagem carregam `image`; os demais (dia do mundo, ficha do
    NPC) publicam o que têm."""
    data = {"job_id": job["id"]}
    if result.get("image"):
        data["image"] = result["image"]
    if job["type"] == "character_portrait":
        events.publish(job["adventure_id"], "portrait_ready",
                       {**data, "card_id": result["card_id"]})
    elif job["type"] == "scene_backdrop":
        events.publish(job["adventure_id"], "backdrop_ready",
                       {**data, "ref_id": job["ref_id"]})
    elif job["type"] == "see_image":
        events.publish(job["adventure_id"], "see_ready", data)
    elif job["type"] == "card_assets":
        events.publish(job["adventure_id"], "card_assets_ready",
                       {**data, "card_id": result["card_id"],
                        "filled": result.get("filled") or []})


def _run_job(job: dict, transport: Any) -> None:
    """Executa um job claimed e grava done/error. Aventura apagada no meio
    termina canceled, sem evento (o canal dela já foi fechado)."""
    _progress(job)
    try:
        executor = _EXECUTORS.get(job["type"])
        if executor is None:
            raise GameError(500, "generation_failed", f"unknown job type {job['type']}")
        result = executor(job, transport)
    except db.JobCanceled:
        log.info("job %s (%s): cancelado antes da publicação", job["id"], job["type"])
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
        if _finish(job, "done", result=result):
            log.info("job %s (%s) concluído", job["id"], job["type"])
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
