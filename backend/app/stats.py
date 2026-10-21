"""Atributos de personagem: point-buy, sorteio e estimativa.

Módulo puro. Não importa o app, não lê banco e não chama LLM.
"""

from __future__ import annotations

import random
import re
import unicodedata

ABILITIES = ("str", "dex", "con", "int", "wis", "cha")

ABILITY_LABELS = {
    "str": "Força",
    "dex": "Destreza",
    "con": "Constituição",
    "int": "Inteligência",
    "wis": "Sabedoria",
    "cha": "Carisma",
}

STAT_MIN = 1
STAT_MAX = 20
POINT_BUY_BUDGET = 27
POINT_BUY_MIN = 8
POINT_BUY_MAX = 15
# Tabela padrão 5e. 14 e 15 custam mais que o passo de 1.
POINT_BUY_COSTS = {8: 0, 9: 1, 10: 2, 11: 3, 12: 4, 13: 5, 14: 7, 15: 9}

# Palavra (PT/EN) → rótulo da classe e quanto sobe acima da base 10.
# A ordem é a do contrato: o primeiro grupo que casar nomeia a classe.
_ARCHETYPES: tuple[tuple[str, tuple[str, ...], dict[str, int]], ...] = (
    ("Guerreiro", ("guerreiro", "knight", "guarda", "paladin", "paladino", "warrior"),
     {"str": 4, "con": 3}),
    ("Ladino", ("assassino", "ladino", "thief", "ranger", "arqueiro"),
     {"dex": 4}),
    ("Mago", ("mago", "wizard", "scholar", "erudito"),
     {"int": 4}),
    ("Clérigo", ("druida", "clerigo", "priest", "healer"),
     {"wis": 4}),
    ("Bardo", ("bardo", "charmer", "diplomata", "rei", "rainha"),
     {"cha": 4}),
)


def _fold(text: str) -> str:
    """Minúsculas sem acento, para casar 'clérigo' com 'clerigo'."""
    decomposed = unicodedata.normalize("NFKD", text or "")
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch)).lower()


def _clamp(value: int) -> int:
    return max(STAT_MIN, min(STAT_MAX, value))


def _as_stat(value) -> int | None:
    """Inteiro 1..20, ou None quando o valor não é numérico (bool não conta)."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return _clamp(value)
    if isinstance(value, float):
        if value != value:  # NaN
            return None
        return _clamp(int(value))
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            return _clamp(int(text))
        except ValueError:
            return None
    return None


def point_buy_cost(value: int) -> int:
    """Custo padrão 5e dentro de 8..15. Fora da faixa o custo é 0."""
    try:
        number = int(value)
    except (TypeError, ValueError):
        return 0
    if isinstance(value, bool) or not POINT_BUY_MIN <= number <= POINT_BUY_MAX:
        return 0
    return POINT_BUY_COSTS.get(number, 0)


def point_buy_total(stats: dict) -> int:
    """Soma o custo point-buy das seis habilidades presentes."""
    if not isinstance(stats, dict):
        return 0
    total = 0
    for key in ABILITIES:
        if key not in stats or isinstance(stats[key], bool):
            continue
        total += point_buy_cost(stats[key])
    return total


def stat_modifier(value: int) -> int:
    """Modificador de atributo: (valor - 10) // 2."""
    try:
        number = int(value)
    except (TypeError, ValueError):
        number = 10
    return (number - 10) // 2


def has_numeric_ability(raw) -> bool:
    """True se ao menos uma das seis habilidades veio como número."""
    if not isinstance(raw, dict):
        return False
    return any(key in raw and _as_stat(raw[key]) is not None for key in ABILITIES)


def normalize_stats(raw, default: int = 10) -> dict:
    """Sempre as 6 habilidades como int 1..20. Extra é copiado como veio.

    Valor ausente ou não numérico vira `default` (também limitado a 1..20).
    """
    source = raw if isinstance(raw, dict) else {}
    base = _clamp(default if isinstance(default, int) and not isinstance(default, bool) else 10)
    out: dict = {}
    for key in ABILITIES:
        if key in source:
            parsed = _as_stat(source[key])
            out[key] = base if parsed is None else parsed
        else:
            out[key] = base
    for key, value in source.items():
        if key not in ABILITIES:
            out[key] = value
    return out


def _matching_groups(text: str) -> list[tuple[str, dict[str, int]]]:
    folded = _fold(text)
    found: list[tuple[str, dict[str, int]]] = []
    for label, words, boosts in _ARCHETYPES:
        if any(re.search(rf"\b{re.escape(_fold(word))}\b", folded) for word in words):
            found.append((label, boosts))
    return found


def class_from_text(text: str) -> str:
    """Rótulo da primeira classe reconhecida no texto. Vazio se nenhuma."""
    groups = _matching_groups(text)
    return groups[0][0] if groups else ""


def estimate_stats(text: str = "", class_role: str = "") -> dict:
    """Base 10. A classe/texto sobe o atributo do arquétipo, sempre em 1..20."""
    stats = {key: 10 for key in ABILITIES}
    for _label, boosts in _matching_groups(f"{class_role} {text}"):
        for key, delta in boosts.items():
            stats[key] = _clamp(stats[key] + delta)
    return stats


def roll_stats(rng=None) -> dict:
    """4d6 descartando o menor, por atributo. Resultado fica entre 3 e 18."""
    roller = rng.randint if rng is not None else random.randint
    stats: dict[str, int] = {}
    for key in ABILITIES:
        dice = sorted(roller(1, 6) for _ in range(4))
        stats[key] = sum(dice[1:])
    return stats


def stats_block(stats: dict) -> str:
    """Linha de atributos para o narrador: 'Força 12 (+1), Destreza 15 (+2)'."""
    clean = normalize_stats(stats)
    parts = []
    for key in ABILITIES:
        value = clean[key]
        modifier = stat_modifier(value)
        signed = f"+{modifier}" if modifier >= 0 else str(modifier)
        parts.append(f"{ABILITY_LABELS[key]} {value} ({signed})")
    return ", ".join(parts)
