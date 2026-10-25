"""Ficha de pessoa real (contrato C8) e o guardrail do que pode ser pintado.

O bloqueio é só para menores: menor nunca entra em nudez ou sexo, e não
ganha foto de referência nem retrato. Pessoa real adulta pode ser pintada,
inclusive em cena explícita, quando o usuário marcou o card assim.

A imagem principal da Wikipedia (REST page/summary) vai para data/references
com a URL e a licença. Upload manual tem prioridade sobre esse download.
Uma foto de NPC fictício fica em sheet["reference"] e não marca pessoa real.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import re
import time
import urllib.parse
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Callable

import httpx

from app import db, llm_client, npc_assets
from app.errors import GameError
from app.image_plan import FaceRef

log = logging.getLogger("aidungeon.likeness")

CODE_MINOR = "minor"
MSG_MINOR = "Não gero nudez nem sexo com menor."
# Retrato de pessoa real vindo da Wikipedia, sempre a partir da foto.
# Abaixo da âncora fictícia (0.35): 0.45 já é denoise alto demais para o rosto
# da foto sobreviver a outro enquadramento.
REFERENCE_DENOISE = 0.25
# Foto enviada pelo usuário. 0.15 segura o rosto; 0.30 já troca a pessoa.
VISUAL_REFERENCE_DENOISE = 0.15
MAX_REFERENCES = 3
MAX_IMAGE_BYTES = 8 * 1024 * 1024

_USER_AGENT = "AIDungeonLocal/0.9 (local adventure; likeness reference)"

_PT_MARK = re.compile(r"[áàâãéêíóôõúçÁÀÂÃÉÊÍÓÔÕÚÇ]")
_EXPLICIT_STRIP = re.compile(
    r"(?i)\b(?:naked|nude|nudes|topless|bottomless|undressed|undressing|"
    r"uncensored|sex|sexual|fuck(?:ing)?|orgasm|penis|vagina|nipples?|"
    r"intercourse|nua|nuas|nus|pelad[oa]s?)\b|clothes off|do not restore outfits"
)
_FICTIONAL = re.compile(
    r"(?i)\b(?:fictional|fictitious|fictício|ficticio|personagem fictício|"
    r"anime character|comic character|mythological)\b"
)
_BORN = re.compile(
    r"(?i)(?:born|nascimento|nascid[oa]|\bn\.)\D{0,48}((?:1[5-9]|20)\d{2})"
)
_DIED = re.compile(
    r"(?i)(?:died|morte|falecid[oa]|\bm\.)\D{0,48}((?:1[5-9]|20)\d{2})"
)
_AGE = re.compile(r"(?i)\b(\d{1,2})\s*[- ]years?[- ]old\b")
_IMAGE_EXT = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp"}


@dataclass
class FrameDecision:
    cast: list[dict]
    allow_explicit: bool
    refusal: str | None = None
    message: str = ""


@dataclass
class WikiPage:
    title: str
    extract: str
    description: str
    image_url: str
    thumb_url: str
    page_url: str
    fictional: bool
    minor: bool
    disambiguation: bool

    @property
    def is_real_adult(self) -> bool:
        return not self.fictional and not self.minor and not self.disambiguation and bool(self.title)


def sheet_of(card: dict) -> dict:
    return npc_assets.parse_sheet(card.get("sheet") or "")


def likeness_of(card: dict) -> dict:
    raw = sheet_of(card).get("likeness")
    return raw if isinstance(raw, dict) else {}


def is_real_person(card: dict) -> bool:
    if card.get("type") not in (None, "", "character"):
        return False
    return sheet_of(card).get("real_person") is True


def is_minor(card: dict) -> bool:
    sheet = sheet_of(card)
    if sheet.get("minor") is True:
        return True
    try:
        age = sheet.get("age")
        if age is not None and int(age) < 18:
            return True
    except (TypeError, ValueError):
        return False
    return False


def display_name(card: dict) -> str:
    name = " ".join(str(sheet_of(card).get("real_name") or "").split())
    if name:
        return name
    return " ".join(str(card.get("title") or "").split()) or "the person"


def decide_frame(cast: list[dict], *, explicit: bool) -> FrameDecision:
    """Quem pode ficar no quadro e se a pintura pode ser explícita.

    Menor em cena explícita recusa o quadro inteiro. Pessoa real adulta fica.
    """
    minors = [card for card in cast if is_minor(card)]
    if explicit and minors:
        return FrameDecision([], False, CODE_MINOR, MSG_MINOR)
    return FrameDecision(list(cast), bool(explicit), None, "")


def strip_explicit(text: str) -> str:
    """Tira nudez e sexo de um prompt que não pode ser explícito."""
    cleaned = _EXPLICIT_STRIP.sub(" ", text or "")
    cleaned = re.sub(r"\s+([,.;])", r"\1", cleaned)
    cleaned = re.sub(r"(?:\s*,\s*){2,}", ", ", cleaned)
    return " ".join(cleaned.split()).strip(" ,.;")


def english_bits(text: str, limit: int = 220) -> str:
    """Primeira frase em inglês. Um nome acentuado (Júnior) não apaga o resto.

    Frase em português, com acento espalhado, não entra no CLIP.
    """
    flat = " ".join((text or "").split())
    if not flat:
        return ""
    sentence = re.split(r"(?<=[.!?])\s+", flat)[0]
    words = sentence.split()
    if not words:
        return ""
    accented = sum(1 for word in words if _PT_MARK.search(word))
    if accented / len(words) > 0.25:
        return ""
    kept = [word for word in words if not _PT_MARK.search(word)]
    sentence = " ".join(kept)
    if not sentence or _PT_MARK.search(sentence):
        return ""
    return sentence[:limit].rstrip(" ,;:")


def english_descriptor(card: dict) -> str:
    like = likeness_of(card)
    desc = english_bits(str(like.get("descriptor") or ""))
    if desc:
        return desc
    return english_bits(str(sheet_of(card).get("appearance") or ""))


def opening_clause(cards: list[dict], *, explicit: bool = False) -> str:
    """Nome e aparência na frente; foto enviada substitui descrição facial.

    Quando há foto de referência, o texto não descreve cabelo, olhos ou rosto:
    esses traços vêm da imagem. Descrição concorrente fazia o SD trocar a pessoa.
    """
    reals = [card for card in cards if is_real_person(card) and not is_minor(card)]
    others = [card for card in cards if card not in reals]
    rest = npc_assets.likeness_clause(
        [card for card in others if not has_reference_image(card)],
        explicit=explicit,
    )
    photo_names = [
        (card.get("title") or "").strip()
        for card in others if has_reference_image(card)
    ]
    if not reals and not photo_names:
        return rest
    bits: list[str] = []
    for card in reals:
        if has_reference_image(card):
            bits.append(display_name(card))
            continue
        desc = english_descriptor(card)
        if not explicit:
            desc = strip_explicit(desc)
        bits.append(f"{display_name(card)}: {desc}" if desc else display_name(card))
    bits.extend(name for name in photo_names if name)
    lead = "exact likeness of " + ", ".join(bits)
    if not rest:
        return lead
    prefix = "exact likeness, "
    tail = rest[len(prefix):] if rest.startswith(prefix) else rest
    return f"{lead}, {tail}"
def real_person_prefix(card: dict) -> str:
    """Abre o retrato com o nome real. Vazio se não for pessoa real adulta."""
    if not is_real_person(card) or is_minor(card):
        return ""
    desc = strip_explicit(english_descriptor(card))
    name = display_name(card)
    if desc:
        return f"exact likeness of {name}, {desc}"
    return f"exact likeness of {name}"


def face_refs_for(cards: list[dict]) -> list[FaceRef]:
    refs: list[FaceRef] = []
    for card in cards:
        if not is_real_person(card) or is_minor(card):
            continue
        path = absolute_reference(str(likeness_of(card).get("reference_image_path") or ""))
        if path is not None:
            refs.append(FaceRef(image_path=str(path)))
    return refs


def references_dir() -> Path:
    override = os.environ.get("AIDUNGEON_REFERENCES_DIR")
    if override:
        return Path(override)
    return db.REPO_ROOT / "data" / "references"


def absolute_reference(path: str) -> Path | None:
    text = " ".join((path or "").split())
    if not text:
        return None
    candidate = Path(text)
    if not candidate.is_absolute():
        candidate = db.REPO_ROOT / candidate
    try:
        resolved = candidate.resolve()
        root = references_dir().resolve()
    except OSError:
        return None
    if root not in resolved.parents and resolved != root:
        return None
    return resolved if resolved.is_file() else None


def reference_bytes(card: dict) -> bytes | None:
    path = absolute_reference(str(likeness_of(card).get("reference_image_path") or ""))
    if path is None:
        return None
    return path.read_bytes()

def has_reference_image(card: dict) -> bool:
    """Existe foto real para travar a aparência desta carta."""
    return bool(visual_reference_bytes(card) or (
        is_real_person(card) and reference_bytes(card)
    ))



def visual_reference_bytes(card: dict) -> bytes | None:
    """Foto enviada pelo usuário em sheet['reference'], fictícia ou real."""
    raw = sheet_of(card).get("reference")
    if not isinstance(raw, dict):
        return None
    path = absolute_reference(str(raw.get("image_path") or ""))
    if path is None:
        return None
    return path.read_bytes()


def reference_images(cards: list[dict]) -> list[bytes]:
    """Fotos que travam a pessoa na cena, na ordem dos cards.

    Vale a foto enviada e, se não houver, a referência de pessoa real.
    O retrato gerado não entra: ele não é a referência. Menor nunca entra.
    """
    seen: set[str] = set()
    found: list[bytes] = []
    for card in cards:
        if is_minor(card):
            continue
        blob = visual_reference_bytes(card)
        if blob is None and is_real_person(card):
            blob = reference_bytes(card)
        if not blob:
            continue
        digest = hashlib.sha256(blob).hexdigest()
        if digest in seen:
            continue
        seen.add(digest)
        found.append(blob)
        if len(found) >= MAX_REFERENCES:
            break
    return found


def portrait_init(card: dict, anchor: bytes | None, *,
                  anchor_denoise: float,
                  force_reference: bool = False) -> tuple[bytes | None, float | None]:
    """Foto enviada, senão a referência de pessoa real, senão a âncora.

    `force_reference` ignora a âncora: o retrato novo sai só da foto enviada.
    Foto enviada usa o denoise rígido. Menor real não ganha retrato.
    """
    if is_real_person(card) and is_minor(card):
        raise GameError(422, CODE_MINOR, "Não gero retrato de menor.")
    visual = visual_reference_bytes(card)
    if visual:
        return visual, VISUAL_REFERENCE_DENOISE
    if force_reference:
        return None, None
    if is_real_person(card):
        ref = reference_bytes(card)
        if ref:
            return ref, REFERENCE_DENOISE
    if anchor:
        return anchor, anchor_denoise
    return None, None


def age_class(text: str, *, today: date | None = None) -> str:
    """'minor', 'adult' ou 'unknown' a partir do texto da Wikipedia."""
    blob = text or ""
    today = today or date.today()
    born = _year(_BORN, blob)
    died = _year(_DIED, blob)
    age_match = _AGE.search(blob)
    if age_match and died is None:
        return "minor" if int(age_match.group(1)) < 18 else "adult"
    if born and died:
        return "minor" if died - born < 18 else "adult"
    if born and not died:
        years = today.year - born
        return "minor" if years <= 18 else "adult"
    return "unknown"


def _year(pattern: re.Pattern[str], text: str) -> int | None:
    match = pattern.search(text or "")
    if not match:
        return None
    return int(match.group(1))


def _client(transport: httpx.BaseTransport | None) -> httpx.Client:
    kwargs: dict[str, Any] = {
        "timeout": httpx.Timeout(20.0, connect=5.0),
        "headers": {"User-Agent": _USER_AGENT, "Accept": "application/json"},
        "follow_redirects": True,
        "trust_env": False,
    }
    if transport is not None:
        kwargs["transport"] = transport
    return httpx.Client(**kwargs)


def _summary_url(lang: str, title: str) -> str:
    slug = urllib.parse.quote(title.replace(" ", "_"), safe="")
    return f"https://{lang}.wikipedia.org/api/rest_v1/page/summary/{slug}"


def _page_from_summary(data: dict) -> WikiPage | None:
    if not isinstance(data, dict):
        return None
    title = " ".join(str(data.get("title") or "").split())
    if not title:
        return None
    kind = str(data.get("type") or "")
    extract = " ".join(str(data.get("extract") or "").split())
    description = " ".join(str(data.get("description") or "").split())
    blob = f"{description}\n{extract}"
    image = ""
    thumb_url = ""
    original = data.get("originalimage")
    thumb = data.get("thumbnail")
    if isinstance(original, dict) and original.get("source"):
        image = str(original["source"])
    if isinstance(thumb, dict) and thumb.get("source"):
        thumb_url = str(thumb["source"])
    if not image:
        image = thumb_url
    content = ((data.get("content_urls") or {}).get("desktop") or {})
    page_url = str(content.get("page") or "")
    return WikiPage(
        title=title,
        extract=extract,
        description=description,
        image_url=image,
        thumb_url=thumb_url,
        page_url=page_url,
        fictional=_FICTIONAL.search(blob) is not None,
        minor=age_class(blob) == "minor",
        disambiguation=kind == "disambiguation",
    )


def _search_title(client: httpx.Client, lang: str, query: str) -> str:
    response = client.get(
        f"https://{lang}.wikipedia.org/w/api.php",
        params={
            "action": "opensearch",
            "search": query,
            "limit": 1,
            "namespace": 0,
            "format": "json",
        },
    )
    if response.status_code != 200:
        return ""
    try:
        payload = response.json()
    except ValueError:
        return ""
    if not isinstance(payload, list) or len(payload) < 2 or not payload[1]:
        return ""
    return " ".join(str(payload[1][0]).split())


def _fetch_summary(client: httpx.Client, lang: str, title: str) -> WikiPage | None:
    response = client.get(_summary_url(lang, title))
    if response.status_code != 200:
        return None
    try:
        return _page_from_summary(response.json())
    except ValueError:
        return None


def lookup_person(query: str, transport: httpx.BaseTransport | None = None) -> WikiPage | None:
    """Página da Wikipedia para o nome. None se a rede ou o título falhar."""
    query = " ".join((query or "").split())
    if len(query) < 2:
        return None
    try:
        with _client(transport) as client:
            for lang in ("en", "pt"):
                page = _fetch_summary(client, lang, query)
                if page and not page.disambiguation:
                    return page
                found = _search_title(client, lang, query)
                if found and found.casefold() != query.casefold():
                    page = _fetch_summary(client, lang, found)
                    if page and not page.disambiguation:
                        return page
    except httpx.HTTPError as exc:
        log.warning("likeness: wikipedia falhou para %s (%s)", query, exc)
        return None
    return None


def _sniff_ext(content: bytes, content_type: str, url: str) -> str:
    if content.startswith(b"\xff\xd8\xff"):
        return ".jpg"
    if content.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png"
    if content.startswith(b"RIFF") and content[8:12] == b"WEBP":
        return ".webp"
    kind = content_type.split(";", 1)[0].strip().lower()
    if kind in _IMAGE_EXT:
        return _IMAGE_EXT[kind]
    lower = url.lower()
    for ext in (".jpg", ".jpeg", ".png", ".webp"):
        if lower.split("?", 1)[0].endswith(ext):
            return ".jpg" if ext == ".jpeg" else ext
    return ""


def _image_url(url: str) -> str:
    """Tira o utm da Wikipedia. O arquivo em si não muda."""
    return (url or "").split("?", 1)[0]


def _download(client: httpx.Client, url: str) -> tuple[bytes, str]:
    target = _image_url(url)
    response = None
    for attempt in range(2):
        response = client.get(target, headers={"Accept": "image/*,*/*"})
        if response.status_code != 429 or attempt == 1:
            break
        time.sleep(2.5)
    if response is None or response.status_code != 200:
        raise GameError(502, "likeness_unavailable", "Não consegui baixar a foto de referência.")
    content = response.content
    if not content or len(content) > MAX_IMAGE_BYTES:
        raise GameError(502, "likeness_unavailable", "A foto de referência veio vazia ou grande demais.")
    ext = _sniff_ext(content, response.headers.get("content-type", ""), target)
    if not ext:
        raise GameError(502, "likeness_unavailable", "A referência não é uma imagem.")
    return content, ext


def _download_with_fallback(client: httpx.Client, primary: str, thumb: str) -> tuple[bytes, str]:
    """O original às vezes toma 429. O thumbnail do mesmo arquivo serve."""
    urls: list[str] = []
    for url in (primary, thumb):
        cleaned = _image_url(url)
        if cleaned and cleaned not in urls:
            urls.append(cleaned)
    last: GameError | None = None
    for url in urls:
        try:
            return _download(client, url)
        except GameError as exc:
            last = exc
    if last is not None:
        raise last
    raise GameError(502, "likeness_unavailable", "Não consegui baixar a foto de referência.")


def _commons_filename(image_url: str) -> str:
    """Nome do File: no Commons, inclusive quando a URL é um thumbnail."""
    path = urllib.parse.unquote(_image_url(image_url))
    parts = [part for part in path.split("/") if part]
    if "thumb" in parts:
        index = parts.index("thumb")
        # .../thumb/a/ab/Nome.jpg/320px-Nome.jpg
        if index + 3 < len(parts):
            return parts[index + 3]
    if parts:
        return parts[-1]
    return ""


def _commons_license(client: httpx.Client, image_url: str) -> str:
    name = _commons_filename(image_url)
    if not name:
        return ""
    try:
        response = client.get(
            "https://commons.wikimedia.org/w/api.php",
            params={
                "action": "query",
                "titles": f"File:{name}",
                "prop": "imageinfo",
                "iiprop": "extmetadata",
                "format": "json",
            },
        )
        if response.status_code != 200:
            return ""
        pages = ((response.json().get("query") or {}).get("pages") or {})
    except (httpx.HTTPError, ValueError):
        return ""
    for page in pages.values():
        info = (page.get("imageinfo") or [{}])[0]
        meta = (info.get("extmetadata") or {})
        short = (meta.get("LicenseShortName") or {}).get("value") or ""
        text = re.sub(r"<[^>]+>", "", str(short))
        return " ".join(text.split())[:120]
    return ""


def _safe_id(card_id: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_-]", "", card_id)[:80]
    return cleaned or "ref"


def _write_reference(card_id: str, content: bytes, ext: str) -> str:
    folder = references_dir()
    folder.mkdir(parents=True, exist_ok=True)
    filename = f"{_safe_id(card_id)}{ext}"
    path = folder / filename
    path.write_bytes(content)
    override = os.environ.get("AIDUNGEON_REFERENCES_DIR")
    if override:
        return str(path)
    return f"data/references/{filename}"


def _default_describe(content: bytes, llm_cfg: dict | None,
                      transport: httpx.BaseTransport | None) -> str:
    """Visão do llama-server. Sem mmproj ou sem prazo, fica o texto da Wikipedia."""
    if not llm_cfg:
        return ""
    encoded = base64.b64encode(content).decode("ascii")
    kind = "image/png" if content.startswith(b"\x89PNG") else "image/jpeg"
    messages = [{
        "role": "user",
        "content": [
            {
                "type": "text",
                "text": (
                    "Describe only this adult person's face for an image model. "
                    "One English sentence: hair, skin, eyes, age range, distinctive marks. "
                    "No name, no clothes, no nudity."
                ),
            },
            {"type": "image_url", "image_url": {"url": f"data:{kind};base64,{encoded}"}},
        ],
    }]
    try:
        text = llm_client.chat_completion(
            llm_cfg, messages, role="assistant", temperature=0.2,
            max_tokens=80, timeout_s=25, transport=transport,
        )
    except Exception as exc:
        log.info("likeness: visão indisponível (%s)", exc)
        return ""
    return english_bits(text)

_VISUAL_CARD_SYSTEM = """You analyze one reference photo for an RPG character card.
Describe only visible, non-sensitive physical attributes. Do not identify the person.
Return STRICT JSON only:
{
  "appearance": "face, hair color/style, eyes, skin, distinctive marks",
  "body": "visible build, posture and physical features",
  "clothing": "visible outfit, accessories, jewelry, glasses, tattoos",
  "portrait_prompt": "one detailed English portrait prompt matching the photo",
  "entry_visual": "one concise visual description for the story card"
}
Do not invent biography, personality, age, ethnicity or identity. No nudity."""


def analyze_visual_card(content: bytes, llm_cfg: dict | None,
                        transport: httpx.BaseTransport | None) -> dict:
    """Lê a foto e devolve campos visuais para atualizar o card."""
    if not llm_cfg:
        return {}
    encoded = base64.b64encode(content).decode("ascii")
    kind = "image/png" if content.startswith(b"\x89PNG") else "image/jpeg"
    messages = [
        {"role": "system", "content": _VISUAL_CARD_SYSTEM},
        {"role": "user", "content": [
            {"type": "text", "text": "Analyze this reference photo and fill the JSON."},
            {"type": "image_url", "image_url": {"url": f"data:{kind};base64,{encoded}"}},
        ]},
    ]
    try:
        raw = llm_client.chat_completion(
            llm_cfg, messages, role="assistant", temperature=0.1,
            max_tokens=350, timeout_s=45, transport=transport,
        )
        start, end = raw.find("{"), raw.rfind("}")
        if start < 0 or end <= start:
            return {}
        data = json.loads(raw[start:end + 1])
        return data if isinstance(data, dict) else {}
    except Exception as exc:
        log.info("likeness: análise visual do card indisponível (%s)", exc)
        return {}


def apply_visual_card(conn, card: dict, content: bytes, *,
                      llm_cfg: dict | None,
                      transport: httpx.BaseTransport | None) -> dict:
    """Atualiza os campos visuais do card sem tocar na mente ou no estado."""
    analysis = analyze_visual_card(content, llm_cfg, transport)
    if not analysis:
        return card
    sheet = dict(sheet_of(card))
    for key in ("appearance", "body", "clothing"):
        value = analysis.get(key)
        if isinstance(value, str) and value.strip():
            sheet[key] = value.strip()
    fields: dict = {"sheet": json.dumps(sheet, ensure_ascii=False)}
    prompt = analysis.get("portrait_prompt")
    if isinstance(prompt, str) and prompt.strip():
        fields["portrait_prompt"] = prompt.strip()
    visual = analysis.get("entry_visual")
    if isinstance(visual, str) and visual.strip():
        old_entry = str(card.get("entry") or "").strip()
        marker = "Visual de referência:"
        clean = re.sub(r"(?:^|\n)Visual de referência:.*?(?=\n|$)", "", old_entry).strip()
        fields["entry"] = f"{marker} {visual.strip()}" + (f"\n{clean}" if clean else "")
    updated = db.update_card(conn, card["adventure_id"], card["id"], fields)
    return updated or card


def _store_sheet(conn, card: dict, sheet: dict) -> dict:
    updated = db.update_card(conn, card["adventure_id"], card["id"], {
        "sheet": json.dumps(sheet, ensure_ascii=False),
    })
    return updated or card


def resolve_card(conn, card: dict, *, query: str | None = None,
                 transport: httpx.BaseTransport | None = None,
                 llm_cfg: dict | None = None, force: bool = False,
                 describe: Callable[[bytes], str] | None = None) -> dict:
    """Baixa a referência, grava o likeness e marca a pessoa como real.

    Upload com verified_by_user só é trocado com force. Menor não ganha arquivo.
    """
    if is_minor(card):
        raise GameError(422, CODE_MINOR, "Não guardo referência de menor.")
    current = likeness_of(card)
    if current.get("verified_by_user") and current.get("reference_image_path") and not force:
        return card
    page = lookup_person(query or display_name(card), transport=transport)
    if page is None or page.fictional or page.disambiguation or not page.title:
        raise GameError(422, "not_a_real_person",
                        "A Wikipedia não confirmou essa pessoa como real.")
    sheet = dict(sheet_of(card))
    if page.minor:
        sheet["minor"] = True
        sheet["real_person"] = False
        sheet["wiki_title"] = page.title
        _store_sheet(conn, card, sheet)
        raise GameError(422, CODE_MINOR, "Não guardo referência de menor.")
    if not page.image_url:
        raise GameError(502, "likeness_unavailable", "A Wikipedia não tem uma foto principal.")
    try:
        with _client(transport) as client:
            content, ext = _download_with_fallback(client, page.image_url, page.thumb_url)
            license_name = _commons_license(client, page.image_url)
    except GameError:
        raise
    except httpx.HTTPError as exc:
        raise GameError(502, "likeness_unavailable",
                        "Não consegui baixar a foto de referência.") from exc
    rel = _write_reference(card["id"], content, ext)
    if describe is not None:
        descriptor = english_bits(describe(content))
    else:
        descriptor = _default_describe(content, llm_cfg, transport)
    if not descriptor:
        descriptor = english_bits(page.extract)
    sheet["real_person"] = True
    sheet["minor"] = False
    sheet["real_name"] = page.title
    sheet["wiki_title"] = page.title
    sheet["likeness"] = {
        "descriptor": descriptor,
        "reference_image_path": rel,
        "source_url": _image_url(page.image_url),
        "license": license_name,
        "page_url": page.page_url,
        "verified_by_user": False,
    }
    return _store_sheet(conn, card, sheet)


def save_upload(conn, card: dict, content: bytes, *, filename: str = "",
                mode: str = "reference") -> dict:
    """Upload manual. `reference` não marca pessoa real; `real_person` marca."""
    if mode not in ("reference", "real_person"):
        raise GameError(400, "invalid_payload",
                        "mode deve ser 'reference' ou 'real_person'.")
    if is_minor(card):
        raise GameError(422, CODE_MINOR, "Não guardo referência de menor.")
    if not content or len(content) > MAX_IMAGE_BYTES:
        raise GameError(400, "invalid_payload", "Envie uma imagem de até 8 MB.")
    ext = _sniff_ext(content, "", filename)
    if not ext:
        raise GameError(400, "invalid_payload", "A referência precisa ser JPEG, PNG ou WebP.")
    rel = _write_reference(card["id"], content, ext)
    sheet = dict(sheet_of(card))
    sheet["reference"] = {
        "image_path": rel,
        "source": "upload",
        "mode": mode,
        "created_at": db.utcnow(),
    }
    if mode == "real_person":
        previous = likeness_of(card)
        sheet["real_person"] = True
        if not str(sheet.get("real_name") or "").strip():
            sheet["real_name"] = display_name(card)
        sheet["likeness"] = {
            "descriptor": previous.get("descriptor") or "",
            "reference_image_path": rel,
            "source_url": "",
            "license": "user upload",
            "page_url": previous.get("page_url") or "",
            "verified_by_user": True,
        }
    conn.execute(
        "UPDATE story_cards SET portrait_anchor_id='', updated_at=? WHERE id=?",
        (db.utcnow(), card["id"]),
    )
    return _store_sheet(conn, card, sheet)


def clear_reference(conn, card: dict) -> dict:
    """Apaga a foto enviada e o caminho espelhado no likeness."""
    sheet = dict(sheet_of(card))
    like = dict(likeness_of(card))
    raw_ref = sheet.get("reference") if isinstance(sheet.get("reference"), dict) else {}
    seen: set[Path] = set()
    for raw in (raw_ref.get("image_path"), like.get("reference_image_path")):
        path = absolute_reference(str(raw or ""))
        if path is None or path in seen:
            continue
        seen.add(path)
        path.unlink(missing_ok=True)
    sheet["reference"] = {}
    like["reference_image_path"] = ""
    like["source_url"] = ""
    like["verified_by_user"] = False
    sheet["likeness"] = like
    return _store_sheet(conn, card, sheet)
