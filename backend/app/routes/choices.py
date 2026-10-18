"""Router /api/adventures/{id}/choices — sugestões de próxima ação (v2).

Após ler a cena recente, o LLM sugere `settings.choices.count` opções curtas
(type do/say/story, texto ≤ 120 chars, em inglês). Desabilitado
(settings.choices.enabled=false) → 200 {"choices": []}. Não usa o lock de
geração: roda no slot de choices (C1) com prazo curto, em paralelo à
narrativa, e falha limpa (503 llm_timeout) em vez de travar a aventura.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from fastapi import APIRouter, Depends

from app import db, llm_client, settings_store
from app.deps import get_http_transport
from app.errors import GameError

log = logging.getLogger("aidungeon.choices")

router = APIRouter()

_CHOICE_TYPES = ("do", "say", "story")
_MAX_TEXT = 120

_SYSTEM = """You suggest the next actions a player could take in an interactive fiction game.
Given the recent scene, output STRICT JSON: an array of exactly {count} options, each {{"type": "do"|"say"|"story", "text": "..."}}.
- "do": a physical action ("draw your sword")
- "say": something to speak ("ask the innkeeper about the missing caravan")
- "story": a narrative beat the player injects ("a storm rolls in")
Texts in English, concise (under 120 chars), varied. Output ONLY the JSON array."""


def _choices_cfg() -> dict:
    cfg = settings_store.load_file_settings().get("choices", {})
    return {
        "enabled": bool(cfg.get("enabled", True)),
        "count": int(cfg.get("count", 3)),
        "max_tokens": int(cfg.get("max_tokens", 220)),
        "temperature": float(cfg.get("temperature", 0.8)),
    }


def _parse_choices(content: str, count: int) -> list[dict]:
    """Extração robusta do array JSON + normalização (type válido, ≤120 chars)."""
    cleaned = re.sub(r"```(?:json)?", "", content).strip()
    start, end = cleaned.find("["), cleaned.rfind("]")
    if start < 0 or end <= start:
        raise ValueError("resposta sem array JSON")
    raw = json.loads(cleaned[start:end + 1])
    if not isinstance(raw, list):
        raise ValueError("resposta não é uma lista")
    choices: list[dict] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        text = str(item.get("text") or "").strip()[:_MAX_TEXT]
        if not text:
            continue
        type_ = str(item.get("type") or "").strip().lower()
        if type_ not in _CHOICE_TYPES:
            type_ = "do"
        choices.append({"type": type_, "text": text})
        if len(choices) >= count:
            break
    return choices


@router.post("/{adventure_id}/choices")
def post_choices(adventure_id: str,
                 transport: Any = Depends(get_http_transport)) -> dict:
    cfg = _choices_cfg()
    with db.session() as conn:
        adv = db.get_adventure(conn, adventure_id)
        if not adv:
            raise GameError.not_found(f"adventure {adventure_id} not found")
        if not cfg["enabled"]:
            return {"choices": []}
        actions = db.list_actions(conn, adventure_id)
        llm_cfg = settings_store.effective_llm_settings(conn)

    scene = "".join(a["text"] for a in actions if a["type"] != "see")[-2000:]
    messages = [
        {"role": "system", "content": _SYSTEM.replace("{count}", str(cfg["count"]))},
        {"role": "user", "content": f"Recent scene:\n{scene.strip() or '(beginning of the adventure)'}"},
    ]
    try:
        content = llm_client.chat_completion(
            llm_cfg, messages, role="choices", temperature=cfg["temperature"],
            max_tokens=cfg["max_tokens"], transport=transport)
    except llm_client.LlmError as exc:
        raise GameError(503, exc.code, exc.message) from exc
    try:
        choices = _parse_choices(content, cfg["count"])
    except (ValueError, TypeError) as exc:
        log.warning("choices: resposta do LLM não parseável: %s", exc)
        raise GameError(503, "generation_failed",
                        "LLM returned an unparseable choices response") from exc
    return {"choices": choices}
