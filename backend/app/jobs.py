"""Pipeline assíncrono de assets (v0.3.0) — worker asyncio sobre a tabela jobs.

Fluxo: rotas enfileiram (`enqueue`, contrato C5); o worker (único por
processo) faz claim do de menor prioridade e, nela, do mais antigo, e executa
num thread (SQLite é síncrono). Prioridades: 0 See, 1 retrato de NPC
presente, 2 fundo de cena, 3 retratos em lote, 4 dia de vida, 5 ficha,
6 clipe Wan da cena, 7 clipe de retrato. Com narrativa em curso só o See (0)
roda. Job novo com a mesma `supersede_key` cancela os pendentes antigos.
Cada mudança de estado sai no bus (`job_queued`, `job_progress`).
Tipos:
- `character_portrait` — se o card não tem portrait_prompt (e não há âncora
  nem foto de referência), o job nasce em `preparing`: o LLM (role
  image_prompt) escreve o prompt fora do worker e só então o job entra na
  fila. O worker só gera a imagem no provider efetivo (mesmo do "See") e
  vincula ao card (images kind='portrait'). Quem já tem prompt não chama o LLM.
  A primeira foto vira a âncora (`portrait_anchor_id`). Geração automática
  não substitui um retrato que já existe. Pedido explícito de regenerar
  usa img2img a partir dessa âncora, para o rosto fictício continuar o mesmo.
  Pessoa real com arquivo de referência ignora essa âncora e parte da foto
  (`likeness.portrait_init`, denoise `REFERENCE_DENOISE`). Foto enviada pelo
  usuário usa `VISUAL_REFERENCE_DENOISE` e também entra como ControlNet
  nas cenas (See e fundo), sem virar img2img da cena.
  CUDA out of memory não reenfileira o mesmo card na hora: a UI pede a cena
  de novo a cada poucos segundos, e cada erro virava outro txt2img.
- `scene_backdrop` — pinta o fundo do lugar atual (payload.prompt já pronto,
  sem LLM) em images kind='backdrop'. Wide 1344×768 (bucket SDXL).
- `see_image` — See assíncrono (C6). Nasce em `preparing`: o prompt (LLM,
  role image_prompt) roda numa thread e só então o job entra na fila do SD,
  para não prender o worker. Termina em `see_ready` ou `see_failed`.
- `scene_clip` — MP4 Wan2.2 TI2V a partir de um PNG já gravado. Prioridade 6
  (cena) ou 7 (retrato): espera a narrativa e o resto da fila. Cache por
  modelo+prompt+fonte. Supersede na mesma imagem de origem. Cancelamento
  manda /interrupt e /free no Comfy e não publica o arquivo. O evento
  terminal é `clip_ready`. Com `auto_scene` / `auto_portraits` (default
  false), o PNG de `scene_backdrop` ou `character_portrait` que acabou de
  concluir enfileira o clipe. Retrato automático só para quem está em cena
  (prioridade `<= PRIORITY_PRESENT_PORTRAIT`). Lote inicial e offscene
  (`PRIORITY_BULK_PORTRAIT`), inclusive do jogador, ficam estáticos. O
  follow-up não cancela um clipe manual ativo na mesma fonte; o pedido
  explícito continua substituindo o pendente. Falha nesse follow-up não muda
  o job de origem. Não chama narrador, voz nem LLM extra.

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
import time
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
PRIORITY_SCENE_CLIP = 6
PRIORITY_PORTRAIT_CLIP = 7

# A cena aberta pede retrato de novo assim que o job anterior termina em erro.
# Sem esta espera, um CUDA OOM vira outro txt2img a cada poucos segundos.
CUDA_OOM_BACKOFF_S = 300
CUDA_OOM_RETRY_CAP = 3

_wakeup = threading.Event()
_prepare_lock = threading.Lock()
_prepare_inflight: set[str] = set()
_PREPARE_VISIBLE_S = 120.0

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
_NUDITY_BAN = npc_assets.NUDITY_BAN
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


def _terminal_event(job: dict, status: str, result: dict | None,
                    error: str | None) -> tuple[str, dict] | None:
    """Evento terminal de mídia, gravado no mesmo commit do status do job."""
    if status == "error" and job.get("type") == "see_image":
        code = (result or {}).get("code") or "generation_failed"
        return ("see_failed", {
            "job_id": job["id"], "code": code, "message": error or "",
        })
    if status != "done" or not isinstance(result, dict):
        return None
    data = {"job_id": job["id"]}
    if result.get("image"):
        data["image"] = result["image"]
    if job["type"] == "character_portrait" and result.get("card_id"):
        return ("portrait_ready", {**data, "card_id": result["card_id"]})
    if job["type"] == "scene_backdrop":
        return ("backdrop_ready", {**data, "ref_id": job.get("ref_id") or ""})
    if job["type"] == "see_image":
        return ("see_ready", data)
    if job["type"] == "scene_clip":
        image = result.get("image") or {}
        return ("clip_ready", {
            "adventure_id": job["adventure_id"],
            "job_id": job["id"],
            "source_image_id": result.get("source_image_id") or job.get("ref_id") or "",
            "image": {
                "id": image.get("id"),
                "url": image.get("url"),
                "media_type": "video/mp4",
                "duration_ms": image.get("duration_ms"),
            },
        })
    if job["type"] == "card_assets" and result.get("card_id"):
        return ("card_assets_ready", {
            **data, "card_id": result["card_id"], "filled": result.get("filled") or [],
        })
    return None


def _finish(job: dict, status: str, *, result: dict | None = None,
            error: str | None = None, terminal: tuple[str, dict] | None = None) -> bool:
    with db.session() as conn:
        finished = db.finish_job(conn, job["id"], status, result=result, error=error)
        if finished:
            events.record(
                conn, job["adventure_id"], "job_progress",
                {"job_id": job["id"], "type": job["type"], "status": status},
                entity_id=job["id"], correlation_id=job["id"],
                idempotency_key=f"job:{job['id']}:{status}",
            )
            if terminal is not None:
                event_type, data = terminal
                events.record(
                    conn, job["adventure_id"], event_type, data,
                    entity_id=job["id"], correlation_id=job["id"],
                    idempotency_key=f"job:{job['id']}:{event_type}",
                )
    return finished

# ------------------------------------------------------------- enfileirar
def enqueue(conn, type_: str, adventure_id: str, ref_id: str | None = None,
            payload: dict | None = None, priority: int = PRIORITY_BACKDROP,
            supersede_key: str | None = None, *, status: str = "pending") -> dict:
    """Job novo (C5). Fila e eventos saem no commit de quem chamou."""
    job = db.insert_job(conn, adventure_id=adventure_id, type=type_, ref_id=ref_id or "",
                        payload=payload, priority=priority,
                        supersede_key=supersede_key or "", status=status)
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


def _portrait_request_state(conn, adventure_id: str, card: dict,
                            *, force_reference: bool = False) -> str:
    adventure = db.get_adventure(conn, adventure_id) or {}
    img_cfg = image_client.effective_settings(conn)
    anchor_id = "" if force_reference else str(
        card.get("portrait_anchor_id") or card.get("portrait_image_id") or "")
    anchor_row = db.get_image(conn, anchor_id) if anchor_id else None
    anchor_bytes = _portrait_bytes(anchor_row) if anchor_row else None
    reference_bytes, _ = likeness.portrait_init(
        card, anchor_bytes, anchor_denoise=PORTRAIT_VARIANT_DENOISE,
        force_reference=force_reference)
    return npc_assets.portrait_state_key(
        card, img_cfg, image_style=adventure.get("image_style") or "",
        reference_bytes=reference_bytes or b"",
    )


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
    if card and not variant_key:
        variant_key = _portrait_request_state(
            conn, adventure_id, card, force_reference=force_reference)
    if card and card.get("portrait_image_id") and not replace and not variant_key:
        return None
    active = db.find_active_job(conn, "character_portrait", card_id)
    if active:
        active_key = str((active.get("payload") or {}).get("state_key") or "")
        if variant_key and active_key != variant_key \
                and active.get("status") in ("preparing", "pending"):
            db.cancel_job(conn, active["id"])
        elif active_key == variant_key:
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
    status = "preparing" if _portrait_needs_prompt(card, payload) else "pending"
    if status == "preparing" and not image_client.effective_settings(conn).get("enabled"):
        status = "pending"
    job = enqueue(conn, "character_portrait", adventure_id, card_id, payload,
                  priority=priority, status=status)
    if job and variant_key:
        _set_portrait_state(conn, adventure_id, card_id, variant_key, job["id"])
    if status == "preparing":
        _arm_portrait_prepare(job["id"])
    return job


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


def _reference_blobs(job: dict, *, people: bool) -> list[bytes]:
    """Recarrega as âncoras persistidas. A ordem e os hashes são os do enqueue.

    Sem a chave no payload, um fundo sem gente segue sem referência. Cena com
    gente sem ids, arquivo sumido ou pixels diferentes falha: não pinta parcial.
    """
    payload = job.get("payload") or {}
    if "reference_image_ids" not in payload:
        if people:
            raise likeness.portrait_required()
        return []
    ids = [str(item) for item in (payload.get("reference_image_ids") or [])]
    if people and not ids:
        raise likeness.portrait_required()
    with db.session() as conn:
        blobs = (
            likeness.load_reference_images(conn, job["adventure_id"], ids)
            if ids else []
        )
    found = likeness.reference_hashes(blobs)
    if "reference_hashes" in payload and list(payload.get("reference_hashes") or []) != found:
        raise likeness.portrait_required()
    return blobs


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


def _fail_preparing(job: dict, code: str, message: str) -> None:
    """Erro de preparação. Cancelado no meio permanece cancelado."""
    with db.session() as conn:
        current = db.get_job(conn, job["id"])
    if current is not None and current["status"] == "preparing":
        _failed(job, code, message)


def _fail_see(job: dict, code: str, message: str) -> None:
    _fail_preparing(job, code, message)


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
    """Melhor esforço para interromper o render de um job running cancelado."""
    if job["type"] not in ("character_portrait", "scene_backdrop", "see_image",
                           "location_image", "scene_clip"):
        return False
    try:
        with db.session() as conn:
            cfg = image_client.effective_settings(conn)
        if job["type"] == "scene_clip":
            image_client.interrupt_comfy(cfg, transport)
        else:
            if (cfg.get("provider") or "sdwebui") not in ("sdwebui", "comfyui"):
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
    if not file_path:
        return
    (db.REPO_ROOT / file_path).unlink(missing_ok=True)


def _discard_render(file_path: str) -> None:
    """Apaga o MP4 publicado e o arquivo parcial do mesmo id."""
    _discard_png(file_path)
    if file_path:
        _discard_png(f"{file_path}.partial")


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


def _portrait_needs_prompt(card: dict | None, payload: dict) -> bool:
    """O executor antigo só chamava o LLM sem prompt, âncora e referência."""
    if not card:
        return False
    if str(card.get("portrait_prompt") or "").strip():
        return False
    if str(payload.get("init_image_id") or ""):
        return False
    if likeness.reference_images([card]):
        return False
    return True


def _claim_prepare(job_id: str) -> bool:
    with _prepare_lock:
        if job_id in _prepare_inflight:
            return False
        _prepare_inflight.add(job_id)
        return True


def _release_prepare(job_id: str) -> None:
    with _prepare_lock:
        _prepare_inflight.discard(job_id)


def _prepare_portrait_job(job: dict, transport: Any) -> None:
    """Uma preparação: prompt (role image_prompt) e preparing → pending.

    Cancelamento ou supersede no meio não promove. Sem card, sem aventura
    ou com a imagem desligada, promove mesmo assim para o executor reportar
    o erro sem ocupar a GPU com o LLM.
    """
    with db.session() as conn:
        current = db.get_job(conn, job["id"])
    if current is None or current["status"] != "preparing":
        return
    payload = dict(current.get("payload") or {})
    try:
        with db.session() as conn:
            adv = db.get_adventure(conn, current["adventure_id"])
            card = db.get_card(conn, current["adventure_id"], current["ref_id"])
            needs = False
            llm_cfg = None
            if adv and card:
                img_cfg = image_client.effective_settings(conn)
                if img_cfg.get("enabled") and _portrait_needs_prompt(card, payload):
                    llm_cfg = settings_store.effective_llm_settings(conn)
                    needs = True
        if needs:
            _build_portrait_prompt(adv, card, llm_cfg, transport)
    except (GameError, LlmError) as exc:
        _fail_preparing(current, exc.code, exc.message)
        return
    except Exception:
        log.exception("portrait %s: prompt falhou", current["id"])
        _fail_preparing(current, "generation_failed",
                        "could not build the image prompt")
        return
    with db.session() as conn:
        promoted = db.promote_job(conn, current["id"], payload)
    if promoted is None:
        return
    _progress(promoted)
    wake()


def _prepare_outstanding_portraits(transport: Any) -> None:
    """Completa os retratos em `preparing`. Uma preparação por job."""
    with db.session() as conn:
        rows = conn.execute(
            "SELECT id FROM jobs WHERE type='character_portrait' AND status='preparing'"
            " ORDER BY priority ASC, created_at ASC, rowid ASC",
        ).fetchall()
    for row in rows:
        job_id = row["id"]
        if not _claim_prepare(job_id):
            continue
        try:
            with db.session() as conn:
                job = db.get_job(conn, job_id)
            if job is not None:
                _prepare_portrait_job(job, transport)
        finally:
            _release_prepare(job_id)


def _portrait_prepare_thread(job_id: str) -> None:
    try:
        deadline = time.monotonic() + _PREPARE_VISIBLE_S
        job = None
        while True:
            try:
                with db.session() as conn:
                    job = db.get_job(conn, job_id)
            except sqlite3.OperationalError:
                job = None
            if job is not None:
                break
            if time.monotonic() >= deadline:
                raise TimeoutError("portrait preparation job did not become visible")
            time.sleep(0.02)
        _prepare_portrait_job(job, None)
    except Exception:
        log.exception("portrait %s: preparação não armou", job_id)
        try:
            with db.session() as conn:
                stuck = db.get_job(conn, job_id)
            if stuck is not None and stuck["status"] == "preparing":
                _fail_preparing(stuck, "generation_failed",
                                "could not prepare the portrait job")
        except Exception:
            log.exception("portrait %s: fallback de falha também errou", job_id)
    finally:
        _release_prepare(job_id)


def _arm_portrait_prepare(job_id: str) -> None:
    """Uma thread por job quando o worker está ligado.

    Nos testes o worker fica desligado e `process_pending` prepara no
    transport falso, sem rede e sem segunda chamada.
    """
    if not worker_enabled():
        return
    if not _claim_prepare(job_id):
        return
    thread = threading.Thread(
        target=_portrait_prepare_thread, args=(job_id,),
        name="portrait-prompt", daemon=True,
    )
    try:
        thread.start()
    except Exception:
        _release_prepare(job_id)
        raise


def _portrait_bytes(image: dict) -> bytes:
    """PNG da foto já gravada (caminho relativo ao repo ou absoluto)."""
    try:
        return likeness.read_stored_image_bytes(image)
    except OSError as exc:
        raise GameError(503, "generation_failed",
                        "canonical portrait file is missing") from exc


def _execute_portrait(job: dict, transport: Any) -> dict:
    """Pinta um character_portrait já preparado. Não chama o LLM."""
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
        if anchor_id:
            anchor = db.get_image(conn, anchor_id)
            if not anchor:
                raise GameError(503, "generation_failed",
                                "canonical portrait image is missing")
    state_key = str(payload.get("state_key") or "")
    prompt = (npc_assets.portrait_variant_prompt(card) if anchor_id
              else str(card.get("portrait_prompt") or "").strip()
              or npc_assets.portrait_variant_prompt(card))
    final = " ".join(p for p in (adv.get("image_style") or "",
                                 prompt,
                                 img_cfg["style_suffix"] or "") if p).strip()
    prefix = (likeness.real_person_prefix(card)
              if img_cfg.get("provider") == "sdwebui" else "")
    if prefix:
        final = f"{prefix}, {final}" if final else prefix
    look = npc_assets.sheet_context(card.get("sheet") or "")
    if look:
        if state_key:
            final = f"{final}. Current look, follow the story: {look}"
        else:
            final = f"{final}. Mandatory appearance, do not contradict: {look}"
    from app.scene import is_explicit as _scene_explicit
    portrait_ban = ""
    if not _scene_explicit(final):
        kind = npc_assets.creature_kind(card)
        if kind and "natural skin texture" in final:
            final = final.replace(
                "natural skin texture",
                "scales, hide or fur, not human skin",
            )
        if kind:
            final = f"{final}, {kind}, {npc_assets.CREATURE_LOCK}"
        else:
            final = f"{final}, {npc_assets.CLOTHED_LOCK}"
        portrait_ban = _NUDITY_BAN
    init_width = init_height = None
    anchor_bytes = None
    if anchor:
        anchor_bytes = _portrait_bytes(anchor)
        init_width, init_height = anchor.get("width"), anchor.get("height")
        final = f"{final}, same person, keep the same face".strip()
    references = likeness.portrait_pixels(
        card, None if force_reference else anchor_bytes)
    init_bytes, denoise = likeness.portrait_init(
        card, None if force_reference else anchor_bytes,
        anchor_denoise=PORTRAIT_DENOISE if replace else PORTRAIT_VARIANT_DENOISE,
        force_reference=force_reference,
    )
    identity_bytes = init_bytes
    if force_reference:
        init_bytes, denoise = None, None
    if init_bytes and not anchor:
        final = f"{final}, same person, keep the same face".strip()
    if img_cfg.get("provider") == "comfyui":
        init_bytes = None
        denoise = 1.0 if references else None
    elif force_reference and references:
        init_bytes = references[0]
        denoise = 0.12
    if not state_key:
        state_key = npc_assets.portrait_state_key(
            card, img_cfg, image_style=adv.get("image_style") or "",
            reference_bytes=identity_bytes or b"",
        )
    cache_key = state_key
    with db.session() as conn:
        cached = None if replace else db.find_cached_image(
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
    meta = image_client.generate_image(
        img_cfg, final, adventure_id=adv["id"],
        image_id=image_id, transport=transport,
        width=init_width, height=init_height,
        init_image=init_bytes,
        denoising_strength=denoise,
        reference_images=(references if img_cfg.get("provider") == "comfyui" else None),
        negative_extra=portrait_ban,
    )
    if not anchor_id:
        generated = _portrait_bytes({"file_path": meta["file_path"]})
        anchored_card = {**card, "portrait_anchor_id": image_id,
                         "portrait_image_id": image_id}
        next_reference, _ = likeness.portrait_init(
            anchored_card, generated, anchor_denoise=PORTRAIT_VARIANT_DENOISE)
        stored_key = npc_assets.portrait_state_key(
            anchored_card, img_cfg, image_style=adv.get("image_style") or "",
            reference_bytes=next_reference or b"",
        )
    try:
        with db.session() as conn:
            image = db.insert_image(
                conn, adventure_id=adv["id"], action_seq=-1,
                file_path=meta["file_path"], prompt=final,
                negative_prompt=", ".join(
                    part for part in (img_cfg["negative_prompt"], portrait_ban) if part
                ),
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
                       explicit: bool,
                       reference_images: list[bytes] | None = None) -> str:
    ban = ""
    if explicit:
        ban = _CAST_NEGATIVE_EXPLICIT
    elif include_people:
        ban = _CAST_NEGATIVE
    else:
        ban = _BACKDROP_NEGATIVE
    return image_client.request_key(
        img_cfg, prompt, width=_BACKDROP_WIDTH, height=_BACKDROP_HEIGHT,
        negative_extra=ban, reference_images=reference_images,
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
    references = (
        _reference_blobs(job, people=bool(payload.get("include_people")))
        if img_cfg.get("provider") == "comfyui" else []
    )
    cache_key = str(payload.get("cache_key") or "") or backdrop_cache_key(
        img_cfg, prompt,
        include_people=bool(payload.get("include_people")),
        explicit=bool(payload.get("explicit")),
        reference_images=references or None,
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
        negative_extra=ban, reference_images=references or None,
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


_SCENE_CLIP_MOTION = (
    "Coherent continuous motion, stable camera, no jump cut. "
    "Keep the same place, lighting and the identity of anyone already visible."
)
_PORTRAIT_CLIP_HOLD = "Preserve the same face, clothes and framing. Stable camera."
_PORTRAIT_CLIP_STILL = (
    "No stated action, emotion or pose. Do not invent a gesture. "
    "Subtle environmental motion only."
)
_CLIP_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
_RELATION_STAGE_KEYS = ("trust", "fear", "affinity", "stage", "alignment")


def _card_sheet(card: dict) -> dict:
    raw = card.get("sheet") if isinstance(card, dict) else None
    if isinstance(raw, dict):
        return raw
    return npc_assets.parse_sheet(raw or "")


def _scene_clip_prompt(job: dict) -> str:
    """Prompt do fundo mais movimento coerente. Sem segundo LLM."""
    base = " ".join(str((job.get("payload") or {}).get("prompt") or "").split())
    if not base:
        return _SCENE_CLIP_MOTION
    return f"{base}. {_SCENE_CLIP_MOTION}"


def _relation_stage_bits(sheet: dict, roster: list[dict]) -> list[str]:
    """Estágio de cada relação já gravada. Sem peso, histórico ou seq."""
    from app import npc_bonds, progression

    relations = sheet.get("relations") if isinstance(sheet.get("relations"), dict) else {}
    by_id = {item.get("id"): item for item in roster}
    bits: list[str] = []
    for other_id, raw in relations.items():
        if not isinstance(raw, dict):
            continue
        if not any(key in raw for key in _RELATION_STAGE_KEYS):
            continue
        label = raw.get("stage") if isinstance(raw.get("stage"), str) else ""
        if not label and isinstance(raw.get("alignment"), str):
            label = str(raw.get("alignment") or "")
        label = " ".join(label.split())
        if label and any(ch.isdigit() for ch in label):
            label = ""
        if not label:
            details = progression.relation_with(sheet, str(other_id))
            label = npc_bonds.alignment_from_bonds({
                "trust": details["trust"],
                "relationship": details["affinity"],
                "distrust": 0,
                "fear": details["fear"],
            })
        other = by_id.get(other_id)
        name = " ".join(str((other or {}).get("title") or "").split())
        if name:
            bits.append(f"toward {name}: {label}")
        elif label:
            bits.append(f"relationship stage: {label}")
    return bits


def _portrait_clip_prompt(card: dict, roster: list[dict] | None = None) -> str:
    """Movimento do retrato a partir do card e do estado visível. Sem LLM."""
    from app import npc_bonds

    sheet = _card_sheet(card)
    name = " ".join(str(card.get("title") or "").split())
    identity = " ".join(bit for bit in npc_assets.visual_bits(card) if bit)
    reaction = npc_assets.visual_reaction(sheet)
    stage = npc_assets.alignment_stage(sheet)
    if not stage and isinstance(sheet.get("bonds"), dict):
        stage = npc_bonds.alignment_from_bonds(npc_bonds.read_bonds(sheet))
    parts: list[str] = []
    if name:
        parts.append(f"Portrait of {name}.")
    if identity:
        parts.append(identity + ".")
    if stage:
        parts.append(f"alignment stage: {stage}.")
    relations = _relation_stage_bits(sheet, roster or [])
    if relations:
        parts.append("Relationship stages: " + "; ".join(relations) + ".")
    motion = [
        f"{key}: {reaction[key]}"
        for key in ("action", "emotion", "pose")
        if reaction.get(key)
    ]
    if motion:
        parts.append(
            "Animate only this visible state, coherent motion: "
            + "; ".join(motion) + "."
        )
    else:
        parts.append(_PORTRAIT_CLIP_STILL)
    parts.append(_PORTRAIT_CLIP_HOLD)
    return " ".join(" ".join(parts).split())


def enqueue_scene_clip(conn, adventure_id: str, source_image_id: str, prompt: str, *,
                       cache_key: str = "",
                       priority: int | None = None,
                       kind: str = "",
                       supersede: bool = True) -> dict:
    """Mesma fonte+prompt ativo é deduplicado; prompt novo substitui o pendente.

    A rota manual omite `priority`, `kind` e `supersede` e continua na
    prioridade 6: outro prompt cancela o pendente da mesma fonte.
    `supersede=False` (follow-up automático) devolve o job ativo mesmo com
    prompt diferente e não apaga o pedido. `kind="portrait"` sem prioridade
    explícita fica atrás da cena.
    """
    active = db.find_active_job(conn, "scene_clip", source_image_id)
    if active and not supersede:
        return active
    if active and cache_key and \
            str((active.get("payload") or {}).get("cache_key") or "") == cache_key:
        return active
    if priority is None:
        priority = (PRIORITY_PORTRAIT_CLIP if kind == "portrait"
                    else PRIORITY_SCENE_CLIP)
    payload = {
        "prompt": prompt,
        "source_image_id": source_image_id,
        "cache_key": cache_key,
    }
    label = " ".join(str(kind or "").split())
    if label:
        payload["kind"] = label
    return enqueue(
        conn, "scene_clip", adventure_id, source_image_id, payload,
        priority=int(priority),
        supersede_key=f"clip:{adventure_id}:{source_image_id}",
    )


def _portrait_auto_clip(job: dict) -> bool:
    """Só retratos reativos/presentes; todo lote fica estático."""
    try:
        priority = int(job.get("priority"))
    except (TypeError, ValueError):
        return False
    return priority <= PRIORITY_PRESENT_PORTRAIT


def _enqueue_clip_followup(job: dict, result: dict) -> None:
    """Fonte é o image.id do result. Não chama LLM."""
    job_type = job.get("type")
    if job_type not in ("character_portrait", "scene_backdrop"):
        return
    image_id = str(((result or {}).get("image") or {}).get("id") or "")
    if not image_id:
        return
    with db.session() as conn:
        cfg = image_client.effective_settings(conn)
        video = image_client.video_settings(cfg)
        if job_type == "scene_backdrop" and not video.get("auto_scene"):
            return
        if job_type == "character_portrait" and not video.get("auto_portraits"):
            return
        image = db.get_image(conn, image_id)
        if image is None or str(image.get("adventure_id") or "") != str(
                job.get("adventure_id") or ""):
            return
        raw = likeness.read_stored_image_bytes(image)
        if not raw.startswith(_CLIP_PNG_MAGIC):
            return
        if job_type == "scene_backdrop":
            prompt = _scene_clip_prompt(job)
            clip_kind = "scene"
            clip_priority = PRIORITY_SCENE_CLIP
        else:
            card = db.get_card(conn, job["adventure_id"], job.get("ref_id") or "")
            if not card or not _portrait_auto_clip(job):
                return
            prompt = _portrait_clip_prompt(card, db.list_cards(conn, job["adventure_id"]))
            clip_kind = "portrait"
            clip_priority = PRIORITY_PORTRAIT_CLIP
        if not str(prompt or "").strip():
            return
        enqueue_scene_clip(
            conn, job["adventure_id"], image["id"], prompt,
            cache_key=image_client.clip_request_key(cfg, prompt, raw),
            priority=clip_priority, kind=clip_kind, supersede=False,
        )


def _schedule_clip_followup(job: dict, result: dict | None) -> None:
    """Depois do done. Exceção aqui não reabre o job de origem."""
    try:
        _enqueue_clip_followup(job, result or {})
    except Exception:
        log.exception(
            "job %s (%s): clipe automático não entrou na fila",
            job.get("id"), job.get("type"),
        )


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
    references = (
        _reference_blobs(job, people=False)
        if img_cfg.get("provider") == "comfyui" else []
    )
    cache_key = str(payload.get("cache_key") or "") or image_client.request_key(
        img_cfg, prompt, negative_extra=negative_extra,
        variation=payload.get("variation"), reference_images=references or None)
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
        transport=transport, negative_extra=negative_extra,
        reference_images=references or None)
    try:
        image = store_see(job["adventure_id"], str(payload.get("caption") or ""), meta,
                          image_id=image_id, prompt=prompt,
                          negative_prompt=str(payload.get("negative_prompt") or ""),
                          cache_key=cache_key, job_id=job["id"])
    except db.JobCanceled:
        _discard_png(meta["file_path"])
        raise
    return {"image": image}

def _execute_location_image(job: dict, transport: Any) -> dict:
    """Gera a imagem de um local e liga ao card no mapa."""
    from app import world_map
    payload = job.get("payload") or {}
    prompt = str(payload.get("prompt") or "").strip()
    if not prompt:
        raise GameError(500, "generation_failed", "location image job has no prompt")
    with db.session() as conn:
        img_cfg = image_client.effective_settings(conn)
    if not img_cfg["enabled"]:
        raise GameError(503, "image_generation_disabled", "Image generation is disabled")
    cache_key = str(payload.get("cache_key") or image_client.request_key(
        img_cfg, prompt, width=1344, height=768))
    image_id = db.new_id()
    meta = image_client.generate_image(
        img_cfg, prompt, adventure_id=job["adventure_id"],
        image_id=image_id, transport=transport, width=1344, height=768,
    )
    with db.session() as conn:
        image = db.insert_image(
            conn, adventure_id=job["adventure_id"], action_seq=-1,
            file_path=meta["file_path"], prompt=prompt,
            negative_prompt=img_cfg["negative_prompt"],
            provider=meta["provider"], width=meta["width"], height=meta["height"],
            image_id=image_id, kind="location", ref_id=job["ref_id"],
            cache_key=cache_key, job_id=job["id"],
        )
        world_map.attach_location_image(
            conn, job["adventure_id"], job["ref_id"], image["id"],
            state_key=cache_key[:40],
        )
    return {"image": db.image_ref(image), "card_id": job["ref_id"]}



def _clip_source_png(job: dict) -> tuple[dict, bytes]:
    payload = job.get("payload") or {}
    source_id = str(payload.get("source_image_id") or job.get("ref_id") or "")
    with db.session() as conn:
        source = db.get_image(conn, source_id) if source_id else None
    if (not source or source.get("adventure_id") != job["adventure_id"]
            or str(source.get("media_type") or "image/png").split(";", 1)[0].lower()
            not in ("image/png", "image/x-png")):
        raise GameError(404, "not_found", "source image not found")
    try:
        raw = likeness.read_stored_image_bytes(source)
    except (OSError, FileNotFoundError) as exc:
        raise GameError(404, "not_found", "source image not found") from exc
    if not raw.startswith(b"\x89PNG\r\n\x1a\n"):
        raise GameError(404, "not_found", "source image not found")
    return source, raw


def _execute_scene_clip(job: dict, transport: Any) -> dict:
    """Gera o MP4 Wan e grava kind=clip. Cancelamento não deixa arquivo."""
    payload = job.get("payload") or {}
    prompt = " ".join(str(payload.get("prompt") or "").split())
    if not prompt:
        raise GameError(500, "generation_failed", "scene clip job has no prompt")
    with db.session() as conn:
        img_cfg = image_client.effective_settings(conn)
    if not img_cfg["enabled"]:
        raise GameError(503, "image_disabled",
                        "image generation is disabled (settings.image.enabled)")
    source, raw = _clip_source_png(job)
    source_id = source["id"]
    cache_key = image_client.clip_request_key(img_cfg, prompt, raw)
    with db.session() as conn:
        cached = db.find_cached_image(
            conn, job["adventure_id"], kind="clip", ref_id=source_id,
            cache_key=cache_key,
        )
    reused_from = ""
    meta: dict
    if cached and cached.get("media_type") == "video/mp4":
        try:
            ready = likeness.stored_image_path(cached["file_path"]).is_file()
        except OSError:
            ready = False
        if ready:
            meta = {
                "file_path": cached["file_path"], "provider": cached["provider"],
                "width": cached["width"], "height": cached["height"],
                "media_type": "video/mp4", "duration_ms": cached["duration_ms"],
            }
            reused_from = cached["id"]
        else:
            cached = None
    if not reused_from:
        image_id = db.new_id()
        meta = image_client.generate_clip(
            img_cfg, prompt, adventure_id=job["adventure_id"], image_id=image_id,
            source_png=raw, transport=transport)
    else:
        image_id = db.new_id()
    try:
        with db.session() as conn:
            image = db.insert_image(
                conn, adventure_id=job["adventure_id"], action_seq=-1,
                file_path=meta["file_path"], prompt=prompt,
                negative_prompt="", provider=meta.get("provider") or "comfyui",
                width=meta.get("width"), height=meta.get("height"),
                image_id=image_id, kind="clip", ref_id=source_id,
                cache_key=cache_key, reused_from=reused_from, job_id=job["id"],
                media_type="video/mp4", duration_ms=meta.get("duration_ms"),
                source_image_id=source_id,
            )
    except db.JobCanceled:
        if not reused_from:
            _discard_render(meta["file_path"])
        raise
    except sqlite3.IntegrityError:
        if not reused_from:
            _discard_render(meta["file_path"])
        _raise_if_gone(job["adventure_id"], meta["file_path"])
        raise
    return {
        "source_image_id": source_id,
        "image": {
            "id": image["id"],
            "url": image["url"],
            "media_type": "video/mp4",
            "duration_ms": image["duration_ms"],
        },
    }


_EXECUTORS: dict[str, Callable[[dict, Any], dict]] = {
    "character_portrait": _execute_portrait,
    "card_assets": _execute_card_assets,
    "scene_backdrop": _execute_backdrop,
    "see_image": _execute_see,
    "npc_day_tick": _execute_day_tick,
    "location_image": _execute_location_image,
    "scene_clip": _execute_scene_clip,
}


def _failed(job: dict, code: str, message: str) -> None:
    result = {"code": code}
    _finish(job, "error", result=result, error=message,
            terminal=_terminal_event(job, "error", result, message))


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
        if _finish(job, "done", result=result,
                   terminal=_terminal_event(job, "done", result, None)):
            log.info("job %s (%s) concluído", job["id"], job["type"])
            if job["type"] in ("character_portrait", "scene_backdrop"):
                _schedule_clip_followup(job, result)


def process_pending(transport: Any = None, *, should_pause=None,
                    prepare: bool | None = None) -> int:
    """Drena a fila em ordem de prioridade (worker e, diretamente, testes).

    should_pause, quando passado, é consultado antes de cada claim: enquanto
    devolver verdade (narrativa em curso), só o See sai da fila e o resto
    espera em pending (a GPU fica com o LLM da narrativa).

    Retrato sem prompt fica em `preparing` e não é claim. O worker de
    produção não monta esse prompt. Com o worker desligado, os testes
    completam a preparação aqui, no mesmo `transport`, antes do claim.
    """
    if prepare is None:
        prepare = not worker_enabled()
    if prepare:
        _prepare_outstanding_portraits(transport)
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
        failed_rows = [dict(row) for row in conn.execute(
            "SELECT id, adventure_id, type FROM jobs "
            "WHERE type='see_image' AND status IN ('preparing','pending','running')"
        )]
        running_rows = [dict(row) for row in conn.execute(
            "SELECT id, adventure_id, type FROM jobs "
            "WHERE type<>'see_image' AND status='running'"
        )]
        requeued, failed = db.requeue_interrupted_jobs(conn)
        preparing_portraits = [
            row["id"] for row in conn.execute(
                "SELECT id FROM jobs WHERE type='character_portrait'"
                " AND status='preparing' ORDER BY priority ASC, created_at ASC",
            )
        ]
        for row in failed_rows:
            if db.get_adventure(conn, row["adventure_id"]) is None:
                continue
            events.record(
                conn, row["adventure_id"], "job_progress",
                {"job_id": row["id"], "type": row["type"], "status": "error"},
                entity_id=row["id"], correlation_id=row["id"],
                idempotency_key=f"job:{row['id']}:error",
            )
            events.record(
                conn, row["adventure_id"], "see_failed",
                {"job_id": row["id"], "code": "interrupted",
                 "message": "interrupted by a restart"},
                entity_id=row["id"], correlation_id=row["id"],
                idempotency_key=f"job:{row['id']}:see_failed",
            )
        for row in running_rows:
            if db.get_adventure(conn, row["adventure_id"]) is None:
                continue
            events.record(
                conn, row["adventure_id"], "job_progress",
                {"job_id": row["id"], "type": row["type"], "status": "pending"},
                entity_id=row["id"], correlation_id=row["id"],
                idempotency_key=f"job:{row['id']}:requeued",
            )
    for job_id in preparing_portraits:
        _arm_portrait_prepare(job_id)
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
                prepare=False,
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
