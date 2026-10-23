"""Vínculo vivo do NPC: emoção, o que quer agora, e o lado em relação ao jogador.

Não é um segundo motor de regras. Cinco inteiros na ficha (0–100) mudam
com a ação do jogador, por regras fixas, e o turno seguinte mostra o
resultado só na user final — o prefixo cacheado da janela não se mexe.

Pesos: relationship, trust, distrust, fear, desire, boldness.
Postura: help, resist, flirt, flee, attack.
"""

from __future__ import annotations

import json
import re

from app import db, npc_assets
from app.voice_emotion import emotion_from_text

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

# (nome, agulhas, delta). "*" no fim é prefixo. Várias regras no mesmo
# texto somam; o clamp fica no fim.
RULES: tuple[tuple[str, tuple[str, ...], dict[str, int]], ...] = (
    ("kindness", (
        "thank", "thanks", "obrigad*", "agradec*", "gentil", "comfort*",
        "heal", "heals", "healed", "protege*", "protect*", "kindness", "acolh*",
    ), {"trust": 8, "relationship": 5, "fear": -4, "distrust": -6}),
    ("threat", (
        "threat*", "kill*", "matar", "attack*", "atacar", "ataco",
        "ataca", "atacou", "soco", "punch*", "ameac*",
    ), {"fear": 18, "trust": -10, "relationship": -6, "distrust": 10}),
    ("gift", (
        "gift", "presente", "moeda", "moedas", "recompensa",
        "give you", "gives you", "gave you", "te dou", "lhe dou",
    ), {"desire": 6, "relationship": 8, "trust": 3, "distrust": -4}),
    ("flirt", (
        "flirt*", "kiss*", "beij*", "seduc*", "te amo", "love you",
    ), {"desire": 10, "relationship": 4, "boldness": 2}),
    ("insult", (
        "insult*", "idiota", "stupid", "shut up", "cala a boca", "imbecil",
    ), {"relationship": -8, "trust": -6, "boldness": 4, "fear": 2, "distrust": 8}),
    ("assault", (
        "cascud*", "soco", "socar", "soca", "chute", "chutar", "chuto",
        "esfaque*", "estrangul*", "matar", "mato", "kill*", "punch*",
        "defec*", "cagar", "cago", "mij*", "cuspir", "cuspo",
    ), {"fear": 22, "trust": -16, "relationship": -14, "boldness": 8, "distrust": 14}),
)

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
_TURNED_DOWN = re.compile(
    r"(?i)\b(recus\w*|refus\w*|afast\w*|n[aã]o deixa|n[aã]o cede|n[aã]o obedece|"
    r"segura o pr[oó]prio espa[cç]o|d[aá] um passo para tr[aá]s)\b"
)
REFUSAL_DELTA = {
    "trust": -6,
    "relationship": -4,
    "fear": 6,
    "desire": -8,
    "boldness": 2,
    "distrust": 8,
}


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


def _hit(text: str, needle: str) -> bool:
    prefix = needle.endswith("*")
    word = needle[:-1] if prefix else needle
    if not word:
        return False
    if prefix:
        pattern = rf"(?<!\w){re.escape(word)}"
    else:
        pattern = rf"(?<!\w){re.escape(word)}(?!\w)"
    return re.search(pattern, text) is not None


def deltas_for(text: str) -> dict[str, int]:
    """Soma as regras que a frase dispara. Sem regra, dict vazio."""
    folded = (text or "").casefold()
    if not folded.strip():
        return {}
    total: dict[str, int] = {}
    for _name, needles, delta in RULES:
        if not any(_hit(folded, needle) for needle in needles):
            continue
        for key, value in delta.items():
            total[key] = total.get(key, 0) + value
    return total


def apply_deltas(bonds: dict[str, int], text: str) -> dict[str, int]:
    """Aplica as regras e corta em 0–100. Não grava."""
    current = read_bonds({"bonds": bonds})
    for key, value in deltas_for(text).items():
        current[key] = clamp(current[key] + value)
    return current


def stance_of(bonds: dict[str, int]) -> str:
    """Postura diante do jogador, só pelos números."""
    trust = bonds["trust"]
    fear = bonds["fear"]
    desire = bonds["desire"]
    boldness = bonds["boldness"]
    relationship = bonds["relationship"]
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


def apply_turn(conn, adventure_id: str, player_text: str, narration: str) -> int:
    """Atualiza os pesos de quem a ação tocou. Devolve quantos cards mudaram.

    A ação do jogador manda quando ela mesma dispara uma regra. Sem isso,
    vale a narração (um continue, por exemplo). Quem tem nome na ação do
    jogador é o alvo; sem nome, todo NPC vivo citado na narração.
    """
    player_text = player_text or ""
    narration = narration or ""
    player_delta = deltas_for(player_text)
    source = player_text if player_delta else narration
    delta = player_delta or deltas_for(narration)
    # Um beijo recusado não pode subir desejo: a tentativa falhou.
    # Medo e queda de confiança de uma agressão ficam.
    if player_delta and delta.get("desire", 0) > 0 and _TURNED_DOWN.search(narration or ""):
        delta = dict(REFUSAL_DELTA)
    if not delta:
        return 0
    cards = db.list_cards(conn, adventure_id)
    if player_delta:
        targets = characters_in_text(cards, player_text) or characters_in_text(cards, narration)
    else:
        targets = characters_in_text(cards, source)
    changed = 0
    for card in targets:
        sheet = npc_assets.parse_sheet(card.get("sheet") or "")
        if not _living(sheet):
            continue
        bonds = read_bonds(sheet)
        for key, value in delta.items():
            bonds[key] = clamp(bonds[key] + value)
        updated = write_bonds(sheet, bonds)
        db.update_card(conn, adventure_id, card["id"], {
            "sheet": json.dumps(updated, ensure_ascii=False),
        })
        changed += 1
    return changed


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
        stance = stance_of(bonds)
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
