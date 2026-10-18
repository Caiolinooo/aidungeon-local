"""Summarizer — memory bank automático + story summary rolante (PLANO.md §4.3).

Hook best-effort chamado pelo game_engine após cada geração:
    maybe_update(adventure_id)            # assinatura do hook (import tardio)
    maybe_update(adventure_id, *, conn=..., llm_client=..., settings=...,
                 estimator=..., transport=...)   # injeção p/ testes

(a) MEMORY BANK — ações que saíram da janela recente (a mesma regra do
    context_builder) e ainda não estão cobertas por memories.seq_end são
    arquivadas em pedaços de settings.memory_bank.trigger_tokens (default
    800). Cada pedaço vira um resumo de fatos (nomes, decisões, promessas,
    mortes, itens, lugares, relações), na língua do trecho. Se o LLM falhar
    ou voltar vazio, grava o extrato verbatim: nada sai da janela sem
    registro. Um pedaço por chamada; o resto fica para a geração seguinte.

(b) STORY SUMMARY rolante — quando cresce o conjunto de ações antigas FORA da
    janela recente (mesma regra do context_builder, bloco 6), reescreve
    adventures.story_summary incorporando os fatos novos, sempre <= budget
    (400 tokens). Posição coberta é persistida na tabela settings
    (chave memory.summary_covered_seq.<adventure_id> — dado, não schema).
    Aventura importada que JÁ tem story_summary (backup): na 1ª passada só
    marca a posição — o resumo existente já cobre o passado.

(c) NUNCA lança exceção para o caller (try/except + log por passo e global) e
    NUNCA bloqueia a resposta ao jogador. Falha do LLM no memory bank grava o
    extrato verbatim e avança o seq coberto. Falha no story summary não
    reescreve o resumo nem avança a posição (tenta de novo na próxima geração).

(d) `cancel` (ação nova do jogador, G3): chamada cancelada, breaker aberto ou
    fila estourada não é falha do resumo. Nada é gravado, nem o extrato, e a
    passada seguinte tenta de novo.
"""

from __future__ import annotations

import importlib
import logging
import sqlite3
import threading
from typing import Any, Callable

try:
    from app import context_builder, db, embeddings_client, llm_client, settings_store
except ImportError:  # pragma: no cover
    from backend.app import (  # type: ignore
        context_builder, db, embeddings_client, llm_client, settings_store)

log = logging.getLogger("aidungeon.memory")


def _embeddings_module() -> Any:
    for prefix in ("app.", "backend.app."):
        try:
            return importlib.import_module(prefix + "embeddings_client")
        except ImportError:
            continue
    return None

DEFAULT_TRIGGER_TOKENS = 800
DEFAULT_SUMMARY_MAX_WORDS = 120
DEFAULT_SUMMARY_TEMPERATURE = 0.3
DEFAULT_SUMMARY_BUDGET = 400
MEMORY_MAX_TOKENS = 200
ROLLING_MAX_TOKENS = 250
# Teto de segurança do trecho enviado ao LLM (ex.: 1ª passada em aventura longa
# sem summary, ou ação única gigante): mantém o fim (eventos mais recentes).
MAX_EXCERPT_CHARS = 6000

Estimator = Callable[[str], int]

MEMORY_BANK_PROMPT = (
    "Extract the facts that must not be forgotten from this RPG story excerpt. "
    "Write in the same language as the excerpt, in at most {max_words} words. "
    "Keep names, decisions, promises, deaths, items gained or lost, places, "
    "and relationship changes. Do not invent anything that is not in the excerpt. "
    "Text:\n{text}"
)

ROLLING_SUMMARY_PROMPT = (
    "You maintain a running summary of an ongoing RPG adventure. Current summary:\n"
    "{summary}\n\nNew events:\n{text}\n\nRewrite the summary incorporating the new "
    "events, in at most 150 words, in the same language as the story, focusing on "
    "facts that must not be forgotten. Do not copy image prompts."
)


_DEFERRED = (llm_client.LlmCancelled, llm_client.LlmDegraded, llm_client.LlmQueueTimeout)


def _default_estimator(text: str) -> int:
    return (len(text) + 3) // 4


def _summarize(llm: Any, llm_cfg: dict, prompt: str, *, temperature: float,
               max_tokens: int, transport: Any, cancel: threading.Event | None) -> str:
    kwargs: dict[str, Any] = {"role": "summary", "temperature": temperature,
                              "max_tokens": max_tokens, "transport": transport}
    if cancel is not None:
        kwargs["cancel"] = cancel
    return llm.chat_completion(llm_cfg, [{"role": "user", "content": prompt}], **kwargs)


