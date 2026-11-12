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
  fila. O worker traduz o prompt final para inglês e gera a imagem no provider
  efetivo (mesmo do "See") e vincula ao card (images kind='portrait'). Quem já tem prompt não chama o LLM
  de construção do retrato.
  A primeira foto vira a âncora (`portrait_anchor_id`). Geração automática
  não substitui um retrato que já existe. Pedido explícito de regenerar
  usa img2img a partir dessa âncora, para o rosto fictício continuar o mesmo.
  Pessoa real com arquivo de referência ignora essa âncora e parte da foto
  (`likeness.portrait_init`, denoise `REFERENCE_DENOISE`). Foto enviada pelo
  usuário usa `VISUAL_REFERENCE_DENOISE` e também entra como ControlNet
  nas cenas (See e fundo), sem virar img2img da cena.
  CUDA out of memory não reenfileira o mesmo card na hora: a UI pede a cena
  de novo a cada poucos segundos, e cada erro virava outro txt2img.
- `scene_backdrop` — monta o fundo do lugar atual sem LLM em `scene._prompt`;
  o executor traduz o texto final para inglês e grava images kind='backdrop'.
  Wide 1344×768 (bucket SDXL).
- `see_image` — See assíncrono (C6). Nasce em `preparing`: o prompt (LLM,
  role image_prompt) roda numa thread e só então o job entra na fila do SD,
  para não prender o worker. Termina em `see_ready` ou `see_failed`.
- `scene_clip` — MP4 image-to-video (LTXV; Wan se engine=wan) a partir de um PNG já gravado. Prioridade 3
  (cena) ou 7 (retrato): cenas passam antes de retratos em lote e enriquecimento.
  modelo+prompt+fonte. Supersede na mesma imagem de origem. Cancelamento
  manda /interrupt e /free no Comfy e não publica o arquivo. O evento
  terminal é `clip_ready`. Com `auto_scene` / `auto_portraits` (default
  false), o PNG de `scene_backdrop` ou `character_portrait` que acabou de
  concluir enfileira o clipe. Retrato automático só para quem está em cena
  (prioridade `<= PRIORITY_PRESENT_PORTRAIT`). Lote inicial e offscene
  (`PRIORITY_BULK_PORTRAIT`), inclusive do jogador, ficam estáticos. O
  follow-up não cancela um clipe manual ativo na mesma fonte; o pedido
  explícito continua substituindo o pendente. Falha nesse follow-up não muda
  o job de origem. Não chama narrador nem voz; o executor traduz o prompt
  final para inglês antes de gerar o clipe. Atualizações de backdrop, inclusive
  cache hits, não interrompem clipes automáticos; ações novas preemptam à parte.
- `character_turnaround` — gera mestre de corpo inteiro e ficha multivista; o
  Pillow monta o split com o rosto canônico, sem outra inferência. Usa o split
  no giro 360°, extrai 0/90/180/270 com OpenCV e monta a folha 2×2. Publica todos
  os assets e atualiza `reference.turnaround` no mesmo commit; termina em `turnaround_ready`. Cancelamento interrompe o Comfy
  e remove todos os arquivos ainda não publicados.

Aventura apagada no meio do render: o PNG é descartado e o job termina
cancelado. O worker é desligável por env (AIDUNGEON_JOBS_WORKER=0) para
testes determinísticos — a suíte chama `process_pending()` diretamente.
Um reaper independente, a cada 5 s, fecha `running` cujo prazo venceu.
O prazo conta do mais recente entre `started_at` e `payload.heartbeat_at`.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import random
import re
import sqlite3
import threading
import time
from collections import deque
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from app import (db, events, game_engine, image_client, image_english, likeness, llm_client,
                    look_lock, npc_assets, outfit_lock, settings_store, turnaround)
from app.errors import GameError
from app.image_client import ImageError
from app.llm_client import LlmError

log = logging.getLogger("aidungeon.jobs")

PRIORITY_SEE = 0
PRIORITY_PRESENT_PORTRAIT = 1
PRIORITY_BACKDROP = 2
PRIORITY_SCENE_CLIP = 3
PRIORITY_BULK_PORTRAIT = 4
PRIORITY_LIFE_TICK = 5
PRIORITY_CARD_ASSETS = 6
PRIORITY_PORTRAIT_CLIP = 7

# A cena aberta pede retrato de novo assim que o job anterior termina em erro.
# Sem esta espera, um CUDA OOM vira outro txt2img a cada poucos segundos.
CUDA_OOM_BACKOFF_S = 300
CUDA_OOM_RETRY_CAP = 3

# O See assíncrono segura o job em `preparing` enquanto os retratos do elenco
# são gerados. Sem isso, clicar em See logo depois de uma mudança de roupa
# falhava com `portrait_required` por poucos segundos.
SEE_PORTRAIT_WAIT_S = float(os.environ.get("AIDUNGEON_SEE_PORTRAIT_WAIT", "90"))
SEE_PORTRAIT_POLL_S = 0.5

