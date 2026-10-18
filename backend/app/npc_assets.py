"""Assets derivados da ficha do NPC (v0.5.0).

Fluxo síncrono sobre o LLM efetivo:

- `enrich_card` — preenche a ficha estruturada (story_cards.sheet, JSON:
  appearance/body/clothing/personality/stats/notes) e o portrait_prompt a
  partir do entry + contexto da aventura. Cobre cards legados (aventuras
  já existentes) sob demanda.

Erros: LLM fora do ar → LlmError (mapeado p/ 503 pelas rotas); resposta sem
JSON parseável → GameError(503, generation_failed), mesmo contrato do jobs.
"""

from __future__ import annotations

import json
import logging
import re
import unicodedata
from typing import Any

from app import db, llm_client
from app.errors import GameError

log = logging.getLogger("aidungeon.npc_assets")

STATS_KEYS = ("str", "dex", "con", "int", "wis", "cha")

_ENRICH_SYSTEM = """You build structured RPG character sheets for an interactive fiction game.
Given the character and the story context, output STRICT JSON (no markdown, no commentary) with exactly these keys:
{
  "appearance": "face, hair, eyes, distinguishing marks — vivid visual detail",
  "body": "build, height, skin, posture, physical condition",
  "clothing": "typical outfit, armor, accessories",
  "personality": "traits, mannerisms, speech style",
  "stats": {"str": 1-20, "dex": 1-20, "con": 1-20, "int": 1-20, "wis": 1-20, "cha": 1-20},
  "status": "alive, dead, missing or injured — only if the text establishes it",
  "notes": "abilities, secrets, plot-relevant facts (optional, short)",
  "portrait_prompt": "one or two English sentences for a text-to-image model: physical appearance, clothing, expression, bust/upper-body framing, neutral background — visual traits only, no names, no quotes"
}
Base stats on the character concept (a frail wizard has low str, high int). Keep every text field to 1-2 sentences. Never change an existing appearance, body or status unless the description says that the body itself changed. Output ONLY the JSON object."""


def _extract_json(text: str) -> dict:
    """Parse tolerante: remove code fences e decodifica o primeiro objeto JSON
    (raw_decode tolera lixo após o objeto, comum em respostas de LLM)."""
    cleaned = re.sub(r"```(?:json)?", "", text)
    start = cleaned.find("{")
    if start < 0:
        raise ValueError("resposta sem objeto JSON")
    obj, _ = json.JSONDecoder().raw_decode(cleaned[start:])
    if not isinstance(obj, dict):
        raise ValueError("resposta não é um objeto JSON")
    return obj


def _generation_failed(detail: str) -> GameError:
    log.warning("npc_assets: falha ao interpretar resposta do LLM: %s", detail)
    return GameError(503, "generation_failed",
                     "LLM returned an unparseable response; try again")


def parse_sheet(raw: str) -> dict:
    """sheet armazenada (JSON ou '') → dict; tolerante a legado/inválido."""
    if not raw:
        return {}
    try:
        obj = json.loads(raw)
    except (ValueError, TypeError):
        return {}
    return obj if isinstance(obj, dict) else {}


def _normalize_sheet(data: dict, previous: dict | None = None) -> dict:
    """Recorta o JSON do LLM para o contrato da ficha (campos desconhecidos
    fora; stats coagidos a int 1-20)."""
    sheet: dict[str, Any] = {}
    for key in ("appearance", "body", "clothing", "personality", "notes", "status"):
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            sheet[key] = value.strip()
    if sheet.get("status") not in ("alive", "dead", "missing", "injured"):
        sheet.pop("status", None)
    stats = data.get("stats")
    if isinstance(stats, dict):
        coerced = {}
        for key in STATS_KEYS:
            try:
                coerced[key] = max(1, min(20, int(stats.get(key))))
            except (TypeError, ValueError):
                continue
        if coerced:
            sheet["stats"] = coerced
    # A ficha já gravada é canônica: enriquecer de novo não troca o rosto,
    # não apaga a morte e não some com um fato antigo.
    if previous:
        if not data.get("look_changed"):
            for key in ("appearance", "body"):
                if previous.get(key):
                    sheet[key] = previous[key]
        if previous.get("status") and "status" not in sheet:
            sheet["status"] = previous["status"]
        if previous.get("notes") and previous["notes"] not in sheet.get("notes", ""):
            sheet["notes"] = (sheet.get("notes", "") + "\n" + previous["notes"]).strip()
        if previous.get("stats") and "stats" not in sheet:
            sheet["stats"] = previous["stats"]
    return sheet


