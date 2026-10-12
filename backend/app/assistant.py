"""Assistente de criação de cenários (v2) — geração estruturada via LLM.

POST /api/assistant/scenario: brief em linguagem natural → rascunho completo
(ScenarioDraft do openapi.yaml). POST /api/assistant/field: regenera UM campo
mantendo coerência com o resto do rascunho. NADA é persistido aqui — o
frontend revisa e depois chama POST /api/scenarios.

O LLM é instruído a responder JSON ESTRITO; a extração é robusta (remove
code fences, recorta do primeiro '{' ao último '}'). JSON inválido →
GameError 503 generation_failed.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from app import llm_client
from app.errors import GameError

log = logging.getLogger("aidungeon.assistant")

TEXT_FIELDS = ("title", "description", "ai_instructions", "plot_essentials",
               "authors_note", "opening_text")
DRAFT_FIELDS = TEXT_FIELDS + ("tags", "story_cards")

_SCENARIO_SYSTEM = """You are a scenario designer for an AI interactive fiction game (AI Dungeon style).
Given the player's idea, produce the scenario text as STRICT JSON (no markdown, no commentary) with exactly these keys:
{
  "title": "short evocative title",
  "description": "1-2 sentence hook shown in the scenario library",
  "ai_instructions": "style/genre instructions for the AI narrator",
  "plot_essentials": "protagonist sheet + premise, AI Dungeon format, e.g. [Name: ...; Age: ...; Class: ...] plus world rules",
  "authors_note": "tone guidance injected near the end of context",
  "opening_text": "second-person opening scene that starts the adventure",
  "tags": ["genre", "tone", "setting"]
}
Rules: narrative texts in {language}; opening_text in second person; output ONLY the JSON object. Do not include story cards in this response."""

_CARDS_SYSTEM = """You are a scenario designer for an AI interactive fiction game (AI Dungeon style).
Given the player's idea and the scenario draft, produce 3 to 8 story cards as STRICT JSON (no markdown, no commentary):
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
Regenerate exactly ONE field of the scenario draft below, keeping coherence with the other fields.
{field_instruction}
Narrative texts in {language}. Output ONLY the JSON object, no markdown, no commentary."""

_FIELD_INSTRUCTIONS = {
    "title": 'Return {"value": "new short evocative title"}.',
    "description": 'Return {"value": "new 1-2 sentence hook"}.',
    "ai_instructions": 'Return {"value": "new style/genre instructions for the AI narrator"}.',
    "plot_essentials": 'Return {"value": "new protagonist sheet + premise (AI Dungeon format)"}.',
    "authors_note": 'Return {"value": "new tone guidance"}.',
    "opening_text": 'Return {"value": "new second-person opening scene"}.',
    "tags": 'Return {"value": "comma, separated, tags"}.',
    "story_cards": 'Return {"story_cards": [{"title": "...", "type": "character|location|faction|item|other", "triggers": ["..."], "entry": "...", "sheet": {"appearance": "...", "body": "...", "clothing": "...", "personality": "...", "stats": {"str": 10, "dex": 10, "con": 10, "int": 10, "wis": 10, "cha": 10}}}]} with 3 to 8 cards (sheet only for type character).',
}


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


def _normalize_cards(value: Any) -> list[dict]:
    """3–8 cards com defaults (type 'other', triggers list[str]).

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
        card = {
            "title": title,
            "type": card_type,
            "triggers": _normalize_tags(triggers),
            "entry": (_as_str(raw.get("entry")) or _as_str(raw.get("description"))
                      or _as_str(raw.get("text")) or _as_str(raw.get("content"))),
        }
        # v0.5.0: ficha estruturada (só faz sentido em character)
        if isinstance(raw.get("sheet"), dict) and card_type.lower() == "character":
            card["sheet"] = raw["sheet"]
        cards.append(card)
    return cards[:8]


def _generation_failed(detail: str) -> GameError:
    log.warning("assistant: falha ao interpretar resposta do LLM: %s", detail)
    return GameError(503, "generation_failed",
                     "LLM returned an unparseable response; try again")


def _chat(llm_cfg: dict, system: str, user: str, *, max_tokens: int,
          temperature: float, transport: Any) -> str:
    messages = [{"role": "system", "content": system},
                {"role": "user", "content": user}]
    return llm_client.chat_completion(llm_cfg, messages, temperature=temperature,
                                      max_tokens=max_tokens, transport=transport)


