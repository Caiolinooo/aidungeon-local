"""Aparência canônica do personagem, lida da referência e gravada no card.

A referência manda: com foto enviada, as fotos decidem rosto, cabelo
(comprimento, textura, cor), óculos, pele, porte e toda peça de roupa que
aparece; sem foto, a âncora (retrato aceito) vira a referência. A leitura é
feita uma vez pelo modelo de visão do narrador (llama-server com mmproj),
com temperatura 0, e gravada em `look_lock` com o hash das imagens: muda só
quando a referência muda. Os traços de identidade (cabelo, óculos, pele,
rosto) entram em todo prompt que pinta a pessoa — turnaround, retrato, See e
fundo — e estado de roupa da história nunca os troca.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import re
from io import BytesIO
from typing import Any

from PIL import Image, ImageOps

from app import db, likeness, llm_client, npc_assets, settings_store

log = logging.getLogger("aidungeon.look_lock")

LOOK_VERSION = 1
# Até cinco fotos (a principal primeiro). Mais que isso só gasta tokens de
# imagem no narrador sem mudar a maioria.
MAX_PHOTOS = 5
IDENTITY_KEYS = ("hair_colour", "hair_texture", "hair_length", "hairstyle",
                 "glasses", "facial_hair", "skin", "eyes", "face", "build")
CLOTHING_KEYS = ("top", "bottom", "footwear", "accessories")
_NOT_VISIBLE = re.compile(r"(?i)^\s*(not visible|unknown|n/?a|unclear|cannot|can't)\b")
_NONE = re.compile(r"(?i)^\s*(none|no|nothing)\b")

_SYSTEM = """You read reference images of one fictional RPG character and fix the character's canonical look.
Reply with ONLY one JSON object with exactly these string keys:
"hair_colour", "hair_texture", "hair_length", "hairstyle", "glasses", "facial_hair", "skin", "eyes", "face", "build",
"top", "bottom", "footwear", "accessories".
Rules:
- English, concrete and precise, 2 to 14 words per key. Exact colours (e.g. "jet black", "dark indigo wash").
- hair_length must name a body landmark the hair reaches, e.g. "chin length", "just past the shoulders", "mid-back".
- glasses: if the character wears glasses in most images, describe the frames exactly, e.g. "large round thin
  gold-rimmed glasses"; otherwise "none". Never answer just "yes".
