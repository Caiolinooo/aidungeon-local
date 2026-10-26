"""Consistência de morte e presença de personagens na narrativa e imagens."""

from __future__ import annotations

import re

from app import npc_assets

_RESURRECTION = re.compile(
    r"(?i)\b(?:ressuscit\w*|resurrect\w*|voltou à vida|voltou a vida|"
    r"returned to life|revived|reviveu|renasc\w*|reborn)\b"
)
_ALIVE_ACTION = re.compile(
    r"(?i)\b(?:fala|falou|diz|disse|murmura|murmurou|grita|gritou|"
    r"responde|respondeu|age|ageu|move|moveu|ajusta|ajustou|"
    r"pressiona|pressionou|toca|tocou|sorri|sorriu|"
    r"walks|walked|moves|moved|speaks|spoke|says|said|"
    r"adjusts|adjusted|presses|pressed|smiles|smiled|"
    r"stands|stood|acts|acted|works|worked)\b"
)
_CORPSE_WORDS = re.compile(
    r"(?i)\b(?:cadáver|cadaver|corpo sem vida|corpo morto|"
    r"restos mortais|remains|corpse|dead body|decapitad|"
    r"sem cabeça|headless|cabeça cortada|severed head)\b"
)


def is_dead(card: dict) -> bool:
    sheet = npc_assets.parse_sheet(card.get("sheet") or "")
    return str(sheet.get("status") or "").strip().lower() == "dead"


def resurrection_established(text: str, card: dict | None = None) -> bool:
    """Só aceita ressurreição explícita, nunca sobrevivência inventada."""
    if not _RESURRECTION.search(text or ""):
        return False
    if card is None:
        return True
    title = str(card.get("title") or "")
    if not title:
        return False
    for sentence in re.split(r"(?<=[.!?])\s+", text or ""):
        if _RESURRECTION.search(sentence) and re.search(
            rf"(?<!\w){re.escape(title)}(?!\w)", sentence, re.IGNORECASE
        ):
            return True
    return False


def dead_acting_alive(cards: list[dict], narration: str) -> list[str]:
    """Nomes de mortos que a prosa colocou agindo como vivos."""
    found: list[str] = []
    for card in cards:
        if not is_dead(card):
            continue
        title = str(card.get("title") or "").strip()
        if not title:
            continue
        for sentence in re.split(r"(?<=[.!?])\s+", narration or ""):
            if not re.search(rf"(?<!\w){re.escape(title)}(?!\w)", sentence, re.IGNORECASE):
                continue
            if _CORPSE_WORDS.search(sentence) and not _ALIVE_ACTION.search(sentence):
                continue
            if _ALIVE_ACTION.search(sentence):
                found.append(title)
                break
    return list(dict.fromkeys(found))


def living_cast(cards: list[dict]) -> list[dict]:
    """Elenco que pode aparecer vivo. Mortos ficam fora das imagens de ação."""
    return [card for card in cards if not is_dead(card)]


def corpse_visual(card: dict) -> str:
    """Descrição visual permitida para um morto, se a cena exigir o corpo."""
    sheet = npc_assets.parse_sheet(card.get("sheet") or "")
    wounds = str(sheet.get("body_state") or "")
    title = str(card.get("title") or "the person")
    if "decapit" in wounds.lower() or "amput" in wounds.lower():
        return f"lifeless decapitated body of {title}, motionless on the ground"
    return f"lifeless body of {title}, motionless on the ground"