def _assistant_cfg(cfg: dict | None) -> dict:
    cfg = cfg or {}
    return {
        "max_tokens": int(cfg.get("max_tokens", 2500)),
        "field_max_tokens": int(cfg.get("field_max_tokens", 400)),
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


def _cards_from_content(content: str) -> list[dict]:
    """Cards de um JSON inteiro ou, se ele quebrou, dos objetos que fecharam."""
    try:
        raw = _extract_json(content)
    except (ValueError, TypeError, json.JSONDecodeError):
        raw = None
    cards = _normalize_cards(_card_items(raw))
    if cards:
        return cards
    salvaged = [obj for obj in _iter_json_objects(content or "") if _looks_like_card(obj)]
    return _normalize_cards(salvaged)


def _draft_cards(llm_cfg: dict, assistant_cfg: dict | None, *, brief: str,
                 draft: dict, language: str, transport: Any) -> list[dict]:
    """Segunda etapa. Um retry, com prompt mais curto, se a lista vier vazia."""
    cfg = _assistant_cfg(assistant_cfg)
    language = language or "en"
    systems = (
        _CARDS_SYSTEM.replace("{language}", language),
        _CARDS_RETRY_SYSTEM.replace("{language}", language),
    )
    snapshot = {k: v for k, v in draft.items() if k in TEXT_FIELDS and v}
    snapshot["tags"] = draft.get("tags") or []
    user = (f"Player's idea: {brief.strip()}\n\nScenario so far:\n"
            + json.dumps(snapshot, ensure_ascii=False))
    last_exc: llm_client.LlmError | None = None
    saw_content = False
    for attempt, system in enumerate(systems, start=1):
        try:
            content = _chat(llm_cfg, system, user, max_tokens=cfg["cards_max_tokens"],
                            temperature=cfg["temperature"], transport=transport)
        except llm_client.LlmError as exc:
            last_exc = exc
            log.info("assistant: chamada de cards falhou (%s); tentando de novo", exc.message)
            continue
        saw_content = True
        cards = _cards_from_content(content)
        if cards:
            return cards
        snippet = " ".join(content.split())[:180]
        log.info("assistant: story_cards vazio (tentativa %s, %s chars): %s",
                 attempt, len(content), snippet)
    if last_exc is not None and not saw_content:
        raise last_exc
    raise _generation_failed("resposta sem 'story_cards'")


def generate_scenario_draft(llm_cfg: dict, assistant_cfg: dict | None,
                            brief: str, language: str = "en",
                            transport: Any = None) -> dict:
    """brief → ScenarioDraft (dict). Levanta LlmError / GameError(503).

    Duas chamadas: texto do cenário, depois os cards. Cada etapa que falha
    (JSON cortado, sem título, sem cards) ganha um retry. Sem card o rascunho
    não é sucesso.
    """
    cfg = _assistant_cfg(assistant_cfg)
    language = language or "en"
    system = _SCENARIO_SYSTEM.replace("{language}", language)
    idea = f"Player's idea: {brief.strip()}"
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

    draft = {field: _as_str(raw.get(field)) for field in TEXT_FIELDS}
    draft["tags"] = _normalize_tags(raw.get("tags"))
    # O LLM às vezes omite/trunca campos longos (observado com opening_text).
    # Regenera individualmente, usando o próprio rascunho como contexto.
    for field in ("opening_text", "description"):
        if not draft[field]:
            log.info("assistant: campo '%s' vazio no rascunho; regenerando", field)
            draft[field] = regenerate_field(llm_cfg, assistant_cfg, field,
                                            brief=brief, draft=draft,
                                            language=language,
                                            transport=transport)["value"]
    draft["story_cards"] = _draft_cards(
        llm_cfg, assistant_cfg, brief=brief, draft=draft, language=language,
        transport=transport)
    return draft


def regenerate_field(llm_cfg: dict, assistant_cfg: dict | None, field: str, *,
                     brief: str | None = None, current: str | None = None,
                     draft: dict | None = None, language: str = "en",
                     transport: Any = None) -> dict:
    """Regenera UM campo. Campos de texto → {"field": f, "value": "..."}
    (tags como string separada por vírgula); story_cards →
    {"field": "story_cards", "story_cards": [...]}."""
    cfg = _assistant_cfg(assistant_cfg)
    instruction = _FIELD_INSTRUCTIONS[field]
    system = _FIELD_SYSTEM.replace("{field_instruction}", instruction) \
                          .replace("{language}", language or "en")

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
        return {"field": "story_cards", "story_cards": cards}

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
    return {"field": field, "value": value}