_wakeup = threading.Event()
_prepare_lock = threading.Lock()
_prepare_inflight: set[str] = set()
_prepare_queue: deque[str] = deque()
_prepare_queued: set[str] = set()
_prepare_running = False
_PREPARE_VISIBLE_S = 120.0
_HEARTBEAT_INTERVAL_S = 5.0
_REAPER_INTERVAL_S = 5.0
_PREEMPT_IMAGE_REASON = "preempted by queued image"
_IMAGE_JOB_TYPES = (
    "character_portrait", "scene_backdrop", "see_image", "location_image",
)
_heartbeat_lock = threading.Lock()
_heartbeat_mono: dict[str, float] = {}
_clip_watch_mono: dict[str, float] = {}
_clip_interrupted: set[str] = set()

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
# Cena nua: "costume change" vestia de novo, mas a roupa em si continua
# negativa. Sem ela o Flux2 mantém calça e thong mesmo com "completely naked".
_CAST_NEGATIVE_EXPLICIT = (
    "extra person, crowd, duplicate face, wrong hair, "
    + npc_assets.IDENTITY_NEGATIVE
    + ", " + npc_assets.EXPLICIT_CLOTHING_NEGATIVE
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
    if job["type"] == "character_turnaround":
        return ("turnaround_ready", {
            **data,
            "card_id": result["card_id"],
            "sheet": result["sheet"],
            "clip": result["clip"],
            "views": result["views"],
        })
    if job["type"] == "turnaround_clip":
        if not result.get("clip"):
            return None
        return ("turnaround_ready", {
            "job_id": job["id"], "card_id": result["card_id"], "clip": result["clip"],
        })
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


def cancel_adventure_jobs(conn, adventure_id: str, reason: str = "adventure revision changed") -> list[dict]:
    """Cancel active media work and publish terminal progress in the undo transaction."""
    rows = conn.execute(
        "SELECT * FROM jobs WHERE adventure_id=? AND status IN ('preparing','pending','running')",
        (adventure_id,),
    ).fetchall()
    canceled = []
    for row in rows:
        job = db.job_to_dict(row)
        result = db.cancel_job(conn, job["id"], reason=reason)
        if not result:
            continue
        canceled.append(result)
        events.record(conn, adventure_id, "job_progress",
                      {"job_id": job["id"], "type": job["type"], "status": "canceled"},
                      entity_id=job["id"], correlation_id=job["id"],
                      idempotency_key=f"job:{job['id']}:canceled")
        if job["type"] == "see_image":
            events.record(conn, adventure_id, "see_failed",
                          {"job_id": job["id"], "code": "superseded",
                           "message": reason}, entity_id=job["id"],
                          correlation_id=job["id"],
                          idempotency_key=f"job:{job['id']}:see_failed")
    return canceled

# ------------------------------------------------------------- enfileirar
def enqueue(conn, type_: str, adventure_id: str, ref_id: str | None = None,
            payload: dict | None = None, priority: int = PRIORITY_BACKDROP,
            supersede_key: str | None = None, *, status: str = "pending") -> dict:
    """Job novo (C5). Fila e eventos saem no commit de quem chamou."""
    job = db.insert_job(conn, adventure_id=adventure_id, type=type_, ref_id=ref_id or "",
                        payload=payload, priority=priority,
                        supersede_key=supersede_key or "", status=status)
    superseded = db.supersede_jobs(conn, supersede_key, job["id"]) if supersede_key else []
    # Backdrop refreshes frequently reuse cache. They must not cancel an
    # automatic clip before that lookup; explicit turn actions preempt separately.
    if type_ in _IMAGE_JOB_TYPES and type_ != "scene_backdrop":
        _cancel_running_automatic_clips(conn)
    elif type_ == "scene_clip":
        # O clipe da cena é o que o jogador está vendo; um giro 360° em
        # andamento (até 9 min, três sementes) cede a GPU e volta para a fila.
        _cancel_running_turnaround_clips(conn)
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
    # job_queued is itself an event; use the post-enqueue snapshot as origin.
    conn.execute(
        "UPDATE jobs SET source_revision=COALESCE((SELECT state_revision"
        " FROM world_event_counters WHERE adventure_id=?),0) WHERE id=?",
        (adventure_id, job["id"]))
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


AUTO_RETRY_BASE_S = 30.0
AUTO_RETRY_MAX_S = 600.0
_AUTO_RETRY_WINDOW = 6


def auto_retry_hold(conn, type_: str, ref_id: str, *,
                    now: datetime | None = None) -> str | None:
    """Espera entre tentativas automáticas depois de erros seguidos no alvo.

    A cena pede o retrato de quem não tem a cada leitura. Sem esta espera, um
    erro que se repete (tradução do prompt, provider fora) virava um job novo
    por leitura, cada um ocupando um slot do LLM. O primeiro erro tenta de novo
    na hora; do segundo em diante espera 30 s, 60 s, ... até 10 min. Um job que
    não é erro zera a sequência. O botão de regenerar não espera.
    """
    recent = _recent_jobs(conn, type_, ref_id, _AUTO_RETRY_WINDOW)
    streak = 0
    for job in recent:
        if job["status"] != "error":
            break
        streak += 1
    if streak < 2:
        return None
    wait = min(AUTO_RETRY_BASE_S * (2 ** (streak - 2)), AUTO_RETRY_MAX_S)
    moment = now or datetime.now(timezone.utc)
    finished = _utc_stamp(recent[0].get("finished_at") or recent[0].get("created_at"))
    if finished is None:
        return None
    remaining = wait - (moment - finished).total_seconds()
    if remaining > 0:
        return f"{streak} failed attempts; automatic retry waits {max(1, int(remaining))}s"
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
                     variant_key: str = "",
                     automatic: bool | None = None) -> dict | None:
    """Enfileira retrato do card (dedupe: reaproveita job ativo, que passa à
    frente se o pedido novo for mais urgente).

    `automatic` marca o pedido que ninguém clicou (a cena, o "gerar todos"):
    ele respeita `auto_retry_hold`. Sem o argumento, vale `not replace`.

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
    if automatic is None:
        automatic = not replace
    if automatic and auto_retry_hold(conn, "character_portrait", card_id):
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


# O plot essentials é o único lugar que guarda o protagonista quando a
# aventura foi criada sem ficha de jogador (import antigo, cenário gerado
# antes do card do jogador). Sem este card não há rosto nem sexo travados, e
# "Caio" saía como um estranho ou ficava de fora da cena.
_PROTAGONIST_FIELD = re.compile(
    r"(?i)\b(name|nome|role|papel|class|classe|appearance|aparência|aparencia)"
    r"\s*:\s*([^;\[\]\n]+)"
)
_PROTAGONIST_KEYS = {
    "name": "name", "nome": "name",
    "role": "role", "papel": "role", "class": "role", "classe": "role",
    "appearance": "appearance", "aparência": "appearance", "aparencia": "appearance",
}
_PROTAGONIST_TRIM = " \t\r\n.,;:—–-"
_PLAYER_OUTFIT = re.compile(
    r"(?i)\b(?:wearing|dressed\s+in|clad\s+in|vestindo|usando)\b\s*(.+)$"
)


def _fold_name(text: str) -> str:
    return " ".join(str(text or "").casefold().split())


def protagonist_from_plot(plot: str) -> dict:
    """Nome, papel e aparência do protagonista no plot essentials.

    O primeiro bloco `[Name: ...]` é o jogador; os seguintes são NPCs. Também
    entende `Nome:`/`Papel:`/`Aparência:` e o prefixo "Protagonista — ...".
    """
    text = " ".join(str(plot or "").split())
    if not text:
        return {}
    blocks = re.findall(r"\[([^\]]+)\]", text) or [text]
    for block in blocks:
        fields: dict[str, str] = {}
        for match in _PROTAGONIST_FIELD.finditer(block):
            key = _PROTAGONIST_KEYS.get(match.group(1).casefold())
            value = match.group(2).strip(_PROTAGONIST_TRIM)
            if key and value and key not in fields:
                fields[key] = value
        name = fields.get("name") or ""
        if 2 <= len(name) <= 80 and 1 <= len(name.split()) <= 6:
            return fields
    return {}


def _player_sheet_from_protagonist(info: dict) -> dict:
    """Ficha mínima para o retrato e as cenas: aparência, corpo e roupa."""
    from app import stats as stats_mod

    appearance = " ".join(str(info.get("appearance") or "").split())
    role = " ".join(str(info.get("role") or "").split())
    body = npc_assets.without_outfit(appearance) if appearance else ""
    outfit = ""
    match = _PLAYER_OUTFIT.search(appearance)
    if match:
        outfit = match.group(1).strip(_PROTAGONIST_TRIM)
    sheet: dict[str, Any] = {
        "status": "alive",
        "stats": stats_mod.estimate_stats(" ".join((body, appearance)), role),
        "notes": (
            "Criado automaticamente a partir dos essenciais do cenário; "
            "edite a ficha para ajustar."
        ),
    }
    if role:
        sheet["class"] = role
    if body:
        sheet["appearance"] = body
    elif appearance:
        sheet["appearance"] = appearance
    if outfit:
        sheet["clothing"] = outfit
    return sheet


def ensure_player_card(conn, adventure: dict) -> dict | None:
    """Garante o card do protagonista em aventuras sem ficha de jogador.

    Não sobrescreve ficha existente. Adota um card de personagem com o mesmo
    nome (o assistant já remove o protagonista dos NPCs, então isso só evita
    duplicata em dados antigos). Enfileira o retrato; sem ele o See falha com
    `portrait_required` e a cena não sai com um rosto inventado.
    """
    adventure_id = str((adventure or {}).get("id") or "")
    username = str((adventure or {}).get("owner") or "").strip()
    if not adventure_id or not username:
        return None
    current = db.player_character(conn, adventure_id, username)
    if current is not None:
        return current
    info = protagonist_from_plot((adventure or {}).get("plot_essentials") or "")
    name = info.get("name") or ""
    if not name or not (info.get("appearance") or info.get("role")):
        return None
    sheet = _player_sheet_from_protagonist(info)
    entry = " — ".join(
        part for part in (
            " ".join(str(info.get("role") or "").split()),
            " ".join(str(info.get("appearance") or "").split()),
        ) if part
    )
    folded = _fold_name(name)
    twin = next((
        card for card in db.list_cards(conn, adventure_id)
        if (card.get("type") or "") == "character"
        and not (card.get("player_username") or "").strip()
        and _fold_name(card.get("title")) == folded
    ), None)
    if twin is not None:
        merged = npc_assets.parse_sheet(twin.get("sheet") or "")
        for key, value in sheet.items():
            merged.setdefault(key, value)
        fields: dict[str, Any] = {
            "player_username": username,
            "use_for_character_creation": True,
            "sheet": json.dumps(merged, ensure_ascii=False),
        }
        if not (twin.get("entry") or "").strip() and entry:
            fields["entry"] = entry
        card = db.update_card(conn, adventure_id, twin["id"], fields)
    else:
        card = db.upsert_player_character(
            conn, adventure_id, username, title=name, entry=entry,
            sheet=json.dumps(sheet, ensure_ascii=False),
        )
    if card is not None and not likeness.reference_image_id(card):
        enqueue_portrait(conn, adventure_id, card["id"])
    return card



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


def _reference_blobs(job: dict, *, people: bool, provider: str) -> list[bytes]:
    """Recarrega âncoras persistidas e replaneja quando o provider mudou.

    Sem a chave no payload, um fundo sem gente segue sem referência. Cena com
    gente sem ids, arquivo sumido ou pixels diferentes falha: não pinta parcial.
    """
    payload = job.get("payload") or {}
    provider = str(provider or "sdwebui")
    planned_provider = str(payload.get("reference_provider") or provider)
    if planned_provider != provider:
        from app import scene

        card_ids = [str(item) for item in (payload.get("cast_card_ids") or [])]
        if people and not card_ids:
            raise likeness.portrait_required()
        with db.session() as conn:
            cards = [db.get_card(conn, job["adventure_id"], card_id)
                     for card_id in card_ids]
            if any(card is None for card in cards):
                raise likeness.portrait_required()
            plan = scene.cast_reference_plan(
                conn, job["adventure_id"], cards,
                provider=provider, explicit=_reference_faces(payload),
            )
        ids = list(plan["reference_image_ids"])
        blobs = list(plan["blobs"])
        payload["reference_image_ids"] = ids
        payload["reference_hashes"] = list(plan["reference_hashes"])
        payload["reference_provider"] = provider
    else:
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
    if _reference_faces(payload):
        return likeness.identity_face_pngs(blobs)
    return blobs


def _reference_faces(payload: dict) -> bool:
    """Referência só de rosto: cena explícita com alguém nu. Job antigo sem o
    campo segue o `explicit`, como antes."""
    if "reference_faces" in payload:
        return bool(payload.get("reference_faces"))
    return bool(payload.get("explicit"))


def start_see(adventure_id: str, payload: dict, plan: Callable[[], dict]) -> dict:
    """See assíncrono (C6): grava o job em `preparing` e devolve na hora; `plan`
    (o prompt, via LLM) roda numa thread e completa o payload com `prompt`,
    `negative_extra`, `negative_prompt` e `caption`."""
    with db.session() as conn:
        job = db.insert_job(conn, adventure_id=adventure_id, type="see_image",
                            payload=payload, priority=PRIORITY_SEE, status="preparing")
        _cancel_running_automatic_clips(conn)
        events.record(
            conn, adventure_id, "job_queued",
            {"job_id": job["id"], "type": "see_image", "ref_id": ""},
            entity_id=job["id"], correlation_id=job["id"],
            idempotency_key=f"job:{job['id']}:queued",
        )
    threading.Thread(target=_prepare_see, args=(job, plan), name="see-prompt",
                     daemon=True).start()
    return job


def _has_active_portrait(adventure_id: str) -> bool:
    with db.session() as conn:
        row = conn.execute(
            "SELECT 1 FROM jobs WHERE adventure_id=? AND type='character_portrait'"
            " AND status IN ('preparing','pending','running') LIMIT 1",
            (adventure_id,),
        ).fetchone()
    return row is not None


def _wait_for_portraits(adventure_id: str, deadline: float) -> tuple[bool, bool]:
    """Espera os retratos ativos do elenco. Devolve (viu_job_ativo, pode_tentar).

    `pode_tentar` é False quando o prazo acabou; nesse caso o See falha com a
    mensagem original em vez de ficar preparando para sempre.
    """
    seen_active = False
    while True:
        if not _has_active_portrait(adventure_id):
            return seen_active, time.monotonic() < deadline
        seen_active = True
        if time.monotonic() >= deadline:
            return seen_active, False
        time.sleep(SEE_PORTRAIT_POLL_S)


def _prepare_see(job: dict, plan: Callable[[], dict]) -> None:
    deadline = time.monotonic() + SEE_PORTRAIT_WAIT_S
    retried_without_job = False
    while True:
        try:
            ready = {**job["payload"], **plan()}
            break
        except (GameError, LlmError) as exc:
            waitable = (
                isinstance(exc, GameError)
                and exc.code == "portrait_required"
                and time.monotonic() < deadline
            )
            if waitable:
                # `_require_cast` já enfileirou os retratos que faltam.
                seen_active, can_retry = _wait_for_portraits(job["adventure_id"], deadline)
                if can_retry and (seen_active or not retried_without_job):
                    retried_without_job = not seen_active
                    continue
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


def cancel(conn, job_id: str, *, reason: str | None = None) -> dict | None:
    """Cancela um job ativo e grava seu evento no mesmo commit."""
    job = db.cancel_job(conn, job_id, reason=reason)
    if job is not None:
        events.record(
            conn, job["adventure_id"], "job_progress",
            {"job_id": job["id"], "type": job["type"], "status": job["status"]},
            entity_id=job["id"], correlation_id=job["id"],
            idempotency_key=f"job:{job['id']}:{job['status']}",
        )
    return job


def _cancel_running_automatic_clips(conn) -> list[dict]:
    """Cancela clipes automáticos running. O interrupt espera o commit.

    O giro 360° (minutos no Wan) cede a GPU do mesmo jeito, mas volta para a
    fila no fim dela: as vistas já estão publicadas e o clipe só melhora o
    preview.
    """
    rows = conn.execute(
        "SELECT id FROM jobs WHERE status='running' AND ("
        "type='turnaround_clip' OR (type='scene_clip'"
        " AND COALESCE(json_extract(payload, '$.automatic'), 0) = 1))",
    ).fetchall()
    canceled = []
    for row in rows:
        job = cancel(conn, row["id"], reason=_PREEMPT_IMAGE_REASON)
        if job is not None:
            canceled.append(job)
            if job["type"] == "turnaround_clip":
                _requeue_turnaround_clip(conn, job)
    return canceled


# Um turno novo pode interromper o mesmo giro várias vezes; depois disso ele
# desiste e fica o giro da ficha.
_TURNAROUND_CLIP_MAX_REQUEUES = 3


def _requeue_turnaround_clip(conn, job: dict) -> dict | None:
    payload = dict(job.get("payload") or {})
    payload.pop("heartbeat_at", None)
    requeues = int(payload.get("requeues") or 0) + 1
    if requeues > _TURNAROUND_CLIP_MAX_REQUEUES:
        log.info("job %s: giro 360° desistiu depois de %d interrupções",
                 job["id"], requeues - 1)
        return None
    payload["requeues"] = requeues
    return enqueue(
        conn, "turnaround_clip", job["adventure_id"], job.get("ref_id") or "",
        payload, priority=PRIORITY_PORTRAIT_CLIP,
        supersede_key=job.get("supersede_key") or None,
    )


def _cancel_running_turnaround_clips(conn) -> list[dict]:
    """Giro 360° running cede a GPU a qualquer ação nova e volta para a fila."""
    rows = conn.execute(
        "SELECT id FROM jobs WHERE type='turnaround_clip' AND status='running'",
    ).fetchall()
    canceled = []
    for row in rows:
        job = cancel(conn, row["id"], reason="preempted for a new action")
        if job is not None:
            canceled.append(job)
            _requeue_turnaround_clip(conn, job)
    return canceled


def interrupt_image(job: dict, transport: Any = None) -> bool:
    """Melhor esforço para interromper o render de um job running cancelado."""
    if job["type"] not in ("character_portrait", "scene_backdrop", "see_image",
                           "location_image", "scene_clip", "character_turnaround",
                           "turnaround_clip"):
        return False
    is_clip = job["type"] in ("scene_clip", "turnaround_clip")
    if is_clip:
        with _heartbeat_lock:
            if job["id"] in _clip_interrupted:
                return False
            _clip_interrupted.add(job["id"])
    completed = False
    try:
        with db.session() as conn:
            cfg = image_client.effective_settings(conn)
        if job["type"] in ("scene_clip", "character_turnaround", "turnaround_clip"):
            image_client.interrupt_comfy(cfg, transport)
        else:
            if (cfg.get("provider") or "sdwebui") not in ("sdwebui", "comfyui"):
                return False
            image_client.interrupt_sdwebui(cfg, transport)
        completed = True
    except ImageError as exc:
        log.warning("job %s: interrupt do WebUI falhou: %s", job["id"], exc.message)
        return False
    except Exception as exc:
        log.warning("job %s: interrupt do WebUI indisponível (%s)",
                    job["id"], type(exc).__name__)
        return False
    finally:
        if is_clip and not completed:
            with _heartbeat_lock:
                _clip_interrupted.discard(job["id"])
    return True


def _interval_open(bucket: dict[str, float], job_id: str, *, force: bool = False) -> bool:
    now = time.monotonic()
    with _heartbeat_lock:
        last = bucket.get(job_id)
        if not force and last is not None and now - last < _HEARTBEAT_INTERVAL_S:
            return False
        bucket[job_id] = now
        return True


def _interval_reset(bucket: dict[str, float], job_id: str) -> None:
    with _heartbeat_lock:
        bucket.pop(job_id, None)


def _forget_job_runtime(job_id: str) -> None:
    with _heartbeat_lock:
        _heartbeat_mono.pop(job_id, None)
        _clip_watch_mono.pop(job_id, None)
        _clip_interrupted.discard(job_id)


def _write_heartbeat(job_id: str, *, force: bool = False) -> bool:
    """No máximo um write a cada 5 s. O prazo do reaper não usa este carimbo."""
    if not _interval_open(_heartbeat_mono, job_id, force=force):
        return False
    try:
        with db.session() as conn:
            return db.touch_job(conn, job_id, db.utcnow())
    except sqlite3.OperationalError:
        log.warning("job %s: heartbeat não gravou", job_id)
        _interval_reset(_heartbeat_mono, job_id)
        return False


def _interrupt_clip_if_stopped(job: dict, transport: Any) -> None:
    """Lê o status já commitado, no máximo a cada 5 s, e interrompe se parou."""
    job_id = job["id"]
    with _heartbeat_lock:
        if job_id in _clip_interrupted:
            return
    if not _interval_open(_clip_watch_mono, job_id):
        return
    try:
        with db.session() as conn:
            current = db.get_job(conn, job_id)
    except sqlite3.OperationalError:
        _interval_reset(_clip_watch_mono, job_id)
        return
    if current is not None and current["status"] == "running":
        return
    if interrupt_image(job, transport):
        with _heartbeat_lock:
            _clip_interrupted.add(job_id)


def _generation_progress(job: dict, transport: Any, *, watch_cancel: bool):
    def progress(*_args, **_kwargs) -> None:
        _write_heartbeat(job["id"])
        if watch_cancel:
            _interrupt_clip_if_stopped(job, transport)
    return progress


def preempt_automatic_clips(adventure_id: str, transport: Any = None) -> int:
    """Uma ação nova interrompe só clipes automáticos, nunca o pedido manual.

    O worker é serial: sem preempção, um clipe longo impediria o See e o
    próximo turno. O PNG já pronto continua visível; o próximo evento visual
    cria outro clipe. O cancelamento do running impede publicação tardia.
    """
    running: list[dict] = []
    canceled_count = 0
    with db.session() as conn:
        for job in _cancel_running_turnaround_clips(conn):
            canceled_count += 1
            running.append(job)
        rows = conn.execute(
            "SELECT id FROM jobs WHERE adventure_id=? AND type='scene_clip'"
            " AND status IN ('preparing','pending','running')",
            (adventure_id,),
        ).fetchall()
        for row in rows:
            job = db.get_job(conn, row["id"])
            if not job or not (job.get("payload") or {}).get("automatic"):
                continue
            canceled = cancel(conn, job["id"], reason="preempted for a new action")
            if canceled is None:
                continue
            canceled_count += 1
            if job.get("status") == "running":
                running.append(canceled)
    for job in running:
        interrupt_image(job, transport)
    return canceled_count


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
    with image_english.IMAGE_LLM_LOCK:
        prompt = llm_client.chat_completion(
            llm_cfg, messages, role="image_prompt",
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


def _pop_queued_portrait() -> str | None:
    """Só com `_prepare_lock` já tomado."""
    while _prepare_queue:
        job_id = _prepare_queue.popleft()
        _prepare_queued.discard(job_id)
        if job_id in _prepare_inflight:
            continue
        return job_id
    return None


def _want_prepare_worker() -> bool:
    """Marca a thread global se há fila e ninguém narrando. Exige o lock."""
    global _prepare_running
    if _prepare_running or game_engine.is_any_generation_active():
        return False
    if not _prepare_queue:
        return False
    _prepare_running = True
    return True


def _start_prepare_worker() -> None:
    global _prepare_running
    thread = threading.Thread(
        target=_portrait_prepare_worker, name="portrait-prompt", daemon=True,
    )
    try:
        thread.start()
    except Exception:
        with _prepare_lock:
            _prepare_running = False
        raise


def _portrait_worker_stopped() -> None:
    """A thread saiu: no máximo uma sucessora, e só sem narrativa."""
    global _prepare_running
    start = False
    with _prepare_lock:
        _prepare_running = False
        start = _want_prepare_worker()
    if start:
        _start_prepare_worker()


def _run_one_portrait_prepare(job_id: str) -> None:
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


def _portrait_prepare_worker() -> None:
    """Uma thread global. Cada volta pega no máximo o próximo da FIFO."""
    try:
        while True:
            with _prepare_lock:
                if game_engine.is_any_generation_active():
                    return
                job_id = _pop_queued_portrait()
                if job_id is None:
                    return
                _prepare_inflight.add(job_id)
            try:
                _run_one_portrait_prepare(job_id)
            finally:
                _release_prepare(job_id)
    finally:
        _portrait_worker_stopped()


def _arm_portrait_prepare(job_id: str) -> None:
    """Enfileira o retrato. A thread só nasce sem narrativa e sem outra preparação.

    Com o worker desligado, `process_pending` prepara no transport do teste.
    """
    if not worker_enabled():
        return
    start = False
    with _prepare_lock:
        if job_id in _prepare_inflight or job_id in _prepare_queued:
            return
        _prepare_queue.append(job_id)
        _prepare_queued.add(job_id)
        start = _want_prepare_worker()
    if start:
        _start_prepare_worker()


def resume_portrait_prepare() -> None:
    """Solta no máximo a próxima preparação quando a narrativa termina.

    `release_generation_lock` chama isto no sucesso e no erro. Sem worker,
    não faz nada: os testes continuam síncronos.
    """
    if not worker_enabled():
        return
    start = False
    with _prepare_lock:
        start = _want_prepare_worker()
    if start:
        _start_prepare_worker()


def _portrait_bytes(image: dict) -> bytes:
    """PNG da foto já gravada (caminho relativo ao repo ou absoluto)."""
    try:
        return likeness.read_stored_image_bytes(image)
    except OSError as exc:
        raise GameError(503, "generation_failed",
                        "canonical portrait file is missing") from exc


def _execute_portrait(job: dict, transport: Any) -> dict:
    """Pinta um character_portrait preparado; traduz o prompt final antes do provider."""
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
        llm_cfg = settings_store.effective_llm_settings(conn)
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
    # Traços lidos da referência (cabelo, óculos, pele): valem em qualquer
    # estado da história, que só troca a roupa.
    identity = look_lock.identity_text(look_lock.current(card), level="face")
    if identity:
        final = f"{final}. Identity from the reference, keep exactly: {identity}"
    # Sem roupa do momento (`current_outfit`) e sem cena explícita, o retrato
    # usa a mesma roupa travada do turnaround, só as peças que o busto mostra.
    # Só lê a trava: o worker de retrato não chama o LLM (quem cria é o
    # turnaround, que já traduz com ele).
    if not _card_sheet(card).get("current_outfit") and not _scene_explicit(final):
        visible = outfit_lock.visible_text(outfit_lock.current(card))
        if visible:
            final = f"{final}. {visible}"
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
    final = image_english.render_english(
        final, transport=transport, llm_cfg=llm_cfg)
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
        reference_images=(
            references if img_cfg.get("provider") == "comfyui"
            else references[:1] if img_cfg.get("provider") == "sdwebui"
            else None
        ),
        negative_extra=portrait_ban,
        progress=_generation_progress(job, transport, watch_cancel=False),
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


def _backdrop_cfg(img_cfg: dict, *, explicit: bool) -> dict:
    """CFG 2.0 na cena nua: com CFG 1.0 o negativo de roupa é ignorado."""
    return image_client.with_flux2_cfg(img_cfg, 2.0 if explicit else None)


def backdrop_negative(img_cfg: dict, *, include_people: bool, explicit: bool) -> str:
    base = npc_assets.negative_without_nudity_ban(img_cfg["negative_prompt"]) if explicit else img_cfg["negative_prompt"]
    if explicit:
        ban = _CAST_NEGATIVE_EXPLICIT
    elif include_people:
        ban = _CAST_NEGATIVE
    else:
        ban = _BACKDROP_NEGATIVE
    return ", ".join(part for part in (base, ban) if part)


def backdrop_cache_key(img_cfg: dict, prompt: str, *, include_people: bool,
                       explicit: bool,
                       reference_images: list[bytes] | None = None) -> str:
    img_cfg = _backdrop_cfg(img_cfg, explicit=explicit)
    ban = ""
    if explicit:
        ban = _CAST_NEGATIVE_EXPLICIT
    elif include_people:
        ban = _CAST_NEGATIVE
    else:
        ban = _BACKDROP_NEGATIVE
    return image_client.request_key(
        {
            **img_cfg,
            "negative_prompt": (
                npc_assets.negative_without_nudity_ban(img_cfg["negative_prompt"])
                if explicit else img_cfg["negative_prompt"]
            ),
        },
        prompt, width=_BACKDROP_WIDTH, height=_BACKDROP_HEIGHT,
        negative_extra=ban, reference_images=reference_images,
    )


def _read_missing_looks(job: dict, transport: Any, llm_cfg: dict) -> None:
    """Lê o look de quem está no fundo e ainda não tem um.

    O prompt do fundo nasce no pedido da cena, que não chama o LLM. Depois de
    uma versão nova do leitor ninguém tem look, e o fundo de produção saiu
    sem a linha do Caio (sem barba, com os dentes do ladrão). Este fundo
    ainda usa o prompt antigo; a leitura fica gravada e o próximo pedido da
    cena já monta o prompt com os traços de cada um.
    """
    ids = [str(item) for item in (job.get("payload") or {}).get("cast_card_ids") or []]
    if not ids:
        return
    try:
        with db.session() as conn:
            cards = [db.get_card(conn, job["adventure_id"], card_id) for card_id in ids]
        look_lock.ensure_many(job["adventure_id"], [card for card in cards if card],
                              transport=transport, llm_cfg=llm_cfg)
    except Exception as exc:  # leitura é melhoria: nunca derruba o fundo
        log.warning("job %s: look do elenco indisponível: %s", job["id"], type(exc).__name__)


def _execute_backdrop(job: dict, transport: Any) -> dict:
    """Executa um job scene_backdrop; devolve o result (ou levanta)."""
    prompt = str((job.get("payload") or {}).get("prompt") or "").strip()
    if not prompt:
        raise GameError(500, "generation_failed", "scene backdrop job has no prompt")
    payload = job.get("payload") or {}
    with db.session() as conn:
        img_cfg = image_client.effective_settings(conn)
        llm_cfg = settings_store.effective_llm_settings(conn)
    if not img_cfg["enabled"]:
        raise GameError(503, "image_generation_disabled",
                        "Image generation is disabled in settings")
    _read_missing_looks(job, transport, llm_cfg)
    img_cfg = _backdrop_cfg(img_cfg, explicit=bool(payload.get("explicit")))
    ban = ""
    if payload.get("explicit"):
        ban = _CAST_NEGATIVE_EXPLICIT
    elif payload.get("include_people"):
        ban = _CAST_NEGATIVE
    else:
        ban = _BACKDROP_NEGATIVE
    base_negative = (
        npc_assets.negative_without_nudity_ban(img_cfg["negative_prompt"])
        if payload.get("explicit") else img_cfg["negative_prompt"]
    )
    negative = ", ".join(
        part for part in (base_negative, ban) if part
    )
    img_cfg["negative_prompt"] = base_negative
    references = (
        _reference_blobs(
            job, people=bool(payload.get("include_people")),
            provider=str(img_cfg.get("provider") or "sdwebui"),
        )
        if img_cfg.get("provider") in ("comfyui", "sdwebui") else []
    )
    prompt = image_english.render_english(
        prompt, transport=transport, llm_cfg=llm_cfg)
    cache_key = backdrop_cache_key(
        img_cfg, prompt,
        include_people=bool(payload.get("include_people")),
        explicit=bool(payload.get("explicit")),
        reference_images=references or None,
    )
    effective_ref = f"{job['adventure_id']}:{cache_key[:40]}"
    with db.session() as conn:
        cached = db.find_cached_image(
            conn, job["adventure_id"], kind="backdrop",
            ref_id=effective_ref, cache_key=cache_key,
        )
        if cached:
            return {"image": db.image_ref(cached), "ref_id": effective_ref,
                    "reused": True}
    image_id = db.new_id()
    meta = image_client.generate_image(
        img_cfg, prompt, adventure_id=job["adventure_id"],
        image_id=image_id, transport=transport,
        width=_BACKDROP_WIDTH, height=_BACKDROP_HEIGHT,
        negative_extra=ban, reference_images=references or None,
        expected_people=int(payload.get("expected_people") or 0) or None,
        progress=_generation_progress(job, transport, watch_cancel=False),
    )
    try:
        with db.session() as conn:
            image = db.insert_image(
                conn, adventure_id=job["adventure_id"], action_seq=-1,
                file_path=meta["file_path"], prompt=prompt,
                negative_prompt=negative,
                provider=meta["provider"], width=meta["width"], height=meta["height"],
                image_id=image_id, kind="backdrop", ref_id=effective_ref,
                cache_key=cache_key, job_id=job["id"],
            )
    except db.JobCanceled:
        _discard_png(meta["file_path"])
        raise
    except sqlite3.IntegrityError:
        _raise_if_gone(job["adventure_id"], meta["file_path"])
        raise
    return {"image": db.image_ref(image), "ref_id": effective_ref}


def store_see(adventure_id: str, caption: str, meta: dict, *, image_id: str,
              prompt: str, negative_prompt: str, cache_key: str = "",
              reused_from: str = "", job_id: str | None = None) -> dict:
    """Grava a action `see` no fim da aventura e a imagem no mesmo seq (rota
    síncrona e job). Devolve o ImageResult."""
    try:
        with game_engine.appending(adventure_id) as conn:
            if job_id:
                # Undo e retry cancelam o job na própria transação
                # (cancel_adventure_jobs); o status é a única prova de que o
                # cânone ainda vale. `state_revision` sobe a cada evento,
                # inclusive o job_progress deste job, e não serve de comparação.
                job = db.get_job(conn, job_id)
                if not job or job["status"] not in db.JOB_ACTIVE:
                    raise db.JobCanceled(job_id)
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
def _default_look(card: dict) -> dict:
    """O card sem a roupa do momento: o turnaround é a aparência padrão."""
    sheet = _card_sheet(card)
    if not sheet.get("current_outfit"):
        return card
    sheet = {key: value for key, value in sheet.items() if key != "current_outfit"}
    return {**card, "sheet": json.dumps(sheet, ensure_ascii=False)}


def _turnaround_identity(card: dict, *, with_look: bool = True) -> str:
    """Quem é a pessoa, para todo prompt do turnaround (aparência padrão).

    `with_look=False` devolve só o nome e o texto do card, para traduzir; os
    traços da referência (`look_lock`) já estão em inglês e entram literais.
    """
    card = _default_look(card)
    # Com look, só o nome: os traços da referência mandam sobre o texto do card.
    looked = {str(card.get("id"))} if likeness._has_look(card) else set()
    identity = likeness._opening_clause([card], name_only=looked)
    if not identity:
        bits = npc_assets.scene_bits(card)
        name = str(card.get("title") or "character").strip()
        identity = f"{name}: {'; '.join(bits)}" if bits else name
    identity = identity[:700]
    if with_look:
        look = likeness._look_bits([card], level="full")
        if look:
            identity = f"{identity}. {look}"
    return identity


_TURNAROUND_SIDES = (
    "Keep every distinctive and asymmetrical detail on the character's own "
    "LEFT or RIGHT side exactly as shown; never mirror or move it."
)
_TURNAROUND_STUDIO = (
    "One single person only, alone in the frame. Clean seamless light grey studio "
    "background, soft even lighting, sharp focus, 85mm lens. No text, no labels, "
    "no logos, no extra panels, no split screen."
)
_TURNAROUND_FULL_BODY = (
    "The whole figure from the top of the hair to the soles of the shoes is inside "
    "the frame, with empty floor visible below the shoes and empty space above the "
    "head; the figure fills about 85 percent of the image height, centred."
)
# Cada vista é uma imagem própria (antes era um painel recortado da ficha).
_TURNAROUND_VIEWS = {
    "front": (
        "Full-body photograph of this exact character standing in a relaxed neutral "
        "pose, body and face turned straight to the camera, arms relaxed at the sides, "
        "feet slightly apart."
    ),
    "profile": (
        "Full-body photograph of this exact character standing in a strict side "
        "profile view: head, body and feet turned 90 degrees so the character's right "
        "side faces the camera and the face points to the left edge of the image, "
        "arms relaxed at the sides."
    ),
    "back": (
        "Full-body photograph of this exact character seen from directly behind: the "
        "back of the head, the back and the heels face the camera, the face is not "
        "visible, arms relaxed at the sides."
    ),
    # "Head-and-shoulders portrait" com a roupa inteira no prompt saía de meio
    # corpo (calça e tênis puxam o quadro para baixo): 6 de 6 na L4. Com o
    # enquadramento pedido em centímetros de imagem e só as peças visíveis da
    # trava, 4 de 4 saíram cabeça e ombros.
    "face": (
        "Close-up headshot photograph of this exact character looking straight at the "
        "camera: only the head, the neck and the top of the shoulders, cropped at the "
        "collarbones, with a little empty space above the hair; the face fills about "
        "half of the image height. Calm neutral expression, sharp eyes."
    ),
}


def _turnaround_prompts(card: dict, lock: dict | None = None, *,
                        identity: str | None = None) -> dict[str, str]:
    """Mestre, as quatro vistas e o giro, com a mesma identidade e a roupa
    travada repetida literalmente.

    `identity` (nome e texto do card, já traduzidos) vem do executor; sem ela
    sai do card, como no enqueue. Os traços da referência entram por pintura:
    tudo no mestre e no close-up, o que distingue de longe nas vistas e no giro.
    """
    if identity is None:
        identity = _turnaround_identity(card, with_look=False)
    base = identity

    def who(level: str) -> str:
        look = likeness._look_bits([_default_look(card)], level=level)
        return f"{base}. {look}" if look else base

    if lock is None:
        lock = outfit_lock.current(_default_look(card))
    outfit = outfit_lock.full_text(lock)
    outfit = f" {outfit}" if outfit else ""
    keep = f"Keep {who('views')}, body, outfit and accessories exactly identical to the references."
    prompts = {
        "master": (
            f"Create a full-body master portrait of this exact character. {who('full')}. "
            "Standing upright in a relaxed neutral pose, facing the camera, arms at the "
            "sides, feet slightly apart. Preserve the face, hair, body, outfit and "
            f"accessories exactly.{outfit} {_TURNAROUND_SIDES} {_TURNAROUND_FULL_BODY} "
            f"{_TURNAROUND_STUDIO}"
        ),
    }
    visible = outfit_lock.visible_text(lock)
    visible = f" {visible}." if visible else ""
    studio = _TURNAROUND_STUDIO
    if look_lock.has_lettering((lock or {}).get("top") or ""):
        # "No text" apagava as palavras da estampa que a roupa travada pede.
        studio = studio.replace(
            "No text, no labels, no logos,",
            "No captions, no labels, no watermark, no lettering other than the print on the top,")
        prompts["master"] = prompts["master"].replace(_TURNAROUND_STUDIO, studio)
    for kind, pose in _TURNAROUND_VIEWS.items():
        if kind == "face":
            prompts[kind] = (
                f"{pose} Keep {who('face')}, face, hair and accessories exactly identical to "
                f"the references.{visible} {_TURNAROUND_SIDES} {studio}"
            )
            continue
        view_outfit, view_studio = outfit, studio
        top = (lock or {}).get("top") or ""
        if kind == "back" and look_lock.has_print(top):
            # De costas a estampa do peito não aparece: com a peça descrita
            # inteira, a vista de costas saía com "OCEAN CLUB" nas costas.
            plain = {**lock, "top": f"{look_lock.without_print(top)}, plain back with no print"}
            view_outfit = " " + outfit_lock.full_text(plain, front_note=False)
            view_studio = _TURNAROUND_STUDIO
        prompts[kind] = (
            f"{pose} {keep}{view_outfit} {_TURNAROUND_SIDES} {_TURNAROUND_FULL_BODY} "
            f"{view_studio}"
        )
    prompts["clip"] = (
        "Turntable shot of one person standing alone in a seamless grey studio. "
        f"Keep {who('views')}, body, outfit and accessories exactly unchanged.{outfit} "
        "The person slowly turns the whole body in place through exactly one full "
        "360 degree turn, always in the same direction: front, the right side in "
        "profile, the back, the left side in profile, then facing the camera again "
        "exactly as in the first frame. Feet stay planted on the floor, legs straight, "
        "smooth constant rotation, static locked-off camera, calm neutral expression, "
        f"no talking. {_TURNAROUND_SIDES} No extra people, no text."
    )
    return prompts

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
                       supersede: bool = True,
                       automatic: bool = False) -> dict:
    """Mesma fonte+prompt ativo é deduplicado; prompt novo substitui o pendente.

    A rota manual omite `priority`, `kind` e `supersede` e usa a prioridade 3:
    outro prompt cancela o pendente da mesma fonte.
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
    if automatic:
        payload["automatic"] = True
    label = " ".join(str(kind or "").split())
    if label:
        payload["kind"] = label
    return enqueue(
        conn, "scene_clip", adventure_id, source_image_id, payload,
        priority=int(priority),
        supersede_key=f"clip:{adventure_id}:{source_image_id}",
    )


