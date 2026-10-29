"""Verificação de possibilidade da ação do jogador, antes do narrador.

A parte determinística usa o que `world_facts` já marcou neste turno. A parte
LLM só roda quando o mundo não barrou a ação sozinho, e qualquer falha vira
`None`: o turno segue sem veredito.
"""

from __future__ import annotations

import importlib
import json
import logging
from dataclasses import dataclass, field
from typing import Any

from app import db, llm_client, npc_assets, npc_bonds, npc_life, world_facts

log = logging.getLogger("aidungeon.action_check")

_MEMORY_BUDGET = 400
_MEMORY_TOP_K = 4
_UI_CHARS = 1200
_CANON_CHARS = 1200
_INVENTORY_CHARS = 1200
_RULES_CHARS = 1200
_MEMORY_CHARS = 160
_MEMORY_KEEP = 4
_ACTION_CHARS = 240
_ACTION_KEEP = 6
_ABSENT_KEEP = 20

_SYSTEM = """Você verifica se a ação do jogador é possível neste instante da história.
Julgue só coerência, possibilidade de estado e física. Não julgue liberdade de roleplay.
Ação atípica, ousada ou incomum é possível. Ação que contradiz o estado do mundo não é possível.
Confira o local, os NPCs presentes e ausentes, o status e o humor de cada NPC, a ficha do jogador (stats, status, roupa), os itens que existem, os fatos canônicos e a memória relevante.
Se a ação for possível, proponha em cost o custo mínimo concreto: ruído, fadiga, sangue, atenção, um recurso que o jogador já tem, ou uma mudança de relação. consequence é o efeito imediato desse custo.
Se a ação não for possível, cost e consequence são strings vazias.
Responda SOMENTE um JSON estrito, sem markdown e sem texto fora do objeto:
{"possible": true, "blockers": ["motivo"], "risks": ["risco"], "notes": "nota curta", "cost": "custo minimo", "consequence": "efeito imediato"}
possible é boolean. blockers e risks são listas de strings. notes, cost e consequence são strings.
Se a ação for possível, blockers é []. Risco não torna a ação impossível. O custo mínimo não torna a ação impossível."""


@dataclass
class Verdict:
    possible: bool
    blockers: list[str] = field(default_factory=list)
    risks: list[str] = field(default_factory=list)
    notes: str = ""
    cost: str = ""
    consequence: str = ""

    def to_dict(self) -> dict:
        possible = bool(self.possible)
        return {
            "possible": possible,
            "blockers": list(self.blockers),
            "risks": list(self.risks),
            "notes": self.notes or "",
            "cost": (self.cost or "") if possible else "",
            "consequence": (self.consequence or "") if possible else "",
        }


def collect_state(conn, adventure: dict, actions: list[dict], cards: list[dict],
                  user: str) -> dict:
    """Retrato do mundo com os módulos que já existem. Não chama o LLM."""
    memories = _story_memories(conn, adventure.get("id") or "", _query_text(actions))
    assessment = world_facts.assess(adventure, actions, cards, memories)
    present, absent = _split_npcs(conn, adventure.get("id") or "", cards, assessment, actions)
    return {
        "where": assessment.where,
        "places": list(assessment.places),
        "items": list(assessment.items),
        "facts_line": assessment.facts_line,
        "canon_blob": assessment.canon_blob,
        "blocked_items": list(assessment.blocked_items),
        "blocked_places": list(assessment.blocked_places),
        "resisted": list(assessment.resisted),
        "absent": absent,
        "invented_people": list(assessment.invented_people),
        "invented_places": list(assessment.invented_places),
        "present_npcs": present,
        "player": _player_view(conn, adventure, user),
        "recent_actions": _recent_actions(actions),
        "memories": [row.get("text") or "" for row in memories if (row.get("text") or "").strip()],
        "character_rules": adventure.get("character_rules") or "",
    }


