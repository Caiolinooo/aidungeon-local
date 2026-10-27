"""Adjudicador do turno: uma chamada LLM separada do narrador.

O adjudicador recebe a ação, a cena, o perfil e o estado de TODOS os
personagens vivos (ficha, vínculos, memórias, corpo, dor, expressão) e decide
como o mundo responde. Não existe piso, crítico, muro de poder nem frase
pronta: números entram como contexto, nunca como sentença.

Saída canônica (JSON):
    decision: allow | partial | refuse | own_beat
    outcome: o que de fato aconteceu, em uma ou duas frases
    reactions[]: {npc, card_id, reaction, line}
    initiative: {npc, card_id, line} | null
    cue: instrução curta para o narrador, sem prosa pronta para o jogador

O chronicle continua sendo a autoridade única sobre ferimentos/dor; aqui o
adjudicador só descreve consequências físicas no `outcome`/`cue`.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from app import body_state, db, llm_client, npc_assets, npc_bonds, npc_life

log = logging.getLogger("aidungeon.npc_gate")

DECISIONS = ("allow", "partial", "refuse", "own_beat")
ALIGNMENTS = ("aliado", "neutro", "desconfiado", "oposto")

_SYSTEM = """You are the impartial GM/adjudicator of a persistent roleplaying game.
You are NOT the narrator. Decide what the world and every present person actually do.

RULES:
- Judge each action on its own merits, using the full situation: profiles, goals,
  personality, class, current outfit, injuries/pain/expression, bonds, memories,
  relationships, power/mana, who is present, what was just said and done.
- Power, mana, stats and level are context, not a automatic wall. A weaker person
  can still be clever, use leverage, flee, call allies, or pay a price. A stronger
  person can be surprised, restrained, wounded, or refuse. Never use a fixed result.
- NPCs are not puppets. Each one can refuse, negotiate, partially comply, lie,
  attack, help, flee, or act on their own motive. Consent, fear, loyalty, shame,
  anger, duty and self-preservation all matter.
- `profile.obedience = obedient_compelled` means the person is already a loyal
  servant, minion, thrall or mentally compelled subordinate. They obey unless
  the story explicitly breaks that control. Fear changes trembling, hesitation
  or desperation, not sudden rebellion. Do not invent a new defiance.
- Persistent consequences in `adventure.consequences` are canon: altered reality,
  perception, memory, injuries and social knowledge must shape every decision.
  A person only knows what they witnessed or learned; do not give them omniscience.
- Levels and unlocked skills change what is possible. Do not let a character use
  an ability they have not unlocked, and let higher-level characters perform
  stronger actions when the established rules allow it.
- Relationships between NPCs matter: allies may help each other, rivals may
  undermine each other, and fear can change who acts first.
- The player controls only their own attempt, never NPC reactions.
- Physical consequences must be plausible. Small visible wounds can appear on the
  face/hands/neck for a bust portrait; hidden wounds stay covered by clothing.
- Do not write the narration and do not answer as the narrator. Do not use canned
  lines. Lines must be specific to these people and this moment.
- If nobody is present, adjudicate the environment/action alone and leave
  reactions empty.
- If the action is impossible or the world cannot answer, choose refuse and state
  the concrete reason in outcome. If `world_check.possible` is false, its blockers
  are physical facts: do not override them, but you still decide how people react.
- Never mention rules, JSON, fields, numbers, "allow", "partial" or "refuse" to
  the player.

