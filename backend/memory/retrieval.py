"""Recuperação de memórias do memory bank (PLANO.md §4.3 & Milestone 2 R1 Fase 4).

Score de cada memória:
    Quando vetores estão disponíveis (query e memória válidos):
        score = cosine_similarity * 1000 + keyword_bonus + recency
    Fallback silencioso (sem vetores ou serviço indisponível):
        score = keyword_overlap * OVERLAP_WEIGHT + recency

    keyword_overlap = palavras significativas em comum com recent_text
                      (stopwords pt-BR e en, palavras com < 3 letras fora).
    keyword_bonus   = 50 pontos por palavra significativa coincidente + 50 pontos
                      adicionais se a palavra coincidente estiver capitalizada (nomes próprios).
    recency         = seq_end (NULL cai para o id).

Seleção: até top_k que caibam no budget, por ranking de score. A memória mais
recente entra sempre que top_k > 1 e ela cabe (troca o pior slot se preciso).
O retorno é CRONOLÓGICO (a história na ordem), não o fim primeiro.

Assinatura consumida por app.context_builder (import tardio):
    select_memories(conn, adventure_id, recent_text, budget_tokens=...) -> list[dict]
"""

from __future__ import annotations

import logging
import math
import re
import sqlite3
from typing import Any, Callable

try:
    from app import db, embeddings_client
except ImportError:  # pragma: no cover
    from backend.app import db, embeddings_client  # type: ignore

log = logging.getLogger("aidungeon.memory.retrieval")

DEFAULT_TOP_K = 12
DEFAULT_BUDGET = 800
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


def _keyword_bonus(memory_text: str, reference_words: set[str]) -> float:
    """Bônus de palavra-chave para nomes próprios e palavras exatas."""
    if not memory_text or not reference_words:
        return 0.0
    mem_words = significant_words(memory_text)
    matching = mem_words & reference_words
    if not matching:
        return 0.0
    bonus = len(matching) * 50.0
    # Bônus adicional para tokens capitalizados no texto original (nomes próprios)
    orig_tokens = set(_WORD_RE.findall(memory_text))
    for word in matching:
        if word.capitalize() in orig_tokens or word.upper() in orig_tokens:
            bonus += 50.0
    return bonus


def _get_valid_vector(mem: dict, target_model: str | None, target_dim: int | None) -> list[float] | None:
    """Retorna vetor válido se coincidir com o modelo e a dimensão ativos."""
    if not target_model or not target_dim:
        return None
    blob = mem.get("embedding")
    if not blob:
        return None
    model = mem.get("embedding_model")
    if model != target_model:
        return None
    dim = mem.get("embedding_dim")
    if dim is not None and dim != target_dim:
        return None
    return embeddings_client.unpack_vector(blob, expected_dim=target_dim)


def score_memory(
    memory: dict,
    reference_words: set[str],
    *,
    query_vec: list[float] | None = None,
    mem_vec: list[float] | None = None,
) -> float:
    """Scoring híbrido: cosine * 1000 + keyword_bonus + recency.

    Fallback automático: se query_vec ou mem_vec for None, pontua por keyword overlap + recency.
    """
    recency = _recency(memory)
    if query_vec is not None and mem_vec is not None:
        sim = embeddings_client.cosine_similarity(query_vec, mem_vec)
        if not math.isfinite(sim):
            sim = 0.0
        bonus = _keyword_bonus(memory.get("text") or "", reference_words)
        return sim * 1000.0 + bonus + recency
    overlap = len(significant_words(memory.get("text")) & reference_words)
    return overlap * OVERLAP_WEIGHT + recency


def _same_memory(left: dict, right: dict) -> bool:
    left_id = left.get("id")
    right_id = right.get("id")
    if left_id is not None and right_id is not None:
        return left_id == right_id
    return left is right


def select_memories(
    conn: sqlite3.Connection,
    adventure_id: str,
    recent_text: str = "",
    budget_tokens: int = DEFAULT_BUDGET,
    *,
    top_k: int | None = None,
    settings: dict | None = None,
    estimator: Estimator | None = None,
    query_vector: list[float] | None = None,
    embeddings_client_mod: Any = None,
    transport: Any = None,
) -> list[dict]:
    """Top-k por similaridade híbrida (vetor + palavras-chave + recência).

    Invariantes estritos:
    - Teto de orçamento de 800 tokens (budget_tokens).
    - top_k=12 por default.
    - Memória mais recente sempre inclusa se top_k > 1 e couber.
    - Fallback silencioso para overlap de palavras se o serviço de embeddings cair.
    - Vetores com modelo ou dimensão divergente são ignorados.
    - Retorno em ordem estritamente cronológica.
    """
    estimator = estimator or _default_estimator
    cfg = _memory_settings(conn, settings)
    if top_k is None:
        top_k = int(cfg.get("top_k", DEFAULT_TOP_K))
    if top_k <= 0 or budget_tokens <= 0:
        return []

    # Carrega memórias com os vetores BLOB para cálculo interno
    memories = db.list_memories(conn, adventure_id, include_vector=True)
    if not memories:
        return []

    emb_mod = embeddings_client_mod or embeddings_client
    emb_cfg = None
    if isinstance(settings, dict):
        if "embeddings" in settings:
            emb_cfg = settings["embeddings"]
        elif isinstance(settings.get("memory_bank"), dict) and "embeddings" in settings["memory_bank"]:
            emb_cfg = settings["memory_bank"]["embeddings"]
    if emb_cfg is None:
        try:
            from app import settings_store
            emb_cfg = settings_store.effective_embeddings_settings(conn)
        except Exception:
            emb_cfg = {}

    target_model = emb_cfg.get("model", "Qwen3-Embedding-0.6B") if emb_cfg.get("enabled", True) else None

    # Obter vetor de consulta
    query_vec = query_vector
    clean_query = (recent_text or "").strip()
    if query_vec is None and target_model and clean_query:
        query_vec = emb_mod.get_embedding(clean_query, cfg=emb_cfg, transport=transport)

    target_dim = len(query_vec) if query_vec is not None else None
    reference = significant_words(recent_text)

    # Calcular score de cada memória
    scores: dict[int, float] = {}
    for mem in memories:
        mem_id = mem.get("id") or 0
        valid_vec = _get_valid_vector(mem, target_model, target_dim)
        scores[mem_id] = score_memory(
            mem, reference, query_vec=query_vec, mem_vec=valid_vec
        )

    newest = max(memories, key=lambda m: (_recency(m), m.get("id") or 0))
    ranked = sorted(
        memories,
        key=lambda m: (scores[m.get("id") or 0], _recency(m), m.get("id") or 0),
        reverse=True,
    )

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

    # Invariante: a mais recente entra sempre que top_k > 1 e cabe
    if top_k > 1 and picked and not any(_same_memory(item, newest) for item in picked):
        cost = estimator(newest["text"] or "")
        victim = min(
            picked,
            key=lambda m: (scores[m.get("id") or 0], _recency(m), m.get("id") or 0),
        )
        victim_cost = estimator(victim["text"] or "")
        if used - victim_cost + cost <= budget_tokens:
            picked = [item for item in picked if not _same_memory(item, victim)]
            picked.append(newest)

    picked.sort(key=lambda m: (_recency(m), m.get("id") or 0))

    # Limpar bytes brutos do embedding antes de devolver
    clean_picked = []
    for m in picked:
        clean = dict(m)
        clean.pop("embedding", None)
        clean_picked.append(clean)
    return clean_picked
