"""Emoção da fala, escolhida da linha e, se a linha não disser, do NPC.

O servidor de voz de hoje (Supertonic 3) não atua. A direção em colchetes
é o formato do Drama-3 (Fish Audio): um recado curto antes das palavras,
do jeito que se dirige um ator. Enquanto `drama3_enabled` estiver falso,
essa frase vai só no campo `instructions` — o texto falado continua limpo,
para o Supertonic não ler a etiqueta em voz alta.
"""

from __future__ import annotations

import re

# Emoções que a API aceita. O avatar 3D usa outro vocabulário (happy/love);
# os apelidos abaixo só entram como dica do cliente.
EMOTIONS = (
    "joy", "anger", "fear", "sadness", "tenderness",
    "surprise", "disgust", "neutral",
)

_ALIASES = {
    "happy": "joy",
    "sad": "sadness",
    "angry": "anger",
    "love": "tenderness",
}

# Agulha com "*" no fim casa prefixo (flexão). Sem "*", palavra inteira.
_KEYWORDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("fear", (
        "medo", "fear", "afraid", "scared", "pavor", "terror",
        "assust*", "aterroriz*", "tremend*",
    )),
    ("anger", (
        "raiva", "anger", "angry", "rage", "furios*", "irritad*",
        "grita*", "bravo", "brava",
    )),
    ("sadness", (
        "triste*", "sad", "sadness", "chora*", "cry", "cries", "crying",
        "lágrima*", "lagrima*", "melancol*",
    )),
    ("disgust", ("nojo", "disgust*", "repuls*", "revolt*")),
    ("surprise", ("surpres*", "surprise*", "startl*", "ofeg*", "gasp*")),
    ("tenderness", (
        "amor", "love", "tender*", "carinh*", "beij*", "kiss*",
        "abraç*", "abrac*", "gentil*",
    )),
    ("joy", (
        "feliz", "happy", "alegre*", "alegria", "joy", "contente*",
        "sorri*", "smile*", "laugh*", "rindo",
    )),
)

# Direção em linguagem comum, como a doc do Drama-3 pede nos colchetes.
DIRECTIONS = {
    "joy": "[warm and joyful, a real smile in the voice]",
    "anger": "[angry, clipped, holding back a shout]",
    "fear": "[frightened, breath short, voice unsteady]",
    "sadness": "[sad, quiet, close to tears]",
    "tenderness": "[tender, soft, close to the listener]",
    "surprise": "[startled, a sudden lift in the voice]",
    "disgust": "[disgusted, pulling away from the words]",
    "neutral": "[calm and even]",
}


def canonical_emotion(value: str | None) -> str | None:
    """Normaliza uma dica. None se não for emoção conhecida."""
    if not isinstance(value, str):
        return None
    key = value.strip().casefold()
    key = _ALIASES.get(key, key)
    if key in EMOTIONS:
        return key
    return None


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


# Casos que o vocabulário simples não cobre: alívio, luto, ansiedade e afeto.
_SPECIAL_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("joy", re.compile(
        r"(?i)(?:lágrimas?\s+de\s+(?:alegria|felicidade)|tears?\s+of\s+joy|"
        r"alívio|aliviad|relief|relieved|felicidade)")),
    ("sadness", re.compile(r"(?i)(?:\bluto\b|\bpesar\b|grief|desculp|sorry)")),
    ("fear", re.compile(r"(?i)(?:ansiedade|ansios|nervos|preocup|anxious|anxiety)")),
    ("tenderness", re.compile(r"(?i)(?:\bgosta\b|\bafeto\b|paixão|apaixon)")),
)

_NEGATED_EMOTIONS = {
    "joy": r"(?:alegria|felicidade|feliz|happy|joy)",
    "anger": r"(?:raiva|anger|angry)",
    "fear": r"(?:medo|fear|afraid|anxiety|ansiedade|preocup)",
    "sadness": r"(?:tristeza|sadness|sad)",
    "tenderness": r"(?:amor|love|gosta)",
    "surprise": r"(?:surpresa|surprise)",
    "disgust": r"(?:nojo|disgust)",
}


def _negated(text: str, emotion: str) -> bool:
    needle = _NEGATED_EMOTIONS[emotion]
    return re.search(
        rf"(?i)\b(?:not|no|never|nao|não|nunca|nem|sem)\s+"
        rf"(?:\w+\s+){{0,2}}{needle}",
        text,
    ) is not None


def _emotion_in_text(text: str, *, include_expression: bool = True) -> str | None:
    for emotion, pattern in _SPECIAL_PATTERNS:
        if pattern.search(text) and not _negated(text, emotion):
            return emotion
    for emotion, needles in _KEYWORDS:
        if _negated(text, emotion):
            continue
        if not include_expression and emotion == "joy":
            needles = tuple(needle for needle in needles
                            if needle not in {"sorri*", "smile*", "laugh*"})
        if any(_hit(text, needle) for needle in needles):
            return emotion
    return None


def emotion_from_text(text: str) -> str | None:
    """Emoção explícita na frase, com contraste e negação por emoção."""
    folded = (text or "").casefold()
    if not folded.strip():
        return None
    contrast = re.split(r"(?i)\b(?:mas|porém|contudo|however|but)\b", folded)
    if len(contrast) > 1:
        # Um sorriso após "mas" pode esconder a emoção anterior; outra emoção
        # declarada ("feliz, mas nervosa") muda a dominante.
        found = _emotion_in_text(contrast[-1], include_expression=False)
        if found:
            return found
    return _emotion_in_text(folded)


def emotion_near_name(text: str, name: str) -> str | None:
    """Última emoção da oração que contém a pessoa, nunca da vizinha."""
    result = None
    for sentence in re.split(r"(?<=[.!?…])\s+", text or ""):
        clauses = re.split(r"(?i)\b(?:mas|porém|contudo|however|but)\b|;", sentence)
        for clause in clauses:
            if re.search(rf"(?<!\w){re.escape(name)}(?!\w)", clause, re.IGNORECASE):
                found = emotion_from_text(clause)
                if found:
                    result = found
    return result


def choose_emotion(text: str, *, npc_emotion: str | None = None,
                   hint: str | None = None) -> str:
    """A linha vence. Sem palavra de emoção, vale o estado do NPC. Senão a dica."""
    found = emotion_from_text(text)
    if found:
        return found
    npc = canonical_emotion(npc_emotion)
    if npc and npc != "neutral":
        return npc
    client = canonical_emotion(hint)
    if client:
        return client
    return "neutral"


def direction_for(emotion: str | None) -> str:
    """Colchetes de direção para o Drama-3. Emoção desconhecida vira tom calmo."""
    key = canonical_emotion(emotion) or "neutral"
    return DIRECTIONS[key]