# ------------------------------------------------------------------- enrich
def enrich_card(conn, adv: dict, card: dict, llm_cfg: dict,
                transport: Any, *, role: str = "assistant") -> dict:
    """LLM → ficha + portrait_prompt persistidos; devolve o card atualizado."""
    existing = parse_sheet(card["sheet"])
    user = (
        f"Story: {adv['title']} — {adv['description'][:600]}\n"
        f"Character: {card['title']} ({card['type']})\n"
        f"Description: {card['entry'][:1200]}"
    )
    if existing:
        user += f"\nExisting sheet (improve, don't contradict): {json.dumps(existing, ensure_ascii=False)[:800]}"
    messages = [{"role": "system", "content": _ENRICH_SYSTEM},
                {"role": "user", "content": user}]
    content = llm_client.chat_completion(llm_cfg, messages, role=role, temperature=0.4,
                                         max_tokens=600, transport=transport)
    try:
        data = _extract_json(content)
    except (ValueError, TypeError) as exc:
        raise _generation_failed(str(exc)) from exc

    sheet = _normalize_sheet(data, existing)
    if not sheet:
        raise _generation_failed("resposta sem campos de ficha aproveitáveis")
    portrait_prompt = data.get("portrait_prompt")
    fields: dict[str, Any] = {"sheet": json.dumps(sheet, ensure_ascii=False)}
    if isinstance(portrait_prompt, str) and portrait_prompt.strip():
        fields["portrait_prompt"] = portrait_prompt.strip()
    updated = db.update_card(conn, adv["id"], card["id"], fields)
    log.info("npc_assets: ficha enriquecida p/ card %s (%s)", card["id"], card["title"])
    return updated


def sheet_context(raw_sheet: str) -> str:
    """Resumo textual da ficha p/ compor prompts de retrato ('' se ausente)."""
    sheet = parse_sheet(raw_sheet)
    if not sheet:
        return ""
    parts = [f"{key}: {sheet[key]}" for key in
             ("appearance", "body", "clothing") if sheet.get(key)]
    return "\n".join(parts)


_OUTFIT_WORDS = (
    "dress", "gown", "shirt", "armor", "armour", "outfit", "wearing",
    "clothed", "robe", "robes", "lace", "vestido", "roupa", "camis",
    "tunic", "túnica", "tunica", "coat", "jacket", "suit", "lingerie",
    "bra", "panties", "jeans", "skirt", "uniform", "cloak", "cape",
    "silk", "plate", "helmet", "helm", "boots", "boot", "glove", "casaco",
    "choker", "necklace", "heel", "heels", "stocking", "corset", "colar",
)

# Enquadramento de retrato. Na cena ele puxa um busto e apaga o gesto.
_FRAMING_WORDS = (
    "bust", "portrait", "headshot", "close-up", "closeup", "background",
    "captured", "framing",
)

# Negative quando o card já tem rosto. Não lista a roupa: cena nua pode trocá-la.
IDENTITY_NEGATIVE = "different person, wrong hair color, wrong skin tone, extra unnamed person"

_HAIR_COLORS = (
    "black", "blonde", "brown", "red", "white", "gray", "grey", "silver",
    "auburn", "ginger", "dark",
)

# Frases da ficha em português. O CLIP do SDXL ignora "pele pálida" e pinta
# outra pessoa; a mesma ficha vira pele, cabelo e porte em inglês.
_PT_LOOK_PHRASES = (
    ("cabelos pretos longos e lisos", "long straight black hair"),
    ("cabelo preto longo e liso", "long straight black hair"),
    ("cabelos longos e lisos", "long straight hair"),
    ("cabelos pretos longos", "long black hair"),
    ("cabelos loiros longos", "long blonde hair"),
    ("cabelos ruivos longos", "long red hair"),
    ("pele palida como marmore", "pale marble skin"),
    ("pele palida", "pale skin"),
    ("pele morena", "brown skin"),
    ("pele negra", "dark skin"),
    ("pele branca", "fair skin"),
    ("labios vermelhos", "red lips"),
    ("olhos escuros", "dark eyes"),
    ("olhos verdes", "green eyes"),
    ("olhos azuis", "blue eyes"),
    ("olhos castanhos", "brown eyes"),
    ("cabelos pretos", "black hair"),
    ("cabelos loiros", "blonde hair"),
    ("cabelos ruivos", "red hair"),
    ("cabelos castanhos", "brown hair"),
    ("cabelos cacheados", "curly hair"),
    ("cabelos lisos", "straight hair"),
    ("estrutura fina mas atletica", "slim athletic build"),
    ("postura elegante", "elegant posture"),
    ("jovem adulta", "young adult woman"),
    ("jovem adulto", "young adult man"),
)

