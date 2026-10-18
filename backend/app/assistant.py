"""Assistente de criação e evolução de histórias/cenários (v2 / v0.9.0 Story Studio).

Funcionalidades:
- POST /api/assistant/scenario: brief + parâmetros ricos (genre, tone, pov,
  protagonista, image_style, card_count, web_search_mode) → ScenarioDraft
  completo em 1 chamada unificada rápida (com fallback automático em 2 etapas
  se o modelo omitir story_cards ou campos longos).
- POST /api/assistant/field: regenera ou refina UM campo com instrução livre
  opcional (`instruction`) e pesquisa web opcional (`web_search`).
- POST /api/assistant/card: cria ou enriquece 1 Story Card individual (com
  ficha estruturada de NPC e pesquisa web opcional).
- POST /api/assistant/sync-plot: analisa os turnos recentes de uma aventura em
  andamento e atualiza Plot Essentials / Author's Note coerentemente.
- POST /api/assistant/web-search: pesquisa direta na web (Wikipedia + DuckDuckGo)
  para fundamentar lore, fatos históricos, franquias e lugares reais.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from app import llm_client, web_search as web_search_mod
from app.errors import GameError

log = logging.getLogger("aidungeon.assistant")

TEXT_FIELDS = (
    "title",
    "description",
    "ai_instructions",
    "plot_essentials",
    "authors_note",
    "opening_text",
)
DRAFT_FIELDS = TEXT_FIELDS + ("image_style", "tags", "story_cards", "third_person")

_SCENARIO_SYSTEM = """You are a master scenario architect for an AI interactive fiction game (AI Dungeon style).
Given the player's idea and configuration parameters, produce a complete, rich scenario as STRICT JSON (no markdown, no commentary) with these keys:
{
  "title": "short evocative title",
  "description": "1-2 sentence hook shown in the scenario library",
  "ai_instructions": "clear style, pacing, genre and narrator instructions",
  "plot_essentials": "protagonist sheet + world premise + core conflict, e.g. [Name: ...; Role: ...; Appearance: ...] plus key world rules",
  "authors_note": "immediate tone, atmosphere, and narrative tension guidance",
  "opening_text": "immersive 2-3 paragraph opening scene that starts the adventure in medias res and ends with an open hook for the player",
  "image_style": "concise English visual art direction for scene/portrait generation (e.g. 'dark cinematic cyberpunk photography, neon rain, 35mm lens')",
  "tags": ["genre", "tone", "setting"],
  "story_cards": [
    {
      "title": "name",
      "type": "character|location|faction|item|other",
      "triggers": ["keywords", "that", "surface", "the", "card"],
      "entry": "essential lore and canon facts the AI must know when triggered",
      "sheet": {
        "appearance": "face, hair, eyes, distinctive marks (English or target language)",
        "body": "build, height, skin, posture",
        "clothing": "typical outfit and gear",
        "personality": "traits, voice, mannerisms, motivations",
        "stats": {"str": 10, "dex": 10, "con": 10, "int": 10, "wis": 10, "cha": 10}
      }
    }
  ]
}
Rules:
- Write all narrative fields in {language} (image_style should be in English for the image model).
- Write opening_text in {pov_label} perspective, separated into 2 or 3 well-structured paragraphs using \\n\\n.
- Include {card_count_rule} story_cards covering key characters, locations, and factions/items. Include "sheet" ONLY for type "character".
- Output ONLY the valid JSON object."""

_CARDS_SYSTEM = """You are a scenario designer for an AI interactive fiction game (AI Dungeon style).
Given the player's idea and the scenario draft, produce {card_count_rule} story cards as STRICT JSON (no markdown, no commentary):
{"story_cards": [
  {"title": "name", "type": "character|location|faction|item|other",
   "triggers": ["keywords", "that", "surface", "the", "card"],
   "entry": "facts the AI must know when triggered",
   "sheet": {"appearance": "face, hair, eyes, marks", "body": "build, height, skin, posture",
             "clothing": "typical outfit", "personality": "traits, mannerisms",
             "stats": {"str": 1-20, "dex": 1-20, "con": 1-20, "int": 1-20, "wis": 1-20, "cha": 1-20}}}
]}
Rules: narrative texts in {language}; include characters, a place, and at least one faction or item when the idea supports them; sheet only for type character (omit otherwise); every card needs a non-empty title, triggers, and entry; output ONLY the JSON object."""

_CARDS_RETRY_SYSTEM = """You are a scenario designer. The previous card list could not be read.
Return ONE JSON object and nothing else (no markdown, no array at the top level):
{"story_cards": [
  {"title": "name", "type": "character|location|faction|item", "triggers": ["word"], "entry": "one sentence of facts"}
]}
3 to 6 cards. Narrative texts in {language}. Omit sheet. Every card needs title, type, triggers and entry."""

_FIELD_SYSTEM = """You are a scenario designer for an AI interactive fiction game (AI Dungeon style).
Regenerate or refine exactly ONE field of the scenario draft below, keeping coherence with the other fields.
{field_instruction}
Narrative texts in {language}. Output ONLY the JSON object, no markdown, no commentary."""

_FIELD_INSTRUCTIONS = {
    "title": 'Return {"value": "new short evocative title"}.',
    "description": 'Return {"value": "new 1-2 sentence hook"}.',
    "ai_instructions": 'Return {"value": "new style/genre instructions for the AI narrator"}.',
    "plot_essentials": 'Return {"value": "new protagonist sheet + premise (AI Dungeon format)"}.',
    "authors_note": 'Return {"value": "new tone guidance"}.',
    "opening_text": 'Return {"value": "new immersive multi-paragraph opening scene"}.',
    "image_style": 'Return {"value": "concise English art direction / photography style for image generation"}.',
    "tags": 'Return {"value": "comma, separated, tags"}.',
    "story_cards": 'Return {"story_cards": [{"title": "...", "type": "character|location|faction|item|other", "triggers": ["..."], "entry": "...", "sheet": {"appearance": "...", "body": "...", "clothing": "...", "personality": "...", "stats": {"str": 10, "dex": 10, "con": 10, "int": 10, "wis": 10, "cha": 10}}}]} with 3 to 8 cards (sheet only for type character).',
}

_SINGLE_CARD_SYSTEM = """You are a lore and character designer for an AI interactive fiction game.
Create or enrich ONE Story Card based on the concept, story context, and any web research provided.
Return STRICT JSON only (no markdown, no commentary):
{
  "title": "canonical name/title",
  "type": "character|location|faction|item|other",
  "triggers": ["keyword1", "keyword2", "alias"],
  "entry": "rich, concise lore entry (2-4 sentences) with concrete facts, relationships, and hooks",
  "portrait_prompt": "concise English visual prompt for portrait/illustration (only if character)",
  "sheet": {
    "appearance": "face, hair, eyes, distinctive features",
    "body": "build, height, posture",
    "clothing": "signature outfit and gear",
    "personality": "temperament, speech style, goals",
    "status": "alive",
    "stats": {"str": 10, "dex": 10, "con": 10, "int": 10, "wis": 10, "cha": 10}
  }
}
Rules:
- Write narrative texts in {language} (keep portrait_prompt in English).
- Include "sheet" and "portrait_prompt" ONLY when type is "character" (omit them for location/faction/item/other).
- Output ONLY the JSON object."""

_SYNC_PLOT_SYSTEM = """You are the continuity editor for an interactive fiction game.
Read the current Plot Essentials, Author's Note, and recent story turns.
Update Plot Essentials so it accurately reflects current protagonist state, key allies/enemies, location, and active objectives without losing core premise facts. Also suggest an updated Author's Note if the tone/scene shifted.
Return STRICT JSON only:
{
  "plot_essentials": "updated plot essentials in {language}",
  "authors_note": "updated tone/scene guidance in {language}",
  "summary_delta": "1-2 sentence summary of what changed"
}
Output ONLY the JSON object."""


# ---------------------------------------------------------------- helpers
def _extract_json(text: str) -> Any:
    """Extração robusta: remove code fences e recorta do primeiro '{'/'[' ao
    último '}'/']'. Levanta ValueError se não houver JSON parseável."""
    blob = _json_blob(text)
    if blob is None:
        raise ValueError("resposta sem objeto/array JSON")
    try:
        return json.loads(blob)
    except json.JSONDecodeError:
        repaired = re.sub(r",\s*([}\]])", r"\1", blob)
        try:
            return json.loads(repaired)
        except json.JSONDecodeError as exc:
            raise ValueError("resposta com JSON incompleto") from exc


def _json_blob(text: str) -> str | None:
    """Trecho entre o primeiro '{'/'[' e o último '}'/']', sem fences nem <think>."""
    cleaned = re.sub(r"```(?:json)?", "", text or "")
    cleaned = re.sub(r"<think>.*?</think>", "", cleaned, flags=re.DOTALL).strip()
    start_candidates = [i for i in (cleaned.find("{"), cleaned.find("[")) if i >= 0]
    if not start_candidates:
        return None
    start = min(start_candidates)
    end = max(cleaned.rfind("}"), cleaned.rfind("]"))
    if end <= start:
        return None
    return cleaned[start:end + 1]


def _as_str(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _normalize_tags(value: Any) -> list[str]:
    """Aceita array ou string separada por vírgula; devolve list[str] limpa."""
    if isinstance(value, str):
        value = value.split(",")
    if not isinstance(value, list):
        return []
    return [str(t).strip() for t in value if str(t).strip()]


def _normalize_cards(value: Any, max_cards: int = 8) -> list[dict]:
    """Cards com defaults (type 'other', triggers list[str]).

    Aceita os aliases que o modelo troca no JSON: name/title, description/entry,
    keywords/triggers. Card sem título é descartado.
    """
    if isinstance(value, dict):
        value = [value]
    if not isinstance(value, list):
        return []
    cards: list[dict] = []
    for raw in value:
        if not isinstance(raw, dict):
            continue
        title = _as_str(raw.get("title")) or _as_str(raw.get("name"))
        if not title:
            continue
        triggers = raw.get("triggers")
        if triggers is None:
            triggers = raw.get("trigger")
        if triggers is None:
            triggers = raw.get("keywords")
        card_type = _as_str(raw.get("type")) or _as_str(raw.get("kind")) or "other"
        card: dict[str, Any] = {
            "title": title,
            "type": card_type,
            "triggers": _normalize_tags(triggers) or [title],
            "entry": (
                _as_str(raw.get("entry"))
                or _as_str(raw.get("description"))
                or _as_str(raw.get("text"))
                or _as_str(raw.get("content"))
            ),
        }
        if _as_str(raw.get("portrait_prompt")):
            card["portrait_prompt"] = _as_str(raw.get("portrait_prompt"))
        if isinstance(raw.get("sheet"), dict) and card_type.lower() == "character":
            card["sheet"] = raw["sheet"]
        cards.append(card)
    return cards[:max_cards]


def _generation_failed(detail: str) -> GameError:
    log.warning("assistant: falha ao interpretar resposta do LLM: %s", detail)
    return GameError(503, "generation_failed",
                     "LLM returned an unparseable response; try again")


def _chat(llm_cfg: dict, system: str, user: str, *, max_tokens: int,
          temperature: float, transport: Any) -> str:
    messages = [{"role": "system", "content": system},
                {"role": "user", "content": user}]
    return llm_client.chat_completion(
        llm_cfg, messages, role="assistant",
        timeout_s=llm_client.scaled_timeout(llm_cfg, "assistant", max_tokens),
        temperature=temperature, max_tokens=max_tokens, transport=transport)


def _assistant_cfg(cfg: dict | None) -> dict:
    cfg = cfg or {}
    return {
        "max_tokens": int(cfg.get("max_tokens", 2800)),
        "field_max_tokens": int(cfg.get("field_max_tokens", 500)),
        "cards_max_tokens": int(cfg.get("cards_max_tokens", 2000)),
        "temperature": float(cfg.get("temperature", 0.7)),
    }


# ------------------------------------------------------------------ público
def _parse_object(content: str) -> dict | None:
    """JSON objeto ou None. JSON cortado no meio não derruba a etapa."""
    try:
        raw = _extract_json(content)
    except (ValueError, TypeError, json.JSONDecodeError):
        return None
    return raw if isinstance(raw, dict) else None


def _chat_object(llm_cfg: dict, system: str, user: str, *, max_tokens: int,
                 temperature: float, transport: Any) -> dict | None:
    content = _chat(llm_cfg, system, user, max_tokens=max_tokens,
                    temperature=temperature, transport=transport)
    return _parse_object(content)


def _looks_like_card(raw: dict) -> bool:
    """Objeto de card, não o rascunho do cenário (que também tem title)."""
    if not (_as_str(raw.get("title")) or _as_str(raw.get("name"))):
        return False
    return any(raw.get(key) not in (None, "", [])
               for key in ("entry", "triggers", "trigger", "type", "kind", "keywords"))


def _card_items(raw: Any) -> list:
    """Lista de cards a partir de objeto, array solto ou um card único."""
    if isinstance(raw, list):
        return raw
    if not isinstance(raw, dict):
        return []
    for key in ("story_cards", "cards", "storyCards"):
        value = raw.get(key)
        if isinstance(value, list):
            return value
        if isinstance(value, dict):
            return [value]
    if _looks_like_card(raw):
        return [raw]
    return []


def _iter_json_objects(text: str) -> list[dict]:
    """Objetos {...} completos, mesmo se o array em volta estiver cortado."""
    found: list[dict] = []
    start = 0
    while True:
        brace = text.find("{", start)
        if brace < 0:
            return found
        parsed, end = _read_json_object(text, brace)
        if parsed is None:
            start = brace + 1
            continue
        found.append(parsed)
        start = end


def _read_json_object(text: str, start: int) -> tuple[dict | None, int]:
    depth = 0
    in_str = False
    escape = False
    for index in range(start, len(text)):
        char = text[index]
        if in_str:
            if escape:
                escape = False
            elif char == "\\":
                escape = True
            elif char == '"':
                in_str = False
            continue
        if char == '"':
            in_str = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                blob = text[start:index + 1]
                try:
                    parsed = json.loads(blob)
                except json.JSONDecodeError:
                    repaired = re.sub(r",\s*([}\]])", r"\1", blob)
                    try:
                        parsed = json.loads(repaired)
                    except json.JSONDecodeError:
                        return None, index + 1
                if isinstance(parsed, dict):
                    return parsed, index + 1
                return None, index + 1
    return None, start + 1


def _cards_from_content(content: str, max_cards: int = 8) -> list[dict]:
    """Cards de um JSON inteiro ou, se ele quebrou, dos objetos que fecharam."""
    try:
        raw = _extract_json(content)
    except (ValueError, TypeError, json.JSONDecodeError):
        raw = None
    cards = _normalize_cards(_card_items(raw), max_cards=max_cards)
    if cards:
        return cards
    salvaged = [obj for obj in _iter_json_objects(content or "") if _looks_like_card(obj)]
    return _normalize_cards(salvaged, max_cards=max_cards)


def _draft_cards(llm_cfg: dict, assistant_cfg: dict | None, *, brief: str,
                 draft: dict, language: str, card_count: int | None = None,
                 web_context: str = "", transport: Any) -> list[dict]:
    """Etapa de fallback para cards quando a 1ª chamada não os incluiu."""
    cfg = _assistant_cfg(assistant_cfg)
    language = language or "en"
    count_rule = f"exactly {card_count}" if card_count else "3 to 6"
    systems = (
        _CARDS_SYSTEM.replace("{language}", language).replace("{card_count_rule}", count_rule),
        _CARDS_RETRY_SYSTEM.replace("{language}", language),
    )
    snapshot = {k: v for k, v in draft.items() if k in TEXT_FIELDS and v}
    snapshot["tags"] = draft.get("tags") or []
    user_parts = [f"Player's idea: {brief.strip()}"]
    if web_context:
        user_parts.append(web_context)
    user_parts.append("Scenario so far:\n" + json.dumps(snapshot, ensure_ascii=False))
    user = "\n\n".join(user_parts)

    last_exc: llm_client.LlmError | None = None
    saw_content = False
    best: list[dict] = []
    for attempt, system in enumerate(systems, start=1):
        try:
            content = _chat(llm_cfg, system, user, max_tokens=cfg["cards_max_tokens"],
                            temperature=cfg["temperature"], transport=transport)
        except llm_client.LlmError as exc:
            last_exc = exc
            log.info("assistant: chamada de cards falhou (%s); tentando de novo", exc.message)
            continue
        saw_content = True
        cards = _cards_from_content(content, max_cards=card_count or 8)
        if _card_list_complete(cards, card_count):
            return cards
        if len(cards) > len(best):
            best = cards
        snippet = " ".join(content.split())[:180]
        if cards and card_count:
            log.info("assistant: story_cards incompleto (%s/%s, tentativa %s): %s",
                     len(cards), card_count, attempt, snippet)
        else:
            log.info("assistant: story_cards vazio (tentativa %s, %s chars): %s",
                     attempt, len(content), snippet)
    if best:
        return best
    if last_exc is not None and not saw_content:
        raise last_exc
    raise _generation_failed("resposta sem 'story_cards'")


# context_builder nunca lê third_person — o POV só chega ao narrador por ai_instructions.
_POV_DIRECTIVES = {
    "second": "Narrate in second person: address the player as 'you' ('você' in Portuguese).",
    "third": "Narrate in third person: refer to the protagonist by name or he/she/they; never 'you'.",
    "first": "Narrate in first person: the protagonist narrates as 'I' ('eu' in Portuguese); never address the player as 'you'.",
}


def _card_list_complete(cards: list[dict], card_count: int | None) -> bool:
    """Lista vazia, ou mais curta que card_count pedido, ainda não está pronta."""
    if not cards:
        return False
    return card_count is None or len(cards) >= card_count


def _prepend_pov_directive(draft: dict, pov: str | None) -> None:
    """Prefixa a diretiva de POV. O context_builder preserva o começo do bloco.

    Não duplica se o texto exato já estiver em qualquer posição.
    """
    directive = _POV_DIRECTIVES.get(pov or "")
    if not directive:
        return
    current = draft.get("ai_instructions") or ""
    if directive in current:
        return
    draft["ai_instructions"] = (
        f"{directive}\n\n{current.lstrip()}" if current.strip() else directive
    )


def _pov_label(pov: str | None) -> str:
    if pov == "third":
        return "third-person ('he/she/they' or character name)"
    if pov == "first":
        return "first-person ('I')"
    return "second-person ('you' / 'você')"


def generate_scenario_draft(
    llm_cfg: dict,
    assistant_cfg: dict | None,
    brief: str,
    language: str = "en",
    transport: Any = None,
    *,
    genre: str | None = None,
    tone: str | None = None,
    pov: str | None = None,
    protagonist_name: str | None = None,
    protagonist_role: str | None = None,
    protagonist_appearance: str | None = None,
    image_style: str | None = None,
    card_count: int | None = None,
    web_search_mode: str = "auto",
) -> dict:
    """brief (+ parâmetros opcionais do Story Studio) → ScenarioDraft (dict).

    Otimizado em v0.9.0: pede o cenário completo + story_cards em 1 única
    chamada ao LLM. Se o modelo omitir story_cards (ou nos mocks de teste de
    2 etapas), faz fallback transparente para `_draft_cards`.
    """
    cfg = _assistant_cfg(assistant_cfg)
    language = language or "en"
    count_rule = f"exactly {card_count}" if card_count else "3 to 6"
    system = (
        _SCENARIO_SYSTEM
        .replace("{language}", language)
        .replace("{pov_label}", _pov_label(pov))
        .replace("{card_count_rule}", count_rule)
    )

    # Pesquisa dinâmica na web (quando aplicável)
    research = web_search_mod.research_context(
        brief,
        llm_cfg=llm_cfg,
        language=language,
        mode=web_search_mode,
        max_results=4,
        transport=transport,
    )
    web_context = research.get("context_block") or ""
    web_sources = research.get("sources") or []

    spec_lines = [f"Player's idea: {brief.strip()}"]
    if genre:
        spec_lines.append(f"Preferred Genre / Setting: {genre.strip()}")
    if tone:
        spec_lines.append(f"Narrative Tone & Pacing: {tone.strip()}")
    if pov:
        spec_lines.append(f"Narrative POV: {_pov_label(pov)}")
    if protagonist_name or protagonist_role or protagonist_appearance:
        proto_bits = []
        if protagonist_name:
            proto_bits.append(f"Name: {protagonist_name.strip()}")
        if protagonist_role:
            proto_bits.append(f"Role/Class: {protagonist_role.strip()}")
        if protagonist_appearance:
            proto_bits.append(f"Appearance: {protagonist_appearance.strip()}")
        spec_lines.append("Protagonist Specification: " + "; ".join(proto_bits))
    if image_style:
        spec_lines.append(f"Visual / Image Art Direction: {image_style.strip()}")
    if web_context:
        spec_lines.append(web_context)

    idea = "\n".join(spec_lines)
    raw: dict | None = None
    last_exc: llm_client.LlmError | None = None
    saw_content = False
    for _attempt in range(2):
        try:
            raw = _chat_object(llm_cfg, system, idea, max_tokens=cfg["max_tokens"],
                               temperature=cfg["temperature"], transport=transport)
        except llm_client.LlmError as exc:
            last_exc = exc
            raw = None
            log.info("assistant: chamada do rascunho falhou (%s); tentando de novo", exc.message)
            continue
        saw_content = True
        if raw and _as_str(raw.get("title")):
            break
        log.info("assistant: rascunho sem título ou JSON inválido; tentando de novo")
        raw = None
    if not raw or not _as_str(raw.get("title")):
        if last_exc is not None and not saw_content:
            raise last_exc
        raise _generation_failed("resposta sem 'title'")

    draft: dict[str, Any] = {field: _as_str(raw.get(field)) for field in TEXT_FIELDS}
    draft["tags"] = _normalize_tags(raw.get("tags"))
    if image_style or _as_str(raw.get("image_style")):
        draft["image_style"] = (image_style or _as_str(raw.get("image_style"))).strip()
    if pov is not None:
        draft["third_person"] = (pov == "third")

    for field in ("opening_text", "description"):
        if not draft[field]:
            log.info("assistant: campo '%s' vazio no rascunho; regenerando", field)
            draft[field] = regenerate_field(llm_cfg, assistant_cfg, field,
                                            brief=brief, draft=draft,
                                            language=language,
                                            transport=transport)["value"]

    # card_count pedido e lista curta contam como incompletos: cai no fallback.
    has_card_key = any(k in raw for k in ("story_cards", "cards", "storyCards"))
    inline_cards = _normalize_cards(_card_items(raw), max_cards=card_count or 8) if has_card_key else []
    if _card_list_complete(inline_cards, card_count):
        draft["story_cards"] = inline_cards
    else:
        fallback = _draft_cards(
            llm_cfg, assistant_cfg, brief=brief, draft=draft, language=language,
            card_count=card_count, web_context=web_context, transport=transport
        )
        draft["story_cards"] = fallback if len(fallback) >= len(inline_cards) else inline_cards

    _prepend_pov_directive(draft, pov)
    if web_sources:
        draft["web_sources"] = web_sources
    return draft


def regenerate_field(
    llm_cfg: dict,
    assistant_cfg: dict | None,
    field: str,
    *,
    brief: str | None = None,
    current: str | None = None,
    draft: dict | None = None,
    instruction: str | None = None,
    web_search: bool = False,
    language: str = "en",
    transport: Any = None,
) -> dict:
    """Regenera ou refina UM campo. Campos de texto → {"field": f, "value": "..."}
    (tags como string separada por vírgula); story_cards →
    {"field": "story_cards", "story_cards": [...]}."""
    cfg = _assistant_cfg(assistant_cfg)
    field_instr = _FIELD_INSTRUCTIONS[field]
    if instruction and instruction.strip():
        field_instr += f"\nSpecific user direction to follow: {instruction.strip()}"
    system = (
        _FIELD_SYSTEM
        .replace("{field_instruction}", field_instr)
        .replace("{language}", language or "en")
    )

    context_parts = []
    if brief:
        context_parts.append(f"Original idea: {brief.strip()}")
    if isinstance(draft, dict) and draft:
        snapshot = {k: v for k, v in draft.items() if k in DRAFT_FIELDS and v}
        if snapshot:
            context_parts.append("Current draft:\n"
                                 + json.dumps(snapshot, ensure_ascii=False, indent=1))
    if current:
        context_parts.append(f"Current value of '{field}' (keep the direction, "
                             f"improve it): {current.strip()}")
    if instruction and instruction.strip():
        context_parts.append(f"User refinement instruction: {instruction.strip()}")

    web_sources: list[dict] = []
    if web_search:
        query_seed = " ".join(
            part for part in (
                instruction or "",
                (draft or {}).get("title") or "",
                brief or "",
            ) if part
        ).strip()
        if query_seed:
            research = web_search_mod.research_context(
                query_seed, llm_cfg=llm_cfg, language=language, mode="always", transport=transport
            )
            web_sources = research.get("sources") or []
            if research.get("context_block"):
                context_parts.append(research["context_block"])

    user = "\n\n".join(context_parts) or "No additional context."

    token_budget = cfg["cards_max_tokens"] if field == "story_cards" else cfg["field_max_tokens"]
    content = _chat(llm_cfg, system, user, max_tokens=token_budget,
                    temperature=cfg["temperature"], transport=transport)
    try:
        raw = _extract_json(content)
    except (ValueError, TypeError) as exc:
        raise _generation_failed(str(exc)) from exc
    if not isinstance(raw, dict):
        raise _generation_failed("resposta não é um objeto JSON")

    if field == "story_cards":
        cards = _normalize_cards(raw.get("story_cards"))
        if not cards:
            raise _generation_failed("resposta sem 'story_cards'")
        out: dict[str, Any] = {"field": "story_cards", "story_cards": cards}
        if web_sources:
            out["web_sources"] = web_sources
        return out

    if field == "tags":
        # contrato: tags voltam como string separada por vírgula
        value = raw.get("value")
        if isinstance(value, list):
            value = ", ".join(_normalize_tags(value))
        value = _as_str(value)
    else:
        value = _as_str(raw.get("value"))
    if not value:
        raise _generation_failed(f"resposta sem 'value' para o campo {field}")
    out = {"field": field, "value": value}
    if web_sources:
        out["web_sources"] = web_sources
    return out


def generate_single_card(
    llm_cfg: dict,
    *,
    concept: str,
    card_type: str = "character",
    story_context: str = "",
    current_card: dict | None = None,
    instruction: str | None = None,
    web_search: bool = False,
    language: str = "pt",
    transport: Any = None,
) -> dict:
    """Gera ou enriquece 1 Story Card individual com suporte opcional a Web Search."""
    system = _SINGLE_CARD_SYSTEM.replace("{language}", language or "pt")
    parts = [f"Target Card Concept / Name: {concept.strip()}", f"Target Card Type: {card_type}"]
    if instruction and instruction.strip():
        parts.append(f"Refinement Instruction: {instruction.strip()}")
    if current_card:
        parts.append("Existing Card to Enrich:\n" + json.dumps(current_card, ensure_ascii=False))
    if story_context:
        parts.append(f"Story / World Context:\n{story_context[:1800]}")

    web_sources: list[dict] = []
    if web_search:
        research = web_search_mod.research_context(
            concept, llm_cfg=llm_cfg, language=language, mode="always", max_results=4, transport=transport
        )
        web_sources = research.get("sources") or []
        if research.get("context_block"):
            parts.append(research["context_block"])

    content = _chat(
        llm_cfg, system, "\n\n".join(parts),
        max_tokens=700, temperature=0.6, transport=transport,
    )
    try:
        raw = _extract_json(content)
    except (ValueError, TypeError) as exc:
        raise _generation_failed(str(exc)) from exc
    cards = _normalize_cards([raw] if isinstance(raw, dict) else raw, max_cards=1)
    if not cards:
        raise _generation_failed("resposta sem card válido")
    card = cards[0]
    if web_sources:
        card["web_sources"] = web_sources
    return {"card": card, "web_sources": web_sources}


def sync_adventure_plot(
    llm_cfg: dict,
    *,
    adventure: dict,
    recent_actions: list[dict],
    instruction: str | None = None,
    language: str = "pt",
    transport: Any = None,
) -> dict:
    """Sugere atualização de Plot Essentials e Author's Note a partir da história recente."""
    system = _SYNC_PLOT_SYSTEM.replace("{language}", language or "pt")
    recent_text = "\n".join(
        a.get("text", "") for a in recent_actions if a.get("type") != "see" and a.get("text")
    )[-2500:]
    parts = [
        f"Adventure Title: {adventure.get('title') or ''}",
        f"Current Plot Essentials:\n{adventure.get('plot_essentials') or '(empty)'}",
        f"Current Author's Note:\n{adventure.get('authors_note') or '(empty)'}",
        f"Recent Story Passage:\n{recent_text or '(no actions yet)'}",
    ]
    if instruction and instruction.strip():
        parts.append(f"Player Instruction: {instruction.strip()}")
    content = _chat(
        llm_cfg, system, "\n\n".join(parts),
        max_tokens=600, temperature=0.3, transport=transport,
    )
    try:
        raw = _extract_json(content)
    except (ValueError, TypeError) as exc:
        raise _generation_failed(str(exc)) from exc
    if not isinstance(raw, dict):
        raise _generation_failed("resposta de sync-plot inválida")
    return {
        "plot_essentials": _as_str(raw.get("plot_essentials")) or (adventure.get("plot_essentials") or ""),
        "authors_note": _as_str(raw.get("authors_note")) or (adventure.get("authors_note") or ""),
        "summary_delta": _as_str(raw.get("summary_delta")),
    }
