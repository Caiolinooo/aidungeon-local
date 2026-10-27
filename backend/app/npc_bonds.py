"""Vínculo vivo do NPC: emoção, intenção e relação com o jogador e outros NPCs.

O commit do turno chama `apply_adjudication`: deltas pequenos, teto por campo,
clamp 0–100 e log de seq/evidência. O turno seguinte mostra o resultado só na
user final — o prefixo cacheado da janela não se mexe.

Pesos: relationship, trust, distrust, fear, desire, boldness.
Postura: help, resist, flirt, flee, attack.
"""

from __future__ import annotations

import json
import re

from app import db, npc_assets, progression
from app.voice_emotion import canonical_emotion, emotion_from_text

BOND_KEYS = ("relationship", "trust", "distrust", "fear", "desire", "boldness")
DEFAULTS = {
    "relationship": 40,
    "trust": 40,
    "distrust": 30,
    "fear": 15,
    "desire": 15,
    "boldness": 40,
}
BOND_MIN = 0
BOND_MAX = 100

STANCES = ("help", "resist", "flirt", "flee", "attack")

_WANTS = {
    "help": "help the player right now",
    "resist": "resist the player",
    "flirt": "move closer to the player",
    "flee": "leave this scene",
    "attack": "attack the player",
}


MOMENT_MARKER = "[NPC moment]"
_SCENE_LIMIT = 4
_MOMENT_CUE = (
    "Private cue: follow it, do not quote it. "
    "Each living person takes one visible action this beat "
    "(a step, a hand, a refusal, a demand) besides any spoken line. "
    "stance resist, flee or attack: an attempt to grab, move, undress, kiss or command them fails, "
    "and they stop it with their body. "
    "stance flirt: they choose the next move and do not instantly undress, leave or submit. "
    "stance help: they help the task, not surrender. "
    "Narrate the cost and the consequence. The scene must change."
)


def clamp(value: int) -> int:
    return max(BOND_MIN, min(BOND_MAX, int(value)))


def read_bonds(sheet: dict | None) -> dict[str, int]:
    """Lê os pesos. Ficha sem `bonds` usa o ponto de partida, sem gravar."""
    raw = sheet.get("bonds") if isinstance(sheet, dict) else None
    bonds = dict(DEFAULTS)
    if isinstance(raw, dict):
        for key in BOND_KEYS:
            if key not in raw:
                continue
            try:
                bonds[key] = clamp(int(raw[key]))
            except (TypeError, ValueError):
                continue
    return bonds


def write_bonds(sheet: dict, bonds: dict[str, int]) -> dict:
    """Devolve a ficha com os cinco pesos. O resto do JSON fica."""
    updated = dict(sheet or {})
    updated["bonds"] = {key: clamp(bonds[key]) for key in BOND_KEYS}
    return updated


# Mesmos tetos do histórico NPC↔NPC em progression.update_relation.
PLAYER_TARGET = "player"
NPC_RELATION_KEYS = ("trust", "fear", "affinity")
MAX_DELTA_PER_FIELD = 20
MAX_RELATION_UPDATES = 12
BOND_LOG_CAP = 8
EVIDENCE_CAP = 240
PUBLIC_CHANGE_KEYS = ("card_id", "name", "target", "delta", "reason", "evidence")