def _llm_module() -> Any:
    for prefix in ("app.", "backend.app."):
        try:
            return importlib.import_module(prefix + "llm_client")
        except ImportError:
            continue
    raise ImportError("llm_client não encontrado")  # pragma: no cover


def _covered_key(adventure_id: str) -> str:
    return f"memory.summary_covered_seq.{adventure_id}"


def _llm_cfg(conn: sqlite3.Connection, cfg: dict) -> dict:
    if isinstance(cfg, dict) and isinstance(cfg.get("llm"), dict):
        return cfg["llm"]
    return settings_store.effective_llm_settings(conn)


def _fmt(action: dict) -> str:
    """Mesma renderização do context_builder (do/say embrulhados, prosa verbatim)."""
    return context_builder.format_action_text(action["type"], action["text"])


def _mem_cfg(cfg: dict) -> dict:
    return cfg.get("memory_bank", {}) if isinstance(cfg, dict) else {}


def _recent_window_budget(cfg: dict) -> int:
    """Regra dinâmica do bloco 6 do context_builder (com defaults defensivos)."""
    try:
        return context_builder.recent_window_budget(cfg)
    except Exception:  # cfg parcial (sem budgets/context_size)
        budgets = cfg.get("budgets", {}) if isinstance(cfg, dict) else {}
        fixed = sum(int(budgets.get(k, 0))
                    for k in context_builder.FIXED_BUDGET_KEYS)
        reserve = int(budgets.get("response_reserve", 512))
        size = int(cfg.get("context_size", 8192)) if isinstance(cfg, dict) else 8192
        return max(0, size - fixed - reserve)


def _evicted_actions(actions: list[dict], window_budget: int,
                     estimator: Estimator) -> list[dict]:
    """Ações fora da janela recente. Mesmo corte do context_builder.

    `see` não entra (não consome budget nem some sem arquivo). A última
    ação narrativa nunca é antiga. O descarte sobe em WINDOW_DROP_CHUNK,
    igual ao prompt — o que sai da janela é o que se arquiva.
    """
    _narrative, _kept, evicted, _tokens = context_builder.narrative_window(
        actions, window_budget, estimator)
    return evicted


def _verbatim_excerpt(excerpt: str) -> str:
    """Registro cru quando o resumo falha. O fato não pode sumir da janela."""
    text = (excerpt or "").strip()
    if len(text) > MAX_EXCERPT_CHARS:
        text = text[-MAX_EXCERPT_CHARS:]
    return text


# ------------------------------------------------------------- memory bank
def _pending_archivable_actions(
    actions: list[dict],
    window_budget: int,
    horizon_tokens: int,
    chunk_tokens: int,
    covered: int | None,
    estimator: Estimator,
) -> list[dict]:
    """Retorna ações pendentes de arquivamento no Memory Bank / Story Summary.

    1) Tudo que já saiu fisicamente da `window_budget` entra imediatamente.
    2) Quando `window_budget` é grande (ex.: 64k–256k) e nada saiu ainda da
       janela total, ações mais antigas que `horizon_tokens` (cena ativa,
       default 4096 tk) são arquivadas proativamente assim que acumulam pelo
       menos `chunk_tokens` (800 tk). Isso garante memórias vetoriais e
       story_summary sempre atualizados em campanhas longas sem esperar
       estourar 64k–256k tokens.
    """
    outside = _evicted_actions(actions, window_budget, estimator)
    pending = [a for a in outside if covered is None or a["seq"] > covered]
    if pending:
        return pending
    if horizon_tokens > 0 and window_budget > horizon_tokens:
        beyond_horizon = _evicted_actions(actions, horizon_tokens, estimator)
        proactive = [a for a in beyond_horizon if covered is None or a["seq"] > covered]
        proactive_tokens = sum(estimator(_fmt(a)) for a in proactive)
        if proactive_tokens >= chunk_tokens:
            return proactive
    return []


