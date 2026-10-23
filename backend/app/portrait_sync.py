"""Sincroniza o retrato exibido com o estado atual da ficha.

O rosto é a âncora (`portrait_anchor_id`); roupa, expressão e feridas
visíveis geram uma variante img2img. Cache por `portrait_state_key` evita
regerar e permite trocar na hora quando a variante já existe.

Job antigo nunca sobrescreve estado novo: `_execute_portrait` compara a chave
desejada na ficha com a chave do job e marca o resultado como stale.
"""

from __future__ import annotations

import json
import logging

from app import db, npc_assets

log = logging.getLogger("aidungeon.portrait_sync")


def _save_sheet(conn, adventure_id: str, card: dict, sheet: dict) -> dict | None:
    return db.update_card(conn, adventure_id, card["id"], {
        "sheet": json.dumps(sheet, ensure_ascii=False),
    })


def sync_card(conn, adventure_id: str, card_id: str, *,
              force: bool = False) -> dict | None:
    """Aplica cache-hit ou enfileira a variante; devolve o card atualizado."""
    from app import jobs

    card = db.get_card(conn, adventure_id, card_id)
    if not card:
        return None
    sheet = npc_assets.parse_sheet(card.get("sheet") or "")
    key = npc_assets.portrait_state_key(card)
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
            sheet["portrait_state_key"] = key
            card = _save_sheet(conn, adventure_id, card, sheet) or card
            return card

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