def check_action(action_text: str, action_type: str, state: dict, *,
                 llm_cfg: dict, transport: Any, cfg: dict,
                 cancel=None, allow_llm: bool = True,
                 blockers: list[str] | None = None) -> Verdict | None:
    """Veredito, ou None se a checagem LLM não pôde ser lida.

    Bloqueio duro não consulta o modelo. `blockers` informado não é
    recalculado. `allow_llm=False` não abre rede: devolve o bloqueio
    determinístico ou um veredito vazio com possible=True.
    """
    if blockers is None:
        resolved = hard_blockers(state) if isinstance(state, dict) else []
    else:
        resolved = [item.strip() for item in blockers if isinstance(item, str) and item.strip()]
    if resolved:
        return Verdict(
            possible=False,
            blockers=resolved,
            risks=[],
            notes="Contradiz o estado do mundo ja estabelecido.",
        )
    if not allow_llm:
        return Verdict(possible=True)
    if not isinstance(state, dict):
        return None
    try:
        raw = llm_client.chat_completion(
            llm_client.role_config(llm_cfg, "turn_check"),
            _messages(action_text, action_type, state),
            role="turn_check",
            temperature=0.15,
            max_tokens=_max_tokens(cfg),
            timeout_s=_timeout(cfg),
            transport=transport,
            cancel=cancel,
            response_format={"type": "json_schema", "json_schema": {
                "name": "action_check", "strict": True,
                "schema": {
                    "type": "object", "additionalProperties": False,
                    "required": ["possible", "blockers", "risks", "notes", "cost", "consequence"],
                    "properties": {
                        "possible": {"type": "boolean"},
                        "blockers": {"type": "array", "items": {"type": "string"}},
                        "risks": {"type": "array", "items": {"type": "string"}},
                        "notes": {"type": "string"},
                        "cost": {"type": "string"},
                        "consequence": {"type": "string"},
                    },
                },
            }},
        )
    except llm_client.LlmCancelled:
        raise
    except Exception:
        return None
    verdict = _verdict_from_text(raw)
    if verdict is None:
        log.warning("action_check: JSON inválido")
    return verdict


def verdict_block(verdict: Verdict) -> str:
    """Bloco injetado no contexto do narrador. Não é texto para o jogador."""
    label = "POSSIVEL" if verdict.possible else "NAO E POSSIVEL"
    blockers = "; ".join(verdict.blockers) if verdict.blockers else "nenhum"
    risks = "; ".join(verdict.risks) if verdict.risks else "nenhum"
    notes = (verdict.notes or "").strip() or "nenhuma"
    if verdict.possible:
        cost = (verdict.cost or "").strip()
        consequence = (verdict.consequence or "").strip()
    else:
        cost = ""
        consequence = ""
    return (
        "[Verificacao de mundo]\n"
        f"A acao do jogador e {label}.\n"
        f"Obstaculos: {blockers}\n"
        f"Riscos: {risks}\n"
        f"Custo esperado: {cost}\n"
        f"Consequência: {consequence}\n"
        f"Nota: {notes}\n"
        "Regra: narra a tentativa e o que a impede. "
        "Nao mencione esta verificacao ao jogador."
    )


def hard_blockers(state: dict) -> list[str]:
    blockers: list[str] = []
    for item in _strings(state.get("blocked_items")):
        blockers.append(f"Sem item: {item}.")
    for place in _strings(state.get("blocked_places")):
        blockers.append(f"Não existe: {place}.")
    for name in _strings(state.get("resisted")):
        blockers.append(f"{name} recusa.")
    for name in _strings(state.get("invented_people")):
        blockers.append(f"{name} não está estabelecido no mundo.")
    return _unique(blockers)


def _clip_value(value: Any, limit: int) -> Any:
    """Corta JSON ou texto no limite. Abaixo do limite o valor original fica."""
    if value is None:
        return None
    if isinstance(value, str):
        return npc_assets._clip(value, limit)
    try:
        raw = json.dumps(value, ensure_ascii=False)
    except (TypeError, ValueError):
        raw = json.dumps(value, ensure_ascii=False, default=str)
    if len(raw) <= limit:
        return value
    return npc_assets._clip(raw, limit)


def _last_dice(value: Any) -> Any:
    """Só o último resultado. Histórico e anotações saem do prompt."""
    if isinstance(value, list):
        return value[-1] if value else None
    if not isinstance(value, dict):
        return value
    if "last" in value:
        return {"last": value["last"]}
    for key in ("history", "rolls", "results", "log"):
        seq = value.get(key)
        if isinstance(seq, list) and seq:
            return seq[-1]
    return value


def _memory_text(item: Any) -> str:
    if isinstance(item, str):
        return item
    if isinstance(item, dict):
        text = item.get("text")
        return text if isinstance(text, str) else ""
    return str(item)


def _absent_rows(items: Any) -> list[dict]:
    rows: list[dict] = []
    if not isinstance(items, list):
        return rows
    for item in items:
        status = ""
        if isinstance(item, dict):
            name = str(item.get("name") or "").strip()
            raw_status = item.get("status")
            status = raw_status.strip() if isinstance(raw_status, str) else ""
        elif isinstance(item, str):
            name = str(item).strip()
            raw_status = getattr(item, "status", "")
            status = raw_status.strip() if isinstance(raw_status, str) else ""
        else:
            continue
        if not name:
            continue
        rows.append({"name": name, "status": status})
        if len(rows) >= _ABSENT_KEEP:
            break
    return rows


def _bound_player(player: dict) -> dict:
    row = dict(player)
    if "ui_state" in row:
        row["ui_state"] = _clip_value(row.get("ui_state"), _UI_CHARS)
    if "canon" in row:
        row["canon"] = _clip_value(row.get("canon"), _CANON_CHARS)
    if "inventory" in row:
        row["inventory"] = _clip_value(row.get("inventory"), _INVENTORY_CHARS)
    if "dice" in row:
        row["dice"] = _last_dice(row.get("dice"))
    return row