Reply STRICT JSON only:
{
  "decision": "allow|partial|refuse|own_beat",
  "outcome": "1-2 sentences in the language of the action, concrete and specific",
  "reactions": [
    {"npc": "exact name", "card_id": "exact id", "reaction": "what they do/feel",
     "line": "optional short spoken line, or empty"}
  ],
  "initiative": {"npc": "exact name", "card_id": "exact id", "line": "optional self-initiated action"} or null,
  "cue": "short instruction to the narrator about tone, consequences and what must not be contradicted"
}"""


def _obedience_context(card: dict) -> str:
    from app import world_facts
    return world_facts.obedience_of(card)


def plain(text: str) -> str:
    raw = text or ""
    say = re.match(r'^>\s*You\s+say\s+"(.*)"\s*$', raw, re.IGNORECASE | re.DOTALL)
    if say:
        return say.group(1).strip()
    do = re.match(r"^>\s*You\s+(.*)$", raw, re.IGNORECASE | re.DOTALL)
    if do:
        return do.group(1).strip()
    return raw.strip()


def _coerce_scale(value) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, str):
        text = value.strip().casefold()
        if text in ("infinite", "infinito", "inf", "∞"):
            return 999
        value = text
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def scales_of(sheet: dict | None, blob: str = "") -> dict:
    sheet = sheet if isinstance(sheet, dict) else {}
    stats = sheet.get("stats") if isinstance(sheet.get("stats"), dict) else {}
    mana = _coerce_scale(sheet.get("mana"))
    if mana is None:
        mana = _coerce_scale(stats.get("mana"))
    power = _coerce_scale(sheet.get("power"))
    if power is None:
        power = _coerce_scale(sheet.get("poder"))
    if power is None:
        power = _coerce_scale(stats.get("power") if stats.get("power") is not None else stats.get("poder"))
    god = any(word in (blob or "").casefold() for word in (
        "infinit", "deus", "deusa", "divindade", "godlike"))
    if god:
        mana = max(mana or 0, 999)
        power = max(power or 0, 999)
    return {"mana": mana, "power": power, "tier": "god" if god else "mortal"}


def alignment_from_text(text: str) -> str:
    folded = (text or "").casefold()
    wary = any(word in folded for word in ("desconf", "cautel", "perigos", "wary", "suspicious"))
    good_text = folded.replace("desconf", "").replace("desconfi", "")
    bad = any(word in folded for word in (
        "inimig", "odi", "caça", "caca", "rival", "opost", "hostil", "enemy", "hate"))
    good = any(word in good_text for word in (
        "aliad", "aliia", "amig", "leal", "confia", "ally", "friend"))
    if bad and good:
        return "desconfiado"
    if bad:
        return "oposto"
    if wary:
        return "desconfiado"
    if good:
        return "aliado"
    return "neutro"


def _blob(card: dict) -> str:
    sheet = npc_assets.parse_sheet(card.get("sheet") or "")
    return " ".join(str(part) for part in (
        card.get("title") or "",
        card.get("entry") or "",
        sheet.get("personality") or "",
        sheet.get("class") or "",
        sheet.get("goals") or "",
        sheet.get("notes") or "",
        sheet.get("background") or "",
    ) if part)


def _living_cards(cards: list[dict]) -> list[dict]:
    found = []
    for card in cards or []:
        if (card.get("type") or "") != "character" or card.get("player_username"):
            continue
        sheet = npc_assets.parse_sheet(card.get("sheet") or "")
        if npc_bonds._living(sheet):
            found.append(card)
    return found


def _has_living_npc(conn, adventure_id: str) -> bool:
    """Roster real: personagem vivo que não é o jogador. Morto e missing ficam de fora."""
    return bool(_living_cards(db.list_cards(conn, adventure_id)))


def ensure_saved(conn, adventure_id: str) -> int:
    """Garante os campos que o adjudicador lê; nunca decide por ninguém."""
    everyone = [
        card for card in db.list_cards(conn, adventure_id)
        if (card.get("type") or "") == "character"
    ]
    if not everyone:
        return 0
    dirty = 0
    for card in everyone:
        sheet = npc_assets.parse_sheet(card.get("sheet") or "")
        changed = False
        if not card.get("player_username"):
            if sheet.get("alignment") not in ALIGNMENTS:
                sheet["alignment"] = alignment_from_text(_blob(card))
                changed = True
            if not isinstance(sheet.get("body_state"), dict):
                sheet["body_state"] = body_state.normalize_body_state({})
                changed = True
        if changed:
            db.update_card(conn, adventure_id, card["id"], {
                "sheet": json.dumps(sheet, ensure_ascii=False),
            })
            dirty += 1
    return dirty


def _scene_of(actions: list[dict]) -> str:
    return "\n".join(
        (action.get("text") or "") for action in actions if action.get("type") != "see"
    )[-4000:]


def _card_context(card: dict, *, present: bool) -> dict:
    sheet = npc_assets.parse_sheet(card.get("sheet") or "")
    bonds = npc_bonds.read_bonds(sheet)
    state = body_state.body_state_of(sheet)
    scale = scales_of(sheet, _blob(card))
    return {
        "card_id": card.get("id"),
        "name": card.get("title") or "Alguém",
        "present": present,
        "profile": {
            "class": sheet.get("class") or "",
            "personality": sheet.get("personality") or "",
            "goals": sheet.get("goals") or "",
            "background": sheet.get("background") or "",
            "notes": sheet.get("notes") or "",
            "entry": card.get("entry") or "",
            "alignment": sheet.get("alignment") or "neutro",
            "obedience": _obedience_context(card),
        },
        "body": {
            "pain": state.get("pain"),
            "expression": sheet.get("expression") or "",
            "conditions": body_state.condition_context(state, limit=10),
            "visual_portrait": body_state.visual_prompt(state, portrait=True),
            "visual_scene": body_state.visual_prompt(state, portrait=False),
        },
        "bonds": bonds,
        "progression": __import__("app.progression", fromlist=["progression_of"]).progression_of(sheet),
        "relationships": sheet.get("relations") if isinstance(sheet.get("relations"), dict) else {},
        "power": scale,
        "memories": [],
    }


def _player_context(card: dict | None, username: str) -> dict:
    if not card:
        return {"name": username or "Jogador", "card_id": "", "body": {}, "profile": {}}
    sheet = npc_assets.parse_sheet(card.get("sheet") or "")
    state = body_state.body_state_of(sheet)
    return {
        "name": card.get("title") or username or "Jogador",
        "card_id": card.get("id"),
        "profile": {
            "class": sheet.get("class") or "",
            "personality": sheet.get("personality") or "",
            "goals": sheet.get("goals") or "",
            "entry": card.get("entry") or "",
        },
        "body": {
            "pain": state.get("pain"),
            "expression": sheet.get("expression") or "",
            "conditions": body_state.condition_context(state, limit=10),
        },
        "progression": __import__("app.progression", fromlist=["progression_of"]).progression_of(sheet),
        "power": scales_of(sheet, _blob(card)),
    }


# JSON da adjudicação (decision, outcome, reactions, initiative, cue).
# 400 cabe no schema; o recorte de outcome/cue continua em 900 caracteres.
GATE_MAX_TOKENS = 400


def _neutral_decision(payload: dict) -> dict:
    """Sem NPC vivo o ambiente não precisa de chamada auxiliar.

    `allow` com reações vazias deixa o narrador seguir a ação. Recusa física
    continua vindo do world_check, não daqui.
    """
    action = " ".join(str(payload.get("action") or "").split())
    if payload.get("mode") == "continue" or not action:
        outcome = "Ninguém age por conta própria neste instante."
    else:
        outcome = "A ação segue no ambiente, sem outra pessoa para responder."
    return {
        "decision": "allow",
        "outcome": outcome,
        "reactions": [],
        "initiative": None,
        "cue": outcome,
    }


def _call(llm_cfg: dict, payload: dict, transport, *, max_tokens: int = GATE_MAX_TOKENS,
          cancel=None) -> dict | None:
    try:
        raw = llm_client.chat_completion(
            llm_cfg,
            [
                {"role": "system", "content": _SYSTEM},
                {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
            ],
            role="meta",
            temperature=0.35,
            max_tokens=max_tokens,
            timeout_s=45,
            transport=transport,
            cancel=cancel,
            response_format={"type": "json_schema", "json_schema": {
                "name": "turn_adjudication", "strict": True,
                "schema": {
                    "type": "object",
                    "required": ["decision", "outcome", "reactions", "initiative", "cue"],
                    "properties": {
                        "decision": {"type": "string", "enum": list(DECISIONS)},
                        "outcome": {"type": "string"},
                        "reactions": {"type": "array", "items": {"type": "object"}},
                        "initiative": {"type": ["object", "null"]},
                        "cue": {"type": "string"},
                    },
                },
            }},
        )
    except llm_client.LlmCancelled:
        raise
    except Exception:
        log.warning("npc_gate: adjudicador indisponível", exc_info=True)
        return None
    try:
        data = npc_assets._extract_json(raw)
    except Exception:
        if max_tokens == GATE_MAX_TOKENS:
            return _call(llm_cfg, payload, transport, max_tokens=900,
                         cancel=cancel)
        log.warning("npc_gate: resposta sem JSON: %r", (raw or "")[:300])
        return None
    return data if isinstance(data, dict) else None


def _payload(conn, adventure_id: str, text: str, user: str) -> tuple[dict, list[dict]] | None:
    cards = db.list_cards(conn, adventure_id)
    living = _living_cards(cards)
    actions = db.list_actions(conn, adventure_id)
    scene = _scene_of(actions)
    present = npc_bonds.characters_in_text(living, scene or "")
    present_ids = {card["id"] for card in present}
    if not present_ids:
        mentioned = npc_bonds.characters_in_text(living, plain(text))
        present_ids = {card["id"] for card in mentioned}
    roster = []
    for card in living:
        item = _card_context(card, present=card["id"] in present_ids)
        memories = npc_life.list_memories(conn, adventure_id, card["id"], limit=8)
        item["memories"] = [
            {"kind": row.get("kind"), "text": (row.get("text") or "")[:240]}
            for row in memories
        ]
        roster.append(item)
    player = _player_context(db.player_character(conn, adventure_id, user), user)
    try:
        adventure = db.get_adventure(conn, adventure_id) or {}
    except Exception:
        adventure = {}
    payload = {
        "action": plain(text) if text else "",
        "mode": "player_action" if text else "continue",
        "scene_tail": scene,
        "adventure": {
            "title": adventure.get("title") or "",
            "premise": adventure.get("premise") or "",
            "plot_essentials": adventure.get("plot_essentials") or "",
            "consequences": __import__("app.consequences", fromlist=["effect_context"]).effect_context(adventure),
        },
        "player": player,
        "people": roster,
        "relationships_between_npcs": __import__("app.progression", fromlist=["relationship_context"]).relationship_context(living),
        "language": "pt-BR",
    }
    return payload, roster


def decide(adventure_id: str, text: str, llm_cfg: dict, transport, user: str = "",
           rng=None, world_check: dict | None = None, cancel=None) -> dict | None:
    """Adjudicação de uma ação do jogador. Sem LLM, devolve None.

    Sem NPC vivo elegível a decisão é neutra (`allow`) e o LLM não é chamado.
    Jogador, morto e `missing` não contam como roster.
    """
    with db.session() as conn:
        ensure_saved(conn, adventure_id)
        if not _has_living_npc(conn, adventure_id):
            return _neutral_decision({
                "action": plain(text),
                "mode": "player_action" if plain(text) else "continue",
            })
        packed = _payload(conn, adventure_id, text, user)
        if packed is None:
            return None
        payload, roster = packed
    if not roster:
        return _neutral_decision(payload)
    if isinstance(world_check, dict):
        payload["world_check"] = world_check
    data = _call(llm_cfg or {}, payload, transport, cancel=cancel)
    return _normalize_payload(data, payload)


def own_beat(adventure_id: str, llm_cfg: dict | None = None, transport=None,
             cancel=None) -> dict | None:
    """Continue: a IA decide se alguém age por conta própria e como.

    Sem NPC vivo não há beat próprio nem chamada auxiliar.
    """
    with db.session() as conn:
        ensure_saved(conn, adventure_id)
        if not _has_living_npc(conn, adventure_id):
            return None
        packed = _payload(conn, adventure_id, "", "")
        if packed is None:
            return None
        payload, roster = packed
    if not roster:
        return None
    payload["mode"] = "continue"
    payload["instruction"] = (
        "No player action this beat. Decide whether a present NPC acts on their own "
        "motive. If nobody acts, use decision=allow, empty reactions, initiative=null."
    )
    data = _call(llm_cfg or {}, payload, transport, cancel=cancel)
    result = _normalize_payload(data, payload)
    if result is None:
        return None
    if result.get("decision") == "allow" and result.get("initiative") is None:
        return None
    return result


def _normalize_payload(data: dict | None, payload: dict) -> dict | None:
    names = [item for item in (payload.get("people") or []) if item.get("name")]
    by_name = {str(item.get("name")).casefold(): item for item in names}
    by_id = {item.get("card_id"): item for item in names}
    if not isinstance(data, dict):
        return None
    decision = str(data.get("decision") or "").strip().lower()
    if decision not in DECISIONS:
        decision = "allow"
    reactions = []
    for raw in (data.get("reactions") or [])[:24]:
        if not isinstance(raw, dict):
            continue
        person = by_id.get(raw.get("card_id")) or by_name.get(
            str(raw.get("npc") or "").casefold())
        if not person:
            continue
        reaction = " ".join(str(raw.get("reaction") or "").split())[:500]
        line = " ".join(str(raw.get("line") or "").split())[:300]
        if not reaction and not line:
            continue
        reactions.append({
            "npc": person.get("name") or "Alguém",
            "card_id": person.get("card_id"),
            "reaction": reaction,
            "line": line,
        })
    initiative = None
    raw_beat = data.get("initiative")
    if isinstance(raw_beat, dict):
        person = by_id.get(raw_beat.get("card_id")) or by_name.get(
            str(raw_beat.get("npc") or "").casefold())
        line = " ".join(str(raw_beat.get("line") or "").split())[:300]
        if person and line:
            initiative = {"npc": person.get("name") or "Alguém",
                          "card_id": person.get("card_id"), "line": line}
    outcome = " ".join(str(data.get("outcome") or "").split())[:900]
    cue = " ".join(str(data.get("cue") or "").split())[:900]
    if not cue:
        cue = outcome
    if not cue and not reactions and initiative is None:
        return None
    return {
        "decision": decision,
        "outcome": outcome,
        "reactions": reactions,
        "initiative": initiative,
        "cue": cue,
    }


def cue_block(result: dict | None, roster: list[dict] | None = None) -> str:
    """Instrução para o narrador. Nunca contém fala pronta para substituir a prosa."""
    if not result:
        return ""
    lines = ["[Adjudicação do GM — obedeça sem citar estas regras]"]
    if result.get("outcome"):
        lines.append("Fato decidido: " + result["outcome"])
    if result.get("decision") == "refuse":
        lines.append("A tentativa do jogador NÃO tem o efeito pretendido.")
    elif result.get("decision") == "partial":
        lines.append("A tentativa só alcança parte do pretendido, com custo ou limite.")
    elif result.get("decision") == "own_beat":
        lines.append("O beat é dos personagens; o jogador não comandou isto.")
    for reaction in result.get("reactions") or []:
        bits = [reaction.get("reaction") or ""]
        if reaction.get("line"):
            bits.append('Fala possível: "' + reaction["line"] + '"')
        if any(bits):
            lines.append(f"{reaction.get('npc')}: " + " | ".join(bit for bit in bits if bit))
    beat = result.get("initiative") or {}
    if beat.get("line"):
        lines.append(f"Iniciativa de {beat.get('npc')}: {beat['line']}")
    if result.get("cue"):
        lines.append("Tom/consequência: " + result["cue"])
    if roster:
        names = [item.get("name") for item in roster if item.get("name")]
        if len(names) != len({str(name).casefold() for name in names}):
            lines.append("Pessoas diferentes com nomes iguais: não troque corpos, vozes ou memórias.")
    lines.append("Não contradiga o fato decidido; não transforme recusa em obediência.")
    return "\n".join(lines)


def remember(conn, adventure_id: str, result: dict | None) -> int:
    """Guarda reações recusadas/parciais como memória; não mexe nos vínculos."""
    if not result or result.get("decision") not in ("refuse", "partial"):
        return 0
    saved = 0
    for reaction in result.get("reactions") or []:
        card_id = reaction.get("card_id")
        text = (reaction.get("reaction") or reaction.get("line") or "").strip()
        if not card_id or not text:
            continue
        npc_life.add_memory(conn, adventure_id, card_id, "event", text[:240], "")
        saved += 1
    return saved
