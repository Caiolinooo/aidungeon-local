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
Given the player's idea, produce a COMPLETE scenario draft as STRICT JSON (no markdown, no commentary) with exactly these keys:
{
  "title": "short evocative title",
  "description": "1-2 sentence hook shown in the scenario library",
  "ai_instructions": "style/genre instructions for the AI narrator",
  "plot_essentials": "protagonist sheet + premise, AI Dungeon format, e.g. [Name: ...; Age: ...; Class: ...] plus world rules",
  "authors_note": "tone guidance injected near the end of context",
  "opening_text": "second-person opening scene that starts the adventure",
  "tags": ["genre", "tone", "setting"],
  "story_cards": [
    {"title": "name", "type": "character|location|faction|item|other",
     "triggers": ["keywords", "that", "surface", "the", "card"],
     "entry": "facts the AI must know when triggered",
     "sheet": {"appearance": "face, hair, eyes, marks", "body": "build, height, skin, posture",
               "clothing": "typical outfit", "personality": "traits, mannerisms",
               "stats": {"str": 1-20, "dex": 1-20, "con": 1-20, "int": 1-20, "wis": 1-20, "cha": 1-20}}}
  ]
}
Rules: narrative texts in {language}; story_cards: 3 to 8 entries; sheet only for type character (omit otherwise); opening_text in second person; output ONLY the JSON object."""

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
    cleaned = re.sub(r"```(?:json)?", "", text).strip()
    start_candidates = [i for i in (cleaned.find("{"), cleaned.find("[")) if i >= 0]
    if not start_candidates:
        raise ValueError("resposta sem objeto/array JSON")
    start = min(start_candidates)
    end = max(cleaned.rfind("}"), cleaned.rfind("]"))
    if end <= start:
        raise ValueError("resposta com JSON incompleto")
    return json.loads(cleaned[start:end + 1])


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
    """3–8 cards com defaults (type 'other', triggers list[str])."""
    if not isinstance(value, list):
        return []
    cards: list[dict] = []
    for raw in value:
        if not isinstance(raw, dict):
            continue
        title = _as_str(raw.get("title"))
        if not title:
            continue
        card = {
            "title": title,
            "type": _as_str(raw.get("type")) or "other",
            "triggers": _normalize_tags(raw.get("triggers")),
            "entry": _as_str(raw.get("entry")),
        }
        # v0.5.0: ficha estruturada (só faz sentido em character)
        if isinstance(raw.get("sheet"), dict) and card["type"] == "character":
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
        "max_tokens": int(cfg.get("max_tokens", 1400)),
        "field_max_tokens": int(cfg.get("field_max_tokens", 400)),
        "temperature": float(cfg.get("temperature", 0.7)),
    }


# ------------------------------------------------------------------ público
def generate_scenario_draft(llm_cfg: dict, assistant_cfg: dict | None,
                            brief: str, language: str = "en",
                            transport: Any = None) -> dict:
    """brief → ScenarioDraft (dict). Levanta LlmError / GameError(503)."""
    cfg = _assistant_cfg(assistant_cfg)
    system = _SCENARIO_SYSTEM.replace("{language}", language or "en")
    content = _chat(llm_cfg, system, f"Player's idea: {brief.strip()}",
                    max_tokens=cfg["max_tokens"], temperature=cfg["temperature"],
                    transport=transport)
    try:
        raw = _extract_json(content)
    except (ValueError, TypeError) as exc:
        raise _generation_failed(str(exc)) from exc
    if not isinstance(raw, dict):
        raise _generation_failed("resposta não é um objeto JSON")

    draft = {field: _as_str(raw.get(field)) for field in TEXT_FIELDS}
    if not draft["title"]:
        raise _generation_failed("resposta sem 'title'")
    draft["tags"] = _normalize_tags(raw.get("tags"))
    draft["story_cards"] = _normalize_cards(raw.get("story_cards"))
    # O LLM às vezes omite/trunca campos longos (observado com opening_text).
    # Regenera individualmente, usando o próprio rascunho como contexto.
    for field in ("opening_text", "description"):
        if not draft[field]:
            log.info("assistant: campo '%s' vazio no rascunho; regenerando", field)
            draft[field] = regenerate_field(llm_cfg, assistant_cfg, field,
                                            brief=brief, draft=draft,
                                            transport=transport)["value"]
    return draft


def regenerate_field(llm_cfg: dict, assistant_cfg: dict | None, field: str, *,
                     brief: str | None = None, current: str | None = None,
                     draft: dict | None = None, transport: Any = None) -> dict:
    """Regenera UM campo. Campos de texto → {"field": f, "value": "..."}
    (tags como string separada por vírgula); story_cards →
    {"field": "story_cards", "story_cards": [...]}."""
    cfg = _assistant_cfg(assistant_cfg)
    instruction = _FIELD_INSTRUCTIONS[field]
    system = _FIELD_SYSTEM.replace("{field_instruction}", instruction) \
                          .replace("{language}", "en")

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

    content = _chat(llm_cfg, system, user, max_tokens=cfg["field_max_tokens"],
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