_EXPRESSION_FOR = {
    "joy": "smiling",
    "anger": "angry",
    "fear": "afraid",
    "sadness": "sad",
    "tenderness": "tender",
    "surprise": "surprised",
    "disgust": "disgusted",
    "neutral": "neutral",
}
_MATERIAL = re.compile(
    r"(?i)(?<!\w)(soco\w*|golpe\w*|ferid\w*|sangr\w*|ataqu\w*|attack\w*|threat\w*|"
    r"amea[cç]\w*|matar|mato|kill\w*|punch\w*|presente\w*|gift\w*|beij\w*|kiss\w*|"
    r"recus\w*|refus\w*|trai\w*|salv\w*|proteg\w*|protect\w*|insult\w*|"
    r"empurr\w*|faca|facada|cusp\w*|ajud\w*|thank\w*|obrigad\w*|heal\w*|"
    r"roub\w*|steal\w*|mentir\w*|lied|betray\w*|agress\w*|wound\w*|"
    r"esfaque\w*|estrangul\w*|chute\w*|chutar|apanh\w*|hurt\w*)(?!\w)"
)
_SMALL_TALK = re.compile(
    r"(?i)(?<!\w)(oi|ol[aá]|hey|hi|hello|tudo bem|como vai|bom dia|boa tarde|"
    r"boa noite|how are you|nice weather|small talk|conversa fiada|cumpriment\w*|"
    r"acena\w*|waves?|looks at|olha para|fica perto|standing near|stands near|"
    r"aproxim\w*|por perto)(?!\w)"
)
_RECIPROCAL = re.compile(
    r"(?i)(?<!\w)(retribu\w*|correspond\w*|beija de volta|kisses back|"
    r"aceita o beijo|accepts the kiss|reciproc\w*|devolve o beijo|"
    r"ambos querem|beija tamb[eé]m|kisses too)(?!\w)"
)
_HIDDEN = re.compile(
    r"(?i)(motiva[cç][aã]o oculta|hidden motive|private knowledge|"
    r"segredo\s*:|secret\s*:|\[npc moment\]|\[adjudica)"
)


def alignment_from_bonds(bonds: dict[str, int]) -> str:
    """Lado diante do jogador, só a partir dos pesos. Não lê o rótulo antigo."""
    trust = int(bonds.get("trust", 0))
    relationship = int(bonds.get("relationship", 0))
    distrust = int(bonds.get("distrust", 0))
    fear = int(bonds.get("fear", 0))
    if relationship <= 25 and distrust >= 55 and trust <= 30:
        return "oposto"
    if distrust >= 55 or (trust <= 30 and distrust >= 40) or (fear >= 55 and trust <= 35):
        return "desconfiado"
    if trust >= 60 and relationship >= 55 and distrust <= 40 and fear < 60:
        return "aliado"
    return "neutro"


def _expression_for(emotion: str, current: str) -> str:
    mapped = _EXPRESSION_FOR.get(emotion or "")
    if not mapped:
        return current
    if emotion == "neutral" and current.strip():
        return current
    return mapped


def _limit_delta(raw, allowed: tuple[str, ...]) -> dict[str, int]:
    if not isinstance(raw, dict):
        return {}
    limited: dict[str, int] = {}
    for key in allowed:
        if key not in raw or isinstance(raw[key], bool):
            continue
        try:
            value = int(raw[key])
        except (TypeError, ValueError):
            continue
        if value == 0:
            continue
        limited[key] = max(-MAX_DELTA_PER_FIELD, min(MAX_DELTA_PER_FIELD, value))
    return limited


def _public_text(text: str, hidden: str = "") -> str:
    cleaned = " ".join(str(text or "").split())
    secret = " ".join(str(hidden or "").split())
    if secret and len(secret) >= 12 and secret.casefold() in cleaned.casefold():
        cleaned = re.sub(re.escape(secret), " ", cleaned, flags=re.IGNORECASE)
        cleaned = " ".join(cleaned.split())
    if _HIDDEN.search(cleaned):
        return ""
    return cleaned[:EVIDENCE_CAP]


def _immaterial(own: str, evidence: str, action: str = "") -> bool:
    """Small talk e proximidade não movem vínculo. Fato material no próprio update, sim."""
    if _MATERIAL.search(own or ""):
        return False
    if _SMALL_TALK.search(own or ""):
        return True
    if _SMALL_TALK.search(action or "") and not _MATERIAL.search(action or ""):
        return True
    if not " ".join(str(evidence or "").split()) and not _MATERIAL.search(action or ""):
        return True
    return False


