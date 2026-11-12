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
# v6: estampa vira "forma abstrata" ("graphic print" ainda puxava letras).
# v7: detalhe fino com intensidade (pele, estrutura do rosto, sobrancelhas,
# cachos, expressões) e corpo pela foto de corpo inteiro junto da descrição
# do card (a Renata saía mais larga que a foto do espelho).
LOOK_VERSION = 7
# Até 12 fotos (a principal primeiro); a leitura escolhe as de rosto.
MAX_PHOTOS = 12
HAIR_KEYS = ("hair_colour", "hair_dye", "hair_sheen", "hair_curl", "hair_volume",
             "hair_parting", "hair_length", "hairstyle")
SKIN_KEYS = ("skin_tone", "skin_texture", "skin_marks", "freckles", "skin_shine")
FACE_KEYS = ("face_shape", "cheeks", "jawline", "chin", "nose", "lips", "eyes",
             "lids_lashes", "eyebrows", "face")
EXPRESSION_KEYS = ("smile", "neutral")
BODY_KEYS = ("build", "height", "shoulders", "waist", "hips", "limbs")
IDENTITY_KEYS = (*HAIR_KEYS, "glasses", "facial_hair", *SKIN_KEYS, *FACE_KEYS,
                 *EXPRESSION_KEYS, *BODY_KEYS)
CLOTHING_KEYS = ("top", "bottom", "footwear", "accessories")
_NOT_VISIBLE = re.compile(r"(?i)^\s*(not visible|unknown|n/?a|unclear|cannot|can't)\b")
_NONE = re.compile(r"(?i)^\s*(none|no|nothing)\b")
# Estampa com letras vira texto ilegível na pintura. A peça guarda só "grafismo".
_TEXT_PRINT = re.compile(
    r"(?i)\b(?:(?:white|black|grey|gray|yellow|red|light|dark|bold|small|large|faded)\s+)*"
    r"(?:text|lettering|letters|words|writing|slogan|logo|typography|font|caption|quote)"
    r"(?:\s+(?:print|graphic|design))?\b")
TEXT_PRINT_PHRASE = "small faded light-grey abstract shape"

_SYSTEM = """You read reference images of one fictional RPG character and fix the character's canonical look.
Reply with ONLY one JSON object with exactly these string keys:
hair_colour, hair_dye, hair_sheen, hair_curl, hair_volume, hair_parting, hair_length, hairstyle, glasses, facial_hair,
skin_tone, skin_texture, skin_marks, freckles, skin_shine, face_shape, cheeks, jawline, chin, nose, lips, eyes,
lids_lashes, eyebrows, face, smile, neutral, build, height, shoulders, waist, hips, limbs; plus "face_images"
(array of up to 3 integers, best first) and "glasses_image" (integer, 0 if none).
Rules:
- English, concrete, 2 to 16 words per key. Say HOW STRONG each trait is ("very", "strongly", "slightly",
  "moderately"), not only whether it is there. Never name or identify the person; no age, ethnicity or biography.
- Photos may have colour filters, warm lamps or black-and-white: judge colours from the images with neutral light.
- Hair, face, skin and expressions come from the photos ONLY, never from the card description. They describe
  what MOST images show; image 1 wins ties.
Hair:
- hair_colour: ONLY the base colour words, no undertones or strength words, e.g. "near-black", "dark brown". Black
  with a slight warmth is "near-black"; any warmth belongs in hair_sheen. hair_dye: ONLY clearly dyed sections, e.g. "bright copper dyed ends"; otherwise "none".
  hair_sheen: a natural tint visible only in light, e.g. "faint warm reddish tint in bright light"; otherwise "none".
- hair_curl: pattern and curl size, e.g. "tight springy 3b ringlets, small to medium curls". hair_volume: e.g.
  "very voluminous". hair_parting: e.g. "loose off-centre parting". hair_length: the body landmark where the hair
  ends as it naturally falls in the full-body photos (curls are not stretched), e.g. "just past the shoulders".
- glasses: frames with thickness, shape, size and ONE colour, e.g. "thin round silver metal wire frames, medium
  size"; "none" if the character does not wear glasses in most images. Never just "yes".
Skin:
- skin_tone: tone and undertone, e.g. "medium tan with warm golden undertone". skin_texture: e.g. "smooth, fine
  pores". skin_marks: moles, scars or marks WITH their position, e.g. "small dark mole above the left lip"; "none".
  freckles: density and where, or "none". skin_shine: e.g. "slight natural sheen on cheekbones".
Face structure:
- face_shape, cheeks (fullness), jawline, chin, nose (bridge, tip and width), lips (fullness, shape and colour), eyes
  (shape and size), lids_lashes (eyelid type, lash length), eyebrows (thickness, arch, colour and grooming, e.g.
  "very thick dark brown strongly arched groomed brows"). face: a mask, prosthetic or other distinctive feature,
  otherwise "no distinctive marks".
Expressions:
- smile: how smiling reshapes the face in the photos, e.g. "cheeks lift into round apples, eyes narrow to crescents,
  wide full-lipped smile". neutral: e.g. "soft heavy-lidded gaze, closed full lips".
Body (from the photos only, judging proportions from full-body photos and how the clothes sit; write "not
visible" for anything no photo shows; describe plainly and non-sexually):
- Use absolute words compared with an average adult, because image models read "slim" as average:
  build (thickness and frame, e.g. "very slim, petite, small narrow frame" or "curvy, average frame"), height
  (impression, e.g. "short"), shoulders (e.g. "narrow shoulders"), waist (e.g. "very narrow waist, flat stomach"),
  hips (absolute width AND relation, e.g. "narrow hips, only slightly wider than the waist"), limbs (thickness, e.g.
  "thin arms and thin straight legs").
Face images:
- face_images: up to 3 images, best first, where the whole face is clearly visible and frontal: no phone, hand or
  object covering ANY part of the face, natural colours (no strong colour filter, no bleached or warm tint, not
  black-and-white). glasses_image: the single clearest such image where the glasses are worn; 0 if none."""


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