def _update_memory_bank(conn: sqlite3.Connection, adventure_id: str, *,
                        llm: Any, cfg: dict, estimator: Estimator,
                        transport: Any,
                        embeddings_client: Any = None,
                        cancel: threading.Event | None = None) -> None:
    mem = _mem_cfg(cfg)
    chunk_tokens = int(mem.get("trigger_tokens", DEFAULT_TRIGGER_TOKENS))
    if chunk_tokens <= 0:
        chunk_tokens = DEFAULT_TRIGGER_TOKENS
    horizon_tokens = int(mem.get("archive_horizon_tokens", 4096))
    max_words = int(mem.get("summary_max_words", DEFAULT_SUMMARY_MAX_WORDS))
    temperature = float(mem.get("summary_temperature", DEFAULT_SUMMARY_TEMPERATURE))

    row = conn.execute(
        "SELECT MAX(seq_end) AS m FROM memories WHERE adventure_id=?",
        (adventure_id,),
    ).fetchone()
    covered = row["m"] if row else None

    actions = db.list_actions(conn, adventure_id)
    window_budget = _recent_window_budget(cfg)
    pending = _pending_archivable_actions(
        actions, window_budget, horizon_tokens, chunk_tokens, covered, estimator
    )
    if not pending:
        return

    # Um pedaço por chamada: do mais antigo não coberto até estourar o tamanho
    # (o restante é arquivado nas gerações seguintes — seq_start/end honestos).
    # Texto que já saiu da janela entra mesmo abaixo do tamanho do pedaço.
    chunk: list[dict] = []
    used = 0
    for action in pending:
        chunk.append(action)
        used += estimator(_fmt(action))
        if used >= chunk_tokens:
            break

    excerpt = "".join(_fmt(a) for a in chunk)
    if len(excerpt) > MAX_EXCERPT_CHARS:
        excerpt = excerpt[-MAX_EXCERPT_CHARS:]

    prompt = MEMORY_BANK_PROMPT.format(max_words=max_words, text=excerpt)
    try:
        text = _summarize(llm, _llm_cfg(conn, cfg), prompt, temperature=temperature,
                          max_tokens=MEMORY_MAX_TOKENS, transport=transport, cancel=cancel)
        text = (text or "").strip()
    except _DEFERRED as exc:
        log.info("memory bank: adiado (%s) (adventure=%s)", exc.code, adventure_id)
        return
    except Exception:
        log.warning("memory bank: LLM falhou; gravando extrato (adventure=%s)",
                    adventure_id, exc_info=True)
        text = ""
    if not text:
        text = _verbatim_excerpt(excerpt)
    if not text:
        return

    # Geração de embedding para a nova memória (best-effort / fail-safe)
    embedding_bytes: bytes | None = None
    model_name: str | None = None
    dim: int | None = None

    emb_cfg = cfg.get("embeddings") if isinstance(cfg, dict) else None
    if emb_cfg is None:
        try:
            emb_cfg = settings_store.effective_embeddings_settings(conn)
        except Exception:
            emb_cfg = {}

    if emb_cfg.get("enabled", True):
        try:
            emb_mod = embeddings_client or _embeddings_module()
            if emb_mod is not None:
                vec = emb_mod.get_embedding(text, cfg=emb_cfg, transport=transport)
                if vec and isinstance(vec, list):
                    embedding_bytes = emb_mod.pack_vector(vec)
                    model_name = emb_cfg.get("model", "Qwen3-Embedding-0.6B")
                    dim = len(vec)
        except Exception:
            log.warning("memory bank: falha ao gerar embedding; salvando sem vetor (adventure=%s)",
                        adventure_id, exc_info=True)
            embedding_bytes = None
            model_name = None
            dim = None

    db.insert_memory(
        conn,
        adventure_id,
        text,
        seq_start=chunk[0]["seq"],
        seq_end=chunk[-1]["seq"],
        embedding=embedding_bytes,
        embedding_model=model_name,
        embedding_dim=dim,
    )
    log.info("memory bank: memória gravada (adventure=%s seq %s..%s, embedding=%s)",
             adventure_id, chunk[0]["seq"], chunk[-1]["seq"], bool(embedding_bytes))


# ----------------------------------------------------------- story summary
def _update_story_summary(conn: sqlite3.Connection, adventure_id: str, *,
                          llm: Any, cfg: dict, estimator: Estimator,
                          transport: Any, cancel: threading.Event | None = None) -> None:
    budgets = cfg.get("budgets", {}) if isinstance(cfg, dict) else {}
    summary_budget = int(budgets.get("story_summary", DEFAULT_SUMMARY_BUDGET))
    window_budget = _recent_window_budget(cfg)
    mem = _mem_cfg(cfg)
    horizon_tokens = int(mem.get("archive_horizon_tokens", 4096))
    chunk_tokens = int(mem.get("trigger_tokens", DEFAULT_TRIGGER_TOKENS))

    actions = db.list_actions(conn, adventure_id)
    old_actions = _evicted_actions(actions, window_budget, estimator)
    if not old_actions and horizon_tokens > 0 and window_budget > horizon_tokens:
        beyond_horizon = _evicted_actions(actions, horizon_tokens, estimator)
        if sum(estimator(_fmt(a)) for a in beyond_horizon) >= chunk_tokens:
            old_actions = beyond_horizon
    if not old_actions:
        return

    key = _covered_key(adventure_id)
    raw = db.get_setting(conn, key)
    covered = int(raw) if raw not in (None, "") else None

    adventure = db.get_adventure(conn, adventure_id) or {}
    current = (adventure.get("story_summary") or "").strip()
    if covered is None and current:
        # Aventura importada: o story_summary do backup já cobre o passado —
        # só marca a posição (sem chamar o LLM).
        db.set_setting(conn, key, str(old_actions[-1]["seq"]))
        return

    new_old = [a for a in old_actions if covered is None or a["seq"] > covered]
    if not new_old:
        return

    excerpt = "".join(_fmt(a) for a in new_old)
    if len(excerpt) > MAX_EXCERPT_CHARS:
        excerpt = excerpt[-MAX_EXCERPT_CHARS:]

    temperature = float(_mem_cfg(cfg).get("summary_temperature",
                                          DEFAULT_SUMMARY_TEMPERATURE))
    prompt = ROLLING_SUMMARY_PROMPT.format(summary=current, text=excerpt)
    text = _summarize(llm, _llm_cfg(conn, cfg), prompt, temperature=temperature,
                      max_tokens=ROLLING_MAX_TOKENS, transport=transport, cancel=cancel)
    text = (text or "").strip()
    if not text:
        return
    if estimator(text) > summary_budget:  # teto duro: sempre <= budget
        text = text[: summary_budget * 4].rstrip()
    db.update_adventure_summary(conn, adventure_id, text)
    db.set_setting(conn, key, str(new_old[-1]["seq"]))
    log.info("story summary rolante atualizado (adventure=%s até seq %s)",
             adventure_id, new_old[-1]["seq"])