def normalize_relation_updates(raw, roster_ids: set[str], player_ids: set[str], *,
                               player_text: str = "", hidden: str = "") -> list[dict]:
    """Valida alvos contra o roster e limita o delta por par no turno.

    Alvo inexistente, o card do jogador e auto-alvo saem. Small talk e
    proximidade sem fato material saem. Desejo só sobe com reciprocidade
    explícita. O resultado só tem campos públicos.
    """
    if not isinstance(raw, list):
        return []
    roster = {str(item) for item in roster_ids if item}
    players = {str(item) for item in player_ids if item}
    merged: dict[tuple[str, str], dict] = {}
    order: list[tuple[str, str]] = []
    for item in raw[: MAX_RELATION_UPDATES * 2]:
        if not isinstance(item, dict):
            continue
        card_id = str(item.get("card_id") or "").strip()
        target = str(item.get("target") or "").strip()
        if card_id not in roster or card_id in players:
            continue
        if target.casefold() == PLAYER_TARGET:
            target = PLAYER_TARGET
        elif target in players or target not in roster or target == card_id:
            continue
        allowed = BOND_KEYS if target == PLAYER_TARGET else NPC_RELATION_KEYS
        delta = _limit_delta(item.get("delta"), allowed)
        reason = _public_text(item.get("reason"), hidden)
        evidence = _public_text(item.get("evidence"), hidden)
        action = player_text if target == PLAYER_TARGET else ""
        if delta.get("desire", 0) > 0 and not _RECIPROCAL.search(
            " ".join((action, reason, evidence))
        ):
            delta.pop("desire", None)
        if _immaterial(f"{reason} {evidence}", evidence, action) or not delta:
            continue
        key = (card_id, target)
        slot = merged.get(key)
        if slot is None:
            slot = {
                "card_id": card_id,
                "target": target,
                "delta": {},
                "reason": reason,
                "evidence": evidence,
            }
            merged[key] = slot
            order.append(key)
        elif reason and not slot["reason"]:
            slot["reason"] = reason
        if evidence and slot["evidence"] == "":
            slot["evidence"] = evidence
        for field, value in delta.items():
            total = slot["delta"].get(field, 0) + value
            total = max(-MAX_DELTA_PER_FIELD, min(MAX_DELTA_PER_FIELD, total))
            if total:
                slot["delta"][field] = total
            else:
                slot["delta"].pop(field, None)
    found = []
    for key in order:
        item = merged[key]
        if not item["delta"]:
            continue
        found.append({field: item[field] for field in PUBLIC_CHANGE_KEYS if field != "name"})
        if len(found) >= MAX_RELATION_UPDATES:
            break
    return found


def _seq_of(seq: int) -> int:
    try:
        return int(seq)
    except (TypeError, ValueError):
        return 0


def _already_logged(log, seq: int, target: str) -> bool:
    for entry in log or []:
        if not isinstance(entry, dict):
            continue
        if entry.get("seq") == seq and entry.get("target") == target:
            return True
    return False


def _applied_move(before: dict[str, int], delta: dict[str, int],
                  keys: tuple[str, ...]) -> dict[str, int]:
    applied: dict[str, int] = {}
    for key in keys:
        if key not in delta:
            continue
        after = clamp(int(before.get(key, 0)) + int(delta[key]))
        moved = after - int(before.get(key, 0))
        if moved:
            applied[key] = moved
    return applied


def _public_change(card: dict, target: str, delta: dict[str, int],
                   reason: str, evidence: str) -> dict:
    return {
        "card_id": card.get("id"),
        "name": " ".join(str(card.get("title") or "Alguém").split()) or "Alguém",
        "target": target,
        "delta": dict(delta),
        "reason": reason,
        "evidence": evidence,
    }


def _reaction_action(raw: dict) -> str:
    action = " ".join(str(raw.get("reaction") or "").split())
    line = " ".join(str(raw.get("line") or "").split())
    if line and line not in action:
        action = (action + " " + line).strip()
    return action[:EVIDENCE_CAP]


def _player_pose(adjudication: dict) -> str:
    actor = adjudication.get("actor")
    if isinstance(actor, dict):
        pose = " ".join(str(actor.get("pose") or "").split())
        if pose:
            return pose[:120]
    return " ".join(str(adjudication.get("actor_pose") or "").split())[:120]


def _action_text(player_text: str) -> str:
    from app.npc_gate import plain
    return plain(player_text)[:EVIDENCE_CAP]