def _basis_hash(source: str, images: list[bytes], about: str = "") -> str:
    digest = hashlib.sha256(f"{source}|{about}".encode())
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
    try:
        look["glasses_image"] = max(0, int(data.get("glasses_image") or 0))
    except (TypeError, ValueError):
        look["glasses_image"] = 0
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
    return f"{head} with a {TEXT_PRINT_PHRASE} on the chest"


_BODY_TEXT_SYSTEM = """You extract body proportions stated in a character description.
Reply with ONLY one JSON object with exactly these string keys: build, height, shoulders, waist, hips, limbs.
Use only what the text states, plainly and non-sexually, 2 to 10 words each; "not stated" when it says nothing."""

_CLOTHING_SYSTEM = """You read the clothing of one fictional character in ONE reference photo.
Reply with ONLY one JSON object with exactly these string keys: top, bottom, footwear, accessories.
- Look at the whole photo, including legs and feet in a mirror or full-body shot, before writing "not visible".
- Each with exact colour, garment type, cut and material, e.g. "black high-waisted skinny jeans",
  "light grey low-top canvas sneakers with white soles"; "not visible" only for a piece the photo does not show.
- A print is described only as a graphic, its colour and size; never text, letters, words or logos.
- accessories: jewellery, watches, bracelets, belts, bags, headwear named exactly (a bandana is not a hat); "none".
- Never name or identify the person. No nudity."""


def description(card: dict | None) -> str:
    """Texto do card que completa o que a foto não mostra (corpo, altura)."""
    if not card:
        return ""
    sheet = npc_assets.parse_sheet(card.get("sheet") or "")
    parts = [" ".join(str(card.get("entry") or "").split())]
    for key in ("appearance", "body"):
        value = sheet.get(key)
        if isinstance(value, str) and value.strip():
            parts.append(" ".join(value.split()))
    return " | ".join(part for part in parts if part)[:1500]


def _read(images: list[bytes], *, transport=None, llm_cfg: dict, about: str = "") -> dict | None:
    # A descrição do card não entra nesta leitura: com ela no contexto o modelo
    # copiava "cabelo até as costas" e "quadril pronunciado" do texto por cima
    # do que as fotos mostram. Ela só completa o corpo, depois, em separado.
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
            role="assistant", temperature=0, max_tokens=1100, timeout_s=240,
            transport=transport,
        )
    except Exception as exc:  # visão fora do ar não derruba quem pinta
        log.warning("look: visão indisponível: %s: %s", type(exc).__name__, exc)
        return None
    look = _parse(reply)
    if look is None:
        return None
    # Roupa numa leitura própria só da foto principal: no meio dos traços do
    # rosto e do corpo o modelo dizia "not visible" para a calça e o tênis que
    # o espelho mostra inteiros.
    clothing = _read_clothing(images[0], transport=transport, llm_cfg=llm_cfg)
    for key in CLOTHING_KEYS:
        look[key] = clothing.get(key, "not visible")
    look["top"] = no_text(look["top"])
    return merge_body(look, _read_body_text(about, transport=transport, llm_cfg=llm_cfg))


