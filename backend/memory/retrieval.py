"""Recuperação de memórias do memory bank (PLANO.md §4.3 — v1 sem embeddings).

Score de cada memória:
    score = keyword_overlap * OVERLAP_WEIGHT + recency
    keyword_overlap = palavras significativas em comum com recent_text
                      (stopwords pt-BR e en, palavras com < 3 letras fora).
    recency         = seq_end (NULL cai para o id). Só desempata: um fato
                      antigo que casa com a cena vence um resumo recente vazio.

Seleção: até top_k que caibam no budget, overlap primeiro. A memória mais
recente entra sempre que top_k > 1 e ela cabe (troca o pior slot se preciso).
O retorno é CRONOLÓGICO (a história na ordem), não o fim primeiro.

Assinatura consumida por app.context_builder (import tardio):
    select_memories(conn, adventure_id, recent_text, budget_tokens=...) -> list[dict]
`top_k`/`settings`/`estimator` são injetáveis; omitidos vêm de
settings.memory_bank.top_k (default 12) e chars/4.
"""

from __future__ import annotations

import re
import sqlite3
from typing import Callable

try:
    from app import db
except ImportError:  # pragma: no cover
    from backend.app import db  # type: ignore

DEFAULT_TOP_K = 12
DEFAULT_BUDGET = 600
# Uma palavra em comum vale mais que centenas de ações de distância.
OVERLAP_WEIGHT = 1000

Estimator = Callable[[str], int]

# Stopwords pt-BR e en — não contam como overlap de keyword.
STOPWORDS = frozenset("""
a an the and or but if then else of at by for with about into through during
before after above below to from up down in out on off over under again once
here there when where why how all any both each few more most other some such
no nor not only own same so than too very can will just should now is are was
were be been being have has had having do does did doing would could ought
i you he she it we they them his her their our your its my me him us this that
these those am as
o os um uma uns umas de da do das dos em no na nos nas por para com sem sob
que e ou mas se como quando onde porque porquê lhe lhes isso isto esta este
essa esse aqui ali ja já nao não sim mais muito pouco bem mal tambem também
voce você ele ela eles elas seu sua seus suas meu minha te ti eu nos nós
ao aos à às dum duma num numa pelo pela pelos pelas
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
    """Overlap pesa; recência só desempata."""
    overlap = len(significant_words(memory.get("text")) & reference_words)
    return overlap * OVERLAP_WEIGHT + _recency(memory)


def _same_memory(left: dict, right: dict) -> bool:
    left_id = left.get("id")
    right_id = right.get("id")
    if left_id is not None and right_id is not None:
        return left_id == right_id
    return left is right


def select_memories(conn: sqlite3.Connection, adventure_id: str,
                    recent_text: str = "", budget_tokens: int = DEFAULT_BUDGET, *,
                    top_k: int | None = None,
                    settings: dict | None = None,
                    estimator: Estimator | None = None) -> list[dict]:
    """Top-k por overlap que caibam no budget, em ordem cronológica.

    A memória mais recente ocupa um slot quando top_k > 1 e ela cabe, mesmo
    que o overlap esteja nos fatos antigos.
    """
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
    newest = max(memories, key=lambda m: (_recency(m), m.get("id") or 0))
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

    if (top_k > 1 and picked
            and not any(_same_memory(item, newest) for item in picked)):
        cost = estimator(newest["text"] or "")
        victim = min(picked, key=lambda m: (
            score_memory(m, reference), _recency(m), m.get("id") or 0))
        victim_cost = estimator(victim["text"] or "")
        if used - victim_cost + cost <= budget_tokens:
            picked = [item for item in picked if not _same_memory(item, victim)]
            picked.append(newest)

    picked.sort(key=lambda m: (_recency(m), m.get("id") or 0))
    return picked