def apply_adjudication(session, adventure_id: str, adjudication: dict, seq: int,
                       player_text: str = "", actor_name: str = "") -> list[dict]:
    """Único escritor dos deltas e de `sheet.visual_state` no commit.

    Devolve só `{card_id, name, target, delta, reason, evidence}`.
    O mesmo `seq` não soma outra vez. O card do jogador não entra como NPC.
    """
    adjudication = adjudication if isinstance(adjudication, dict) else {}
    seq_i = _seq_of(seq)
    cards = db.list_cards(session, adventure_id)
    player_ids = {
        card.get("id") for card in cards
        if card.get("player_username") and card.get("id")
    }
    by_id: dict[str, dict] = {}
    for card in cards:
        if (card.get("type") or "") != "character" or card.get("id") in player_ids:
            continue
        sheet = npc_assets.parse_sheet(card.get("sheet") or "")
        if _living(sheet):
            by_id[card["id"]] = card
    updates = normalize_relation_updates(
        adjudication.get("relation_updates"),
        set(by_id),
        player_ids,
        player_text=player_text,
        hidden=str(adjudication.get("cue") or ""),
    )
    dirty: dict[str, dict] = {}

    def sheet_of(card: dict) -> dict:
        current = dirty.get(card["id"])
        if current is None:
            current = npc_assets.parse_sheet(card.get("sheet") or "")
            dirty[card["id"]] = current
        return current

    public: list[dict] = []
    for update in updates:
        card = by_id.get(update["card_id"])
        if card is None:
            continue
        sheet = sheet_of(card)
        target = update["target"]
        if target == PLAYER_TARGET:
            log = sheet.get("bond_log") if isinstance(sheet.get("bond_log"), list) else []
            if _already_logged(log, seq_i, target):
                continue
            before = read_bonds(sheet)
            applied = _applied_move(before, update["delta"], BOND_KEYS)
            if not applied:
                continue
            bonds = dict(before)
            for key, value in applied.items():
                bonds[key] = clamp(before[key] + value)
            sheet = write_bonds(sheet, bonds)
            sheet["alignment"] = alignment_from_bonds(read_bonds(sheet))
            entry = {
                "seq": seq_i,
                "target": target,
                "delta": applied,
                "evidence": (update.get("evidence") or "")[:EVIDENCE_CAP],
            }
            sheet["bond_log"] = [
                item for item in log if isinstance(item, dict)
            ][-BOND_LOG_CAP + 1:] + [entry]
            dirty[card["id"]] = sheet
        else:
            if target not in by_id:
                continue
            relation = progression.relation_with(sheet, target)
            if any(str(item).startswith(f"seq {seq_i} ") for item in relation["history"]):
                continue
            before = {key: relation[key] for key in NPC_RELATION_KEYS}
            applied = _applied_move(before, update["delta"], NPC_RELATION_KEYS)
            if not applied:
                continue
            evidence = update.get("evidence") or ""
            event = (
                f"seq {seq_i} | {json.dumps(applied, ensure_ascii=False)} | {evidence}"
            )[:EVIDENCE_CAP]
            sheet = progression.update_relation(
                sheet, target,
                trust=applied.get("trust", 0),
                fear=applied.get("fear", 0),
                affinity=applied.get("affinity", 0),
                event=event,
            )
            dirty[card["id"]] = sheet
        public.append(_public_change(
            card, target, applied,
            update.get("reason") or "",
            update.get("evidence") or "",
        ))

    visuals: dict[str, dict[str, str]] = {}
    for reaction in adjudication.get("reactions") or []:
        if not isinstance(reaction, dict):
            continue
        card_id = str(reaction.get("card_id") or "")
        if card_id not in by_id:
            continue
        visuals[card_id] = {
            "action": _reaction_action(reaction),
            "emotion": canonical_emotion(str(reaction.get("emotion") or "")) or "",
            "pose": " ".join(str(reaction.get("pose") or "").split())[:120],
        }
    beat = adjudication.get("initiative")
    if isinstance(beat, dict):
        card_id = str(beat.get("card_id") or "")
        if card_id in by_id:
            current = visuals.get(card_id, {"action": "", "emotion": "", "pose": ""})
            line = " ".join(str(beat.get("line") or "").split())[:EVIDENCE_CAP]
            if line:
                current["action"] = line
            emotion = canonical_emotion(str(beat.get("emotion") or ""))
            if emotion:
                current["emotion"] = emotion
            pose = " ".join(str(beat.get("pose") or "").split())[:120]
            if pose:
                current["pose"] = pose
            visuals[card_id] = current
    for card_id, visual in visuals.items():
        if not (visual["action"] or visual["emotion"] or visual["pose"]):
            continue
        sheet = sheet_of(by_id[card_id])
        sheet["visual_state"] = {
            "action": visual["action"],
            "emotion": visual["emotion"],
            "pose": visual["pose"],
            "seq": seq_i,
        }
        if visual["emotion"]:
            sheet["expression"] = _expression_for(
                visual["emotion"], str(sheet.get("expression") or ""),
            )

    action = _action_text(player_text) if player_text else ""
    actor = adjudication.get("actor") if isinstance(adjudication.get("actor"), dict) else {}
    if not action:
        action = " ".join(str(actor.get("action") or "").split())[:EVIDENCE_CAP]
    pose = _player_pose(adjudication)
    if action or pose:
        candidates = [card for card in cards if card.get("id") in player_ids]
        player = None
        if actor_name:
            wanted = " ".join(actor_name.split()).casefold()
            player = next((
                card for card in candidates
                if " ".join(str(card.get("title") or "").split()).casefold() == wanted
            ), None)
        if player is None and candidates:
            player = candidates[0]
        if player is not None:
            sheet = npc_assets.parse_sheet(player.get("sheet") or "")
            sheet["visual_state"] = {
                "action": action,
                "emotion": "",
                "pose": pose,
                "seq": seq_i,
            }
            db.update_card(session, adventure_id, player["id"], {
                "sheet": json.dumps(sheet, ensure_ascii=False),
            })

    for card_id, sheet in dirty.items():
        db.update_card(session, adventure_id, card_id, {
            "sheet": json.dumps(sheet, ensure_ascii=False),
        })
    return public




