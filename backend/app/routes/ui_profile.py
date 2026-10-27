"""Router /api/adventures/{id}/ui-profile — controles dinâmicos definidos pela IA (v0.7.0).

A IA lê o cenário da aventura (título, descrição, instruções, cena inicial) e
devolve um manifesto JSON descrevendo o painel de controles do jogo:

  {
    "tabs": [{"id": "do"|"say"|"story", "label": "...", "placeholder": "..."}],
    "widgets": [
      {"id": "dice",      "type": "dice",      "label": "Dice", "dice": [4,6,8,10,12,20,100]},
      {"id": "stats",     "type": "stats",     "label": "Stats",
       "stats": [{"name": "HP", "value": 20, "max": 20, "min": 0}]},
      {"id": "inventory", "type": "inventory", "label": "Inventory",
       "items": [{"name": "Torch", "qty": 1}]},
      {"id": "character", "type": "character", "label": "Character",
       "fields": [{"name": "Class", "value": "Rogue"}]}
    ]
  }

O manifesto é persistido em adventures.ui_profile; o estado dos widgets (HP
atual, itens, ficha) vive em adventures.ui_state e é editado pelo jogador via
PUT ui-state. Uma história puramente narrativa recebe {"tabs": [...],
"widgets": []} — só o InputBar, sem painel. Desabilitado
(settings.ui_profile.enabled=false) → GET/POST devolvem profile null.

A geração não usa o lock da aventura: roda como auxiliar (role assistant)
em paralelo à narrativa.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from app import auth, consequences, db, llm_client, settings_store, ui_runtime
from app.deps import get_http_transport
from app.errors import GameError

log = logging.getLogger("aidungeon.ui_profile")

router = APIRouter()


def _visible_ui_state(adv: dict | None, username: str, state: dict | None) -> dict:
    """Share não recebe private_facts. O que foi gravado no banco não muda."""
    payload = state if isinstance(state, dict) else {}
    if adv and not db.is_adventure_owner(adv, username):
        return consequences.without_private_facts(payload)
    return payload

_TAB_IDS = ("do", "say", "story")
_WIDGET_TYPES = ("dice", "stats", "inventory", "character")
_DICE_ALLOWED = (2, 3, 4, 6, 8, 10, 12, 20, 100)
_MAX_WIDGETS = 6
_MAX_STATS = 12
_MAX_ITEMS = 30
_MAX_FIELDS = 20
_MAX_LABEL = 24
_MAX_NAME = 40
_MAX_VALUE = 120
_MAX_STATE_BYTES = 16_000


def _ui_cfg() -> dict:
    cfg = settings_store.load_file_settings().get("ui_profile", {})
    return {
        "enabled": bool(cfg.get("enabled", True)),
        "temperature": float(cfg.get("temperature", 0.3)),
        "max_tokens": int(cfg.get("max_tokens", 1000)),
    }


_SYSTEM = """You design the control panel for an interactive fiction game.
Given the adventure setup, output STRICT JSON: {"tabs": [...], "widgets": [...]}.

"tabs": always the 3 ids "do", "say", "story". The labels are fixed by the
app, so echo the ids only; do not translate or rename them.

"widgets": game mechanics the player needs beyond text. Allowed types:
- {"type": "dice", "label": "...", "dice": [4,6,8,10,12,20,100]} — for games
  with chance checks (D&D, tabletop RPG). Pick only the dice the system uses.
- {"type": "stats", "label": "...", "stats": [{"name": "HP", "value": 20,
  "max": 20, "min": 0}]} — tracked numbers (health, mana, gold). At most 6.
- {"type": "inventory", "label": "...", "items": [{"name": "...", "qty": 1}]}
  — items the character starts with. At most 10.
- {"type": "character", "label": "...", "fields": [{"name": "Class",
  "value": "..."}]} — a small character sheet. At most 10 fields.