def enqueue_turnaround(conn, adventure_id: str, card_id: str,
                       source_image_id: str, *, force: bool = False) -> dict:
    """Uma cadeia completa por âncora; `force` só atende à revisão manual."""
    card = db.get_card(conn, adventure_id, card_id)
    if card and not force:
        sheet = _card_sheet(card)
        reference = sheet.get("reference") if isinstance(sheet.get("reference"), dict) else {}
        saved = reference.get("turnaround") if isinstance(reference.get("turnaround"), dict) else {}
        if (saved.get("master_image_id") and saved.get("design_sheet_image_id")
                and str(saved.get("source_image_id") or "") == source_image_id):
            latest = db.latest_job(conn, "character_turnaround", card_id)
            if latest and latest.get("status") == "done":
                return latest
    active = db.find_active_job(conn, "character_turnaround", card_id)
    if active and str((active.get("payload") or {}).get("source_image_id") or "") \
            == source_image_id:
        return active
    prompts = _turnaround_prompts(card or {"id": card_id, "title": "character"})
    return enqueue(
        conn, "character_turnaround", adventure_id, card_id,
        {
            "source_image_id": source_image_id,
            "prompt": prompts["clip"],
            "reference_prompts": prompts,
        },
        priority=PRIORITY_PORTRAIT_CLIP,
        supersede_key=f"turnaround:{adventure_id}:{card_id}",
    )