def _bound_present(npcs: Any) -> Any:
    if not isinstance(npcs, list):
        return npcs
    rows = []
    for npc in npcs:
        if not isinstance(npc, dict):
            rows.append(npc)
            continue
        row = dict(npc)
        if "canon" in row:
            row["canon"] = _clip_value(row.get("canon"), _CANON_CHARS)
        rows.append(row)
    return rows


def _prompt_world(state: dict) -> dict:
    """Copia o mundo do prompt. Não altera o estado que o turno ainda lê."""
    world = dict(state)
    player = world.get("player")
    if isinstance(player, dict):
        world["player"] = _bound_player(player)
    if "present_npcs" in world:
        world["present_npcs"] = _bound_present(world.get("present_npcs"))
    if "canon_blob" in world:
        world["canon_blob"] = _clip_value(world.get("canon_blob") or "", _CANON_CHARS)
    if "character_rules" in world and world.get("character_rules") is not None:
        rules = world.get("character_rules")
        if not isinstance(rules, str):
            rules = json.dumps(rules, ensure_ascii=False, default=str)
        world["character_rules"] = npc_assets._clip(rules, _RULES_CHARS)
    memories = world.get("memories")
    if isinstance(memories, list):
        world["memories"] = [
            npc_assets._clip(_memory_text(item), _MEMORY_CHARS)
            for item in memories[:_MEMORY_KEEP]
        ]
    actions = world.get("recent_actions")
    if isinstance(actions, list):
        kept = []
        for item in actions[-_ACTION_KEEP:]:
            if isinstance(item, dict):
                row = dict(item)
                text = row.get("text")
                row["text"] = npc_assets._clip(text if isinstance(text, str) else str(text or ""), _ACTION_CHARS)
                kept.append(row)
            else:
                kept.append(item)
        world["recent_actions"] = kept
    if "absent" in world:
        world["absent"] = _absent_rows(world.get("absent"))
    return world