def stance_of(bonds: dict[str, int], *, obedience: str = "") -> str:
    """Postura diante do jogador: números + vínculo hierárquico declarado."""
    trust = bonds["trust"]
    fear = bonds["fear"]
    desire = bonds["desire"]
    boldness = bonds["boldness"]
    relationship = bonds["relationship"]
    if obedience == "obedient_compelled":
        # Controle mental ou lealdade absoluta: medo muda a forma de obedecer,
        # não transforma o lacaio em rebelde sem uma ruptura explícita.
        return "help"
    if fear >= 70 and boldness < 55:
        return "flee"
    if fear >= 60 and trust <= 25:
        return "flee"
    if trust <= 25 and boldness >= 60 and relationship <= 35:
        return "attack"
    if bonds.get("distrust", 0) >= 60:
        return "resist"
    if desire >= 65 and trust >= 35 and fear < 50:
        return "flirt"
    if trust >= 55 and relationship >= 50 and fear < 45:
        return "help"
    return "resist"


def emotion_from_bonds(bonds: dict[str, int]) -> str:
    """Emoção quando a prosa da cena não declara uma."""
    if bonds["fear"] >= 60:
        return "fear"
    if bonds["desire"] >= 70 and bonds["trust"] >= 40:
        return "tenderness"
    if bonds["relationship"] <= 25 and bonds["boldness"] >= 55:
        return "anger"
    if bonds["trust"] <= 25 and bonds["relationship"] <= 35:
        return "sadness"
    if bonds["relationship"] >= 70 and bonds["trust"] >= 60:
        return "joy"
    return "neutral"


def _names(card: dict) -> list[str]:
    raw = [card.get("title") or "", *(card.get("triggers") or [])]
    names: list[str] = []
    for item in raw:
        name = " ".join(str(item).split()).strip()
        if len(name) >= 3 and name not in names:
            names.append(name)
    return names


def _mention_at(text: str, name: str) -> int:
    match = re.search(rf"(?<!\w){re.escape(name)}(?!\w)", text, re.IGNORECASE)
    if match is None:
        return -1
    return match.start()


def _last_mention(text: str, name: str) -> int:
    found = -1
    for match in re.finditer(rf"(?<!\w){re.escape(name)}(?!\w)", text, re.IGNORECASE):
        found = match.start()
    return found


def characters_in_text(cards: list[dict], text: str) -> list[dict]:
    """Cards type=character citados no texto, do mais recente para trás não:
    ordem de primeira menção, no máximo `_SCENE_LIMIT` no momento."""
    if not text:
        return []
    found: list[tuple[int, dict]] = []
    for card in cards:
        if (card.get("type") or "") != "character":
            continue
        positions = [_mention_at(text, name) for name in _names(card)]
        positions = [pos for pos in positions if pos >= 0]
        if positions:
            found.append((min(positions), card))
    found.sort(key=lambda item: item[0])
    return [card for _pos, card in found]


