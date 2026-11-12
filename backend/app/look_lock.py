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
import time
from io import BytesIO
from typing import Any

from PIL import Image, ImageOps

from app import db, likeness, llm_client, npc_assets, settings_store

log = logging.getLogger("aidungeon.look_lock")
# Leitura que falhou (visão fora do ar, resposta inválida) não é refeita a
# cada See: espera uma hora por card.
_FAILURE_PAUSE_S = 3600.0
_failures: dict[str, float] = {}

# v2: "dark brown with reddish highlights" virava cabelo vinho no Flux, e
# "black graphic t-shirt" ganhava estampa inventada. Cor dominante simples,
# estampa descrita, e o índice da foto com o rosto mais nítido. v3: reflexo
# em campo próprio ("reddish highlights" junto da cor ainda pintava vinho).
# v4: a bandana do Caio virou "wide-brimmed hat": chapéu e lenço nomeados.
# v5: tinta de verdade (dyed) separada do brilho natural, armação com
# espessura, rosto com formato/sobrancelhas/lábios/bochechas, estampa sem
# texto e as fotos de rosto escolhidas pela leitura entre até 12 fotos.
LOOK_VERSION = 5
# Até 12 fotos (a principal primeiro); a leitura escolhe as de rosto.
MAX_PHOTOS = 12
IDENTITY_KEYS = ("hair_colour", "hair_dye", "hair_sheen", "hair_texture", "hair_length",
                 "hairstyle", "glasses", "facial_hair", "skin", "eyes", "face_shape",
                 "eyebrows", "lips", "face", "build")
CLOTHING_KEYS = ("top", "bottom", "footwear", "accessories")
_NOT_VISIBLE = re.compile(r"(?i)^\s*(not visible|unknown|n/?a|unclear|cannot|can't)\b")
_NONE = re.compile(r"(?i)^\s*(none|no|nothing)\b")
# Estampa com letras vira texto ilegível na pintura. A peça guarda só "grafismo".
_TEXT_PRINT = re.compile(
    r"(?i)\b(?:(?:white|black|grey|gray|yellow|red|light|dark|bold|small|large|faded)\s+)*"
    r"(?:text|lettering|letters|words|writing|slogan|logo|typography|font|caption|quote)"
    r"(?:\s+(?:print|graphic|design))?\b")
TEXT_PRINT_PHRASE = "small faded light-grey graphic"