def _messages(action_text: str, action_type: str, state: dict) -> list[dict]:
    payload = {
        "action_type": action_type,
        "action": action_text,
        "world": _prompt_world(state),
    }
    return [
        {"role": "system", "content": _SYSTEM},
        {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
    ]


def _verdict_from_text(text: str) -> Verdict | None:
    try:
        data = json.loads(text or "")
    except (TypeError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    possible = data.get("possible")
    blockers = data.get("blockers")
    risks = data.get("risks")
    notes = data.get("notes")
    if not isinstance(possible, bool):
        return None
    if not isinstance(blockers, list) or not isinstance(risks, list) or not isinstance(notes, str):
        return None
    if not all(isinstance(item, str) for item in blockers + risks):
        return None
    cost = _optional_text(data.get("cost")) if possible else ""
    consequence = _optional_text(data.get("consequence")) if possible else ""
    return Verdict(
        possible=possible,
        blockers=[item.strip() for item in blockers if item.strip()],
        risks=[item.strip() for item in risks if item.strip()],
        notes=notes.strip(),
        cost=cost,
        consequence=consequence,
    )


def _optional_text(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    return value.strip()


def _player_view(conn, adventure: dict, user: str) -> dict:
    card = db.player_character(conn, adventure.get("id") or "", user) if user else None
    sheet = npc_assets.parse_sheet((card or {}).get("sheet") or "") if card else {}
    ui = adventure.get("ui_state") if isinstance(adventure.get("ui_state"), dict) else {}
    return {
        "name": (card or {}).get("title") or "",
        "class": _sheet_class(sheet),
        "appearance": sheet.get("appearance") or "",
        "status": _sheet_status(sheet),
        "clothing": sheet.get("clothing") or "",
        "stats": _sheet_stats(sheet),
        "canon": npc_assets.card_canon_text(card) if card else "",
        "inventory": ui.get("inventory"),
        "dice": ui.get("dice"),
        "character": ui.get("character"),
        "ui_state": ui,
    }


class _AbsentRef(str):
    """Nome ausente com status. Compara como string no filtro do turno."""

    def __new__(cls, name: str, status: str = ""):
        obj = str.__new__(cls, name)
        obj.status = status or ""
        return obj


def _remember_absent(absent: list, title: str, status: str) -> None:
    if not title or world_facts.name_is_absent(title, absent):
        if title and status:
            folded = title.casefold()
            for index, item in enumerate(absent):
                if str(item).strip().casefold() != folded or getattr(item, "status", ""):
                    continue
                absent[index] = _AbsentRef(str(item).strip(), status)
                break
        return
    absent.append(_AbsentRef(title, status))


def _split_npcs(conn, adventure_id: str, cards: list[dict],
                assessment: world_facts.Assessment, actions: list[dict]) -> tuple[list[dict], list]:
    scene = "\n".join(
        (action.get("text") or "")
        for action in actions
        if action.get("type") != "see"
    )
    blob = "\n".join(part for part in (scene, assessment.canon_blob) if part)
    present: list[dict] = []
    absent = list(assessment.absent)
    for card in cards:
        if (card.get("type") or "") != "character" or card.get("player_username"):
            continue
        title = (card.get("title") or "").strip()
        if world_facts.name_is_absent(title, assessment.absent) or not _card_in_text(card, blob):
            sheet = npc_assets.parse_sheet(card.get("sheet") or "")
            _remember_absent(absent, title, _sheet_status(sheet))
            continue
        present.append(_npc_view(conn, adventure_id, card))
    return present, _unique(absent)


def _npc_view(conn, adventure_id: str, card: dict) -> dict:
    sheet = npc_assets.parse_sheet(card.get("sheet") or "")
    bonds = npc_bonds.read_bonds(sheet)
    memories = npc_life.list_memories(conn, adventure_id, card.get("id") or "", limit=1)
    last = memories[0] if memories else {}
    life = sheet.get("life") if isinstance(sheet.get("life"), dict) else {}
    mood = " ".join(str(life.get("mood") or "").split())
    activity = " ".join(str(life.get("activity") or "").split())
    if not mood:
        mood = " ".join(str(last.get("mood") or "").split())
    bits = [bit for bit in (mood, activity) if bit]
    name = " ".join(str(card.get("title") or "").split()) or "NPC"
    mood_line = f"{name} — {'; '.join(bits)}" if bits else ""
    return {
        "name": name,
        "status": _sheet_status(sheet),
        "class": _sheet_class(sheet),
        "stats": _sheet_stats(sheet),
        "canon": npc_assets.card_canon_text(card),
        "bonds": bonds,
        "mood_line": mood_line,
        "memory": (last.get("text") or "").strip(),
    }


_STATUSES = {"alive", "dead", "missing", "injured"}
# hp pode vir junto no mesmo dict; as outras seis nem sempre estão preenchidas.
_STAT_KEYS = ("str", "dex", "con", "int", "wis", "cha", "hp")


def _sheet_class(sheet: dict) -> str:
    value = sheet.get("class")
    if isinstance(value, str) and value.strip():
        return value.strip()
    return ""


def _sheet_status(sheet: dict) -> str:
    value = sheet.get("status")
    if not isinstance(value, str):
        return ""
    text = value.strip()
    if text.casefold() not in _STATUSES:
        return ""
    return text


def _sheet_stats(sheet: dict) -> dict:
    raw = sheet.get("stats")
    if not isinstance(raw, dict):
        return {}
    return {key: raw[key] for key in _STAT_KEYS if key in raw and raw[key] is not None}


def _card_in_text(card: dict, text: str) -> bool:
    return any(world_facts._last_index(text, label) >= 0 for label in world_facts._card_labels(card))


def _recent_actions(actions: list[dict]) -> list[dict]:
    rows = []
    for action in actions:
        if action.get("type") == "see":
            continue
        rows.append({
            "type": action.get("type") or "",
            "text": (action.get("text") or "").strip(),
        })
    return rows[-6:]


def _query_text(actions: list[dict]) -> str:
    for action in reversed(actions):
        if action.get("type") in world_facts.ATTEMPT_TYPES and (action.get("text") or "").strip():
            return action.get("text") or ""
    return (actions[-1].get("text") or "") if actions else ""


def _story_memories(conn, adventure_id: str, query: str) -> list[dict]:
    if not adventure_id:
        return []
    retrieval = _retrieval()
    if retrieval is None or not hasattr(retrieval, "select_memories"):
        return []
    try:
        found = retrieval.select_memories(
            conn, adventure_id, query, _MEMORY_BUDGET, top_k=_MEMORY_TOP_K,
        )
    except Exception:
        log.warning("action_check: memórias indisponíveis", exc_info=True)
        return []
    return list(found or [])


def _retrieval():
    for name in ("memory.retrieval", "backend.memory.retrieval"):
        try:
            return importlib.import_module(name)
        except ImportError:
            continue
    return None


def _strings(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [item.strip() for item in value if isinstance(item, str) and item.strip()]


def _unique(names: list[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for name in names:
        key = name.casefold()
        if key not in seen:
            seen.add(key)
            out.append(name)
    return out


def _max_tokens(cfg: dict) -> int:
    try:
        return max(1, int((cfg or {}).get("max_tokens", 300)))
    except (TypeError, ValueError):
        return 300


def _timeout(cfg: dict) -> float:
    try:
        return float((cfg or {}).get("timeout_seconds", 20))
    except (TypeError, ValueError):
        return 20.0