_PT_WORD = re.compile(
    r"(?<!\w)(?:pele|cabelo|cabelos|olhos|olho|labios|boca|vestido|estrutura|"
    r"atletica|palida|pretos|preto|lisos|liso|cachead\w*|ruivos|ruivo|loiros|"
    r"loiro|morena|moreno|sobrancelha|nariz|postura)(?!\w)"
)


def _fold(text: str) -> str:
    ascii_text = unicodedata.normalize("NFKD", text or "").encode("ascii", "ignore").decode()
    return " ".join(ascii_text.casefold().split())


def _looks_portuguese(text: str) -> bool:
    if re.search(r"[áàâãéêíóôõúçÁÀÂÃÉÊÍÓÔÕÚÇ]", text or ""):
        return True
    return _PT_WORD.search(_fold(text)) is not None


def _drop_clauses(text: str, words: tuple[str, ...], *, cut: re.Pattern[str] | None = None) -> str:
    kept: list[str] = []
    for part in re.split(r"[,;]", text or ""):
        if cut is not None:
            part = cut.split(part, maxsplit=1)[0]
        folded = part.casefold()
        if any(re.search(rf"(?<!\w){re.escape(word)}", folded) for word in words):
            continue
        bit = " ".join(part.split())
        if bit:
            kept.append(bit)
    return ", ".join(kept)


def without_outfit(text: str) -> str:
    """Tira o trecho de roupa. O rosto fica, para a cena nua não herdar o vestido."""
    return _drop_clauses(
        text,
        tuple(word for word in _OUTFIT_WORDS if word != "wearing"),
        cut=re.compile(r"(?i)\bwearing\b"),
    )


_PORTRAIT_OF = re.compile(
    r"(?i)^(?:a\s+|an\s+)?(?:bust\s+|headshot\s+|close-up\s+|closeup\s+)?portrait\s+of\s+"
)


def without_framing(text: str) -> str:
    """Tira 'bust portrait' e o fundo neutro. O rosto depois de 'portrait of' fica."""
    kept: list[str] = []
    for part in re.split(r"[,;]", text or ""):
        part = _PORTRAIT_OF.sub("", part.strip())
        part = re.split(
            r"(?i)\b(?:captured|bust|headshot|close-up|closeup)\b",
            part,
            maxsplit=1,
        )[0]
        folded = part.casefold()
        if any(re.search(rf"(?<!\w){re.escape(word)}", folded) for word in _FRAMING_WORDS):
            continue
        bit = " ".join(part.split())
        if bit:
            kept.append(bit)
    return ", ".join(kept)


def english_appearance(text: str) -> str:
    """Aparência em inglês curto. Texto que já está em inglês passa inteiro.

    Ficha em português não entra no prompt: o modelo pinta um estranho.
    """
    raw = " ".join((text or "").split())
    if not raw or not _looks_portuguese(raw):
        return raw
    folded = _fold(raw)
    hits: list[tuple[int, str]] = []
    occupied = [False] * len(folded)
    phrases = sorted(_PT_LOOK_PHRASES, key=lambda item: len(item[0]), reverse=True)
    for phrase, english in phrases:
        start = 0
        while True:
            at = folded.find(phrase, start)
            if at < 0:
                break
            end = at + len(phrase)
            if not any(occupied[at:end]):
                hits.append((at, english))
                for index in range(at, end):
                    occupied[index] = True
            start = end
    hits.sort(key=lambda item: item[0])
    # Cláusula que já está em inglês fica. O resto em português não entra.
    kept = [english for _, english in hits]
    for part in re.split(r"[,;]", raw):
        bit = " ".join(part.split())
        if bit and not _looks_portuguese(bit):
            kept.append(bit)
    return ", ".join(dict.fromkeys(kept))


def _portrait_face(card: dict) -> str:
    prompt = " ".join(str(card.get("portrait_prompt") or "").split())
    if not prompt:
        return ""
    return without_framing(without_outfit(prompt))[:240]


def _sheet_person_parts(card: dict) -> list[str]:
    sheet = parse_sheet(card.get("sheet") or "")
    parts: list[str] = []
    for key in ("appearance", "body"):
        value = sheet.get(key)
        if not isinstance(value, str) or not value.strip():
            continue
        text = without_framing(without_outfit(english_appearance(value)))
        if text:
            parts.append(text)
    return parts


