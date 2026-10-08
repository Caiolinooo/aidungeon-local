"""Recuperação de memórias do memory bank (PLANO.md §4.3 — v1 sem embeddings).

Score de cada memória:
    score = recency + keyword_overlap
    recency         = seq_end da memória (mais alto = mais recente; pesa mais —
                      escala de seq domina a contagem de palavras); seq_end NULL
                      (ex.: importadas) cai para o id (ordem de inserção).
    keyword_overlap = contagem de palavras significativas em comum entre o texto
                      da memória e recent_text (stopwords EN curtas e palavras
                      com < 3 letras ignoradas).

Seleção: ranqueia por score (desempate: mais recente), pega até top_k que
caibam em budget_tokens, e devolve MAIS RECENTES PRIMEIRO.

Assinatura consumida por app.context_builder (import tardio):
    select_memories(conn, adventure_id, budget_tokens=...) -> list[dict]
`recent_text` é opcional (o caller atual não o passa → overlap=0 → recency
pura). `top_k`/`settings`/`estimator` são injetáveis; omitidos vêm de
settings.memory_bank.top_k (default 5) e chars/4.

Nota: o retorno é list[dict] (db.memory_to_dict) — o context_builder consome
m["text"] e m.get("id"). Embeddings: fase 4 do plano (coluna já existe).
"""

from __future__ import annotations

import re
import sqlite3
from typing import Callable

try:
    from app import db
except ImportError:  # pragma: no cover
    from backend.app import db  # type: ignore

DEFAULT_TOP_K = 5
DEFAULT_BUDGET = 600

Estimator = Callable[[str], int]

# Stopwords EN curtas — não contam como overlap de keyword.
STOPWORDS = frozenset("""
a an the and or but if then else of at by for with about into through during
before after above below to from up down in out on off over under again once
here there when where why how all any both each few more most other some such
no nor not only own same so than too very can will just should now is are was
were be been being have has had having do does did doing would could ought
i you he she it we they them his her their our your its my me him us this that
these those am as
""".split())

_WORD_RE = re.compile(r"[^\W_]+")  # palavras unicode (letras/acentos/dígitos)


def _default_estimator(text: str) -> int:
    return (len(text) + 3) // 4


def significant_words(text: str | None) -> set[str]:
    """Palavras significativas (casefold, ≥3 letras, fora das stopwords)."""
    return {w for w in _WORD_RE.findall((text or "").casefold())
            if len(w) >= 3 and w not in STOPWORDS}


def _memory_settings(conn: sqlite3.Connection, settings: dict | None) -> dict:
    """Aceita o bloco memory_bank, o runtime_config completo ou None (carrega)."""
    if isinstance(settings, dict):
        if isinstance(settings.get("memory_bank"), dict):
            return settings["memory_bank"]
        return settings
    try:
        from app import settings_store
    except ImportError:  # pragma: no cover
        from backend.app import settings_store  # type: ignore
    try:
        return settings_store.runtime_config(conn).get("memory_bank", {})
    except Exception:
        return {}


def _recency(memory: dict) -> float:
    """seq_end manda; NULL (importadas antigas) → id (ordem de inserção)."""
    seq_end = memory.get("seq_end")
    return float(seq_end if seq_end is not None else memory.get("id") or 0)


def score_memory(memory: dict, reference_words: set[str]) -> float:
    """recency (pesa mais) + keyword overlap com o texto recente."""
    overlap = len(significant_words(memory.get("text")) & reference_words)
    return _recency(memory) + overlap


def select_memories(conn: sqlite3.Connection, adventure_id: str,
                    recent_text: str = "", budget_tokens: int = DEFAULT_BUDGET, *,
                    top_k: int | None = None,
                    settings: dict | None = None,
                    estimator: Estimator | None = None) -> list[dict]:
    """Top-k memórias por relevância que caibam no budget; mais recentes 1º."""
    estimator = estimator or _default_estimator
    cfg = _memory_settings(conn, settings)
    if top_k is None:
        top_k = int(cfg.get("top_k", DEFAULT_TOP_K))
    if top_k <= 0 or budget_tokens <= 0:
        return []

    memories = db.list_memories(conn, adventure_id)
    if not memories:
        return []

    reference = significant_words(recent_text)
    ranked = sorted(memories,
                    key=lambda m: (score_memory(m, reference), _recency(m),
                                   m.get("id") or 0),
                    reverse=True)

    picked: list[dict] = []
    used = 0
    for mem in ranked:
        if len(picked) >= top_k:
            break
        cost = estimator(mem["text"] or "")
        if used + cost > budget_tokens:
            continue  # grande demais p/ o restante — tenta as próximas
        picked.append(mem)
        used += cost

    # apresentação: mais recentes primeiro
    picked.sort(key=lambda m: (_recency(m), m.get("id") or 0), reverse=True)
    return picked