- Identity keys (hair, glasses, facial hair, skin, eyes, face, build) describe what MOST images show; image 1 wins ties.
- Clothing keys describe only what IMAGE 1 shows; write "not visible" for a piece image 1 does not show.
- face: distinctive features or a mask/prosthetic if there is one; otherwise "no distinctive marks".
- Never name or identify the person. No nudity. No guessing of age, ethnicity or biography."""


def _png_small(blob: bytes, side: int = 768) -> bytes:
    with Image.open(BytesIO(blob)) as source:
        image = ImageOps.exif_transpose(source).convert("RGB")
    image.thumbnail((side, side), Image.Resampling.LANCZOS)
    out = BytesIO()
    image.save(out, format="PNG")
    return out.getvalue()


def reference_images(conn, card: dict) -> tuple[str, list[bytes]]:
    """("photo"|"anchor"|"", imagens). Foto enviada primeiro; sem foto, a âncora."""
    if likeness.is_minor(card):
        return "", []
    photos = likeness.visual_reference_images(card)[:MAX_PHOTOS]
    if photos:
        return "photo", photos
    anchor_id = str(card.get("portrait_anchor_id") or card.get("portrait_image_id") or "")
    if anchor_id:
        image = db.get_image(conn, anchor_id)
        if image:
            try:
                return "anchor", [likeness.read_stored_image_bytes(image)]
            except OSError:
                pass
    return "", []


def _basis_hash(source: str, images: list[bytes]) -> str:
    digest = hashlib.sha256(source.encode())
    for blob in images:
        digest.update(hashlib.sha256(blob).digest())
    return digest.hexdigest()[:16]


def _clean(value: Any) -> str:
    return " ".join(str(value or "").split()).strip(" .;,")[:140]


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
    look = {key: _clean(data.get(key)) for key in (*IDENTITY_KEYS, *CLOTHING_KEYS)}
    if not look["hair_colour"] or not look["hair_length"] or not look["glasses"]:
        return None
    return look


def _read(images: list[bytes], *, transport=None, llm_cfg: dict) -> dict | None:
    content: list[dict] = [{"type": "text", "text": (
        f"{len(images)} reference image(s) of the same character follow; image 1 is the "
        "primary reference. Fill the JSON.")}]
    for blob in images:
        encoded = base64.b64encode(_png_small(blob)).decode("ascii")
        content.append({"type": "image_url",
                        "image_url": {"url": f"data:image/png;base64,{encoded}"}})
    try:
        reply = llm_client.chat_completion(
            llm_cfg, [{"role": "system", "content": _SYSTEM},
                      {"role": "user", "content": content}],
            role="assistant", temperature=0, max_tokens=500, timeout_s=180,
            transport=transport,
        )
    except Exception as exc:  # visão fora do ar não derruba quem pinta
        log.warning("look: visão indisponível: %s: %s", type(exc).__name__, exc)
        return None
    return _parse(reply)


def current(card: dict | None) -> dict | None:
    """Look gravado (não confere a base: quem pinta não lê imagens)."""
    if not card:
        return None
    look = npc_assets.parse_sheet(card.get("sheet") or "").get("look_lock")
    if not isinstance(look, dict) or look.get("version") != LOOK_VERSION:
        return None
    return look


def ensure(conn, card: dict, *, transport=None, llm_cfg: dict | None = None) -> dict | None:
    """Lê a referência e grava o look; reaproveita se a referência não mudou."""
    source, images = reference_images(conn, card)
    if not images:
        return current(card)
    basis = _basis_hash(source, images)
    stored = current(card)
    if stored and stored.get("basis_hash") == basis:
        return stored
    if llm_cfg is None:
        llm_cfg = settings_store.effective_llm_settings(conn)
    look = _read(images, transport=transport, llm_cfg=llm_cfg)
    if look is None:
        return stored
    look.update({"version": LOOK_VERSION, "basis_hash": basis, "source": source,
                 "images": len(images), "created_at": db.utcnow()})
    fresh = db.get_card(conn, card["adventure_id"], card["id"]) or card
    sheet = npc_assets.parse_sheet(fresh.get("sheet") or "")
    sheet["look_lock"] = look
    db.update_card(conn, card["adventure_id"], card["id"], {
        "sheet": json.dumps(sheet, ensure_ascii=False)})
    log.info("card %s: look da referência (%s, %d imagens): %s", card.get("id"), source,
             len(images), identity_text(look))
    return look


def _visible(value: str) -> bool:
    return bool(value) and not _NOT_VISIBLE.search(value)


def identity_text(look: dict | None) -> str:
    """Rosto, cabelo, óculos, pele e porte: nunca mudam com a roupa da cena."""
    if not look:
        return ""
    hair = ", ".join(part for part in (
        look.get("hair_length"), look.get("hair_texture"), look.get("hair_colour")) if part)
    bits = [f"{hair} hair" if hair else ""]
    if look.get("hairstyle") and _visible(look["hairstyle"]):
        bits.append(f"hairstyle: {look['hairstyle']}")
    glasses = look.get("glasses") or ""
    if re.match(r"(?i)^\s*yes\b", glasses):
        glasses = "glasses"
    elif glasses and not re.search(r"(?i)glasses|spectacles|eyeglasses", glasses):
        glasses = f"{glasses} glasses"
    if glasses and not _NONE.search(glasses):
        bits.append(f"always wearing {glasses}")
    elif glasses:
        bits.append("no glasses")
    facial = look.get("facial_hair") or ""
    if facial and not _NONE.search(facial) and _visible(facial):
        bits.append(facial)
    for key in ("skin", "eyes", "face", "build"):
        value = look.get(key) or ""
        if value and _visible(value) and not value.lower().startswith("no distinctive"):
            bits.append(f"{key}: {value}")
    return "; ".join(bit for bit in bits if bit)


def visible_clothing(look: dict | None) -> dict[str, str]:
    """Peças que a referência mostra; as outras ficam para a trava de roupa."""
    if not look:
        return {}
    found = {}
    for key in CLOTHING_KEYS:
        value = look.get(key) or ""
        if _visible(value):
            found[key] = value
    return found