def _hair_colors(text: str) -> set[str]:
    found: set[str] = set()
    folded = (text or "").casefold()
    for match in re.finditer(r"(?<!\w)hair(?:ed)?(?!\w)", folded):
        window = folded[max(0, match.start() - 32):match.end() + 8]
        for color in _HAIR_COLORS:
            if re.search(rf"(?<!\w){color}(?!\w)", window):
                found.add(color)
    return found


def _has_face(text: str) -> bool:
    return re.search(
        r"(?<!\w)(?:hair|haired|skin|eyes|eye)(?!\w)",
        text or "",
        re.IGNORECASE,
    ) is not None


def _covered(part: str, blob: str) -> bool:
    """True se cada palavra do trecho já está no outro texto."""
    blob_l = (blob or "").casefold()
    tokens = re.findall(r"[^\W\d_]{4,}", (part or "").casefold())
    if not tokens:
        return True
    return all(
        re.search(rf"(?<!\w){re.escape(token)}(?!\w)", blob_l) for token in tokens
    )


def identity_bits(card: dict) -> list[str]:
    """Rosto, cabelo, pele e porte. Sem a roupa, em palavras que o CLIP lê.

    A cor de cabelo do retrato aceito vence a ficha. A ficha em português
    entra traduzida e só acrescenta o que o retrato não contradiz (liso,
    mármore, porte). Sem isso o See nu repetia a ficha crua no fim do bloco
    e o modelo pintava outra mulher.
    """
    portrait = _portrait_face(card)
    portrait_colors = _hair_colors(portrait)
    agreed: list[str] = []
    for part in _sheet_person_parts(card):
        colors = _hair_colors(part)
        if portrait_colors and colors and colors.isdisjoint(portrait_colors):
            continue
        agreed.append(part)
    joined = " ".join(agreed)
    sheet_has_portrait_hair = bool(portrait_colors & _hair_colors(joined))
    if portrait and (not _has_face(joined) or (portrait_colors and not sheet_has_portrait_hair)):
        extras = [part for part in agreed if not _covered(part, portrait)]
        return [bit for bit in (portrait, *extras) if bit]
    return agreed


def scene_bits(card: dict, *, explicit: bool = False) -> list[str]:
    """O que a cena mostra da pessoa. Nudez não herda roupa nem busto."""
    if explicit:
        return identity_bits(card)
    bits: list[str] = []
    for bit in visual_bits(card):
        cleaned = without_framing(bit)
        if cleaned:
            bits.append(cleaned)
    return bits


def likeness_clause(cards: list[dict], *, explicit: bool = False) -> str:
    """Nome e rosto no começo do prompt. Vazio se ninguém tem visual ainda."""
    lines: list[str] = []
    any_bits = False
    for card in cards:
        bits = scene_bits(card, explicit=explicit)
        title = (card.get("title") or "").strip()
        if bits:
            any_bits = True
            line = f"{title}: " + "; ".join(bits) if title else "; ".join(bits)
        elif title:
            line = title
        else:
            continue
        status = parse_sheet(card.get("sheet") or "").get("status") or ""
        if isinstance(status, str) and status in ("dead", "missing", "injured"):
            line += f"; status {status}"
        lines.append(line)
    if not any_bits or not lines:
        return ""
    return "exact likeness, " + ", ".join(lines)


def visual_bits(card: dict) -> list[str]:
    """O visual que a pintura tem de obedecer.

    O retrato já aceito (portrait_prompt) vence a ficha e a prosa. A ficha
    só descreve o corpo quando ainda não existe retrato — senão um turno que
    veste a princesa de outro jeito troca o rosto da cena.
    """
    prompt = " ".join(str(card.get("portrait_prompt") or "").split())
    if prompt:
        return [prompt[:240]]
    sheet = parse_sheet(card.get("sheet") or "")
    bits = []
    for key in ("appearance", "body", "clothing"):
        value = sheet.get(key)
        if isinstance(value, str) and value.strip():
            bits.append(value.strip())
    return bits


def ensure_portrait_prompt(conn, card: dict) -> dict:
    """Se a foto existe e o texto do retrato sumiu, copia o prompt da imagem.

    Sem isso o See pinta outra pessoa: sobrou a prosa e não o rosto já visto.
    """
    if " ".join(str(card.get("portrait_prompt") or "").split()):
        return card
    image_id = card.get("portrait_image_id")
    if not image_id:
        return card
    image = db.get_image(conn, image_id)
    prompt = " ".join(str((image or {}).get("prompt") or "").split())[:500]
    if not prompt:
        return card
    updated = db.update_card(
        conn, card["adventure_id"], card["id"], {"portrait_prompt": prompt},
    )
    if updated:
        card.update(updated)
    else:
        card["portrait_prompt"] = prompt
    return card


