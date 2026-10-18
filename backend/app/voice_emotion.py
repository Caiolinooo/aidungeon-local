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


def emotion_from_text(text: str) -> str | None:
    """Emoção explícita na frase, ou None se a frase não declara nenhuma."""
    folded = (text or "").casefold()
    if not folded.strip():
        return None
    for emotion, needles in _KEYWORDS:
        if any(_hit(folded, needle) for needle in needles):
            return emotion
    return None


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