_SYSTEM = """You read reference images of one fictional RPG character and fix the character's canonical look.
Reply with ONLY one JSON object with exactly these keys:
"hair_colour", "hair_dye", "hair_sheen", "hair_texture", "hair_length", "hairstyle", "glasses", "facial_hair",
"skin", "eyes", "face_shape", "eyebrows", "lips", "face", "build", "top", "bottom", "footwear", "accessories"
(strings) and "face_images" (array of up to 3 integers, best first).
Rules:
- English, concrete and precise, 2 to 14 words per key.
- Photos may have colour filters, warm lamps or black-and-white: judge colours from the images with neutral light.
- hair_colour: the natural base colour of the hair as it looks in neutral light, plainly, e.g. "near-black",
  "dark brown", "jet black". Hair that reads as black with only a slight warmth is "near-black".
- hair_dye: ONLY clearly dyed sections that look dyed in most images, e.g. "bright copper dyed ends"; otherwise "none".
  A natural warm or reddish tint that shows only in some light is NOT dye: write "none" and use hair_sheen.
- hair_sheen: a natural tint visible only in light, e.g. "faint warm reddish tint in bright light"; otherwise "none".
- hair_length must name a body landmark the hair reaches, e.g. "chin length", "just past the shoulders", "mid-back".
- glasses: if the character wears glasses in most images, describe the frames exactly with thickness, shape, size and
  ONE colour, e.g. "thin round silver metal wire frames, medium size"; otherwise "none". Never answer just "yes".
- face_shape: e.g. "round face with full soft cheeks". eyebrows: e.g. "thick dark strongly arched eyebrows".
  lips: e.g. "full lips". face: other distinctive features or a mask/prosthetic; otherwise "no distinctive marks".
- Identity keys (hair, glasses, facial hair, skin, eyes, face shape, eyebrows, lips, face, build) describe what MOST
  images show; image 1 wins ties.
- Clothing keys describe only what IMAGE 1 shows, each with exact colour, garment type, cut and material, e.g.
  "black high-waisted skinny jeans", "light grey low-top canvas sneakers with white soles"; write "not visible" for
  a piece image 1 does not show.
  A print on clothing is described only as a graphic, its colour and size, e.g. "small faded light-grey graphic on
  the chest"; never mention text, letters, words or logos. Say "plain" when there is no print.
- accessories: name headwear exactly as it looks (a bandana or headscarf is not a hat; say "tricorn hat",
  "wide-brimmed hat", "bandana" only when that is what is there), plus belts, straps, jewellery, bags.
- face_images: the numbers (1 = first) of up to 3 images, best first, where the whole face is clearly visible and
  frontal: no phone, hand or object covering ANY part of the face, natural colours (no strong colour filter, not
  black-and-white). Include one with glasses when the character wears glasses.
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
    look: dict[str, Any] = {key: _clean(data.get(key)) for key in (*IDENTITY_KEYS, *CLOTHING_KEYS)}
    if not look["hair_colour"] or not look["hair_length"] or not look["glasses"]:
        return None
    look["top"] = no_text(look["top"])
    picks = data.get("face_images")
    if not isinstance(picks, list):
        picks = [picks] if picks is not None else []
    faces: list[int] = []
    for value in picks:
        try:
            number = int(value)
        except (TypeError, ValueError):
            continue
        if number >= 1 and number not in faces:
            faces.append(number)
    look["face_images"] = faces[:3] or [1]
    return look


_PRINT_WORDS = re.compile(
    r"(?i)\b(graphic|print|printed|design|logo|text|lettering|letters|words|writing|slogan|"
    r"typography|illustration|artwork|motif|emblem)\b")


def no_text(value: str) -> str:
    """Toda estampa vira o mesmo grafismo pequeno e apagado, sem letras.

    "white text print" pintava texto ilegível; "large white floral graphic"
    pintava flores enormes que a foto não tem. Peça estampada passa a dizer
    só onde a estampa fica, sempre igual de uma rodada para outra.
    """
    if not value or not _PRINT_WORDS.search(value):
        return value
    head = re.split(r"(?i)\s+(?:with|featuring|showing|bearing|that has|having)\s+", value, maxsplit=1)[0]
    head = _PRINT_WORDS.sub("", head)
    head = " ".join(head.split()).strip(" ,;")
    return f"{head} with a {TEXT_PRINT_PHRASE} print on the chest"


def _read(images: list[bytes], *, transport=None, llm_cfg: dict) -> dict | None:
    content: list[dict] = [{"type": "text", "text": (
        f"{len(images)} reference image(s) of the same character follow; image 1 is the "
        "primary reference. Fill the JSON.")}]
    for number, blob in enumerate(images, start=1):
        # Rótulo antes de cada imagem: com 12 fotos a numeração escorregava.
        encoded = base64.b64encode(_png_small(blob)).decode("ascii")
        content.append({"type": "text", "text": f"Image {number}:"})
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
        _failures[str(card.get("id"))] = time.monotonic()
        return stored
    _failures.pop(str(card.get("id")), None)
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


def primary_photo(card: dict) -> bytes | None:
    """A foto principal (roupa e corpo); None sem foto ou para menor."""
    if likeness.is_minor(card):
        return None
    photos = likeness.visual_reference_images(card)[:1]
    return photos[0] if photos else None


def face_photos(card: dict) -> list[bytes]:
    """Até duas fotos de rosto escolhidas pela leitura (nítidas, de frente,
    cores naturais, com óculos se a pessoa usa); sem leitura, a principal."""
    if likeness.is_minor(card):
        return []
    photos = likeness.visual_reference_images(card)[:MAX_PHOTOS]
    if not photos:
        return []
    look = current(card)
    picks = look.get("face_images") if look and look.get("source") == "photo" else None
    chosen = [photos[n - 1] for n in (picks or []) if isinstance(n, int) and 0 < n <= len(photos)]
    # A leitura às vezes escolhe uma selfie de espelho com o celular na frente
    # do rosto (tinha óculos). O detector confere: rosto de frente e grande.
    chosen = [_largest_copy(photo, photos) for photo in chosen]
    usable = []
    for photo in chosen:
        if _frontal_face(photo) and photo not in usable:
            usable.append(photo)
    return usable[:2] or chosen[:1] or photos[:1]


def _thumb_hash(photo: bytes) -> int | None:
    try:
        with Image.open(BytesIO(photo)) as source:
            small = ImageOps.exif_transpose(source).convert("L").resize((16, 16), Image.Resampling.BILINEAR)
    except Exception:
        return None
    pixels = list(small.getdata())
    mean = sum(pixels) / len(pixels)
    return sum(1 << i for i, value in enumerate(pixels) if value > mean)


def _size(photo: bytes) -> int:
    try:
        with Image.open(BytesIO(photo)) as source:
            return source.width * source.height
    except Exception:
        return 0


def _largest_copy(photo: bytes, photos: list[bytes]) -> bytes:
    """A mesma foto em resolução maior, se houver (a da Renata de óculos estava
    também como miniatura de 150 px, e a leitura escolheu a miniatura)."""
    mine = _thumb_hash(photo)
    if mine is None:
        return photo
    best = photo
    for other in photos:
        other_hash = _thumb_hash(other)
        if other_hash is not None and bin(mine ^ other_hash).count("1") <= 20 \
                and _size(other) > _size(best):
            best = other
    return best


def _frontal_face(photo: bytes) -> bool:
    """Rosto detectado, de frente (nariz entre os olhos) e não minúsculo."""
    try:
        import cv2
        import numpy as np
        from app import turnaround

        with Image.open(BytesIO(photo)) as source:
            image = ImageOps.exif_transpose(source).convert("RGB")
        image.thumbnail((1024, 1024))
        pose = turnaround.face_pose(np.asarray(image))
    except Exception:
        return True          # sem detector, confia na leitura
    if pose is None:
        return True
    found, eyes, yaw, size, _below = pose
    return bool(found) and eyes >= 0.33 and abs(yaw) <= 0.15 and size >= 0.2


# Recorte do rosto com cabelo: o rosto ocupa ~40 % da altura; cabelo e
# pescoço entram, roupa quase não. Lado maior de pelo menos 1024 px: o grafo
# leva cada referência a um total fixo de pixels e foto pequena virava bloco.
FACE_CROP_SIDE = 1024


def face_crop(photo: bytes) -> bytes | None:
    """Rosto e cabelo recortados da foto, em PNG grande; None sem rosto."""
    try:
        with Image.open(BytesIO(photo)) as source:
            image = ImageOps.exif_transpose(source).convert("RGB")
    except Exception:
        return None
    box = likeness._largest_face_box(image)
    if not box:
        return None
    x, y, w, h = box
    left = max(0, round(x - 0.75 * w))
    right = min(image.width, round(x + w + 0.75 * w))
    top = max(0, round(y - 0.8 * h))
    bottom = min(image.height, round(y + h + 0.45 * h))
    if right - left < 16 or bottom - top < 16:
        return None
    crop = image.crop((left, top, right, bottom))
    scale = FACE_CROP_SIDE / max(crop.size)
    if scale > 1:
        crop = crop.resize((round(crop.width * scale), round(crop.height * scale)),
                           Image.Resampling.LANCZOS)
    out = BytesIO()
    crop.save(out, format="PNG")
    return out.getvalue()


def face_references(card: dict) -> list[bytes]:
    """Recortes de rosto das fotos de rosto (a foto inteira se não achar rosto)."""
    found = []
    for photo in face_photos(card):
        found.append(face_crop(photo) or photo)
    return found


def _visible(value: str) -> bool:
    return bool(value) and not _NOT_VISIBLE.search(value)


def identity_text(look: dict | None, *, compact: bool = False) -> str:
    """Rosto, cabelo, óculos, pele e porte: nunca mudam com a roupa da cena.

    `compact` (cena com mais gente): cabelo, óculos, barba e o rosto (máscara,
    marca); o resto fica para as referências visuais.
    """
    if not look:
        return ""
    hair = ", ".join(part for part in (
        look.get("hair_length"), look.get("hair_texture"), look.get("hair_colour")) if part)
    bits = [f"{hair} hair" if hair else ""]
    dye = look.get("hair_dye") or ""
    sheen = look.get("hair_sheen") or ""
    if hair and dye and not _NONE.search(dye) and _visible(dye):
        bits[0] += f" with {dye}"
    elif hair and sheen and not _NONE.search(sheen) and _visible(sheen):
        # Brilho natural, não tinta: dito como cor, o Flux pintava pontas vinho.
        bits[0] += f", uniformly dark from roots to ends, only {sheen}"
    if look.get("hairstyle") and _visible(look["hairstyle"]) and not compact:
        bits.append(f"hairstyle: {look['hairstyle']}")
    glasses = look.get("glasses") or ""
    if re.match(r"(?i)^\s*yes\b", glasses):
        glasses = "glasses"
    elif glasses and not re.search(r"(?i)glasses|spectacles|eyeglasses", glasses):
        glasses = f"{glasses} glasses"
    # Óculos primeiro: o fundo corta um pouco de cada pessoa pelo fim, e os
    # óculos da Renata eram justamente o que sumia.
    if glasses and not _NONE.search(glasses):
        bits.insert(0, f"always wearing {glasses}")
    elif glasses:
        # Também na cena com várias pessoas: no fundo de produção os óculos da
        # Renata passaram para o Caio, que não usa.
        bits.insert(0, "no glasses")
    facial = look.get("facial_hair") or ""
    facial = facial if facial and not _NONE.search(facial) and _visible(facial) else ""
    if compact:
        # Cena com várias pessoas: o fundo corta cada uma pelo fim. Na frente o
        # que mais identifica (óculos, máscara/marca do rosto, barba); o cabelo
        # por último. A caveira do Caio sumia do fundo sem isso.
        face = look.get("face") or ""
        face = face if face and _visible(face) and not face.lower().startswith(
            ("no distinctive", "full lips", "smooth")) else ""
        hair_bit = next((bit for bit in bits if bit.endswith("hair") or " hair with " in bit), "")
        head = [bit for bit in bits if bit.startswith(("always wearing", "no glasses"))]
        # Barba antes do rosto: "clean-shaven except for beard" cortado no fim
        # virava "clean-shaven" e o Caio de produção saiu sem barba.
        ordered = head + ([facial] if facial else []) + ([f"face: {face}"] if face else []) \
            + ([hair_bit] if hair_bit else [])
        return "; ".join(bit for bit in ordered if bit)
    if facial:
        bits.append(facial)
    for key in ("face_shape", "eyebrows", "lips"):
        value = look.get(key) or ""
        if value and _visible(value):
            bits.append(value)
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


def ensure_many(adventure_id: str, cards: list[dict], *, transport=None,
                llm_cfg: dict | None = None, limit: int = 6) -> list[dict]:
    """Lê o look de quem tem referência e ainda não tem look; devolve os cards
    atualizados. Teto por chamada: elenco grande não trava o See."""
    now = time.monotonic()
    pending = [card for card in cards
               if card.get("type") == "character" and current(card) is None
               and now - _failures.get(str(card.get("id")), -_FAILURE_PAUSE_S) >= _FAILURE_PAUSE_S
               and not likeness.is_minor(card)
               and (likeness.visual_reference_images(card)[:1]
                    or card.get("portrait_anchor_id") or card.get("portrait_image_id"))]
    if not pending:
        return cards
    updated: dict[str, dict] = {}
    for card in pending[:limit]:
        try:
            with db.session() as conn:
                if ensure(conn, card, transport=transport, llm_cfg=llm_cfg):
                    fresh = db.get_card(conn, adventure_id, card["id"])
                    if fresh:
                        updated[card["id"]] = fresh
        except Exception as exc:  # leitura falhou: o See segue com o que tem
            log.warning("look: card %s sem leitura: %s", card.get("id"), type(exc).__name__)
    return [updated.get(card.get("id"), card) for card in cards]
