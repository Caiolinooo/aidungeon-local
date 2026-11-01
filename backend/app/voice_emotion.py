"""Emoção da fala, atribuída ao NPC ou escolhida da linha quando não há NPC.

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
        "raiva", "anger", "angry", "rage", "furious", "fury", "furios*",
        "irritad*", "irritat*", "grita*", "bravo", "brava",
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


# Casos que o vocabulário simples não cobre: alívio, luto, ansiedade e afeto.
_SPECIAL_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("joy", re.compile(
        r"(?i)(?:lágrimas?\s+de\s+(?:alegria|felicidade)|tears?\s+of\s+joy|"
        r"alívio|aliviad|relief|relieved|felicidade)")),
    ("sadness", re.compile(
        r"(?i)(?:\bluto\b|\bpesar\b|grief|griev|remors|sorry|"
        r"pede\s+desculpas|desculpa-se|arrepend)")),
    ("fear", re.compile(
        r"(?i)(?:ansiedade|ansios|nervos|nervous|preocup|"
        r"anxious|anxiety|worried|worry)")),
    ("tenderness", re.compile(
        r"(?i)(?:\bgost(?:a|o|ava|ou)\b|\blikes\b|\bliked\b|"
        r"\blike\s+(?:you|him|her|them)\b|\bafeto\b|affection|"
        r"paixão|apaixon)")),
)

_EXPRESSION_NEEDLES = {"sorri*", "smile*", "laugh*"}
_NEGATION_BEFORE = re.compile(
    r"(?i)(?:\b(?:not|no|never|without|nao|não|nunca|nem|sem)\b|n't)"
    r"\s*(?:[\wÀ-ÿ'-]+\s*){0,2}$"
)


def _needle_pattern(needle: str) -> re.Pattern[str]:
    prefix = needle.endswith("*")
    word = needle[:-1] if prefix else needle
    suffix = "" if prefix else r"(?!\w)"
    return re.compile(rf"(?i)(?<!\w){re.escape(word)}{suffix}")


def _negated_at(text: str, start: int) -> bool:
    prefix = text[:start]
    boundary = list(re.finditer(r"(?i)\b(?:only|só)\b", prefix))
    if boundary:
        prefix = prefix[boundary[-1].end():]
    return _NEGATION_BEFORE.search(prefix) is not None


def _first_unnegated(text: str, pattern: re.Pattern[str]) -> bool:
    return any(not _negated_at(text, match.start()) for match in pattern.finditer(text))


def emotion_is_negated(text: str, emotion: str) -> bool:
    """True quando uma ocorrência dessa emoção está negada localmente."""
    for candidate, pattern in _SPECIAL_PATTERNS:
        if candidate == emotion and any(
            _negated_at(text, match.start()) for match in pattern.finditer(text)
        ):
            return True
    for candidate, needles in _KEYWORDS:
        if candidate != emotion:
            continue
        for needle in needles:
            pattern = _needle_pattern(needle)
            if any(_negated_at(text, match.start()) for match in pattern.finditer(text)):
                return True
    return False


def _emotion_in_text(text: str, *, include_expression: bool = True) -> str | None:
    for emotion, pattern in _SPECIAL_PATTERNS:
        if _first_unnegated(text, pattern):
            return emotion
    for emotion, needles in _KEYWORDS:
        for needle in needles:
            if not include_expression and needle in _EXPRESSION_NEEDLES:
                continue
            if _first_unnegated(text, _needle_pattern(needle)):
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


_SUBJECT_SPLIT = re.compile(r"(?i)\b(mas|porém|contudo|however|but|and|e)\b")
_PRONOUN_START = re.compile(r"(?i)\s*(?:she|he|they|ela|ele)\b")
_PREDICATE = re.compile(
    r"(?i)\b(?:is|are|was|were|feels?|has|had|likes?|looks?|seems?|"
    r"está|estao|estão|estava|sente|tem|fica|ficam|gosta|sorri|parece)\b"
)
_CAPITALIZED = re.compile(r"(?<!\w)[A-ZÀ-Ý][\wÀ-ÿ'-]*")
_NAME_STOPWORDS = {
    "A", "An", "And", "But", "El", "Ela", "Ele", "E", "He", "I", "Mas",
    "She", "The", "They", "You",
}


def _alias_patterns(subjects: dict[str, list[str]]) -> list[tuple[str, re.Pattern[str]]]:
    owners: dict[str, str] = {}
    ambiguous: set[str] = set()
    for subject, aliases in subjects.items():
        for alias in aliases:
            folded = " ".join(str(alias or "").casefold().split())
            if len(folded) < 2:
                continue
            if folded in owners and owners[folded] != subject:
                ambiguous.add(folded)
            else:
                owners[folded] = subject
    patterns = []
    for alias, subject in owners.items():
        if alias in ambiguous:
            continue
        body = r"\s+".join(re.escape(part) for part in alias.split())
        patterns.append((subject, re.compile(rf"(?i)(?<!\w){body}(?!\w)")))
    return patterns


def _segments(sentence: str):
    start = 0
    connector = ""
    for match in _SUBJECT_SPLIT.finditer(sentence):
        yield connector, sentence[start:match.start()]
        connector = match.group(1).casefold()
        start = match.end()
    yield connector, sentence[start:]


def _mentions(clause: str, patterns: list[tuple[str, re.Pattern[str]]]):
    mentioned: set[str] = set()
    subjects: set[str] = set()
    spans: list[tuple[int, int]] = []
    for subject, pattern in patterns:
        for match in pattern.finditer(clause):
            mentioned.add(subject)
            spans.append(match.span())
            before = clause[:match.start()].strip()
            after = clause[match.end():]
            if not before or re.match(r"(?i)\s*,?\s*(?:who|que)\b", after):
                subjects.add(subject)
    unknown = False
    for match in _CAPITALIZED.finditer(clause):
        if match.group(0) in _NAME_STOPWORDS:
            continue
        if not any(start <= match.start() < end for start, end in spans):
            unknown = True
            break
    return mentioned, subjects, unknown


def emotions_for_subjects(text: str, subjects: dict[str, list[str]]) -> dict[str, str]:
    """Última emoção atribuída a cada sujeito, numa única varredura."""
    patterns = _alias_patterns(subjects)
    found: dict[str, str] = {}
    antecedent: set[str] = set()
    for sentence in re.split(r"(?<=[.!?…])\s+", text or ""):
        current: set[str] = set()
        previous_clause = ""
        for connector, clause in _segments(sentence):
            mentioned, named_subjects, unknown = _mentions(clause, patterns)
            pronominal = _PRONOUN_START.match(clause) is not None
            if mentioned:
                previous_current = set(current)
                previous_only = any(
                    pattern.fullmatch(previous_clause.strip())
                    for _subject, pattern in patterns
                ) if connector in {"and", "e"} and current else False
                shared_predicate = previous_only
                compound_antecedent = shared_predicate or (
                    connector in {"and", "e"} and current and not _PREDICATE.search(clause)
                )
                if shared_predicate:
                    current = current | mentioned
                    named_subjects = named_subjects | current
                else:
                    current = mentioned
                antecedent = set() if unknown else (
                    set(previous_current | mentioned) if compound_antecedent else set(current)
                )
            candidates = named_subjects
            if not candidates and pronominal and len(antecedent) == 1:
                candidates = set(antecedent)
                current = set(antecedent)
            elif not candidates and connector and current and not unknown:
                candidates = set(current)
            emotion = emotion_from_text(clause)
            if emotion and candidates:
                explicit = _emotion_in_text(clause, include_expression=False)
                for subject in candidates:
                    # Sorriso/riso coordenado não apaga emoção explícita anterior.
                    if subject in found and found[subject] != emotion and not explicit:
                        continue
                    found[subject] = emotion
            if unknown or (not mentioned and not pronominal and not connector):
                current = set()
                antecedent = set()
            previous_clause = clause
    return found


def choose_emotion(text: str, *, npc_emotion: str | None = None,
                   hint: str | None = None, prefer_npc: bool = False) -> str:
    """NPC identificado pode vencer varredura global; sem ele, linha → ficha → dica."""
    npc = canonical_emotion(npc_emotion)
    if prefer_npc and npc:
        return npc
    found = emotion_from_text(text)
    if found:
        return found
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
