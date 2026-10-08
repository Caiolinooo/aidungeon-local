"""Summarizer — memory bank automático + story summary rolante (PLANO.md §4.3).

Hook best-effort chamado pelo game_engine após cada geração:
    maybe_update(adventure_id)            # assinatura do hook (import tardio)
    maybe_update(adventure_id, *, conn=..., llm_client=..., settings=...,
                 estimator=..., transport=...)   # injeção p/ testes

(a) MEMORY BANK — quando há >= settings.memory_bank.trigger_tokens (default
    1500, estimador chars/4) de texto novo em actions desde o seq_end da última
    memória, resume UM trecho (do mais antigo não coberto até estourar o
    trigger; o restante fica p/ as próximas chamadas) via llm_client sem
    stream (temperature = summary_temperature, max_tokens ~200) e grava em
    `memories` com seq_start/seq_end do trecho.

(b) STORY SUMMARY rolante — quando cresce o conjunto de ações antigas FORA da
    janela recente (mesma regra do context_builder, bloco 6), reescreve
    adventures.story_summary incorporando os fatos novos, sempre <= budget
    (400 tokens). Posição coberta é persistida na tabela settings
    (chave memory.summary_covered_seq.<adventure_id> — dado, não schema).
    Aventura importada que JÁ tem story_summary (backup): na 1ª passada só
    marca a posição — o resumo existente já cobre o passado.

(c) NUNCA lança exceção para o caller (try/except + log por passo e global) e
    NUNCA bloqueia a resposta ao jogador em falha do LLM: em erro, nada é
    gravado e a posição não avança (tenta de novo na próxima geração).
"""

from __future__ import annotations

import importlib
import logging
import sqlite3
from typing import Any, Callable

try:
    from app import context_builder, db, settings_store
except ImportError:  # pragma: no cover
    from backend.app import context_builder, db, settings_store  # type: ignore

log = logging.getLogger("aidungeon.memory")

DEFAULT_TRIGGER_TOKENS = 1500
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
    "Summarize the key events of this RPG story excerpt in at most {max_words} words. "
    "Keep: decisions, promises, NPC names and roles, places visited, items "
    "gained/lost, unresolved threads. Write in second person, past tense. Text:\n{text}"
)

ROLLING_SUMMARY_PROMPT = (
    "You maintain a running summary of an ongoing RPG adventure. Current summary:\n"
    "{summary}\n\nNew events:\n{text}\n\nRewrite the summary incorporating the new "
    "events, in at most 150 words, second person, focusing on facts that must not "
    "be forgotten."
)


def _default_estimator(text: str) -> int:
    return (len(text) + 3) // 4


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


# ------------------------------------------------------------- memory bank
def _update_memory_bank(conn: sqlite3.Connection, adventure_id: str, *,
                        llm: Any, cfg: dict, estimator: Estimator,
                        transport: Any) -> None:
    mem = _mem_cfg(cfg)
    trigger = int(mem.get("trigger_tokens", DEFAULT_TRIGGER_TOKENS))
    max_words = int(mem.get("summary_max_words", DEFAULT_SUMMARY_MAX_WORDS))
    temperature = float(mem.get("summary_temperature", DEFAULT_SUMMARY_TEMPERATURE))

    row = conn.execute(
        "SELECT MAX(seq_end) AS m FROM memories WHERE adventure_id=?",
        (adventure_id,),
    ).fetchone()
    covered = row["m"] if row else None

    actions = db.list_actions(conn, adventure_id)
    new_actions = [a for a in actions if covered is None or a["seq"] > covered]
    if not new_actions:
        return
    if estimator("".join(_fmt(a) for a in new_actions)) < trigger:
        return  # ainda não acumulou texto novo suficiente

    # Um trecho por chamada: do mais antigo não coberto até estourar o trigger
    # (o restante é resumido nas gerações seguintes — seq_start/end honestos).
    chunk: list[dict] = []
    used = 0
    for action in new_actions:
        chunk.append(action)
        used += estimator(_fmt(action))
        if used >= trigger:
            break

    excerpt = "".join(_fmt(a) for a in chunk)
    if len(excerpt) > MAX_EXCERPT_CHARS:
        excerpt = excerpt[-MAX_EXCERPT_CHARS:]

    prompt = MEMORY_BANK_PROMPT.format(max_words=max_words, text=excerpt)
    text = llm.chat_completion(
        _llm_cfg(conn, cfg), [{"role": "user", "content": prompt}],
        temperature=temperature, max_tokens=MEMORY_MAX_TOKENS,
        transport=transport,
    )
    text = (text or "").strip()
    if not text:
        return
    db.insert_memory(conn, adventure_id, text,
                     seq_start=chunk[0]["seq"], seq_end=chunk[-1]["seq"])
    log.info("memory bank: memória gravada (adventure=%s seq %s..%s)",
             adventure_id, chunk[0]["seq"], chunk[-1]["seq"])


# ----------------------------------------------------------- story summary
def _update_story_summary(conn: sqlite3.Connection, adventure_id: str, *,
                          llm: Any, cfg: dict, estimator: Estimator,
                          transport: Any) -> None:
    budgets = cfg.get("budgets", {}) if isinstance(cfg, dict) else {}
    summary_budget = int(budgets.get("story_summary", DEFAULT_SUMMARY_BUDGET))
    window_budget = _recent_window_budget(cfg)

    actions = db.list_actions(conn, adventure_id)
    if len(actions) < 2:
        return

    # Janela recente = mesma regra do context_builder (bloco 6): anda de trás
    # p/ frente por AÇÃO INTEIRA; a última ação nunca é "antiga" (bloco 8).
    kept = 0
    used = 0
    for action in reversed(actions[:-1]):
        cost = estimator(_fmt(action))
        if used + cost > window_budget:
            break
        used += cost
        kept += 1
    old_actions = actions[: len(actions) - 1 - kept]
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
    text = llm.chat_completion(
        _llm_cfg(conn, cfg), [{"role": "user", "content": prompt}],
        temperature=temperature, max_tokens=ROLLING_MAX_TOKENS,
        transport=transport,
    )
    text = (text or "").strip()
    if not text:
        return
    if estimator(text) > summary_budget:  # teto duro: sempre <= budget
        text = text[: summary_budget * 4].rstrip()
    db.update_adventure_summary(conn, adventure_id, text)
    db.set_setting(conn, key, str(new_old[-1]["seq"]))
    log.info("story summary rolante atualizado (adventure=%s até seq %s)",
             adventure_id, new_old[-1]["seq"])


# ------------------------------------------------------------------- hook
def maybe_update(adventure_id: str, *, conn: sqlite3.Connection | None = None,
                 llm_client: Any = None, settings: dict | None = None,
                 estimator: Estimator | None = None,
                 transport: Any = None) -> None:
    """Hook best-effort pós-geração. NUNCA levanta exceção nem quebra o jogo."""
    try:
        estimator = estimator or _default_estimator
        llm = llm_client or _llm_module()

        def run(c: sqlite3.Connection) -> None:
            cfg = settings or settings_store.runtime_config(c)
            for step in (_update_memory_bank, _update_story_summary):
                try:
                    step(c, adventure_id, llm=llm, cfg=cfg,
                         estimator=estimator, transport=transport)
                except Exception:
                    log.warning("maybe_update: passo %s falhou (adventure=%s)",
                                step.__name__, adventure_id, exc_info=True)

        if conn is not None:
            run(conn)
        else:
            with db.session() as session_conn:
                run(session_conn)
    except Exception:
        log.warning("maybe_update falhou (adventure=%s)", adventure_id,
                    exc_info=True)
