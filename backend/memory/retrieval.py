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

Vetor da consulta: o embedding roda antes do narrador, então conta no TTFT.
Na CPU, com o narrador ocupado, 100 tokens levam ~2 s e os 7500 caracteres
da varredura passam de 30 s. Por isso:
    - o pós-turno grava o vetor da cena (`prime_scene_vector`) e o turno
      seguinte usa esse vetor; a ação nova entra pelas palavras-chave;
    - sem vetor gravado, o turno embute só o fim do texto
      (`memory_bank.query_chars`) com prazo curto (`memory_bank.query_timeout_s`).

Assinatura consumida por app.context_builder (import tardio):
    select_memories(conn, adventure_id, recent_text, budget_tokens=...) -> list[dict]
"""

from __future__ import annotations

import logging
import math
import re
import sqlite3
import threading
from typing import Any, Callable

try:
    from app import db, embeddings_client
except ImportError:  # pragma: no cover
    from backend.app import db, embeddings_client  # type: ignore

log = logging.getLogger("aidungeon.memory.retrieval")

DEFAULT_TOP_K = 12
DEFAULT_BUDGET = 800
DEFAULT_QUERY_CHARS = 1500
DEFAULT_QUERY_TIMEOUT_S = 1.5
# Ações gravadas entre o vetor da cena e o turno que o usa: a do jogador e
# alguns See. Desfazer (seq menor) ou mais que isso pede vetor novo.
SCENE_VECTOR_MAX_LAG = 3
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


def _embeddings_settings(conn: sqlite3.Connection, settings: dict | None) -> dict:
    """Bloco embeddings do settings injetado ou, sem ele, do banco."""
    if isinstance(settings, dict):
        if "embeddings" in settings:
            return settings["embeddings"]
        bank = settings.get("memory_bank")
        if isinstance(bank, dict) and "embeddings" in bank:
            return bank["embeddings"]
    try:
        from app import settings_store
        return settings_store.effective_embeddings_settings(conn)
    except Exception:
        return {}


# --------------------------------------------------------- vetor da cena
_scene_guard = threading.Lock()
_scene_vectors: dict[str, tuple[int, str, list[float]]] = {}


def query_tail(text: str | None, max_chars: int = DEFAULT_QUERY_CHARS) -> str:
    """Fim do texto para o embedding da consulta, começando numa palavra inteira."""
    clean = (text or "").strip()
    if max_chars <= 0 or len(clean) <= max_chars:
        return clean
    tail = clean[-max_chars:]
    space = tail.find(" ")
    return tail[space + 1:] if 0 <= space < 40 else tail


def remember_scene_vector(adventure_id: str, seq: int, model: str,
                          vec: list[float]) -> None:
    with _scene_guard:
        _scene_vectors[adventure_id] = (seq, model, vec)


def scene_vector(adventure_id: str, model: str, last_seq: int) -> list[float] | None:
    """Vetor gravado para a aventura, se ainda vale para o turno em `last_seq`."""
    with _scene_guard:
        hit = _scene_vectors.get(adventure_id)
    if hit is None:
        return None
    seq, hit_model, vec = hit
    if hit_model != model or not 0 <= last_seq - seq <= SCENE_VECTOR_MAX_LAG:
        return None
    return vec


def forget_scene_vectors() -> None:
    with _scene_guard:
        _scene_vectors.clear()


def prime_scene_vector(adventure_id: str, story_text: str, seq: int | None,
                       emb_cfg: dict, *, query_chars: int = DEFAULT_QUERY_CHARS,
                       transport: Any = None,
                       embeddings_client_mod: Any = None) -> bool:
    """Embedding do fim da história no pós-turno, com o prazo normal do serviço.

    Chamar fora de conexão com o banco: na CPU leva de segundos a dezenas de
    segundos. True quando o vetor foi gravado."""
    if not emb_cfg.get("enabled", True):
        return False
    query = query_tail(story_text, query_chars)
    if not query:
        return False
    emb_mod = embeddings_client_mod or embeddings_client
    vec = emb_mod.get_embedding(query, cfg=emb_cfg, transport=transport)
    if vec is None:
        return False
    model = emb_cfg.get("model", embeddings_client.DEFAULT_MODEL)
    remember_scene_vector(adventure_id, -1 if seq is None else seq, model, vec)
    return True


def _query_vector(conn: sqlite3.Connection, adventure_id: str, recent_text: str,
                  mem_cfg: dict, emb_cfg: dict, model: str, emb_mod: Any,
                  transport: Any) -> list[float] | None:
    """Vetor da cena do pós-turno; sem ele, o fim do texto com prazo curto."""
    last = db.max_seq(conn, adventure_id)
    last_seq = -1 if last is None else last
    vec = scene_vector(adventure_id, model, last_seq)
    if vec is not None:
        return vec
    timeout_s = float(mem_cfg.get("query_timeout_s", DEFAULT_QUERY_TIMEOUT_S))
    query = query_tail(recent_text, int(mem_cfg.get("query_chars", DEFAULT_QUERY_CHARS)))
    if timeout_s <= 0 or not query:
        return None
    cfg = dict(emb_cfg)
    cfg["timeout_seconds"] = min(float(emb_cfg.get("timeout_seconds", timeout_s)), timeout_s)
    vec = emb_mod.get_embedding(query, cfg=cfg, transport=transport)
    if vec is None:
        log.info("memória: sem embedding da consulta em %.1f s; só palavras-chave", timeout_s)
        return None
    remember_scene_vector(adventure_id, last_seq, model, vec)
    return vec


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
    emb_cfg = _embeddings_settings(conn, settings)
    target_model = (emb_cfg.get("model", embeddings_client.DEFAULT_MODEL)
                    if emb_cfg.get("enabled", True) else None)

    query_vec = query_vector
    has_vectors = any(m.get("embedding") and m.get("embedding_model") == target_model
                      for m in memories)
    if query_vec is None and target_model and has_vectors:
        query_vec = _query_vector(conn, adventure_id, recent_text or "", cfg, emb_cfg,
                                  target_model, emb_mod, transport)

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
