"""Assets derivados da ficha do NPC (v0.5.0).

Fluxo síncrono sobre o LLM efetivo:

- `enrich_card` — preenche a ficha estruturada (story_cards.sheet, JSON:
  appearance/body/clothing/personality/humor/speech/goals/background/
  lifestyle/habits/likes/sentiments/stats/notes) e o portrait_prompt a
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

SHEET_TEXT_FIELDS = (
    "appearance", "body", "clothing", "personality", "humor", "speech",
    "goals", "background", "lifestyle", "habits", "likes", "sentiments", "notes",
)
LIFESIM_FIELDS = (
    "humor", "speech", "goals", "background", "lifestyle", "habits", "likes", "sentiments",
)
SHEET_FIELD_LIMITS = {"sentiments": 240}

_ENRICH_SYSTEM = """You build structured RPG character sheets for an interactive fiction game.
Given the character and the story context, output STRICT JSON (no markdown, no commentary) with exactly these keys:
{
  "appearance": "face, hair, eyes, distinguishing marks — vivid visual detail",
  "body": "build, height, skin, posture, physical condition",
  "clothing": "typical outfit, armor, accessories",
  "personality": "traits and mannerisms",
  "humor": "how they joke, what amuses them, what they find absurd",
  "speech": "how they talk: vocabulary, rhythm, verbal tics, nicknames, catchphrases",
  "goals": "what they want now and what they fear losing",
  "background": "2-3 sentences of history that explain the person",
  "lifestyle": "daily routine: where they wake, work, eat, sleep, who they live with",
  "habits": "repeatable tics: smokes, fidgets, counts things, whistles",
  "likes": "concrete likes and dislikes",
  "sentiments": "how they feel right now about who is in the scene and about the player; start with the feeling toward the player",
  "stats": {"str": 1-20, "dex": 1-20, "con": 1-20, "int": 1-20, "wis": 1-20, "cha": 1-20},
  "status": "alive, dead, missing or injured — only if the text establishes it",
  "notes": "abilities, secrets, plot-relevant facts (optional, short)",
  "portrait_prompt": "one or two English sentences for a text-to-image model: physical appearance, clothing, expression, bust/upper-body framing, neutral background — visual traits only, no names, no quotes"
}
Every key must be present. Use an empty string when a text field is unknown. status must be one of alive, dead, missing, injured. Base stats on the character concept (a frail wizard has low str, high int). Keep every text field to 1-2 sentences, except background (2-3 sentences). Never change an existing appearance, body or status unless the description says that the body itself changed. Output ONLY the JSON object."""


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


def _field_text(key: str, value: str) -> str:
    """Corta humor/lifestyle/… no teto da chave. Campo vazio depois do strip sai."""
    text = value.strip()
    if key in LIFESIM_FIELDS:
        text = text[: SHEET_FIELD_LIMITS.get(key, 500)]
    return text


def _normalize_sheet(data: dict, previous: dict | None = None) -> dict:
    """Recorta o JSON do LLM para o contrato da ficha (campos desconhecidos
    fora; stats coagidos a int 1-20)."""
    sheet: dict[str, Any] = {}
    for key in (*SHEET_TEXT_FIELDS, "status"):
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            sheet[key] = _field_text(key, value)
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
        # Vínculo vivo (npc_bonds). O enriquecimento não reinventa o número:
        # o que o turno gravou continua na ficha.
        if isinstance(previous.get("bonds"), dict):
            sheet["bonds"] = previous["bonds"]
        # Pessoa real, referência e menor não saem quando a ficha é reescrita.
        for key in ("real_person", "real_name", "wiki_title", "likeness", "minor", "age",
                    "life", "reference"):
            if key in previous:
                sheet[key] = previous[key]
    return sheet


REGENERABLE_FIELDS = (
    "personality", "humor", "speech", "goals", "background",
    "lifestyle", "habits", "likes", "sentiments", "portrait_prompt",
)
SHEET_REGEN_FIELDS = tuple(key for key in REGENERABLE_FIELDS if key != "portrait_prompt")

_FIELD_SYSTEM = """You rewrite one field of an RPG character sheet.
Output STRICT JSON (no markdown, no commentary): {"value": "the new text"}.
One or two sentences. Use an empty string only when the field cannot be known.
Output ONLY the JSON object."""

_ALL_SYSTEM = """You rewrite the living parts of an RPG character sheet.
Output STRICT JSON (no markdown, no commentary) with exactly these keys, every one present
(use an empty string when unknown):
{
  "personality": "traits and mannerisms",
  "humor": "how they joke, what amuses them, what they find absurd",
  "speech": "how they talk: vocabulary, rhythm, verbal tics, nicknames, catchphrases",
  "goals": "what they want now and what they fear losing",
  "background": "2-3 sentences of history that explain the person",
  "lifestyle": "daily routine: where they wake, work, eat, sleep, who they live with",
  "habits": "repeatable tics: smokes, fidgets, counts things, whistles",
  "likes": "concrete likes and dislikes",
  "sentiments": "how they feel right now about who is in the scene and about the player"
}
Do not change appearance, body, clothing, status, stats, notes, or the description.
Output ONLY the JSON object."""


def _sheet_user(title: str, entry: str, sheet: dict, *, story: str = "",
                card_type: str = "") -> str:
    label = f"{title} ({card_type})" if card_type else title
    lines = []
    if story:
        lines.append(story)
    lines.append(f"Character: {label}")
    lines.append(f"Description: {(entry or '')[:1200]}")
    if sheet:
        lines.append(
            "Existing sheet (improve, don't contradict): "
            + json.dumps(sheet, ensure_ascii=False)[:800]
        )
    return "\n".join(lines)


def _complete_sheet(llm_cfg: dict, *, title: str, entry: str, sheet: dict,
                    transport: Any, role: str, story: str = "",
                    card_type: str = "") -> tuple[dict, str | None]:
    messages = [
        {"role": "system", "content": _ENRICH_SYSTEM},
        {"role": "user", "content": _sheet_user(
            title, entry, sheet, story=story, card_type=card_type)},
    ]
    content = llm_client.chat_completion(
        llm_cfg, messages, role=role, temperature=0.4, max_tokens=600, transport=transport,
    )
    try:
        data = _extract_json(content)
    except (ValueError, TypeError) as exc:
        raise _generation_failed(str(exc)) from exc
    normalized = _normalize_sheet(data, sheet)
    if not normalized:
        raise _generation_failed("resposta sem campos de ficha aproveitáveis")
    prompt = data.get("portrait_prompt")
    portrait = prompt.strip() if isinstance(prompt, str) and prompt.strip() else None
    return normalized, portrait


def build_sheet(llm_cfg, *, title: str, entry: str = "", sheet: dict | None = None,
                transport=None, role: str = "assistant", story: str = "",
                card_type: str = "") -> dict:
    """Gera a ficha completa de um NPC por LLM. Devolve o dict normalizado.

    `portrait_prompt`, quando o modelo devolve, vem na mesma dict para o
    caller gravar na coluna do card — não faz parte da ficha persistida.
    `story` e `card_type` só entram no prompt (o enrich precisa dos dois).
    """
    existing = sheet or {}
    normalized, portrait = _complete_sheet(
        llm_cfg, title=title, entry=entry, sheet=existing,
        transport=transport, role=role, story=story, card_type=card_type,
    )
    if portrait:
        normalized["portrait_prompt"] = portrait
    return normalized


def enrich_card(conn, adv: dict, card: dict, llm_cfg: dict,
                transport: Any, *, role: str = "assistant") -> dict:
    """LLM → ficha + portrait_prompt persistidos; devolve o card atualizado."""
    existing = parse_sheet(card["sheet"])
    story = f"Story: {adv['title']} — {(adv.get('description') or '')[:600]}"
    built = build_sheet(
        llm_cfg, title=card["title"], entry=card.get("entry") or "",
        sheet=existing, transport=transport, role=role, story=story,
        card_type=card.get("type") or "",
    )
    portrait = built.pop("portrait_prompt", None)
    fields: dict[str, Any] = {"sheet": json.dumps(built, ensure_ascii=False)}
    if portrait:
        fields["portrait_prompt"] = portrait
    updated = db.update_card(conn, adv["id"], card["id"], fields)
    log.info("npc_assets: ficha enriquecida p/ card %s (%s)", card["id"], card["title"])
    return updated


def _regen_context(adv: dict, card: dict, sheet: dict) -> str:
    return (
        f"Story: {adv.get('title') or ''} — {(adv.get('description') or '')[:600]}\n"
        f"Character: {card.get('title') or ''}\n"
        f"Description: {(card.get('entry') or '')[:1200]}\n"
        f"Current sheet: {json.dumps(sheet, ensure_ascii=False)[:1200]}"
    )


def _filled(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def regenerate_card(conn, adv: dict, card: dict, llm_cfg: dict, *,
                    scope: str, field: str | None = None, overwrite: bool = False,
                    transport: Any = None, role: str = "assistant") -> dict:
    """Regenera um campo ou os 9 textos vivos. Não toca entry/rosto/status."""
    if scope == "field" and field not in REGENERABLE_FIELDS:
        raise GameError(422, "invalid_field", "Campo não regenerável.")
    if scope not in ("all", "field"):
        raise GameError(422, "invalid_field", "Campo não regenerável.")
    sheet = parse_sheet(card.get("sheet") or "")
    context = _regen_context(adv, card, sheet)
    regenerated: list[str] = []
    skipped: list[str] = []
    fields: dict[str, Any] = {}
    try:
        if scope == "field":
            content = llm_client.chat_completion(
                llm_cfg,
                [{"role": "system", "content": _FIELD_SYSTEM},
                 {"role": "user", "content": f"{context}\nField to rewrite: {field}"}],
                role=role, temperature=0.4, max_tokens=300, transport=transport,
            )
            data = _extract_json(content)
            raw = data.get("value") if isinstance(data, dict) else None
            text = _field_text(field, raw) if isinstance(raw, str) and field != "portrait_prompt" else ""
            if field == "portrait_prompt" and isinstance(raw, str):
                text = raw.strip()[:500]
            if text:
                if field == "portrait_prompt":
                    fields["portrait_prompt"] = text
                else:
                    sheet[field] = text
                    fields["sheet"] = json.dumps(sheet, ensure_ascii=False)
                regenerated.append(field)
            else:
                skipped.append(field)
        else:
            content = llm_client.chat_completion(
                llm_cfg,
                [{"role": "system", "content": _ALL_SYSTEM},
                 {"role": "user", "content": context}],
                role=role, temperature=0.4, max_tokens=600, transport=transport,
            )
            data = _extract_json(content)
            for key in SHEET_REGEN_FIELDS:
                if _filled(sheet.get(key)) and not overwrite:
                    skipped.append(key)
                    continue
                raw = data.get(key) if isinstance(data, dict) else None
                text = _field_text(key, raw) if isinstance(raw, str) else ""
                if not text:
                    skipped.append(key)
                    continue
                sheet[key] = text
                regenerated.append(key)
            if regenerated:
                fields["sheet"] = json.dumps(sheet, ensure_ascii=False)
    except GameError:
        raise
    except (ValueError, TypeError) as exc:
        raise _generation_failed(str(exc)) from exc
    updated = card
    if fields:
        saved = db.update_card(conn, adv["id"], card["id"], fields)
        if saved:
            updated = saved
    return {
        "card": db.attach_portrait(conn, updated),
        "regenerated": regenerated,
        "skipped": skipped,
    }


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
    "garment", "garments", "attire", "sleeve", "sleeves", "sock", "socks",
    "scarf", "belt", "buttons", "hem", "vestes", "veste", "seda", "transluc",
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
    ("cabelo flutuando como estrelas", "hair floating like stars"),
    ("cabelos flutuando como estrelas", "hair floating like stars"),
    ("cabelo flutuando", "floating hair"),
    ("olhos de ambar", "amber eyes"),
    ("olhos cor de ambar", "amber eyes"),
    ("vestes translucidas", "translucent garments"),
    ("veste translucida", "translucent garment"),
    ("vestes de seda", "silk garments"),
    ("veste de seda", "silk garment"),
    ("magro", "slim"),
    ("agil", "agile"),
    ("armadura", "armor"),
    ("estrutura fina mas atletica", "slim athletic build"),
    ("postura elegante", "elegant posture"),
    ("jovem adulta", "young adult woman"),
    ("jovem adulto", "young adult man"),
)

_PT_WORD = re.compile(
    r"(?<!\w)(?:pele|cabelo|cabelos|olhos|olho|labios|boca|vestido|estrutura|"
    r"atletica|palida|pretos|preto|lisos|liso|cachead\w*|ruivos|ruivo|loiros|"
    r"loiro|morena|moreno|sobrancelha|nariz|postura|magro|agil|vestes|veste|"
    r"transluc\w*|ambar|flutuando|armadura|elegante)(?!\w)"
)


def _fold(text: str) -> str:
    ascii_text = unicodedata.normalize("NFKD", text or "").encode("ascii", "ignore").decode()
    return " ".join(ascii_text.casefold().split())


def _looks_portuguese(text: str) -> bool:
    if re.search(r"[áàâãéêíóôõúçÁÀÂÃÉÊÍÓÔÕÚÇ]", text or ""):
        return True
    return _PT_WORD.search(_fold(text)) is not None


_LEADING_JUNK = re.compile(r"^[\s,;.:]+")
_DANGLING_TAIL = re.compile(
    r"(?i)\s+(?:with|on|in|at|of|against|over|and|to|from|a|an|the)$"
)


def _trim_part(part: str) -> str:
    """Limpa a sobra de pontuação e a preposição que ficou pendurada."""
    bit = " ".join((part or "").split())
    bit = _LEADING_JUNK.sub("", bit)
    bit = _DANGLING_TAIL.sub("", bit)
    return bit.strip(" ,;:.")


def _clip(text: str, limit: int = 240) -> str:
    """Corta no limite sem deixar palavra pela metade."""
    if len(text) <= limit:
        return text
    cut = text[:limit]
    space = cut.rfind(" ")
    return (cut[:space] if space > 0 else cut).rstrip(" ,;:.")


def _drop_clauses(text: str, words: tuple[str, ...], *, cut: re.Pattern[str] | None = None) -> str:
    kept: list[str] = []
    for part in re.split(r"[,;]", text or ""):
        if cut is not None:
            part = cut.split(part, maxsplit=1)[0]
        folded = part.casefold()
        if any(re.search(rf"(?<!\w){re.escape(word)}", folded) for word in words):
            continue
        bit = _trim_part(part)
        if bit:
            kept.append(bit)
    return ", ".join(kept)


_OUTFIT_PHRASE = re.compile(
    r"(?i)\b(?:wearing|dressed in|clad in|clothed in|covered (?:in|with))\b[^.;,]*"
)


def without_outfit(text: str) -> str:
    """Tira o trecho de roupa. O rosto fica, para a cena nua não herdar o vestido.

    A frase de roupa sai inteira: o resto do retrato (cabelo, olhos, porte)
    sobrevive, senão a cena nua pinta outra pessoa.
    """
    trimmed = _OUTFIT_PHRASE.sub("", text or "")
    return _drop_clauses(trimmed, tuple(word for word in _OUTFIT_WORDS if word != "wearing"))


_PORTRAIT_OF = re.compile(
    r"(?i)^(?:a\s+|an\s+)?(?:bust\s+|headshot\s+|close-up\s+|closeup\s+)?portrait\s+of\s+"
)


_BACKGROUND_PHRASE = re.compile(
    r"(?i)\s+(?:against|on|over|with|in|before|behind)\s+(?:an?|the)\s+"
    r"[\w-]*(?:\s+[\w-]+){0,2}?\s*background\b"
    r"|\s+(?:[\w-]+\s+){0,2}?background\b"
)


def without_framing(text: str) -> str:
    """Tira 'bust portrait' e o fundo neutro. O rosto depois de 'portrait of' fica.

    O fundo sai com a frase, nao com a clausula: a roupa e o cabelo do retrato
    precisam sobreviver para a cena vestir a personagem como a ficha manda.
    """
    kept: list[str] = []
    for part in re.split(r"[,;]", text or ""):
        part = _PORTRAIT_OF.sub("", part.strip())
        part = _BACKGROUND_PHRASE.sub("", part)
        part = re.split(
            r"(?i)\b(?:captured|bust|headshot|close-up|closeup)\b",
            part,
            maxsplit=1,
        )[0]
        folded = part.casefold()
        if any(re.search(rf"(?<!\w){re.escape(word)}", folded) for word in _FRAMING_WORDS):
            continue
        bit = _trim_part(part)
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
    return _clip(without_framing(without_outfit(prompt)))


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
            bits.append(_clip(cleaned))
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
        return [prompt]
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
    life = sheet.get("life") if isinstance(sheet.get("life"), dict) else {}
    mood = life.get("mood")
    if isinstance(mood, str) and mood.strip():
        first.append(f"Mood: {mood.strip()}")
    activity = life.get("activity")
    if isinstance(activity, str) and activity.strip():
        first.append(f"Doing: {activity.strip()}")
    for key, label in (
        ("appearance", "Appearance"),
        ("body", "Body"),
        ("clothing", "Clothing"),
        ("personality", "Personality"),
        ("humor", "Humor"),
        ("speech", "Speech"),
        ("goals", "Goals"),
        ("background", "Background"),
        ("lifestyle", "Lifestyle"),
        ("habits", "Habits"),
        ("likes", "Likes"),
        ("sentiments", "Sentiments"),
    ):
        value = sheet.get(key)
        if isinstance(value, str) and value.strip():
            first.append(f"{label}: {value.strip()}")
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
        line = f"{card.get('title')}: " + _clip("; ".join(bits))
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


def _card_sex_blob(card: dict | None) -> str:
    if not card:
        return ""
    sheet = parse_sheet(card.get("sheet") or "")
    parts = [
        card.get("title") or "",
        card.get("entry") or "",
        card.get("portrait_prompt") or "",
    ]
    for key in ("appearance", "body", "clothing", "personality", "notes"):
        value = sheet.get(key)
        if isinstance(value, str):
            parts.append(value)
    return " ".join(str(part) for part in parts if part)


def _after_name(text: str, end: int) -> str:
    """O resto da frase, e o pronome que abre a frase seguinte."""
    rest = text[end:end + 140]
    pieces = re.split(r"[.!?]", rest, maxsplit=1)
    same = pieces[0]
    if len(pieces) < 2:
        return same
    word = pieces[1].lstrip().split(" ", 1)[0].strip(",;:")
    if word.casefold() in set(_FEMALE_WORD) | set(_MALE_WORD):
        return same + " " + word
    return same


def _sex_sides(text: str, name: str) -> tuple[str, str]:
    """(antes, depois) de cada menção. O pronome dela não vira o sexo de quem vem em seguida."""
    if not text or not name:
        return "", ""
    pattern = re.compile(r"(?<!\w)" + re.escape(name.strip()) + r"(?!\w)", re.IGNORECASE)
    before: list[str] = []
    after: list[str] = []
    for match in pattern.finditer(text):
        before.append(text[max(0, match.start() - 16):match.start()])
        after.append(_after_name(text, match.end()))
    return " ".join(before), " ".join(after)


def _word_hits(text: str, words: tuple[str, ...]) -> int:
    return sum(
        1 for word in words
        if re.search(r"(?<!\w)" + re.escape(word) + r"(?!\w)", text or "", re.IGNORECASE)
    )


def sex_label(name: str, *blobs: str, card: dict | None = None) -> str:
    """'adult woman' ou 'adult man' quando o texto já disse o sexo. Senão vazio.

    O CLIP não lê 'ela'. Sem a palavra woman, um corpo nu vira homem.
    """
    name = " ".join((name or "").split())
    if len(name) < 3:
        return ""
    before, after = _sex_sides("\n".join(blobs), name)
    own = _card_sex_blob(card)
    female = _word_hits(after, _FEMALE_WORD) + _word_hits(own, _FEMALE_WORD)
    male = _word_hits(after, _MALE_WORD) + _word_hits(own, _MALE_WORD)
    first = name.split()[0].casefold()
    if first in _FEMALE_TITLE:
        female += 2
    if first in _MALE_TITLE:
        male += 2
    if re.search(r"(?i)\b(?:" + "|".join(_FEMALE_TITLE) + r")\s+$", before):
        female += 2
    if re.search(r"(?i)\b(?:" + "|".join(_MALE_TITLE) + r")\s+$", before):
        male += 2
    if re.search(r"(?i)\b(?:a|da|na|pela|uma)\s+$", before):
        female += 2
    if re.search(r"(?i)\b(?:o|do|no|pelo|um)\s+$", before):
        male += 2
    if female > male and female:
        return "adult woman"
    if male > female and male:
        return "adult man"
    return ""


def sex_phrase(name: str, label: str, *, explicit: bool) -> str:
    """Frase curta no começo do prompt. O CLIP obedece o início."""
    if label == "adult woman" and explicit:
        return f"{name} is an adult woman with a female body, breasts and a feminine face"
    if label == "adult woman":
        return f"{name} is an adult woman"
    if label == "adult man" and explicit:
        return f"{name} is an adult man with a male body"
    if label == "adult man":
        return f"{name} is an adult man"
    return ""


def image_canon_clause(cards: list[dict], text: str) -> str:
    """Frase curta no começo do prompt de imagem.

    O CLIP corta o que vem depois. Se o rosto for no fim, a prosa (outro
    cabelo, outro vestido) é o que a pintura obedece.
    """
    lines = mentioned_looks(cards, text)
    if not lines:
        return ""
    return "exact likeness, " + ", ".join(lines)


_FEMALE_WORD = (
    "woman", "women", "female", "girl", "lady", "she",
    "mulher", "menina", "garota", "moça", "moca", "ela", "dela", "nela",
    "senhora", "esposa", "wife", "mother", "daughter", "feminina", "irmã", "irma",
)
_MALE_WORD = (
    "man", "men", "male", "boy", "gentleman", "he",
    "homem", "menino", "garoto", "rapaz", "ele", "dele", "nele",
    "marido", "husband", "father", "son", "masculino", "irmão", "irmao",
)
_FEMALE_TITLE = ("princess", "princesa", "queen", "rainha", "lady", "senhora", "duchess")
_MALE_TITLE = ("king", "rei", "prince", "príncipe", "principe", "lord", "senhor", "duke")
_SKIP_NAME = {
    "you", "the", "she", "her", "his", "him", "ela", "ele", "uma", "um",
    "para", "com", "that", "this", "and", "but",
}


def _mentioned(text: str, name: str) -> bool:
    name = (name or "").strip()
    if len(name) < 3 or not text:
        return False
    return re.search(r"(?<!\w)" + re.escape(name) + r"(?!\w)", text, re.IGNORECASE) is not None
