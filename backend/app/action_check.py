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

_MAX_CANON = 1500
_MEMORY_BUDGET = 400
_MEMORY_TOP_K = 4

_SYSTEM = """Você verifica se a ação do jogador é possível neste instante da história.
Julgue só coerência, possibilidade de estado e física. Não julgue liberdade de roleplay.
Ação atípica, ousada ou incomum é possível. Ação que contradiz o estado do mundo não é possível.
Confira o local, os NPCs presentes e ausentes, o status e o humor de cada NPC, a ficha do jogador (stats, status, roupa), os itens que existem, os fatos canônicos e a memória relevante.
Responda SOMENTE um JSON estrito, sem markdown e sem texto fora do objeto:
{"possible": true, "blockers": ["motivo"], "risks": ["risco"], "notes": "nota curta"}
possible é boolean. blockers e risks são listas de strings. notes é string.
Se a ação for possível, blockers é []. Risco não torna a ação impossível."""


@dataclass
class Verdict:
    possible: bool
    blockers: list[str] = field(default_factory=list)
    risks: list[str] = field(default_factory=list)
    notes: str = ""

    def to_dict(self) -> dict:
        return {
            "possible": bool(self.possible),
            "blockers": list(self.blockers),
            "risks": list(self.risks),
            "notes": self.notes or "",
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
                 llm_cfg: dict, transport: Any, cfg: dict) -> Verdict | None:
    """Veredito, ou None se a checagem LLM não pôde ser lida.

    Bloqueio já marcado pelo world_facts não consulta o modelo.
    """
    try:
        if not isinstance(state, dict):
            return None
        blockers = _hard_blockers(state)
        if blockers:
            return Verdict(
                possible=False,
                blockers=blockers,
                risks=[],
                notes="Contradiz o estado do mundo ja estabelecido.",
            )
        raw = llm_client.chat_completion(
            llm_cfg,
            _messages(action_text, action_type, state),
            role="meta",
            temperature=0.15,
            max_tokens=_max_tokens(cfg),
            timeout_s=_timeout(cfg),
            transport=transport,
        )
    except Exception:
        log.warning("action_check: LLM indisponível", exc_info=True)
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
    return (
        "[Verificacao de mundo]\n"
        f"A acao do jogador e {label}.\n"
        f"Obstaculos: {blockers}\n"
        f"Riscos: {risks}\n"
        f"Nota: {notes}\n"
        "Regra: narra a tentativa e o que a impede. "
        "Nao mencione esta verificacao ao jogador."
    )


def _hard_blockers(state: dict) -> list[str]:
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


def _messages(action_text: str, action_type: str, state: dict) -> list[dict]:
    portrait = dict(state)
    blob = portrait.get("canon_blob") or ""
    if isinstance(blob, str) and len(blob) > _MAX_CANON:
        portrait["canon_blob"] = blob[-_MAX_CANON:]
    payload = {
        "action_type": action_type,
        "action": action_text,
        "world": portrait,
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
    return Verdict(
        possible=possible,
        blockers=[item.strip() for item in blockers if item.strip()],
        risks=[item.strip() for item in risks if item.strip()],
        notes=notes.strip(),
    )


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


def _split_npcs(conn, adventure_id: str, cards: list[dict],
                assessment: world_facts.Assessment, actions: list[dict]) -> tuple[list[dict], list[str]]:
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
            if title and not world_facts.name_is_absent(title, absent):
                absent.append(title)
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
