"""Uma chamada LLM `meta` transforma a frase do jogador em intenção estruturada.

Falha de rede, de JSON ou de prazo devolve None: o turno segue só com narração.
"""

from __future__ import annotations

import json
import logging

from app import llm_client, npc_assets

log = logging.getLogger("aidungeon.sim.intent")

INTENT_KINDS = ("attack", "skill", "save", "use_item", "buy", "take", "rest", "none")
TOUGHNESS = ("weak", "normal", "tough", "boss")
DIFFICULTY = ("trivial", "easy", "medium", "hard", "very_hard")
ACTIVITIES = ("eat", "sleep", "rest", "wash", "social", "fun", "work", "bladder", "other")
SHOCKS = ("none", "minor", "major", "severe")
_ABILITIES = ("str", "dex", "con", "int", "wis", "cha")
_ADV = ("adv", "dis", "")

_SYSTEM = """Classifique a ação do jogador em JSON estrito, sem markdown e sem texto fora do objeto.
Chaves obrigatórias:
{"kind":"attack|skill|save|use_item|buy|take|rest|none","ability":"str|dex|con|int|wis|cha|","skill":"","target":"","toughness":"weak|normal|tough|boss","hostile":false,"weapon":"","item":"","gold":0,"difficulty":"trivial|easy|medium|hard|very_hard","advantage":"adv|dis|","minutes":30,"activity":"eat|sleep|rest|wash|social|fun|work|bladder|other","shock":"none|minor|major|severe","heal":false}
Regras:
- kind "none" para fala, exploração sem risco ou ação sem resultado incerto.
- minutes é o tempo realista da ação, inteiro de 1 a 720.
- Nunca invente um alvo que a ação não citou. target vazio se não houver alvo.
- toughness só quando o ataque é contra uma criatura que ainda não está em inimigos_conhecidos.
- gold é o preço plausível em moedas quando kind é buy; 0 nos outros casos.
- shock só para horror cósmico ou trauma; nos outros casos "none".
- heal true só se a ação é claramente de curar.
- advantage "adv" ou "dis" só se a própria ação descreve vantagem ou desvantagem; senão "".
- ability é a habilidade d20 mais próxima (força, destreza, constituição, inteligência, sabedoria, carisma). Vazio se não couber.
- skill é um nome curto de perícia (Atletismo, Furtividade, Luta) ou "".
- weapon e item são o texto do objeto citado, ou "".
"""


def _clip(value, limit: int = 80) -> str:
    if not isinstance(value, str):
        return ""
    return " ".join(value.split())[:limit]


def _as_int(value, default: int) -> int:
    if isinstance(value, bool) or value is None:
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def normalize(raw: dict | None) -> dict:
    """Sempre devolve todas as chaves, com valor dentro das constantes."""
    data = raw if isinstance(raw, dict) else {}
    kind = data.get("kind")
    if kind not in INTENT_KINDS:
        kind = "none"
    ability = data.get("ability") if data.get("ability") in _ABILITIES else ""
    toughness = data.get("toughness") if data.get("toughness") in TOUGHNESS else "normal"
    difficulty = data.get("difficulty") if data.get("difficulty") in DIFFICULTY else "medium"
    advantage = data.get("advantage") if data.get("advantage") in _ADV else ""
    activity = data.get("activity") if data.get("activity") in ACTIVITIES else "other"
    shock = data.get("shock") if data.get("shock") in SHOCKS else "none"
    hostile = data.get("hostile") is True
    heal = data.get("heal") is True
    minutes = _as_int(data.get("minutes"), 30)
    minutes = max(1, min(720, minutes))
    gold = _as_int(data.get("gold"), 0)
    gold = max(0, min(100000, gold))
    return {
        "kind": kind,
        "ability": ability,
        "skill": _clip(data.get("skill")),
        "target": _clip(data.get("target")),
        "toughness": toughness,
        "hostile": hostile,
        "weapon": _clip(data.get("weapon")),
        "item": _clip(data.get("item")),
        "gold": gold,
        "difficulty": difficulty,
        "advantage": advantage,
        "minutes": minutes,
        "activity": activity,
        "shock": shock,
        "heal": heal,
    }


def classify(text: str, actor_view: dict, foes: list[str], pack: dict | None,
             llm_cfg: dict, transport, cfg: dict, cancel=None) -> dict | None:
    messages = [
        {"role": "system", "content": _SYSTEM},
        {"role": "user", "content": json.dumps({
            "acao": text,
            "personagem": actor_view,
            "inimigos_conhecidos": foes,
            "habilidades": pack.get("abilities") if isinstance(pack, dict) else None,
        }, ensure_ascii=False)},
    ]
    try:
        reply = llm_client.chat_completion(
            llm_cfg, messages, role="meta", temperature=0.1,
            max_tokens=int(cfg.get("max_tokens", 220)),
            timeout_s=cfg.get("timeout_seconds", 20),
            transport=transport, cancel=cancel,
        )
        return normalize(npc_assets._extract_json(reply))
    except llm_client.LlmCancelled:
        raise
    except Exception:
        log.warning("sim intent: classificação falhou", exc_info=True)
        return None
