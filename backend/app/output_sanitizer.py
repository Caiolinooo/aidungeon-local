"""Limpeza da narração antes de gravar (contrato C3, docs/agents/CONTRATOS.md).

`game_engine` chama `clean_narration(text, truncated)` depois do stream e antes
de persistir; o `done` leva o texto já limpo.

O narrador às vezes copia o formato do próprio contexto: um `<lore>`, um
cabeçalho `[World info]`, uma nota `[Continuity Anchor: …]` ou uma fala
`> You` do jogador. As `stop_sequences` do context_builder param a geração
nesses marcadores; aqui ficam as variantes que escapam delas (caixa,
markdown, servidor sem `stop`). Bloco no início sai até a linha em branco
seguinte e a prosa depois dele fica; marcador no meio corta dali até o fim,
como a stop faria.
"""

from __future__ import annotations

import re

LORE_MARKER = re.compile(
    r"<\s*/?\s*lore\s*>"
    r"|\[\s*(?:world\s+info|memories|summary|player\s+state|players|plot|npc\s+minds|verificacao\s+de\s+mundo)\s*\]"
    r"|\[\s*(?:author['’]?s\s+note|active\s+lore|continuity\s+anchor|web\s+research)\b"
    r"|\[\s*(?:style|canon|private)\s*:",
    re.IGNORECASE,
)
PLAYER_LINE = re.compile(r"^[ \t]*>[ \t]*(?:you|voc[eê])\b", re.IGNORECASE | re.MULTILINE)
_LORE_OPEN = re.compile(r"<\s*lore\s*>", re.IGNORECASE)
_LORE_CLOSE = re.compile(r"<\s*/\s*lore\s*>", re.IGNORECASE)
_BLANK_LINE = re.compile(r"\n[ \t]*\n")
_DECORATION = "*_#~` \t\r\n"
# Pontuação final seguida de aspas ou parênteses que fecham, e depois espaço.
_SENTENCE_END = re.compile(r"[.!?…]+[\"'”’»)\]*_]*(?=\s|$)")
_ABBREVIATION = re.compile(
    r"(?:\b(?:Dr|Dra|Sr|Sra|Srta|Mr|Mrs|Ms|Prof|Profa|St|Jr|Av)|(?:^|\s)[A-Z])\.$")


def cut_incomplete_sentence(text: str) -> str:
    """O teto de tokens cortou no meio. A prosa gravada para no fim da última
    frase completa, com as aspas que a fecham."""
    stripped = text.rstrip()
    for end in reversed(list(_SENTENCE_END.finditer(stripped))):
        if _ABBREVIATION.search(stripped[: end.end()]):
            continue
        return stripped[: end.end()]
    return stripped


def _drop_leading_block(body: str, marker: re.Match) -> str:
    if _LORE_CLOSE.fullmatch(marker.group(0)):
        return body[marker.end():]
    if _LORE_OPEN.fullmatch(marker.group(0)):
        close = _LORE_CLOSE.search(body, marker.end())
        if close:
            return body[close.end():]
    blank = _BLANK_LINE.search(body, marker.end())
    return body[blank.end():] if blank else ""


def strip_context(text: str) -> tuple[str, bool]:
    """Tira blocos de contexto e falas do jogador. Devolve o texto e se cortou
    prosa no meio (a última frase pode ter ficado pela metade)."""
    stripped_head = False
    while True:
        body = text.lstrip(_DECORATION)
        player = PLAYER_LINE.match(body)
        if player:
            newline = body.find("\n", player.end())
            text = body[newline + 1:] if newline >= 0 else ""
            stripped_head = True
            continue
        marker = LORE_MARKER.match(body)
        if marker:
            text = _drop_leading_block(body, marker)
            stripped_head = True
            continue
        break
    if stripped_head:
        text = text.lstrip()
    cuts = [m.start() for m in (LORE_MARKER.search(text), PLAYER_LINE.search(text)) if m]
    if cuts:
        return text[: min(cuts)], True
    return text, False


def clean_narration(text: str, truncated: bool) -> str:
    text, cut = strip_context(text)
    if truncated or cut:
        text = cut_incomplete_sentence(text)
    text = text.rstrip()
    if not any(char.isalnum() for char in text):
        return ""
    return text
