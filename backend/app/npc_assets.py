"""Assets derivados da ficha do NPC (v0.5.0).

Dois fluxos síncronos sobre o LLM efetivo:

- `enrich_card` — preenche a ficha estruturada (story_cards.sheet, JSON:
  appearance/body/clothing/personality/stats/notes) e o portrait_prompt a
  partir do entry + contexto da aventura. Cobre cards legados (aventuras
  já existentes) sob demanda.

- `vrm_from_portrait` — lê o retrato atual do card (PNG em disco), envia ao
  LLM multimodal (formato OpenAI vision) e extrai as cores dominantes em hex
  (hair/eyes/skin/top/bottom), fundidas no JSON de card.vrm_id (model/emotion
  preservados). O avatar 3D passa a refletir a imagem gerada.

Erros: LLM fora do ar → LlmError (mapeado p/ 503 pelas rotas); resposta sem
JSON parseável → GameError(503, generation_failed), mesmo contrato do jobs.
"""

from __future__ import annotations

import base64
import json
import logging
import re
from pathlib import Path
from typing import Any

from app import db, llm_client
from app.errors import GameError

log = logging.getLogger("aidungeon.npc_assets")

STATS_KEYS = ("str", "dex", "con", "int", "wis", "cha")
HEX_RE = re.compile(r"^#[0-9a-fA-F]{6}$")
VRM_COLOR_KEYS = ("hair", "eyes", "skin", "top", "bottom")

_ENRICH_SYSTEM = """You build structured RPG character sheets for an interactive fiction game.
Given the character and the story context, output STRICT JSON (no markdown, no commentary) with exactly these keys:
{
  "appearance": "face, hair, eyes, distinguishing marks — vivid visual detail",
  "body": "build, height, skin, posture, physical condition",
  "clothing": "typical outfit, armor, accessories",
  "personality": "traits, mannerisms, speech style",
  "stats": {"str": 1-20, "dex": 1-20, "con": 1-20, "int": 1-20, "wis": 1-20, "cha": 1-20},
  "notes": "abilities, secrets, plot-relevant facts (optional, short)",
  "portrait_prompt": "one or two English sentences for a text-to-image model: physical appearance, clothing, expression, bust/upper-body framing, neutral background — visual traits only, no names, no quotes"
}
Base stats on the character concept (a frail wizard has low str, high int). Keep every text field to 1-2 sentences. Output ONLY the JSON object."""

_VRM_COLORS_SYSTEM = """Look at the character portrait and extract the dominant colors as hex codes.
Keys and what each one means:
- hair: dominant hair color (use '#ffffff' if bald or hidden)
- eyes: iris color
- skin: skin tone
- top: dominant color of the upper-body garment
- bottom: dominant color of the lower-body garment if visible, else repeat top
Every value MUST be a single 6-digit hex code like '#a1b2c3' — no words, no units, no comments.
Example of a valid answer: {"hair": "#2b1d12", "eyes": "#3f6b4f", "skin": "#caa07a", "top": "#5c3a21", "bottom": "#3a2c1c"}
Pick the actual rendered colors, not idealized ones. Output ONLY the JSON object."""


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


def _normalize_sheet(data: dict) -> dict:
    """Recorta o JSON do LLM para o contrato da ficha (campos desconhecidos
    fora; stats coagidos a int 1-20)."""
    sheet: dict[str, Any] = {}
    for key in ("appearance", "body", "clothing", "personality", "notes"):
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            sheet[key] = value.strip()
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
    return sheet


# ------------------------------------------------------------------- enrich
def enrich_card(conn, adv: dict, card: dict, llm_cfg: dict,
                transport: Any) -> dict:
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
    content = llm_client.chat_completion(llm_cfg, messages, temperature=0.4,
                                         max_tokens=600, transport=transport)
    try:
        data = _extract_json(content)
    except (ValueError, TypeError) as exc:
        raise _generation_failed(str(exc)) from exc

    sheet = _normalize_sheet(data)
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


# ------------------------------------------------------- vrm from portrait
def _portrait_file(conn, card: dict) -> Path:
    if not card.get("portrait_image_id"):
        raise GameError(409, "no_portrait",
                        "Card has no portrait yet; generate one first")
    image = db.get_image(conn, card["portrait_image_id"])
    if not image:
        raise GameError(404, "not_found", "portrait image not found")
    path = Path(image["file_path"])
    if not path.is_absolute():
        path = db.REPO_ROOT / path
    if not path.is_file():
        raise GameError(404, "not_found", f"portrait file missing: {path.name}")
    return path


def vrm_from_portrait(conn, adv: dict, card: dict, llm_cfg: dict,
                      transport: Any) -> dict:
    """Extrai cores do retrato via LLM vision e funde no vrm_id do card."""
    path = _portrait_file(conn, card)
    b64 = base64.b64encode(path.read_bytes()).decode()
    messages = [
        {"role": "system", "content": _VRM_COLORS_SYSTEM},
        {"role": "user", "content": [
            {"type": "text", "text": f"Portrait of {card['title']}. Extract the colors."},
            {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}},
        ]},
    ]
    content = llm_client.chat_completion(llm_cfg, messages, temperature=0.1,
                                         max_tokens=400, transport=transport)
    try:
        data = _extract_json(content)
    except (ValueError, TypeError) as exc:
        raise _generation_failed(str(exc)) from exc

    colors = {k: str(data[k]).lower() for k in VRM_COLOR_KEYS
              if isinstance(data.get(k), str) and HEX_RE.match(data[k])}
    if len(colors) < 3:
        raise _generation_failed("resposta sem cores hex suficientes")

    # Merge tolerante: preserva model/emotion da config atual (mesmo contrato
    # do frontend — vrmConfig.js).
    cfg: dict[str, Any] = {"model": "base", "emotion": "neutral"}
    try:
        current = json.loads(card["vrm_id"]) if card["vrm_id"] else {}
        if isinstance(current, dict):
            cfg.update({k: v for k, v in current.items() if k in (*VRM_COLOR_KEYS, "model", "emotion")})
    except (ValueError, TypeError):
        pass
    cfg.update(colors)
    updated = db.update_card(conn, adv["id"], card["id"],
                             {"vrm_id": json.dumps(cfg)})
    log.info("npc_assets: cores VRM do retrato p/ card %s: %s", card["id"], colors)
    return updated