def _portrait_auto_clip(job: dict, card: dict) -> bool:
    """Retrato presente já reagiu a um turno; inicial e lote ficam estáticos."""
    try:
        priority = int(job.get("priority"))
    except (TypeError, ValueError):
        return False
    if priority > PRIORITY_PRESENT_PORTRAIT:
        return False
    visual = _card_sheet(card).get("visual_state")
    return isinstance(visual, dict) and bool(visual.get("seq"))


def _enqueue_clip_followup(job: dict, result: dict) -> None:
    """Fonte é o image.id do result. Não chama LLM."""
    job_type = job.get("type")
    if job_type not in ("character_portrait", "scene_backdrop"):
        return
    if (result or {}).get("reused"):
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
            if not card or not _portrait_auto_clip(job, card):
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
            automatic=True,
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


def _schedule_turnaround_followup(job: dict) -> None:
    """Cria a referência completa uma vez; falha não reabre o retrato."""
    if job.get("type") != "character_portrait":
        return
    try:
        with db.session() as conn:
            cfg = image_client.effective_settings(conn)
            if not image_client.video_settings(cfg).get("auto_turnaround"):
                return
            card = db.get_card(conn, job["adventure_id"], job.get("ref_id") or "")
            if not card:
                return
            source_id = likeness.reference_image_id(card)
            if source_id:
                enqueue_turnaround(conn, job["adventure_id"], card["id"], source_id)
    except Exception:
        log.exception(
            "job %s (%s): referências 360 automáticas não entraram na fila",
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
        llm_cfg = settings_store.effective_llm_settings(conn)
    if not img_cfg["enabled"]:
        raise GameError(503, "image_disabled",
                        "image generation is disabled (settings.image.enabled)")
    negative_extra = str(payload.get("negative_extra") or "")
    if payload.get("explicit"):
        negative_extra = npc_assets.negative_without_nudity_ban(negative_extra)
    if payload.get("explicit"):
        img_cfg["negative_prompt"] = npc_assets.negative_without_nudity_ban(
            img_cfg["negative_prompt"])
    negative_prompt = ", ".join(
        part for part in (img_cfg["negative_prompt"], negative_extra) if part)
    # Cena explícita no Flux2: CFG 2.0 para o negativo de roupa valer.
    img_cfg = image_client.with_flux2_cfg(img_cfg, payload.get("flux2_cfg"))
    references = (
        _reference_blobs(
            job, people=False,
            provider=str(img_cfg.get("provider") or "sdwebui"),
        )
        if img_cfg.get("provider") in ("comfyui", "sdwebui") else []
    )
    prompt = image_english.render_english(
        prompt, transport=transport, llm_cfg=llm_cfg)
    cache_key = image_client.request_key(
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
            negative_prompt=negative_prompt,
            cache_key=cache_key, reused_from=cached["id"], job_id=job["id"],
        )
        return {"image": image, "reused": True}
    image_id = db.new_id()
    meta = image_client.generate_image(
        img_cfg, prompt, adventure_id=job["adventure_id"], image_id=image_id,
        transport=transport, negative_extra=negative_extra,
        reference_images=references or None,
        expected_people=payload.get("expected_people"),
        progress=_generation_progress(job, transport, watch_cancel=False))
    try:
        image = store_see(job["adventure_id"], str(payload.get("caption") or ""), meta,
                          image_id=image_id, prompt=prompt,
                          negative_prompt=negative_prompt,
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
        llm_cfg = settings_store.effective_llm_settings(conn)
    if not img_cfg["enabled"]:
        raise GameError(503, "image_generation_disabled", "Image generation is disabled")
    prompt = image_english.render_english(
        prompt, transport=transport, llm_cfg=llm_cfg)
    cache_key = image_client.request_key(
        img_cfg, prompt, width=1344, height=768)
    image_id = db.new_id()
    meta = image_client.generate_image(
        img_cfg, prompt, adventure_id=job["adventure_id"],
        image_id=image_id, transport=transport, width=1344, height=768,
        progress=_generation_progress(job, transport, watch_cancel=False),
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
        llm_cfg = settings_store.effective_llm_settings(conn)
    if not img_cfg["enabled"]:
        raise GameError(503, "image_disabled",
                        "image generation is disabled (settings.image.enabled)")
    source, raw = _clip_source_png(job)
    source_id = source["id"]
    prompt = image_english.render_english(
        prompt, transport=transport, llm_cfg=llm_cfg)
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
            source_png=raw, transport=transport,
            progress=_generation_progress(job, transport, watch_cancel=True))
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


# Tamanho de cada pintura do turnaround. Corpo inteiro na proporção exata da
# célula das folhas e do quadro do Wan (448×768); close-up em 3:4.
_TURNAROUND_PAINTS = (
    ("master", turnaround.FULL_BODY_SIZE),
    ("front", turnaround.FULL_BODY_SIZE),
    ("profile", turnaround.FULL_BODY_SIZE),
    ("back", turnaround.FULL_BODY_SIZE),
    ("face", turnaround.FACE_SIZE),
)


def _turnaround_references(kind: str, *, faces: list[bytes], primary: list[bytes],
                           photos: list[bytes], anchor: bytes, master: bytes) -> list[bytes]:
    """Referências de cada pintura do turnaround, na ordem (a primeira pesa mais).

    Com foto: mestre = recortes de rosto (a roupa vem do texto travado; a foto
    principal só entra sem recorte, porque a estampa com letras dela voltava
    como texto ilegível); close-up = rostos + mestre; vistas de corpo inteiro
    = o primeiro rosto + o mestre. Sem foto: o caminho antigo, com a âncora.
    """
    if faces or primary:
        if kind == "master":
            refs = [*faces] if faces else [*primary]
        elif kind == "face":
            refs = [*faces, master]
        else:
            refs = [*faces[:1], master]
    elif kind == "master":
        refs = [*photos, anchor]
    elif kind == "face":
        refs = [*photos, master, anchor]
    else:
        refs = [*photos, master]
    found: list[bytes] = []
    for blob in refs:
        if blob and blob not in found:
            found.append(blob)
    return found[:4]


def _execute_turnaround(job: dict, transport: Any) -> dict:
    """Mestre → frente, perfil, costas e close-up, cada um pintado e conferido
    → ficha e folha 2×2 montadas em grade fixa → quadro do Wan e giro de
    reserva. Publica tudo ou não publica nada."""
    with db.session() as conn:
        img_cfg = image_client.effective_settings(conn)
        llm_cfg = settings_store.effective_llm_settings(conn)
        card_id = str(job.get("ref_id") or "")
        card = db.get_card(conn, job["adventure_id"], card_id)
        if card is None:
            raise GameError.not_found(f"card {card_id} not found")
    if not img_cfg["enabled"]:
        raise GameError(503, "image_disabled",
                        "image generation is disabled (settings.image.enabled)")
    # A referência manda: o look (cabelo, óculos, pele, roupa que aparece) é
    # lido da foto (ou da âncora, sem foto) uma vez; a trava de roupa usa as
    # peças visíveis literais e só completa as outras. A identidade é
    # traduzida uma vez e as frases do look e da roupa entram literais.
    with db.session() as conn:
        look_lock.ensure(conn, card, transport=transport, llm_cfg=llm_cfg)
        card = db.get_card(conn, job["adventure_id"], card_id) or card
        lock = outfit_lock.ensure(conn, card, transport=transport, llm_cfg=llm_cfg)
        card = db.get_card(conn, job["adventure_id"], card_id) or card
    identity = image_english.render_english(
        _turnaround_identity(card, with_look=False), transport=transport, llm_cfg=llm_cfg)
    prompts = _turnaround_prompts(card, lock, identity=identity)
    source, raw = _clip_source_png(job)
    reference_cfg = {**img_cfg, "provider": "comfyui"}
    created_paths: list[str] = []
    painted: dict[str, dict] = {}

    def ensure_running() -> None:
        with db.session() as conn:
            current = db.get_job(conn, job["id"])
        if not current or current.get("status") != "running":
            raise db.JobCanceled(job["id"])

    # A referência manda. Com foto enviada: recortes grandes do rosto das fotos
    # de rosto escolhidas na leitura vêm primeiro, e a foto principal traz
    # roupa e corpo; a âncora gerada fica de fora (era ela que fazia o cabelo
    # do mestre mudar entre staging e produção). Sem foto: a âncora, como antes.
    def as_png(blobs: list[bytes]) -> list[bytes]:
        found = []
        for blob in blobs:
            png = turnaround.reference_photo_png(blob) if blob else b""
            if png and png not in found:
                found.append(png)
        return found

    faces: list[bytes] = []
    primary: list[bytes] = []
    photos: list[bytes] = []
    if not likeness.is_minor(card):
        faces = as_png(look_lock.face_references(card))
        primary = as_png([look_lock.primary_photo(card) or b""])
        if not faces and not primary:
            photos = as_png(likeness.portrait_pixels(card)[:1])

    def paint(kind: str, size: tuple[int, int], references: list[bytes]) -> dict:
        """Pinta, confere e, se a vista falhar, tenta mais uma semente.

        Fica a tentativa com menos problemas; nenhuma vista derruba o
        turnaround (o problema vai para o log e para o resultado).
        """
        attempts = []
        for attempt in range(2):
            # Semente fixa por card, vista e tentativa: refazer o turnaround
            # repete o mesmo cabelo e o mesmo tom de jeans.
            cfg = {**reference_cfg, "seed": stable_seed(card_id, kind, attempt)}
            image_id = db.new_id()
            meta = image_client.generate_image(
                cfg, prompts[kind], adventure_id=job["adventure_id"],
                image_id=image_id, transport=transport, width=size[0], height=size[1],
                reference_images=references,
            )
            created_paths.append(str(meta["file_path"]))
            content = likeness.read_stored_image_bytes(meta)
            problems = turnaround.check_view(content, kind)
            attempts.append({"id": image_id, "meta": meta, "raw": content,
                             "problems": problems})
            ensure_running()
            if not problems:
                break
            log.info("job %s: vista %s recusada (tentativa %d): %s",
                     job["id"], kind, attempt + 1, ", ".join(problems))
        best = min(attempts, key=lambda item: len(item["problems"]))
        for item in attempts:
            if item is not best:
                _discard_render(str(item["meta"]["file_path"]))
                created_paths.remove(str(item["meta"]["file_path"]))
        if best["problems"]:
            log.warning("job %s: vista %s publicada com %s", job["id"], kind,
                        ", ".join(best["problems"]))
        return best

    try:
        for kind, size in _TURNAROUND_PAINTS:
            references = _turnaround_references(
                kind, faces=faces, primary=primary, photos=photos, anchor=raw,
                master=painted["master"]["raw"] if "master" in painted else b"")
            painted[kind] = paint(kind, size, references)
        master = painted["master"]
        views = [painted[kind] for kind in turnaround.VIEW_KINDS]
        view_raws = [view["raw"] for view in views]
        folder = likeness.stored_image_path(master["meta"]["file_path"]).parent

        def place(suffix: str = "png") -> tuple[str, Path]:
            image_id = db.new_id()
            path = folder / f"{image_id}.{suffix}"
            created_paths.append(str(path))
            return image_id, path

        split_id, split_path = place()
        frame_size = turnaround.build_master_frame(master["raw"], split_path)
        design_id, design_path = place()
        design_size = turnaround.compose_design_sheet(master["raw"], view_raws, design_path)
        sheet_id, sheet_path = place()
        cell = turnaround.compose_reference_sheet(view_raws, sheet_path)
        # Giro imediato das vistas pintadas. O vídeo do Wan sai depois, em
        # `turnaround_clip`, e só substitui este se passar no portão.
        clip_id, clip_path = place("mp4")
        clip_meta: dict | None = None
        try:
            duration_ms = turnaround.build_view_cycle(view_raws[:3], clip_path)
            clip_meta = {"width": turnaround.CYCLE_SIZE[0],
                         "height": turnaround.CYCLE_SIZE[1], "duration_ms": duration_ms}
        except Exception as exc:  # o giro é só preview: nunca derruba o turnaround
            log.warning("job %s: giro das vistas indisponível: %s: %s",
                        job["id"], type(exc).__name__, exc)
            _discard_render(str(clip_path))
        ensure_running()
        with db.session() as conn:
            card = db.get_card(conn, job["adventure_id"], card_id)
            if card is None:
                raise GameError.not_found(f"card {card_id} not found")

            def insert(image_id, file_path, prompt, kind, width, height, source_id, *,
                       provider="pillow", **extra):
                return db.insert_image(
                    conn, adventure_id=job["adventure_id"], action_seq=-1,
                    file_path=str(file_path), prompt=prompt,
                    negative_prompt=img_cfg.get("negative_prompt") or "",
                    provider=provider, width=width, height=height,
                    image_id=image_id, kind=kind, ref_id=card_id,
                    job_id=job["id"], source_image_id=source_id, **extra,
                )

            master_row = insert(
                master["id"], master["meta"]["file_path"], prompts["master"],
                "character_master", master["meta"].get("width"),
                master["meta"].get("height"), source["id"],
                provider=master["meta"].get("provider") or "comfyui")
            view_rows = [
                insert(view["id"], view["meta"]["file_path"], prompts[kind],
                       "reference_view", view["meta"].get("width"),
                       view["meta"].get("height"), master_row["id"],
                       provider=view["meta"].get("provider") or "comfyui")
                for kind, view in zip(turnaround.VIEW_KINDS, views)
            ]
            design_row = insert(
                design_id, design_path, "Composed: master, profile, back and close-up",
                "character_sheet", design_size[0], design_size[1], master_row["id"])
            split_row = insert(
                split_id, split_path, "Deterministic frame: full-body master alone",
                "turnaround_start", frame_size[0], frame_size[1], master_row["id"])
            sheet_row = insert(
                sheet_id, sheet_path, "Composed: front, profile, back and close-up",
                "reference_sheet", cell[0] * 2, cell[1] * 2, master_row["id"])
            clip = None
            if clip_meta is not None:
                clip = insert(
                    clip_id, clip_path, prompts["clip"], "turnaround_clip",
                    clip_meta["width"], clip_meta["height"], master_row["id"],
                    media_type="video/mp4", duration_ms=clip_meta["duration_ms"])
            reference = {
                "source_image_id": source["id"],
                "master_image_id": master_row["id"],
                "design_sheet_image_id": design_row["id"],
                "split_image_id": split_row["id"],
                "sheet_image_id": sheet_row["id"],
                "clip_image_id": clip["id"] if clip else "",
                "view_image_ids": [row["id"] for row in view_rows],
            }
            likeness.set_turnaround(conn, card, reference, job_id=job["id"])
            video = image_client.turnaround_video_settings(img_cfg)
            if video["engine"] != "none":
                enqueue(
                    conn, "turnaround_clip", job["adventure_id"], card_id,
                    {
                        "card_id": card_id,
                        "source_image_id": split_row["id"],
                        "design_sheet_image_id": design_row["id"],
                        "prompt": prompts["clip"],
                        "automatic": True,
                    },
                    priority=PRIORITY_PORTRAIT_CLIP,
                    supersede_key=f"turnaround_clip:{job['adventure_id']}:{card_id}",
                )
    except sqlite3.IntegrityError:
        for path in created_paths:
            _discard_render(path)
        _raise_if_gone(job["adventure_id"],
                       str(painted["master"]["meta"]["file_path"]) if painted else "")
        raise
    except Exception:
        for path in created_paths:
            _discard_render(path)
        raise
    sheet_ref = db.image_ref(sheet_row)
    return {
        "card_id": card_id,
        "image": sheet_ref,
        "master": db.image_ref(master_row),
        "design_sheet": db.image_ref(design_row),
        "split": db.image_ref(split_row),
        "sheet": sheet_ref,
        "clip": db.image_ref(clip) if clip else None,
        "views": [db.image_ref(row) for row in view_rows],
        "view_problems": {kind: painted[kind]["problems"]
                          for kind in ("master", *turnaround.VIEW_KINDS)
                          if painted[kind]["problems"]},
    }


# Até três sementes: com primeiro e último quadro fixos, 2 de 3 voltas na L4
# saíram limpas e completas; a que falha costuma ir e voltar (wobble).
_TURNAROUND_CLIP_ATTEMPTS = 3
# Rodadas novas quando as três sementes caem no portão: o job volta à fila
# com `not_before` (o worker só pega depois disso), prioridade mais baixa e
# preemptável, então nunca segura o jogo. Sementes diferentes a cada rodada.
_TURNAROUND_CLIP_ROUNDS = 3
_TURNAROUND_CLIP_BACKOFF_S = (10 * 60, 30 * 60)


def stable_seed(*parts: Any) -> int:
    """Semente fixa por card e vista: refazer dá a mesma imagem."""
    digest = hashlib.sha256(":".join(str(part) for part in parts).encode()).digest()
    return int.from_bytes(digest[:4], "big") & 0x7FFFFFFF


def _schedule_clip_round(job: dict, payload: dict) -> dict | None:
    """Próxima rodada do giro, com atraso crescente; None depois da última."""
    current_round = int(payload.get("round") or 1)
    if current_round >= _TURNAROUND_CLIP_ROUNDS:
        return None
    delay = _TURNAROUND_CLIP_BACKOFF_S[min(current_round - 1, len(_TURNAROUND_CLIP_BACKOFF_S) - 1)]
    not_before = datetime.now(timezone.utc) + timedelta(seconds=delay)
    retry = {key: value for key, value in payload.items()
             if key not in ("heartbeat_at", "requeues")}
    retry["round"] = current_round + 1
    retry["not_before"] = not_before.isoformat(timespec="milliseconds").replace("+00:00", "Z")
    with db.session() as conn:
        return enqueue(conn, "turnaround_clip", job["adventure_id"], job.get("ref_id") or "",
                       retry, priority=PRIORITY_PORTRAIT_CLIP,
                       supersede_key=job.get("supersede_key") or None)


def _execute_turnaround_clip(job: dict, transport: Any) -> dict:
    """Giro 360° no Wan a partir do quadro do mestre; troca o giro da ficha
    só se o clipe passar no portão e o turnaround ainda for o mesmo."""
    payload = job.get("payload") or {}
    card_id = str(payload.get("card_id") or job.get("ref_id") or "")
    split_id = str(payload.get("source_image_id") or "")
    prompt = " ".join(str(payload.get("prompt") or "").split())
    if not prompt:
        raise GameError(500, "generation_failed", "turnaround clip job has no prompt")

    def current_turnaround() -> tuple[dict | None, dict]:
        with db.session() as conn:
            card = db.get_card(conn, job["adventure_id"], card_id)
        sheet = _card_sheet(card) if card else {}
        reference = sheet.get("reference") if isinstance(sheet.get("reference"), dict) else {}
        saved = reference.get("turnaround")
        return card, saved if isinstance(saved, dict) else {}

    card, saved = current_turnaround()
    if card is None or str(saved.get("split_image_id") or "") != split_id:
        return {"card_id": card_id, "clip": None, "stale": True}
    with db.session() as conn:
        img_cfg = image_client.effective_settings(conn)
    if not img_cfg["enabled"]:
        raise GameError(503, "image_disabled",
                        "image generation is disabled (settings.image.enabled)")
    video = image_client.turnaround_video_settings(img_cfg)
    if video["engine"] == "none":
        return {"card_id": card_id, "clip": None, "disabled": True}
    _source, raw = _clip_source_png(job)
    # O prompt já chega em inglês: o turnaround traduziu antes de enfileirar.
    progress = _generation_progress(job, transport, watch_cancel=True)
    accepted: dict | None = None
    image_id = ""
    reports: list[dict] = []
    clip_round = int(payload.get("round") or 1)
    for attempt in range(_TURNAROUND_CLIP_ATTEMPTS):
        image_id = db.new_id()
        meta = image_client.generate_clip(
            img_cfg, prompt, adventure_id=job["adventure_id"], image_id=image_id,
            source_png=raw, transport=transport, progress=progress,
            video_overrides=video,
            seed=stable_seed(card_id, split_id, "clip", clip_round, attempt),
        )
        path = likeness.stored_image_path(meta["file_path"])
        try:
            report = turnaround.analyze_clip(path)
        except Exception as exc:  # análise quebrada = clipe recusado, nunca erro do job
            report = {"clean": False, "problems": [f"analysis:{type(exc).__name__}"]}
        reports.append(report)
        if report.get("clean"):
            try:
                # Corta no quadro que volta à frente: o fim emenda no começo.
                looped = path.with_name(f"{path.stem}.loop.mp4")
                duration = turnaround.finalize_loop(path, looped, int(report["loop_end"]))
                looped.replace(path)
                accepted = {**meta, "duration_ms": duration}
                break
            except Exception as exc:
                report = {"clean": False, "problems": [f"loop:{type(exc).__name__}"]}
        log.info("job %s: giro 360° recusado pelo portão (tentativa %d): %s",
                 job["id"], attempt + 1, ", ".join(report.get("problems") or []))
        _discard_render(meta["file_path"])
        with db.session() as conn:
            current = db.get_job(conn, job["id"])
        if not current or current.get("status") != "running":
            raise db.JobCanceled(job["id"])
    if accepted is None:
        retry = _schedule_clip_round(job, payload)
        if retry:
            log.info("job %s: giro 360° recusado na rodada %d; nova rodada %s depois de %s",
                     job["id"], clip_round, retry["id"], retry["payload"]["not_before"])
        return {"card_id": card_id, "clip": None, "rejected": True, "reports": reports,
                "round": clip_round, "retry_job_id": retry["id"] if retry else None}
    try:
        with db.session() as conn:
            card = db.get_card(conn, job["adventure_id"], card_id)
            sheet = _card_sheet(card) if card else {}
            reference = sheet.get("reference") if isinstance(sheet.get("reference"), dict) else {}
            saved = reference.get("turnaround") if isinstance(reference.get("turnaround"), dict) else {}
            if card is None or str(saved.get("split_image_id") or "") != split_id:
                _discard_render(accepted["file_path"])
                return {"card_id": card_id, "clip": None, "stale": True}
            clip = db.insert_image(
                conn, adventure_id=job["adventure_id"], action_seq=-1,
                file_path=accepted["file_path"], prompt=prompt,
                provider=accepted.get("provider") or "comfyui",
                width=accepted.get("width"), height=accepted.get("height"),
                image_id=image_id, kind="turnaround_clip", ref_id=card_id,
                job_id=job["id"], media_type="video/mp4",
                duration_ms=accepted.get("duration_ms"), source_image_id=split_id,
            )
            likeness.set_turnaround(
                conn, card, {**saved, "clip_image_id": clip["id"]}, job_id=job["id"])
    except db.JobCanceled:
        _discard_render(accepted["file_path"])
        raise
    except sqlite3.IntegrityError:
        _discard_render(accepted["file_path"])
        _raise_if_gone(job["adventure_id"], accepted["file_path"])
        raise
    return {"card_id": card_id, "clip": db.image_ref(clip), "image": db.image_ref(clip),
            "reports": reports}


_EXECUTORS: dict[str, Callable[[dict, Any], dict]] = {
    "character_portrait": _execute_portrait,
    "card_assets": _execute_card_assets,
    "scene_backdrop": _execute_backdrop,
    "see_image": _execute_see,
    "npc_day_tick": _execute_day_tick,
    "location_image": _execute_location_image,
    "scene_clip": _execute_scene_clip,
    "character_turnaround": _execute_turnaround,
    "turnaround_clip": _execute_turnaround_clip,
}


def _failed(job: dict, code: str, message: str) -> None:
    result = {"code": code}
    _finish(job, "error", result=result, error=message,
            terminal=_terminal_event(job, "error", result, message))


def _run_job(job: dict, transport: Any) -> None:
    """Executa um job claimed e grava done/error. Aventura apagada no meio
    termina canceled, sem evento (o canal dela já foi fechado)."""
    _progress(job)
    _write_heartbeat(job["id"], force=True)
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
            if job["type"] == "character_portrait":
                _schedule_turnaround_followup(job)
    finally:
        _forget_job_runtime(job["id"])


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


def _timeout_seconds(value: Any, default: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    if number < 0:
        return default
    return number


def _deadline_seconds(job_type: str, img_cfg: dict, llm_cfg: dict) -> float | None:
    """Duração do prazo. A âncora é o mais recente entre started_at e heartbeat."""
    if job_type in _IMAGE_JOB_TYPES:
        timeout = _timeout_seconds(img_cfg.get("timeout_seconds"), 300.0)
        return max(360.0, timeout + 60.0)
    if job_type in ("scene_clip", "turnaround_clip"):
        video = image_client.video_settings(img_cfg)
        timeout = _timeout_seconds(video.get("timeout_seconds"), 1800.0)
        return max(1860.0, timeout + 60.0)
    if job_type == "card_assets":
        timeout = _timeout_seconds(llm_client.role_timeout(llm_cfg, "assets"), 120.0)
        return max(180.0, timeout + 60.0)
    if job_type == "npc_day_tick":
        return 900.0
    return None


def _job_heartbeat(job: dict) -> datetime | None:
    """`payload.heartbeat_at` de um running; o campo de topo é o mesmo valor."""
    payload = job.get("payload")
    raw = payload.get("heartbeat_at") if isinstance(payload, dict) else None
    if not isinstance(raw, str) or not raw:
        raw = job.get("heartbeat_at")
    if not isinstance(raw, str):
        return None
    return _utc_stamp(raw)


def _aware_utc(moment: datetime) -> datetime:
    if moment.tzinfo is None:
        return moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc)


def reap_stalled_jobs(transport: Any = None, now: datetime | None = None) -> int:
    """Marca timeout dos running vencidos e interrompe o render.

    O update condicional de `finish_job` impede um done tardio. Não depende
    de `process_pending`.
    """
    moment = _aware_utc(now or datetime.now(timezone.utc))
    stalled: list[dict] = []
    with db.session() as conn:
        img_cfg = image_client.effective_settings(conn)
        llm_cfg = settings_store.effective_llm_settings(conn)
        rows = conn.execute("SELECT id FROM jobs WHERE status='running'").fetchall()
        for row in rows:
            job = db.get_job(conn, row["id"])
            if job is None:
                continue
            limit = _deadline_seconds(job["type"], img_cfg, llm_cfg)
            if limit is None:
                continue
            started = _utc_stamp(job.get("started_at"))
            if started is None:
                continue
            heartbeat = _job_heartbeat(job)
            anchor = heartbeat if heartbeat is not None and heartbeat > started else started
            if (moment - anchor).total_seconds() >= limit:
                stalled.append(job)
    reaped = 0
    error = "job exceeded its deadline"
    for job in stalled:
        result = {"code": "job_timeout"}
        if _finish(
            job, "error", result=result, error=error,
            terminal=_terminal_event(job, "error", result, error),
        ):
            reaped += 1
            interrupt_image(job, transport)
            log.info("job %s (%s) estourou o prazo", job["id"], job["type"])
    return reaped


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


async def reaper_loop(interval: float = _REAPER_INTERVAL_S) -> None:
    """Loop próprio, de start_worker até stop_worker. Não drena a fila."""
    log.info("jobs reaper iniciado (intervalo %.1fs)", interval)
    while True:
        try:
            await asyncio.to_thread(reap_stalled_jobs, None)
        except Exception:
            log.exception("jobs reaper: erro no ciclo")
        await asyncio.sleep(interval)


def start_worker(app) -> None:
    """Chamado no lifespan; no-op se AIDUNGEON_JOBS_WORKER=0."""
    if not worker_enabled():
        log.info("jobs worker desligado (AIDUNGEON_JOBS_WORKER=0)")
        return
    recover_interrupted()
    app.state.job_worker = asyncio.create_task(worker_loop())
    app.state.job_reaper = asyncio.create_task(reaper_loop())


def stop_worker(app) -> None:
    for name in ("job_worker", "job_reaper"):
        task = getattr(app.state, name, None)
        if task is not None:
            task.cancel()
    wake()
