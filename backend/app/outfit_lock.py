"""Roupa padrão travada por personagem: o mesmo traje em todo turnaround.

Os prompts do mestre e da ficha diziam só "exact likeness of <nome>" e
"preserve the outfit". As referências (foto enviada, retrato âncora) são de
busto, então calça, sapato e o resto do corpo eram inventados a cada
semente: a Renata saiu de calça preta e tênis branco, de regata e sapato
preto, de regata e short. Aqui a roupa vira quatro peças em inglês
(top, bottom, footwear, accessories), gravadas uma vez na ficha em
`outfit_lock` e repetidas literalmente em todo prompt da aparência padrão.

A trava só muda quando o texto de roupa do card muda (`basis`). Estados de
cena (See, fundo, nudez, `current_outfit`) não passam por aqui.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from typing import Any

from app import db, llm_client, npc_assets, settings_store

log = logging.getLogger("aidungeon.outfit_lock")

SLOTS = ("top", "bottom", "footwear", "accessories")
# Sem LLM (ou resposta inválida): peças neutras que servem a qualquer cenário.
# O que o card disser de roupa continua na frente, em `top`.
_FALLBACK = {
    "top": "plain dark top",
    "bottom": "plain dark straight-leg trousers",
    "footwear": "plain dark closed shoes",
    "accessories": "none",
}
_OUTFIT_LINE = re.compile(
    r"(?i)\b(attire|outfit|clothing|clothes|wears|wearing|dressed|garment|"
    r"roupa|roupas|veste|vestindo|traje|usa|usando)\b")
_SYSTEM = """You fix a fictional character's single default outfit for a character reference sheet.
Reply with ONLY a JSON object with exactly these string keys: "top", "bottom", "footwear", "accessories".
Rules:
- English, concrete garments with colour (and material when useful), 3 to 14 words per key.
- Keep every garment, colour and accessory the card states, translated to English. Never contradict the card.
- Fill only what the card leaves out with plain, simple, everyday items that fit the character and the setting.
- "accessories": worn items such as glasses, jewellery, hats, belts or bags; "none" when there are none.
- No nudity, no underwear-only outfits, no logos, no text, no armour or weapons unless the card states them.
- A one-piece garment goes in "top" and "bottom" says "none (one-piece)"."""


def _sheet(card: dict) -> dict:
    return npc_assets.parse_sheet(card.get("sheet") or "")


def basis(card: dict) -> str:
    """Só o texto de roupa do card: editar outra coisa não troca o traje."""
    sheet = _sheet(card)
    parts: list[str] = []
    clothing = sheet.get("clothing")
    if isinstance(clothing, str) and clothing.strip():
        parts.append(" ".join(clothing.split()))
    for line in str(card.get("entry") or "").splitlines():
        line = " ".join(line.split())
        if line and _OUTFIT_LINE.search(line):
            parts.append(line)
    return " | ".join(parts)


def _basis_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def _clean(value: Any) -> str:
    text = " ".join(str(value or "").split()).strip(" .;,")
    return text[:140]


def _parse(reply: str) -> dict | None:
    match = re.search(r"\{.*\}", reply or "", flags=re.S)
    if not match:
        return None
    try:
        data = json.loads(match.group(0))
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    slots = {key: _clean(data.get(key)) for key in SLOTS}
    if not all(slots[key] for key in ("top", "bottom", "footwear")):
        return None
    slots["accessories"] = slots["accessories"] or "none"
    return slots


def _ask_llm(card: dict, text: str, *, transport=None, llm_cfg: dict) -> dict | None:
    sheet = _sheet(card)
    lines = [f"Character: {card.get('title') or 'character'}"]
    if text:
        lines.append(f"Clothing stated on the card: {text}")
    for key in ("appearance", "body", "class"):
        value = sheet.get(key)
        if isinstance(value, str) and value.strip():
            lines.append(f"{key.capitalize()}: {' '.join(value.split())[:400]}")
    entry = " ".join(str(card.get("entry") or "").split())
    if entry:
        lines.append(f"Card text: {entry[:1500]}")
    try:
        reply = llm_client.chat_completion(
            llm_cfg, [
                {"role": "system", "content": _SYSTEM},
                {"role": "user", "content": "\n".join(lines)},
            ],
            role="image_prompt", temperature=0, max_tokens=300, transport=transport,
        )
    except llm_client.LlmError as exc:
        log.warning("card %s: roupa padrão sem LLM: %s", card.get("id"), exc)
        return None
    except Exception as exc:  # LLM fora do ar não pode derrubar o retrato
        log.warning("card %s: roupa padrão sem LLM: %s", card.get("id"), type(exc).__name__)
        return None
    return _parse(reply)


def current(card: dict) -> dict | None:
    """Trava gravada e ainda válida para o texto de roupa atual do card."""
    lock = _sheet(card).get("outfit_lock")
    if not isinstance(lock, dict):
        return None
    if lock.get("basis_hash") != _basis_hash(basis(card)):
        return None
    if not all(isinstance(lock.get(key), str) and lock.get(key) for key in SLOTS):
        return None
    return lock


def ensure(conn, card: dict, *, transport=None, llm_cfg: dict | None = None) -> dict:
    """Devolve a trava do card, criando e gravando na primeira vez.

    Mesmo sem LLM a trava é gravada (peças neutras): o que importa é o
    traje ser igual em todas as rodadas, não ser o mais bonito.
    """
    lock = current(card)
    if lock is not None:
        return lock
    text = basis(card)
    if llm_cfg is None:
        llm_cfg = settings_store.effective_llm_settings(conn)
    slots = _ask_llm(card, text, transport=transport, llm_cfg=llm_cfg)
    source = "llm"
    if slots is None:
        slots = dict(_FALLBACK)
        if text:
            slots["top"] = _clean(text)
        source = "fallback"
    lock = {**slots, "basis_hash": _basis_hash(text), "source": source,
            "created_at": db.utcnow()}
    fresh = db.get_card(conn, card["adventure_id"], card["id"]) or card
    sheet = _sheet(fresh)
    sheet["outfit_lock"] = lock
    db.update_card(conn, card["adventure_id"], card["id"], {
        "sheet": json.dumps(sheet, ensure_ascii=False),
    })
    log.info("card %s: roupa padrão travada (%s): %s", card.get("id"), source,
             "; ".join(f"{key}: {lock[key]}" for key in SLOTS))
    return lock


def full_text(lock: dict | None) -> str:
    """Frase para mestre, ficha e giro: as quatro peças, sempre iguais."""
    if not lock:
        return ""
    pieces = "; ".join(f"{key}: {lock[key]}" for key in SLOTS)
    return (f"Default outfit, always exactly this and nothing else: {pieces}. "
            "Same garments, colours and footwear in every view.")


def visible_text(lock: dict | None) -> str:
    """Para o retrato de busto: só o que aparece, sem puxar o quadro para os pés."""
    if not lock:
        return ""
    bits = [f"top: {lock['top']}"]
    if lock.get("accessories") and lock["accessories"].lower() != "none":
        bits.append(f"accessories: {lock['accessories']}")
    return "Default outfit, keep exactly: " + "; ".join(bits)
