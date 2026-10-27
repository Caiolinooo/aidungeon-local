"""Sincroniza o retrato exibido com o estado atual da ficha.

O rosto é a âncora (`portrait_anchor_id`); roupa, expressão, feridas,
ação, emoção, pose e o estágio de alignment geram uma variante img2img.
A chave inclui a variante, esse estado visual, o estágio (não o inteiro
de bonds), a âncora, os bytes de `likeness.portrait_init`, o estilo da
aventura e o fingerprint do modelo. Cache por `portrait_state_key`
evita regerar: tick numérico sem estágio novo não pede GPU.

Job `running` com outra chave fica na GPU: a chave nova é gravada e o
sync segue para `enqueue_portrait`. O resultado antigo não substitui a
ficha — `_execute_portrait` compara as chaves e marca stale.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from app import db, npc_assets

log = logging.getLogger("aidungeon.portrait_sync")


def _save_sheet(conn, adventure_id: str, card: dict, sheet: dict) -> dict | None:
    return db.update_card(conn, adventure_id, card["id"], {
        "sheet": json.dumps(sheet, ensure_ascii=False),
    })


def _anchor_bytes(conn, card: dict) -> bytes | None:
    """PNG da âncora, ou do retrato atual se a âncora ainda não foi gravada.

    Arquivo ausente vira `None`: o sync ainda enfileira; o render é que falha.
    """
    anchor_id = str(card.get("portrait_anchor_id") or card.get("portrait_image_id") or "")
    if not anchor_id:
        return None
    image = db.get_image(conn, anchor_id)
    if not image:
        return None
    raw = str(image.get("file_path") or "")
    if not raw:
        return None
    path = Path(raw)
    if not path.is_absolute():
        path = db.REPO_ROOT / path
    try:
        if not path.is_file():
            return None
        return path.read_bytes()
    except OSError:
        return None


def sync_card(conn, adventure_id: str, card_id: str, *,
              force: bool = False) -> dict | None:
    """Aplica cache-hit ou enfileira a variante; devolve o card atualizado."""
    from app import jobs, likeness, settings_store
    from app.errors import GameError

    card = db.get_card(conn, adventure_id, card_id)
    if not card:
        return None
    sheet = npc_assets.parse_sheet(card.get("sheet") or "")
    adventure = db.get_adventure(conn, adventure_id) or {}
    try:
        init_bytes, _denoise = likeness.portrait_init(
            card,
            _anchor_bytes(conn, card),
            anchor_denoise=jobs.PORTRAIT_VARIANT_DENOISE,
        )
    except GameError as exc:
        if exc.code == likeness.CODE_MINOR:
            return card
        raise
    key = npc_assets.portrait_state_key(
        card,
        settings_store.effective_image_settings(conn),
        image_style=str(adventure.get("image_style") or ""),
        reference_bytes=init_bytes or b"",
    )
    current_id = card.get("portrait_image_id")
    if current_id:
        current = db.get_image(conn, current_id)
        if current and current.get("cache_key") == key and not force:
            if sheet.get("portrait_state_key") != key or sheet.get("portrait_job_id"):
                sheet["portrait_state_key"] = key
                sheet["portrait_job_id"] = ""
                card = _save_sheet(conn, adventure_id, card, sheet) or card
            return card

    cached = db.find_cached_image(
        conn, adventure_id, kind="portrait", ref_id=card_id, cache_key=key)
    if cached and not force:
        db.set_card_portrait(conn, card_id, cached["id"],
                             anchor_id=card.get("portrait_anchor_id") or cached["id"])
        sheet["portrait_state_key"] = key
        sheet["portrait_job_id"] = ""
        card = _save_sheet(conn, adventure_id, card, sheet) or card
        return card

    active = db.find_active_job(conn, "character_portrait", card_id)
    if active:
        active_key = str((active.get("payload") or {}).get("state_key") or "")
        if active_key == key:
            if sheet.get("portrait_state_key") != key:
                sheet["portrait_state_key"] = key
                sheet["portrait_job_id"] = active["id"]
                card = _save_sheet(conn, adventure_id, card, sheet) or card
            return card
        if active.get("status") in ("preparing", "pending"):
            db.cancel_job(conn, active["id"])
        else:
            # running (ou outro status ativo): grava a chave e cria o substituto.
            sheet["portrait_state_key"] = key
            card = _save_sheet(conn, adventure_id, card, sheet) or card

    job = jobs.enqueue_portrait(
        conn, adventure_id, card_id,
        priority=jobs.PRIORITY_PRESENT_PORTRAIT,
        variant_key=key,
    )
    if job:
        sheet["portrait_state_key"] = key
        sheet["portrait_job_id"] = job.get("id") or ""
        card = _save_sheet(conn, adventure_id, card, sheet) or card
    return card