def merge_body(look: dict, stated: dict) -> dict:
    """A foto manda no que mostra; a descrição completa o resto. Altura a foto
    não mede: a do texto vale quando existe."""
    look = dict(look)
    sources = {}
    for key in BODY_KEYS:
        said = stated.get(key, "")
        said = said if said and not re.match(r"(?i)^\s*not stated", said) else ""
        if key == "height" and said:
            look[key], sources[key] = said, "description"
        elif _value(look, key):
            sources[key] = "photo"
        elif said:
            look[key], sources[key] = said, "description"
    look["body_sources"] = sources
    return look


def _read_body_text(about: str, *, transport=None, llm_cfg: dict) -> dict:
    if not about:
        return {}
    try:
        reply = llm_client.chat_completion(
            llm_cfg, [{"role": "system", "content": _BODY_TEXT_SYSTEM},
                      {"role": "user", "content": about}],
            role="assistant", temperature=0, max_tokens=200, timeout_s=60, transport=transport)
    except Exception as exc:
        log.warning("look: corpo da descrição sem leitura: %s", type(exc).__name__)
        return {}
    match = re.search(r"\{.*\}", reply or "", flags=re.S)
    try:
        data = json.loads(match.group(0)) if match else {}
    except ValueError:
        return {}
    return {key: _clean(data.get(key)) for key in BODY_KEYS
            if isinstance(data, dict) and _clean(data.get(key))}