"hud": 2-5 compact facts the player should always see, drawn only from the
setup. Each item is {"label": "...", "value": "...", "tone": "neutral|info|
good|warn|danger"}. Good labels: Local, Objetivo, Tempo, Risco, Clima.
Values must be concrete and short; do not invent information.

"actions": 2-4 things the player can do RIGHT NOW in the opening scene.
Each item is {"label": "short button", "type": "do|say|story", "text": "the
full action in the player's voice, first person"}. Ground them in the setup
and do not resolve them. They are shortcuts, not narration.

Rules: widgets are for GAME MECHANICS ONLY. A pure narrative, conversation,
slice-of-life, romance or text-adventure story has NO mechanics — output
"widgets": [] for those, but still provide hud and actions. When in doubt, use
no widgets. Only add widgets the genre actually needs. Labels short (≤24
chars), in the adventure's language. Output ONLY the JSON object."""


class UiStateIn(BaseModel):
    """PUT ui-state: estado arbitrário (objeto JSON) dos widgets do painel."""
    state: dict


def _clean_str(value: Any, limit: int) -> str:
    return str(value or "").strip()[:limit]


# O app é PT-BR: label e placeholder das três abas são fixos. O perfil da IA
# só escolhe os widgets — antes ele devolvia "Investigate"/"What do you do?"
# e a barra de entrada saía em inglês para toda aventura.
_TAB_TEXT = {
    "do": ("Ação", "O que você faz?"),
    "say": ("Fala", "O que você diz?"),
    "story": ("História", "O que acontece em seguida?"),
}


def _normalize_tabs(raw: Any) -> list[dict]:
    """Ids do/say/story com o texto fixo em PT-BR; a ordem é sempre a canônica.

    O manifesto da IA só escolhe widgets. As três abas existem sempre — a
    InputBar depende das três para renderizar, e um id fora da lista ou uma
    lista vazia não pode encolher o painel.
    """
    return [
        {"id": id_, "label": _TAB_TEXT[id_][0], "placeholder": _TAB_TEXT[id_][1]}
        for id_ in _TAB_IDS
    ]


def _normalize_widgets(raw: Any) -> list[dict]:
    """Whitelist de tipos + limites rígidos (o LLM pode exagerar)."""
    out: list[dict] = []
    if not isinstance(raw, list):
        return out
    for item in raw:
        if not isinstance(item, dict):
            continue
        type_ = str(item.get("type") or "")
        if type_ not in _WIDGET_TYPES:
            continue
        widget: dict[str, Any] = {
            "id": _clean_str(item.get("id"), 24) or type_,
            "type": type_,
            "label": _clean_str(item.get("label"), _MAX_LABEL) or type_.capitalize(),
        }
        if type_ == "dice":
            dice = sorted({int(d) for d in (item.get("dice") or [])
                           if isinstance(d, (int, float)) and int(d) in _DICE_ALLOWED})
            if not dice:
                continue
            widget["dice"] = dice
        elif type_ == "stats":
            stats = []
            for s in (item.get("stats") or [])[:_MAX_STATS]:
                if not isinstance(s, dict) or not s.get("name"):
                    continue
                stats.append({
                    "name": _clean_str(s.get("name"), _MAX_NAME),
                    "value": int(s.get("value", 0) or 0),
                    "max": int(s["max"]) if isinstance(s.get("max"), (int, float)) else None,
                    "min": int(s["min"]) if isinstance(s.get("min"), (int, float)) else None,
                })
            if not stats:
                continue
            widget["stats"] = stats
        elif type_ == "inventory":
            items = []
            for i in (item.get("items") or [])[:_MAX_ITEMS]:
                if not isinstance(i, dict) or not i.get("name"):
                    continue
                items.append({"name": _clean_str(i.get("name"), _MAX_NAME),
                              "qty": max(1, int(i.get("qty", 1) or 1))})
            widget["items"] = items  # inventário inicial vazio é válido
        elif type_ == "character":
            fields = []
            for f in (item.get("fields") or [])[:_MAX_FIELDS]:
                if not isinstance(f, dict) or not f.get("name"):
                    continue
                fields.append({"name": _clean_str(f.get("name"), _MAX_NAME),
                               "value": _clean_str(f.get("value"), _MAX_VALUE)})
            if not fields:
                continue
            widget["fields"] = fields
        out.append(widget)
        if len(out) >= _MAX_WIDGETS:
            break
    return out


def _parse_profile(content: str) -> dict:
    """Extração robusta do objeto JSON + normalização completa."""
    cleaned = re.sub(r"```(?:json)?", "", content).strip()
    start, end = cleaned.find("{"), cleaned.rfind("}")
    if start == -1 or end <= start:
        raise ValueError("resposta não é um objeto")
    raw = json.loads(cleaned[start:end + 1])
    if not isinstance(raw, dict):
        raise ValueError("resposta não é um objeto")
    return {"tabs": _normalize_tabs(raw.get("tabs")),
            "widgets": _normalize_widgets(raw.get("widgets")),
            "hud": ui_runtime.normalize_hud(raw.get("hud")),
            "actions": ui_runtime.normalize_actions(raw.get("actions"))}


def initial_state(profile: dict | None) -> dict:
    """Estado inicial dos widgets a partir do manifesto (valores do LLM)."""
    state: dict[str, Any] = ui_runtime.seed(profile)
    for w in (profile or {}).get("widgets", []):
        if w["type"] == "stats":
            state[w["id"]] = {s["name"]: s["value"] for s in w["stats"]}
        elif w["type"] == "inventory":
            state[w["id"]] = list(w.get("items") or [])
        elif w["type"] == "character":
            state[w["id"]] = {f["name"]: f["value"] for f in w["fields"]}
    return state


@router.get("/{adventure_id}/ui-profile")
def get_ui_profile(adventure_id: str,
                   info: auth.AuthInfo = Depends(auth.require_auth)) -> dict:
    with db.session() as conn:
        adv = db.get_adventure(conn, adventure_id)
        if not adv:
            raise GameError.not_found(f"adventure {adventure_id} not found")
        # Perfis antigos gravaram abas em inglês; normalizar na leitura corrige
        # o que já está no banco sem migration.
        stored = adv["ui_profile"] if isinstance(adv["ui_profile"], dict) else None
        profile = None
        if stored is not None:
            profile = {
                "tabs": _normalize_tabs(stored.get("tabs")),
                "widgets": stored.get("widgets") or [],
                "hud": ui_runtime.normalize_hud(stored.get("hud")),
                "actions": ui_runtime.normalize_actions(stored.get("actions")),
            }
        return {"profile": profile, "state": _visible_ui_state(adv, info.user, adv["ui_state"])}


@router.put("/{adventure_id}/ui-state")
def put_ui_state(adventure_id: str, body: UiStateIn,
                 info: auth.AuthInfo = Depends(auth.require_auth)) -> dict:
    serialized = json.dumps(body.state, ensure_ascii=False)
    if len(serialized.encode("utf-8")) > _MAX_STATE_BYTES:
        raise GameError(413, "state_too_large",
                        f"ui state exceeds {_MAX_STATE_BYTES} bytes")
    with db.session() as conn:
        adv = db.get_adventure(conn, adventure_id)
        if not adv:
            raise GameError.not_found(f"adventure {adventure_id} not found")
        state = dict(body.state)
        if not db.is_adventure_owner(adv, info.user):
            stored = adv.get("ui_state") if isinstance(adv.get("ui_state"), dict) else {}
            if "private_facts" in stored:
                state["private_facts"] = stored["private_facts"]
            else:
                state.pop("private_facts", None)
        db.update_adventure_ui_state(conn, adventure_id, state)
        return {"state": _visible_ui_state(adv, info.user, state)}


@router.post("/{adventure_id}/ui-profile")
def post_ui_profile(adventure_id: str,
                    transport: Any = Depends(get_http_transport),
                    info: auth.AuthInfo = Depends(auth.require_auth)) -> dict:
    cfg = _ui_cfg()
    with db.session() as conn:
        adv = db.get_adventure(conn, adventure_id)
        if not adv:
            raise GameError.not_found(f"adventure {adventure_id} not found")
        if not cfg["enabled"]:
            return {
                "profile": None,
                "state": _visible_ui_state(adv, info.user, adv["ui_state"]),
            }
        actions = db.list_actions(conn, adventure_id)
        llm_cfg = settings_store.effective_llm_settings(conn)

    setup_parts = [f"Title: {adv['title']}"]
    if adv["description"]:
        setup_parts.append(f"Description: {adv['description']}")
    if adv["ai_instructions"]:
        setup_parts.append(f"AI instructions: {adv['ai_instructions'][:600]}")
    opening = next((a["text"] for a in actions if a["type"] == "start"), "")
    if opening:
        setup_parts.append(f"Opening: {opening[:800]}")
    elif actions:
        setup_parts.append(f"Recent scene: {actions[-1]['text'][:800]}")
    messages = [
        {"role": "system", "content": _SYSTEM},
        {"role": "user", "content": "\n".join(setup_parts)},
    ]
    try:
        content = llm_client.chat_completion(
            llm_cfg, messages, role="assistant", temperature=cfg["temperature"],
            max_tokens=cfg["max_tokens"], transport=transport,
            response_format={"type": "json_schema", "json_schema": {
                "name": "adventure_ui_profile", "strict": True,
                "schema": {
                    "type": "object",
                    "required": ["tabs", "widgets", "hud", "actions"],
                    "properties": {
                        "tabs": {"type": "array", "items": {"type": "object"}},
                        "widgets": {"type": "array", "items": {"type": "object"}},
                        "hud": {"type": "array", "items": {"type": "object"}},
                        "actions": {"type": "array", "items": {"type": "object"}},
                    },
                },
            }},
        )
    except llm_client.LlmError as exc:
        raise GameError(503, exc.code, exc.message) from exc
    try:
        profile = _parse_profile(content)
    except (ValueError, TypeError) as exc:
        log.warning("ui_profile: resposta do LLM não parseável: %s", exc)
        raise GameError(503, "generation_failed",
                        "LLM returned an unparseable ui profile") from exc
    with db.session() as conn:
        adv = db.get_adventure(conn, adventure_id)
        db.update_adventure_ui_profile(conn, adventure_id, profile)
        # Semeia o estado só se o jogador ainda não mexeu no painel. Um perfil
        # novo pode acrescentar HUD/ações; o estado dos widgets continua intacto.
        if adv and not adv["ui_state"]:
            state = initial_state(profile)
            db.update_adventure_ui_state(conn, adventure_id, state)
            return {
                "profile": profile,
                "state": _visible_ui_state(adv, info.user, state),
            }
        state = dict((adv or {}).get("ui_state") or {})
        seed = ui_runtime.seed(profile)
        added = {key: value for key, value in seed.items() if key not in state}
        if added:
            state.update(added)
            db.update_adventure_ui_state(conn, adventure_id, state)
        return {"profile": profile, "state": _visible_ui_state(adv, info.user, state)}