def _window_around(text: str, name: str) -> str:
    at = _last_mention(text, name)
    if at < 0:
        return ""
    start = max(0, at - 180)
    end = min(len(text), at + len(name) + 180)
    return text[start:end]


def emotion_for_card(card: dict, text: str, bonds: dict[str, int]) -> str:
    """O humor simulado vence a prosa; sem humor, a prosa vence o número."""
    sheet = npc_assets.parse_sheet(card.get("sheet") or "")
    life = sheet.get("life") if isinstance(sheet.get("life"), dict) else {}
    mood = str(life.get("mood") or "").strip()
    if mood:
        mapped = emotion_from_text(mood)
        if mapped:
            return mapped
    for name in _names(card):
        found = emotion_from_text(_window_around(text, name))
        if found:
            return found
    return emotion_from_bonds(bonds)


def npc_emotion_for_line(cards: list[dict], text: str) -> str | None:
    """Emoção de ficha do NPC citado por último na linha, se não for neutra."""
    best_at = -1
    best: str | None = None
    for card in characters_in_text(cards, text):
        sheet = npc_assets.parse_sheet(card.get("sheet") or "")
        bonds = read_bonds(sheet)
        emotion = emotion_from_bonds(bonds)
        if emotion == "neutral":
            continue
        for name in _names(card):
            at = _last_mention(text, name)
            if at >= best_at:
                best_at = at
                best = emotion
    return best


def _living(sheet: dict) -> bool:
    return sheet.get("status") not in ("dead", "missing")




def _moment_line(card: dict, text: str) -> str:
    sheet = npc_assets.parse_sheet(card.get("sheet") or "")
    bonds = read_bonds(sheet)
    name = " ".join(str(card.get("title") or "NPC").split())[:40] or "NPC"
    status = str(sheet.get("status") or "").strip().lower()
    if status == "dead":
        emotion, want, stance = "sadness", "stay dead and not act", "resist"
    elif status == "missing":
        emotion = emotion_for_card(card, text, bonds)
        if emotion == "neutral":
            emotion = "sadness"
        want, stance = "stay out of the scene", "flee"
    else:
        emotion = emotion_for_card(card, text, bonds)
        stance = stance_of(bonds, obedience=_obedience_of(card))
        want = _WANTS[stance]
    numbers = " ".join(f"{key} {bonds[key]}" for key in BOND_KEYS)
    alignment = str(sheet.get("alignment") or "").strip()
    align = f"; alignment {alignment}" if alignment in ("aliado", "neutro", "desconfiado", "oposto") else ""
    return f"{name}: emotion {emotion}; wants {want}; stance {stance}{align}; {numbers}"


def format_moment(cards: list[dict], scene_text: str) -> str:
    """Bloco curto. Vazio se ninguém da ficha está na cena."""
    present = characters_in_text(cards, scene_text)[:_SCENE_LIMIT]
    if not present:
        return ""
    lines = [f"{MOMENT_MARKER} {_MOMENT_CUE}"]
    lines.extend(_moment_line(card, scene_text) for card in present)
    return "\n".join(lines)

def _obedience_of(card: dict) -> str:
    from app import world_facts
    return world_facts.obedience_of(card)


def moment_for(conn, adventure_id: str, scene_text: str) -> str:
    cards = db.list_cards(conn, adventure_id)
    return format_moment(cards, scene_text)


def insert_into_final(content: str, block: str) -> str:
    """O bloco entra na user final. Se houver </lore>, fica dentro dele."""
    if not block:
        return content
    close = "</lore>"
    if close in content:
        at = content.index(close)
        return content[:at] + block + "\n" + content[at:]
    return block + "\n" + content


def strip_leaked(text: str) -> str:
    """Tira um [NPC moment] que o narrador tenha copiado para a prosa."""
    idx = text.find(MOMENT_MARKER)
    if idx < 0:
        return text
    prefix = text[:idx].rstrip()
    rest = text[idx + len(MOMENT_MARKER):]
    blank = rest.find("\n\n")
    if blank >= 0:
        tail = rest[blank + 2:].lstrip()
        if prefix and tail:
            return f"{prefix}\n\n{tail}"
        return prefix or tail
    return prefix