def _read_clothing(image: bytes, *, transport=None, llm_cfg: dict) -> dict:
    encoded = base64.b64encode(_png_small(image)).decode("ascii")
    try:
        reply = llm_client.chat_completion(
            llm_cfg, [{"role": "system", "content": _CLOTHING_SYSTEM},
                      {"role": "user", "content": [
                          {"type": "text", "text": "Fill the JSON for this photo."},
                          {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{encoded}"}}]}],
            role="assistant", temperature=0, max_tokens=300, timeout_s=120, transport=transport)
    except Exception as exc:
        log.warning("look: roupa sem leitura: %s", type(exc).__name__)
        return {}
    match = re.search(r"\{.*\}", reply or "", flags=re.S)
    try:
        data = json.loads(match.group(0)) if match else {}
    except ValueError:
        return {}
    return {key: _clean(data.get(key)) for key in CLOTHING_KEYS
            if isinstance(data, dict) and _clean(data.get(key))}


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
    about = description(card)
    basis = _basis_hash(source, images, about)
    stored = current(card)
    if stored and stored.get("basis_hash") == basis:
        return stored
    if llm_cfg is None:
        llm_cfg = settings_store.effective_llm_settings(conn)
    look = _read(images, transport=transport, llm_cfg=llm_cfg, about=about)
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
             len(images), identity_text(look, level="full"))
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
    picks = list(look.get("face_images") or []) if look and look.get("source") == "photo" else []
    # Quem usa óculos: o melhor rosto e a melhor foto de óculos. Sem isso a
    # segunda escolhida podia ser uma selfie com filtro que clareava o cabelo.
    if picks and look and _glasses(look).startswith("always") and look.get("glasses_image"):
        picks = [picks[0], int(look["glasses_image"]), *picks[1:]]
    chosen = [photos[n - 1] for n in picks if isinstance(n, int) and 0 < n <= len(photos)]
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


def _value(look: dict, key: str) -> str:
    value = look.get(key) or ""
    if not isinstance(value, str) or not _visible(value) or _NONE.search(value):
        return ""
    if value.lower().startswith(("no distinctive", "not visible")):
        return ""
    return value


_LABELS = {"face_shape": "face", "cheeks": "cheeks", "jawline": "jaw", "chin": "chin",
           "nose": "nose", "lips": "lips", "eyes": "eyes", "lids_lashes": "lids",
           "eyebrows": "brows", "skin_tone": "skin"}


def _labelled(look: dict, key: str) -> str:
    """O valor com o nome do traço quando ele não aparece no texto
    ("dark brown" sozinho não diz que são os olhos)."""
    value = _value(look, key)
    label = _LABELS.get(key, key)
    if not value or label in value.lower() or label.rstrip("s") in value.lower():
        return value
    return f"{label}: {value}"


def _glasses(look: dict) -> str:
    glasses = look.get("glasses") or ""
    if re.match(r"(?i)^\s*yes\b", glasses):
        glasses = "glasses"
    elif glasses and not re.search(r"(?i)glasses|spectacles|eyeglasses", glasses):
        glasses = f"{glasses} glasses"
    if glasses and not _NONE.search(glasses):
        return f"always wearing {glasses}"
    return "no glasses" if glasses else ""


def _hair(look: dict, *, detail: bool) -> str:
    curl = _value(look, "hair_curl") or _value(look, "hair_texture")
    parts = [_value(look, "hair_length"), curl if detail else (curl.split(",")[0] if curl else ""),
             _value(look, "hair_colour")]
    if detail:
        parts.insert(2, _value(look, "hair_volume"))
    hair = ", ".join(part for part in parts if part)
    if not hair:
        return ""
    hair += " hair"
    if _value(look, "hair_dye"):
        hair += f" with {look['hair_dye']}"
    elif _value(look, "hair_sheen") and detail:
        # Brilho natural, não tinta: dito como cor, o Flux pintava pontas vinho.
        hair += f", uniformly dark from roots to ends, only {look['hair_sheen']}"
    if detail and _value(look, "hair_parting"):
        hair += f", {look['hair_parting']}"
    return hair


def _body(look: dict, *, detail: bool) -> str:
    keys = BODY_KEYS if detail else ("build", "waist", "hips")
    text = []
    for key in keys:
        value = _value(look, key)
        if not value:
            continue
        low = value.lower()
        if key == "build":
            text.append(value if "build" in low else f"{value} build")
        elif key in ("height", "hips"):
            text.append(value if key[:3] in low else f"{key} {value}")
        elif key == "limbs":
            text.append(value if re.search(r"arm|leg|limb", low) else f"{value} limbs")
        else:
            text.append(value if key.rstrip("s") in low else f"{value} {key}")
    return ("body: " + ", ".join(text)) if text else ""


# Quanto do look cada pintura leva. O close-up e o mestre levam tudo; as
# vistas, o que distingue a pessoa de longe; a cena com uma pessoa, a âncora
# de identidade com o sorriso; a cena com várias, o mínimo que separa cada uma.
LEVELS = ("full", "face", "views", "scene", "compact")


def identity_text(look: dict | None, *, compact: bool = False, level: str = "") -> str:
    """Traços da referência no nível de detalhe pedido; nunca a roupa.

    `compact=True` equivale a `level="compact"` (cena com várias pessoas).
    """
    if not look:
        return ""
    level = level or ("compact" if compact else "full")
    glasses = _glasses(look)
    facial = _value(look, "facial_hair")
    mask = _value(look, "face")
    brows = _labelled(look, "eyebrows")
    if level == "compact":
        # O fundo corta cada pessoa pelo fim: o que mais identifica vem antes
        # (óculos, barba, máscara/marca, sobrancelhas), corpo e cabelo depois.
        bits = [glasses, facial, f"face: {mask}" if mask else "",
                brows, _body(look, detail=False), _hair(look, detail=False)]
        return "; ".join(bit for bit in bits if bit)
    face_bits = [_labelled(look, key) for key in ("face_shape", "cheeks")]
    if level == "views":
        bits = [glasses, facial, f"face: {mask}" if mask else "", *face_bits, brows,
                _labelled(look, "skin_tone"), _hair(look, detail=False), _body(look, detail=True)]
        return "; ".join(bit for bit in bits if bit)
    detail_bits = [_labelled(look, key) for key in ("jawline", "chin", "nose", "lips", "eyes", "lids_lashes")]
    skin = "; ".join(bit for bit in (
        _value(look, "skin_tone"), _value(look, "skin_texture"),
        f"marks: {look['skin_marks']}" if _value(look, "skin_marks") else "",
        f"freckles: {look['freckles']}" if _value(look, "freckles") else "",
        _value(look, "skin_shine")) if bit)
    if level == "scene":
        bits = [glasses, facial, f"face: {mask}" if mask else "", *face_bits, brows,
                _labelled(look, "lips"), _hair(look, detail=False), _body(look, detail=False),
                f"when smiling: {look['smile']}" if _value(look, "smile") else ""]
        return "; ".join(bit for bit in bits if bit)
    bits = [glasses, _hair(look, detail=True), facial, f"face: {mask}" if mask else "",
            *face_bits, *detail_bits, brows, f"skin: {skin}" if skin else "",
            f"neutral expression: {look['neutral']}" if _value(look, "neutral") else ""]
    if level == "full":
        bits.append(_body(look, detail=True))
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
