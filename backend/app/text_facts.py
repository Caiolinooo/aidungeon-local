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
_GARMENT = (
    r"(?:coat|jacket|hat|cap|hood|cloak|cape|shirt|blouse|dress|gown|robe|tunic|trousers|pants|"
    r"jeans|skirt|boots?|shoes?|gloves?|scarf|mask|helmet|armou?r|uniform|clothes|clothing|"
    r"raincoat|jumpsuit|sweater|vest|casaco|jaqueta|chap[eé]u|capuz|capa|camisa|blusa|vestido|"
    r"t[uú]nica|cal[cç]as?|saia|botas?|sapatos?|luvas?|cachecol|m[aá]scara|elmo|armadura|"
    r"uniforme|roupas?|macac[aã]o|manto)")
_TRANSIENT = re.compile(
    r"(?i)\b(?:is|are|was|were|'s)\s+(?:now\s+|still\s+|currently\s+)?"
    r"(?:wearing|dressed|walking|standing|sitting|seated|kneeling|lying|running|leaning|"
    r"crouching|looking|watching)\b"
    r"|\b(?:est[aá]|estava|est[aã]o|segue|continua)\s+(?:agora\s+|ainda\s+)?"
    r"(?:vestind[oa]|vestid[oa]|usando|andando|caminhando|sentad[oa]|de\s+p[eé]|ajoelhad[oa]|"
    r"deitad[oa]|correndo|olhando|observando|parad[oa])\b"
    # Tirar e pôr só é roupa com a peça na frase: "He removed the curse" e
    # "Ele tirou a vida do rei" são fatos que ficam.
    r"|\b(?:puts?|pulls?|slips?)\s+on\b[^.!?]{0,40}\b" + _GARMENT + r"\b"
    r"|\b(?:takes?|took)\s+off\b[^.!?]{0,40}\b" + _GARMENT + r"\b"
    r"|\b(?:removes?|removed)\s+(?:his|her|their|the)\s+(?:[\w-]+\s+){0,3}?" + _GARMENT + r"\b"
    r"|\b(?:veste|vestiu|vestem|colocou|coloca|tirou|tira)\s+(?:o|a|os|as|um|uma|seu|sua)\s+"
    r"(?:[\wÀ-ÿ-]+\s+){0,3}?" + _GARMENT + r"\b")


def fold(text: str) -> str:
    """Minúsculas, sem acento, sem pontuação, espaços simples."""
    plain = unicodedata.normalize("NFKD", str(text or ""))
    plain = "".join(ch for ch in plain if not unicodedata.combining(ch)).casefold()
    return " ".join(_WORD.findall(plain))


_REVERSAL = re.compile(
    r"\b(?:no|not|never|longer|anymore|more|but|except|unless|until|without|nor|only|"
    r"nao|nunca|mais|mas|exceto|ate|sem|nem|so|apenas|porem)\b")


def same_fact(left: str, right: str) -> bool:
    """A mesma frase, ou uma que só acrescenta até três palavras ("…the path."
    e "…the path ahead."). Parecida não basta ("Bruno odeia Marina" e "Bruno
    ama Marina"), e a continuação não pode negar nem ressalvar: "He can swim"
    e "He can swim no longer" são fatos opostos."""
    a, b = fold(left), fold(right)
    if not a or not b:
        return False
    if a == b:
        return True
    short, long = (a, b) if len(a) <= len(b) else (b, a)
    if len(short.split()) < 3:
        return False
    if long.startswith(short + " "):
        extra = long[len(short):].strip()
    elif long.endswith(" " + short):
        extra = long[:-len(short)].strip()
    else:
        return False
    return len(extra.split()) <= 3 and not _REVERSAL.search(extra)


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
            # A primeira frase vale até o limite inteiro: com a metade, um
            # card de uma frase longa perdia o texto do jogador. Maior que o
            # limite, fica cortada: nunca é trocada por um fato novo.
            if not head and len(part) >= limit:
                cut = part[:limit]
                space = cut.rfind(" ")
                return (cut[:space] if space > 0 else cut).rstrip(" ,;:")
            room = limit if not head else limit // 2
            if used + len(part) + (1 if head else 0) > room:
                break
            head.append(part)
            used += len(part) + (1 if len(head) > 1 else 0)
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
