"""Montador de contexto — o "sanduíche" do PLANO.md §4.2.

Ordem EXATA dos blocos (budgets de config/settings.json):

    1. system: instrução base de narrador em 2ª pessoa + ai_instructions  (≤ 700)
    2. system: plot_essentials                                            (≤ 400)
    3. system: story cards (pinned + disparados)                          (≤ 1200)
    4. system: memory bank (top-k)                                        (≤ 600)
    5. system: story_summary                                              (≤ 400)
    6. user:   janela recente verbatim (dinâmica, trunca POR AÇÃO INTEIRA)
    7. system: authors_note                                               (≤ 300)
    8. user:   última ação formatada                                      (≤ 200)
    reserva de resposta                                                   (512)

Regra da janela recente (dinâmica): context_size − soma(demais budgets) −
response_reserve — os budgets fixos são pisos, a janela absorve a diferença.

Formatação das ações do jogador (byte a byte como no backup):
    do   → "\\n> You {text}\\n"
    say  → '\\n> You say "{text}"\\n'
    story/start/continue → prosa pura (verbatim)

A formatação é idempotente: textos que já começam com ">" (como os do backup,
que já vêm formatados) NÃO são re-embrulhados.

Story cards e memory bank delegam para backend/memory/ quando o módulo
existir (interface injetável — PLANO.md §5, agente engenheiro-memoria):
    memory.triggers.select_cards(conn, adventure_id, recent_text, budget_tokens)
        -> list[dict]  (cards já ordenados por relevância)
    memory.retrieval.select_memories(conn, adventure_id, budget_tokens)
        -> list[dict]  (memórias já ordenadas por relevância)
Sem o módulo: cards = só pinned; memórias = as mais recentes (top_k).
"""

from __future__ import annotations

import importlib
import sqlite3
from dataclasses import dataclass, field
from typing import Any

BASE_SYSTEM_PROMPT = (
    "You are an AI storyteller running an interactive text adventure. Continue the story in\n"
    "second person (\"You ...\"), matching the established tone. Write vivid prose with dialogue,\n"
    "sensory detail and consequences for the player's actions. Never decide what the player\n"
    "character says or does beyond their stated action. Never break the fourth wall."
)

# Budgets fixos que NÃO são a janela recente nem a reserva (pisos — PLANO §4.2)
FIXED_BUDGET_KEYS = (
    "ai_instructions", "plot_essentials", "story_cards", "memory_bank",
    "story_summary", "authors_note", "last_action",
)


# ------------------------------------------------------------ tokens (≈)
def estimate_tokens(text: str) -> int:
    """Estimador simples (chars/4). Isolado para ser trocável por um tokenizer."""
    return (len(text) + 3) // 4


def _fit_budget(text: str, budget_tokens: int) -> str:
    """Trunca texto ao budget estimado (corte duro por caracteres)."""
    if budget_tokens <= 0:
        return ""
    if estimate_tokens(text) <= budget_tokens:
        return text
    return text[: budget_tokens * 4]


# ------------------------------------------------------- formatação ações
def format_action_text(type_: str, text: str) -> str:
    """Renderiza a ação no formato do modelo. Idempotente p/ textos do backup."""
    if type_ in ("do", "say"):
        if text.lstrip().startswith(">"):
            return text  # já formatado (ex.: importado do backup)
        inner = text.strip()
        if type_ == "do":
            return f"\n> You {inner}\n"
        return f'\n> You say "{inner}"\n'
    return text  # start/continue/story/see: prosa pura, verbatim


# ---------------------------------------------------- módulos de memória
def _memory_module(name: str) -> Any | None:
    """Import tardio de backend/memory/<name> (interface injetável)."""
    for prefix in ("memory.", "backend.memory."):
        try:
            return importlib.import_module(prefix + name)
        except ImportError:
            continue
    return None


def _select_cards(conn: sqlite3.Connection, adventure_id: str, recent_text: str,
                  budget_tokens: int, cards_cfg: dict) -> list[dict]:
    """Story cards do bloco 3. Usa memory.triggers quando disponível."""
    mod = _memory_module("triggers")
    if mod is not None and hasattr(mod, "select_cards"):
        try:
            cards = mod.select_cards(conn, adventure_id, recent_text,
                                     budget_tokens=budget_tokens)
            if cards is not None:
                return list(cards)
        except Exception:
            pass  # fallback defensivo abaixo
    # Fallback: somente pinned, no teto do budget (e budget_por_card por card).
    from app import db
    per_card = int(cards_cfg.get("budget_por_card", 150))
    rows = conn.execute(
        "SELECT * FROM story_cards WHERE adventure_id=? AND pinned=1"
        " ORDER BY created_at ASC", (adventure_id,),
    ).fetchall()
    out: list[dict] = []
    used = 0
    for row in rows:
        card = db.card_to_dict(row)
        piece = _render_card(card, per_card)
        cost = estimate_tokens(piece)
        if used + cost > budget_tokens:
            break
        out.append(card)
        used += cost
    return out


def _render_card(card: dict, per_card_budget: int) -> str:
    title = (card.get("title") or "").strip()
    entry = _fit_budget((card.get("entry") or "").strip(), per_card_budget)
    return f"{title}\n{entry}".strip() if title else entry