# ---------------------------------------------------------------- backfill
def backfill_memories(
    conn: sqlite3.Connection,
    adventure_id: str | None = None,
    *,
    transport: Any = None,
    force_all: bool = False,
    client: Any = None,
) -> int:
    """Percorre memórias sem vetor (ou todas se force_all=True) e gera embeddings.

    Retorna a contagem de memórias atualizadas com sucesso.
    """
    emb_mod = client or _embeddings_module()
    if emb_mod is None:
        return 0
    try:
        cfg = settings_store.effective_embeddings_settings(conn)
    except Exception:
        cfg = {}
    if not cfg.get("enabled", True):
        return 0

    query = "SELECT id, text FROM memories WHERE "
    params: list[Any] = []
    conditions = []
    if not force_all:
        conditions.append("embedding IS NULL")
    if adventure_id:
        conditions.append("adventure_id = ?")
        params.append(adventure_id)

    query += " AND ".join(conditions) if conditions else "1=1"
    query += " ORDER BY id ASC"

    rows = conn.execute(query, params).fetchall()
    count = 0
    model_name = cfg.get("model", "Qwen3-Embedding-0.6B")

    for row in rows:
        mid, text = row["id"], row["text"]
        vec = emb_mod.get_embedding(text, cfg=cfg, transport=transport)
        if vec is not None:
            packed = emb_mod.pack_vector(vec)
            db.update_memory(
                conn,
                mid,
                embedding=packed,
                embedding_model=model_name,
                embedding_dim=len(vec),
            )
            count += 1
    log.info("backfill_memories: %d memórias atualizadas com embeddings", count)
    return count


# ------------------------------------------------------------------- hook
def maybe_update(adventure_id: str, *, conn: sqlite3.Connection | None = None,
                 llm_client: Any = None, embeddings_client: Any = None,
                 settings: dict | None = None,
                 estimator: Estimator | None = None,
                 transport: Any = None,
                 cancel: threading.Event | None = None) -> None:
    """Hook best-effort pós-geração. NUNCA levanta exceção nem quebra o jogo."""
    try:
        estimator = estimator or _default_estimator
        llm = llm_client or _llm_module()

        def run(c: sqlite3.Connection) -> None:
            cfg = settings or settings_store.runtime_config(c)
            try:
                _update_memory_bank(c, adventure_id, llm=llm, cfg=cfg,
                                    estimator=estimator, transport=transport,
                                    embeddings_client=embeddings_client, cancel=cancel)
            except Exception:
                log.warning("maybe_update: _update_memory_bank falhou (adventure=%s)",
                            adventure_id, exc_info=True)
            if cancel is not None and cancel.is_set():
                return
            try:
                _update_story_summary(c, adventure_id, llm=llm, cfg=cfg,
                                      estimator=estimator, transport=transport,
                                      cancel=cancel)
            except _DEFERRED as exc:
                log.info("story summary: adiado (%s) (adventure=%s)", exc.code, adventure_id)
            except Exception:
                log.warning("maybe_update: _update_story_summary falhou (adventure=%s)",
                            adventure_id, exc_info=True)

        if conn is not None:
            run(conn)
        else:
            with db.session() as session_conn:
                run(session_conn)
    except Exception:
        log.warning("maybe_update falhou (adventure=%s)", adventure_id,
                    exc_info=True)