def card_canon_text(card: dict) -> str:
    """Texto do card para o narrador. Sem ficha, fica 'título: entry'.

    Status, rosto e atributos vêm antes da entry: o orçamento corta o fim,
    e morte ou aparência não podem ser a parte que some.
    """
    title = (card.get("title") or "").strip()
    entry = (card.get("entry") or "").strip()
    if title and entry:
        identity = f"{title}: {entry}"
    else:
        identity = title or entry
    sheet = parse_sheet(card.get("sheet") or "")
    # O portrait_prompt é legenda de retrato ("Bust portrait of...").
    # O narrador lê a ficha. A pintura continua usando o prompt do retrato.
    if not sheet:
        return identity
    first: list[str] = []
    status = sheet.get("status")
    if isinstance(status, str) and status.strip():
        first.append(f"Status: {status.strip()}")
    for key, label in (
        ("appearance", "Appearance"),
        ("body", "Body"),
        ("clothing", "Clothing"),
    ):
        value = sheet.get(key)
        if isinstance(value, str) and value.strip():
            first.append(f"{label}: {value.strip()}")
    personality = sheet.get("personality")
    if isinstance(personality, str) and personality.strip():
        first.append(f"Personality: {personality.strip()}")
    stats = sheet.get("stats")
    if isinstance(stats, dict) and stats:
        bits = [f"{key} {stats[key]}" for key in STATS_KEYS if key in stats]
        if bits:
            first.append("Stats: " + ", ".join(bits))
    parts = list(first)
    if identity:
        parts.append(identity)
    notes = sheet.get("notes")
    if isinstance(notes, str) and notes.strip():
        parts.append(f"Notes: {notes.strip()}")
    return "\n".join(part for part in parts if part).strip()


# Palavra que indica que já existe um visual para pintar. Sem isso o primeiro
# retrato inventa um rosto e a âncora prende a cara errada.
_LOOK_WORDS = (
    "hair", "eye", "scar", "beard", "skin", "armor", "armour", "helm",
    "blonde", "redhead", "freckle", "tattoo",
    "cabelo", "olho", "cicatriz", "barba", "pele", "armadura", "elmo",
)


def has_visual(card: dict) -> bool:
    """True se a ficha, o prompt ou o entry já descrevem um corpo ou roupa."""
    sheet = parse_sheet(card.get("sheet") or "")
    blob = " ".join(
        str(sheet.get(key) or "") for key in ("appearance", "body", "clothing")
    )
    blob = f"{blob} {card.get('portrait_prompt') or ''} {card.get('entry') or ''}".lower()
    return any(word in blob for word in _LOOK_WORDS)


def mentioned_looks(cards: list[dict], text: str) -> list[str]:
    """Quem o texto cita e já tem um visual travado. Sem menção, não entra."""
    lines = []
    for card in cards:
        if (card.get("type") or "") != "character":
            continue
        names = [card.get("title") or "", *(card.get("triggers") or [])]
        if not any(_mentioned(text, name) for name in names):
            continue
        bits = visual_bits(card)
        if not bits:
            continue
        line = f"{card.get('title')}: " + "; ".join(bits)
        status = parse_sheet(card.get("sheet") or "").get("status") or ""
        if status in ("dead", "missing", "injured"):
            line += f"; status {status}"
        lines.append(line)
    return lines


def character_canon(cards: list[dict], text: str) -> str:
    """Bloco para o modelo que resume a cena: a ficha vence a prosa."""
    lines = mentioned_looks(cards, text)
    if not lines:
        return ""
    return (
        "Character canon, mandatory whenever they are visible, overrides the prose: "
        + " | ".join(lines)
        + ". Same hair, face, age, build and clothing. Do not invent a different person."
    )


def image_canon_clause(cards: list[dict], text: str) -> str:
    """Frase curta no começo do prompt de imagem.

    O CLIP corta o que vem depois. Se o rosto for no fim, a prosa (outro
    cabelo, outro vestido) é o que a pintura obedece.
    """
    lines = mentioned_looks(cards, text)
    if not lines:
        return ""
    return "exact likeness, " + ", ".join(lines)


def _mentioned(text: str, name: str) -> bool:
    name = (name or "").strip()
    if len(name) < 3 or not text:
        return False
    return re.search(r"(?<!\w)" + re.escape(name) + r"(?!\w)", text, re.IGNORECASE) is not None