def _select_memories(conn: sqlite3.Connection, adventure_id: str,
                     budget_tokens: int, mem_cfg: dict) -> list[dict]:
    """Memory bank do bloco 4. Usa memory.retrieval quando disponível."""
    mod = _memory_module("retrieval")
    if mod is not None and hasattr(mod, "select_memories"):
        try:
            mems = mod.select_memories(conn, adventure_id, budget_tokens=budget_tokens)
            if mems is not None:
                return list(mems)
        except Exception:
            pass
    # Fallback: as mais recentes (top_k), em ordem cronológica, no teto do budget.
    top_k = int(mem_cfg.get("top_k", 5))
    rows = conn.execute(
        "SELECT * FROM memories WHERE adventure_id=? ORDER BY id DESC LIMIT ?",
        (adventure_id, top_k),
    ).fetchall()
    rows.reverse()
    out: list[dict] = []
    used = 0
    for row in rows:
        text = row["text"]
        cost = estimate_tokens(text)
        if used + cost > budget_tokens:
            break
        out.append({"id": row["id"], "text": text})
        used += cost
    return out


# ------------------------------------------------------------- resultado
@dataclass
class BuiltContext:
    messages: list[dict]
    stop_sequences: list[str]
    meta: dict = field(default_factory=dict)


def recent_window_budget(cfg: dict) -> int:
    """Regra dinâmica: context_size − soma(demais budgets) − response_reserve."""
    budgets = cfg["budgets"]
    fixed = sum(int(budgets.get(k, 0)) for k in FIXED_BUDGET_KEYS)
    reserve = int(budgets.get("response_reserve", 512))
    return max(0, int(cfg["context_size"]) - fixed - reserve)


# ---------------------------------------------------------------- builder
def build_context(adventure: dict, actions: list[dict], cfg: dict,
                  conn: sqlite3.Connection) -> BuiltContext:
    """Monta as mensagens na ordem exata do sanduíche (PLANO.md §4.2)."""
    budgets = cfg["budgets"]
    messages: list[dict] = []
    meta: dict[str, Any] = {"budgets": {}}

    def system(content: str) -> None:
        if content:
            messages.append({"role": "system", "content": content})

    # 1 — system: narrador em 2ª pessoa + ai_instructions
    block1 = BASE_SYSTEM_PROMPT
    ai = (adventure.get("ai_instructions") or "").strip()
    if ai:
        block1 += "\n\n" + _fit_budget(ai, int(budgets.get("ai_instructions", 700)))
    system(block1)

    # 2 — system: plot_essentials
    plot = (adventure.get("plot_essentials") or "").strip()
    if plot:
        system(_fit_budget(plot, int(budgets.get("plot_essentials", 400))))

    # 3 — system: story cards (pinned + disparados)
    cards_cfg = cfg.get("story_cards", {})
    scan_chars = int(cards_cfg.get("scan_chars", 1000))
    recent_text = "".join(format_action_text(a["type"], a["text"]) for a in actions)
    cards = _select_cards(conn, adventure["id"], recent_text[-scan_chars:],
                          int(budgets.get("story_cards", 1200)), cards_cfg)
    if cards:
        per_card = int(cards_cfg.get("budget_por_card", 150))
        system("\n\n".join(_render_card(c, per_card) for c in cards))
        meta["cards_included"] = [c.get("id") for c in cards]

    # 4 — system: memory bank (top-k)
    mems = _select_memories(conn, adventure["id"], int(budgets.get("memory_bank", 600)),
                            cfg.get("memory_bank", {}))
    if mems:
        system("\n\n".join(m["text"] for m in mems))
        meta["memories_included"] = [m.get("id") for m in mems]

    # 5 — system: story_summary
    summary = (adventure.get("story_summary") or "").strip()
    if summary:
        system(_fit_budget(summary, int(budgets.get("story_summary", 400))))

    # 6 — user: janela recente verbatim (trunca POR AÇÃO INTEIRA)
    window_budget = recent_window_budget(cfg)
    meta["budgets"]["recent_window"] = window_budget
    window_actions = actions[:-1] if actions else []
    chosen: list[str] = []
    used = 0
    for action in reversed(window_actions):
        piece = format_action_text(action["type"], action["text"])
        cost = estimate_tokens(piece)
        if used + cost > window_budget:
            break  # nunca corta no meio de uma ação; descarta esta e as anteriores
        chosen.append(piece)
        used += cost
    chosen.reverse()
    if chosen:
        messages.append({"role": "user", "content": "".join(chosen)})
    meta["window_action_count"] = len(chosen)
    meta["window_tokens_est"] = used

    # 7 — system(final): author's note (perto do fim = alta influência)
    note = (adventure.get("authors_note") or "").strip()
    if note:
        system(_fit_budget(note, int(budgets.get("authors_note", 300))))

    # 8 — user(final): última ação formatada
    if actions:
        last = actions[-1]
        last_txt = _fit_budget(format_action_text(last["type"], last["text"]),
                               int(budgets.get("last_action", 200)))
        messages.append({"role": "user", "content": last_txt})
    else:
        messages.append({"role": "user", "content": "Begin the story."})

    return BuiltContext(messages=messages,
                        stop_sequences=list(cfg.get("stop_sequences",
                                                    ["\n> You", "\n> you"])),
                        meta=meta)
