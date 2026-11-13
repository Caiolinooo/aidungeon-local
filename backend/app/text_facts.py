"""Fatos em texto corrido: repetição, fatos passageiros e corte por frase.

O registro pós-turno acrescentava fatos às fichas comparando só a frase exata.
A ficha de um personagem virou "He is wearing a grey wool coat. He is wearing a
brown leather hat. He is walking ahead of the group. He is walking…", cortada
no meio da palavra ao bater no limite, e as consequências guardavam a mesma
percepção duas vezes com uma palavra a mais no fim.
"""

from __future__ import annotations

import re
import unicodedata

_SENTENCE = re.compile(r"(?<=[.!?…])\s+|\n+")
_WORD = re.compile(r"[\wÀ-ÿ'’-]+")
# Roupa e postura do momento: têm campo próprio (`current_outfit`) ou deixam de
# valer no turno seguinte. Guardadas como fato, contradiziam a roupa atual.
_TRANSIENT = re.compile(
    r"(?i)\b(?:is|are|was|were|'s)\s+(?:now\s+|still\s+|currently\s+)?"
    r"(?:wearing|dressed|walking|standing|sitting|seated|kneeling|lying|running|leaning|"
    r"crouching|looking|watching)\b"
    r"|\b(?:puts?|pulls?|slips?)\s+on\b|\b(?:takes?|took)\s+off\b"
    r"|\b(?:removes?|removed)\s+(?:his|her|their|the)\b"
    r"|\b(?:est[aá]|estava|est[aã]o|segue|continua)\s+(?:agora\s+|ainda\s+)?"
    r"(?:vestind[oa]|vestid[oa]|usando|andando|caminhando|sentad[oa]|de\s+p[eé]|ajoelhad[oa]|"
    r"deitad[oa]|correndo|olhando|observando|parad[oa])\b"
    r"|\b(?:veste|vestiu|vestem|colocou|coloca|tirou|tira)\s+(?:o|a|os|as|um|uma|seu|sua)\b")


def fold(text: str) -> str:
    """Minúsculas, sem acento, sem pontuação, espaços simples."""
    plain = unicodedata.normalize("NFKD", str(text or ""))
    plain = "".join(ch for ch in plain if not unicodedata.combining(ch)).casefold()
    return " ".join(_WORD.findall(plain))


def same_fact(left: str, right: str) -> bool:
    """A mesma frase, ou uma que só continua a outra ("…the path." e "…the path
    ahead."). Parecida não basta: "Bruno odeia Marina" e "Bruno ama Marina"
    diferem numa palavra e são fatos opostos."""
    a, b = fold(left), fold(right)
    if not a or not b:
        return False
    if a == b:
        return True
    short, long = (a, b) if len(a) <= len(b) else (b, a)
    if len(short.split()) < 3:
        return False
    return long.startswith(short + " ") or long.endswith(" " + short)


def sentences(text: str) -> list[str]:
    return [part.strip() for part in _SENTENCE.split(str(text or "")) if part.strip()]


def is_transient(text: str) -> bool:
    return _TRANSIENT.search(str(text or "")) is not None


def known(fact: str, texts) -> bool:
    return any(same_fact(fact, old) for old in texts)


def fit(text: str, limit: int, *, keep: str = "recent") -> str:
    """Corta em frases inteiras. `recent` guarda as últimas; `head` guarda o
    começo (o que o jogador escreveu) e completa com as mais recentes."""
    text = str(text or "").strip()
    if len(text) <= limit:
        return text
    parts = sentences(text)
    if len(parts) < 2:
        cut = text[:limit]
        space = cut.rfind(" ")
        return (cut[:space] if space > 0 else cut).rstrip(" ,;:")
    head: list[str] = []
    used = 0
    if keep == "head":
        for part in parts:
            if used + len(part) + 1 > limit // 2:
                break
            head.append(part)
            used += len(part) + 1
    tail: list[str] = []
    for part in reversed(parts[len(head):]):
        if used + len(part) + 1 > limit:
            break
        tail.append(part)
        used += len(part) + 1
    kept = [*head, *reversed(tail)]
    if not kept:
        cut = parts[-1][:limit]
        space = cut.rfind(" ")
        return (cut[:space] if space > 0 else cut).rstrip(" ,;:")
    return " ".join(kept)


def append(base: str, facts, limit: int, *, keep: str = "recent",
           skip_transient: bool = False) -> str:
    """Acrescenta só fato novo (nem repetido, nem quase igual a um que já está)."""
    out = str(base or "").strip()
    have = sentences(out)
    for fact in facts or []:
        fact = " ".join(str(fact).split())
        if not fact or (skip_transient and is_transient(fact)):
            continue
        if fold(fact) in fold(out) or known(fact, have):
            continue
        out = f"{out} {fact}".strip()
        have.append(fact)
    return fit(out, limit, keep=keep)
