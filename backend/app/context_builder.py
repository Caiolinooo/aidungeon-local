"""Montador de contexto — o "sanduíche" do PLANO.md §4.2.

Ordem lógica EXATA dos blocos (budgets de config/settings.json):

    1. instrução base de narrador em 2ª pessoa + ai_instructions  (≤ 700)
    2. plot_essentials                                            (≤ 400)
    3. story cards (pinned + disparados)                          (≤ 1200)
    4. memory bank (top-k)                                        (≤ 600)
    5. story_summary                                              (≤ 400)
    6. user:   janela recente verbatim (dinâmica, trunca POR AÇÃO INTEIRA)
    7. authors_note                                               (≤ 300)
    8. user:   última ação formatada                              (≤ 200)
    reserva de resposta                                           (512)

FORMATO NO WIRE (chat template Qwen3 do llama.cpp — validado contra a VM):
o template exige a mensagem system NO INÍCIO e rejeita (HTTP 500 "System
message must be at the beginning") qualquer system após a primeira posição.
Por isso os blocos 1–5 viram UMA ÚNICA mensagem system na posição 0,
concatenados com "\\n\\n" e cabeçalhos sóbrios que preservam a semântica:
    bloco 1  sem cabeçalho (é a diretiva central do narrador)
    bloco 2  "[Plot]"        — quem é você, mundo, regras (Plot Essentials)
    bloco 3  "[World info]"  — story cards disparados/pinned (lorebook)
    bloco 4  "[Memories]"    — memory bank (resumos de eventos antigos)
    bloco 5  "[Summary]"     — story summary rolante
A author's note (bloco 7) vai DENTRO da mensagem user final, como linha
própria imediatamente ANTES da última ação, no formato clássico
"[Author's note: {texto}]" — preserva a influência por recência sem
system fora da 1ª posição. A ordem lógica, os budgets e a regra dinâmica
da janela recente (context_size − soma(demais budgets) − response_reserve;
budgets fixos são pisos) NÃO mudam.

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

from app import world_facts

BASE_SYSTEM_PROMPT = (
    "You are the narrator and environment in an interactive text adventure. You control NPCs,\n"
    "sensory consequences, and the immediate world. The player controls only their own character.\n"
    "NPCs, places, and objects stay under your control.\n"
    "Continue the story in second person (\"You ...\"), matching the established tone. Write vivid\n"
    "prose with dialogue, sensory detail and consequences for the player's actions.\n\n"
    "Player agency invariants (highest priority):\n"
    "- Never write internal thoughts, feelings, decisions, spoken dialogue, or future actions for You.\n"
    "- Describe only the immediate sensory consequences of the player's action and NPC reactions.\n"
    "- Conclude each response with the scene active and unresolved, inviting the player's next move.\n"
    "- If an NPC asks a question or initiates a conflict, stop immediately to let the player answer.\n"
    "- Never break the fourth wall.\n\n"
    "Readability (always — do not copy clipped or run-on prose from earlier turns):\n"
    "- Write complete sentences in the story's language. Keep articles and\n"
    "  prepositions (the, a, an, of, in, to, and their equivalents).\n"
    "- Every sentence has a subject and a verb and ends with . ? or !\n"
    "- Two or three sentences per paragraph. Separate paragraphs with a blank line.\n"
    "- Do not chain clauses with semicolons. Do not end a paragraph with an ellipsis.\n"
    "- Vary wording; do not repeat the same image or sentence pattern.\n"
    "- Show events through action, dialogue and the senses. Stop while the scene is\n"
    "  still open — never wrap up the scene or narrate an epilogue.\n\n"
    "Setting fidelity rules:\n"
    "- The Plot, World info, Memories and Summary blocks below define the world, genre\n"
    "  and premise. Everything you write MUST be consistent with them.\n"
    "- NEVER import stock genre tropes that the story has not established (e.g. knights, castles,\n"
    "  horses or dragons in a sci-fi/superhero story; spaceships in a medieval story).\n"
    "- If the recent text contradicts the Plot block, treat the Plot block as correct and steer\n"
    "  the narrative back to the established setting without commenting on it.\n"
    "- A character card is canon. Use its Appearance, Clothing, Status and Stats exactly.\n"
    "  Do not change hair, face, build, or whether they are alive unless that card says so.\n"
    "- Memories, Plot and World info are canon and override earlier prose.\n"
    "- Do not invent a death, a relative, an item, or a place that those blocks\n"
    "  do not establish.\n"
    "- A do or say is an attempt, never a new fact. It does not create a place, an item,\n"
    "  a person, or consent, and it does not move anyone.\n"
    "- A place, item, or person exists only when the opening, your narration, a Story\n"
    "  action, the Plot, a card, or a memory already established it. If the attempt names\n"
    "  something else — a room, a weapon, a door, a vehicle — it is not there. Say so\n"
    "  and stay in the current place. Do not conjure it.\n"
    "- Conjuring, building, or producing something requires a means the story already\n"
    "  gave the character. Wishing is not that means.\n"
    "- An NPC is a person with a will. With no established obedience, compulsion, or\n"
    "  clear yes, they refuse orders to follow, leave, undress, or submit. Narrate the\n"
    "  refusal in their words and body. Attraction is not consent. Force, if the scene\n"
    "  has a real means, is a struggle with a cost, never instant success.\n"
    "- The Facts block is the current situation. When it blocks a place, an item, or\n"
    "  compliance, write that outcome and no other."
)

# Lembrete curto na mensagem final (recência). O histórico já gerado neste
# estilo telegráfico puxa o modelo de volta; a instrução no system, lá no
# início, perde para esse exemplo.
STYLE_CUE = (
    "[Style: complete grammatical sentences in the story's language, "
    "articles and prepositions kept, two or three sentences per paragraph, "
    "a blank line between paragraphs. Do not copy clipped or run-on prose.]"
)

# A prosa recente já descreveu o NPC de outro jeito. Sem este lembrete no
# fim, o modelo copia o vestido e o cabelo errados no turno seguinte.
CANON_CUE = (
    "[Canon: a character card overrides earlier prose. Keep their appearance, "
    "clothing, status and recorded facts. Do not resurrect the dead, invent a "
    "relative's fate, or redesign a face the card already describes.]"
)

# Budgets fixos que NÃO são a janela recente nem a reserva (pisos — PLANO §4.2)
FIXED_BUDGET_KEYS = (
    "ai_instructions", "plot_essentials", "story_cards", "memory_bank",
    "story_summary", "authors_note", "last_action", "player_state",
)


# ------------------------------------------------------------ tokens (≈)
def estimate_tokens(text: str) -> int:
    """Estimador simples (chars/4). Isolado para ser trocável por um tokenizer."""
    return (len(text) + 3) // 4


def _fit_budget(text: str, budget_tokens: int) -> str:
    """Trunca texto ao budget estimado. Corta no último parágrafo/frase que
    couber (nunca no meio de uma palavra) para não vazar fragmento truncado."""
    if budget_tokens <= 0:
        return ""
    if estimate_tokens(text) <= budget_tokens:
        return text
    cut = text[: budget_tokens * 4]
    boundary = max(cut.rfind("\n\n"), cut.rfind(". "), cut.rfind(".\n"))
    if boundary > len(cut) // 2:
        cut = cut[: boundary + 1]
    return cut.rstrip()


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
    from app import npc_assets
    return _fit_budget(npc_assets.card_canon_text(card), per_card_budget)


def _select_memories(conn: sqlite3.Connection, adventure_id: str, recent_text: str,
                     budget_tokens: int, mem_cfg: dict) -> list[dict]:
    """Memory bank do bloco 4. Usa memory.retrieval quando disponível."""
    mod = _memory_module("retrieval")
    if mod is not None and hasattr(mod, "select_memories"):
        try:
            mems = mod.select_memories(conn, adventure_id, recent_text,
                                       budget_tokens=budget_tokens)
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
def render_player_state(ui_state: dict) -> str:
    """v0.7.0 — bloco compacto "[Player state]" a partir do ui_state do painel
    (stats do tipo {"HP": 14}, inventário [{"name","qty"}], ficha {campo: valor}).
    '' quando vazio — histórias sem widgets não gastam tokens."""
    lines: list[str] = []
    for key, value in (ui_state or {}).items():
        if isinstance(value, dict) and value:
            pairs = ", ".join(f"{k} {v}" for k, v in value.items())
            lines.append(f"{key}: {pairs}")
        elif isinstance(value, list) and value:
            items = ", ".join(
                f"{i.get('name')}×{i.get('qty', 1)}" if isinstance(i, dict) else str(i)
                for i in value)
            lines.append(f"{key}: {items}")
    return "\n".join(lines)


@dataclass
class BuiltContext:
    messages: list[dict]
    stop_sequences: list[str]
    meta: dict = field(default_factory=dict)


def context_scale_factor(cfg: dict) -> float:
    """Fator de escala dinâmico para contextos longos (> 32k tokens).

    Mantém 1.0 em <= 32768 tokens (preservando os pisos originais) e escala
    suavemente até 3.0x em contextos de 64k–256k tokens para permitir mais
    Story Cards, Memórias Vetoriais e Story Summary sem sufocar o cânone.
    """
    dyn = cfg.get("dynamic_scaling") or {}
    if not dyn.get("enabled", True):
        return 1.0
    base = float(dyn.get("base_context_size", 32768) or 32768)
    max_scale = float(dyn.get("max_scale_factor", 3.0) or 3.0)
    ctx = float(cfg.get("context_size", 32768) or 32768)
    if ctx <= base:
        return 1.0
    return min(max_scale, max(1.0, ctx / base))


def recent_window_budget(cfg: dict) -> int:
    """Regra dinâmica: context_size − soma(demais budgets) − response_reserve."""
    budgets = cfg["budgets"]
    fixed = sum(int(budgets.get(k, 0)) for k in FIXED_BUDGET_KEYS)
    reserve = int(budgets.get("response_reserve", 512))
    max_new = int((cfg.get("sampler") or {}).get("max_tokens", 0) or 0)
    effective_reserve = max(reserve, max_new)
    return max(0, int(cfg["context_size"]) - fixed - effective_reserve)


def _card_matches_immediate_turn(card: dict, immediate_norm: str) -> bool:
    """Verifica se o card foi citado/disparado na ação do turno imediato."""
    if not immediate_norm:
        return False
    title = " ".join(str(card.get("title") or "").split()).casefold()
    if title and len(title) >= 3 and title in immediate_norm:
        return True
    mod = _memory_module("triggers")
    if mod is not None and hasattr(mod, "parse_triggers"):
        triggers = mod.parse_triggers(card.get("triggers"))
    else:
        triggers = card.get("triggers") or []
        if isinstance(triggers, str):
            triggers = triggers.split(",")
    for t in triggers:
        t_norm = " ".join(str(t).split()).casefold()
        if t_norm and len(t_norm) >= 3 and t_norm in immediate_norm:
            return True
    return False


def _card_snippet(card: dict) -> str:
    """Extrai um resumo canônico conciso de 1 linha para recency anchor."""
    raw = (card.get("entry") or card.get("content") or card.get("value") or "").strip()
    if not raw:
        try:
            from app import npc_assets
            raw = npc_assets.card_canon_text(card)
        except Exception:
            raw = ""
    if not raw:
        return ""
    first_line = raw.split("\n")[0].strip()
    if ". " in first_line:
        first_line = first_line.split(". ")[0].strip() + "."
    return first_line[:100].rstrip(".")


# ---------------------------------------------------------------- builder
def build_context(adventure: dict, actions: list[dict], cfg: dict,
                  conn: sqlite3.Connection) -> BuiltContext:
    """Monta as mensagens na ordem do sanduíche (PLANO.md §4.2), no formato
    do template Qwen3: UMA system na posição 0 (blocos 1–5) + users."""
    budgets = cfg["budgets"]
    scale = context_scale_factor(cfg)
    meta: dict[str, Any] = {"budgets": {}, "context_scale_factor": scale}

    # ---- system único (posição 0): blocos 1–5 concatenados ----------------
    system_parts: list[str] = []

    # 1 — narrador em 2ª pessoa + ai_instructions (sem cabeçalho)
    block1 = BASE_SYSTEM_PROMPT
    ai = (adventure.get("ai_instructions") or "").strip()
    if ai:
        block1 += "\n\n" + _fit_budget(ai, int(budgets.get("ai_instructions", 700)))
    system_parts.append(block1)

    # 2 — plot_essentials
    plot = (adventure.get("plot_essentials") or "").strip()
    if plot:
        system_parts.append("[Plot]\n" + _fit_budget(plot, int(budgets.get("plot_essentials", 1600))))

    # Em contextos longos (>32k), os tetos de story_cards, memory_bank e
    # story_summary escalam proporcionalmente (até 3x) para injetar mais
    # fatos canônicos sem desperdiçar a janela expandida.
    base_cards_budget = int(budgets.get("story_cards", 1200))
    base_mem_budget = int(budgets.get("memory_bank", 600))
    base_sum_budget = int(budgets.get("story_summary", 800))

    cards_budget = int(base_cards_budget * scale)
    mem_budget = int(base_mem_budget * scale)
    sum_budget = int(base_sum_budget * scale)

    # 4 antes do 3: a memória selecionada também dispara story card. Um nome
    # que já saiu da janela continua puxando a ficha.
    cards_cfg = dict(cfg.get("story_cards", {}))
    scan_chars = int(int(cards_cfg.get("scan_chars", 1000)) * scale)
    recent_text = "".join(format_action_text(a["type"], a["text"]) for a in actions)
    recent_tail = recent_text[-scan_chars:]

    mem_cfg = dict(cfg.get("memory_bank", {}))
    if scale > 1.0:
        mem_cfg["top_k"] = int(int(mem_cfg.get("top_k", 12)) * scale)

    mems = _select_memories(conn, adventure["id"], recent_tail,
                            mem_budget, mem_cfg)
    summary = (adventure.get("story_summary") or "").strip()
    canon_scan = "\n".join(
        part for part in (summary, "\n".join(m["text"] for m in mems)) if part
    )
    if len(canon_scan) >= scan_chars:
        card_scan = canon_scan[-scan_chars:]
    elif canon_scan:
        room = scan_chars - len(canon_scan) - 1
        card_scan = (recent_tail[-room:] + "\n" + canon_scan) if room > 0 else canon_scan
    else:
        card_scan = recent_tail

    # 3 — story cards (pinned + disparados pela janela, pelo resumo e pelas memórias)
    cards = _select_cards(conn, adventure["id"], card_scan,
                          cards_budget, cards_cfg)
    extra_canon_tokens = 0
    if cards:
        per_card = int(cards_cfg.get("budget_por_card", 150))
        cards_block = "\n\n".join(_render_card(c, per_card) for c in cards)
        system_parts.append("[World info]\n" + cards_block)
        meta["cards_included"] = [c.get("id") for c in cards]
        extra_canon_tokens += max(0, estimate_tokens(cards_block) - base_cards_budget)

    # 4 — memory bank (top-k, ordem cronológica)
    if mems:
        mems_block = "\n\n".join(m["text"] for m in mems)
        system_parts.append("[Memories]\n" + mems_block)
        meta["memories_included"] = [m.get("id") for m in mems]
        extra_canon_tokens += max(0, estimate_tokens(mems_block) - base_mem_budget)

    # 5 — story_summary
    if summary:
        sum_block = _fit_budget(summary, sum_budget)
        system_parts.append("[Summary]\n" + sum_block)
        extra_canon_tokens += max(0, estimate_tokens(sum_block) - base_sum_budget)

    # 5b — estado do painel dinâmico (v0.7.0): HP/inventário/ficha que o
    # jogador edita nos widgets. Sem ele a IA narra dano/tesouro no escuro.
    player_state = render_player_state(adventure.get("ui_state") or {})
    if player_state:
        system_parts.append("[Player state]\n" + _fit_budget(
            player_state, int(budgets.get("player_state", 200))))

    messages: list[dict] = [{"role": "system", "content": "\n\n".join(system_parts)}]

    # 6 — user: janela recente verbatim (trunca POR AÇÃO INTEIRA)
    base_window_budget = recent_window_budget(cfg)
    window_budget = max(0, base_window_budget - extra_canon_tokens)
    meta["budgets"]["recent_window"] = base_window_budget
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

    # 7+8 — user(final): author's note, active lore anchors, lembrete de prosa, última ação
    note = (adventure.get("authors_note") or "").strip()
    if note:
        note = _fit_budget(note, int(budgets.get("authors_note", 300)))
    if actions:
        last = actions[-1]
        last_txt = _fit_budget(format_action_text(last["type"], last["text"]),
                               int(budgets.get("last_action", 200)))
    else:
        last_txt = "Begin the story."
    final_parts = []
    if note:
        final_parts.append(f"[Author's note: {note}]")

    # Active entity recency anchor: cards disparados pelo turno imediato
    anchored_titles: set[str] = set()
    if actions and cards:
        immediate_norm = " ".join(last_txt.split()).casefold()
        for c in cards:
            if _card_matches_immediate_turn(c, immediate_norm):
                snippet = _card_snippet(c)
                title = c.get("title") or ""
                if title and snippet:
                    final_parts.append(f"[Active Lore: {title} — {snippet}]")
                    anchored_titles.add(title)

    # Long-Horizon Anti-Hallucination & Continuity Anchor:
    # Quando a janela recente acumula muitos turnos/tokens ("Lost in the Middle"),
    # re-ancora os fatos canônicos e entidades das últimas ações imediatamente
    # antes da geração para impedir alucinações e deriva de estado.
    dyn_cfg = cfg.get("dynamic_scaling") or {}
    min_anchor_tokens = int(dyn_cfg.get("continuity_anchor_min_window_tokens", 1200))
    min_anchor_turns = int(dyn_cfg.get("continuity_anchor_min_turns", 8))
    if used >= min_anchor_tokens or len(chosen) >= min_anchor_turns:
        recent_scene_norm = " ".join("".join(chosen[-3:]).split()).casefold()
        if cards and recent_scene_norm:
            for c in cards:
                title = c.get("title") or ""
                if title in anchored_titles:
                    continue
                if _card_matches_immediate_turn(c, recent_scene_norm):
                    snippet = _card_snippet(c)
                    if title and snippet:
                        final_parts.append(f"[Active Lore: {title} — {snippet}]")
                        anchored_titles.add(title)
                        if len(anchored_titles) >= 4:
                            break
        continuity_bits: list[str] = []
        if plot:
            plot_head = " ".join(plot.splitlines()[:2]).strip()[:180]
            if plot_head:
                continuity_bits.append(f"Protagonist/Premise: {plot_head}")
        if summary:
            sum_tail = " ".join(summary.split())[-220:]
            continuity_bits.append(f"Story so far: {sum_tail}")
        elif mems:
            mem_tail = " ".join((mems[-1].get("text") or "").split())[:200]
            if mem_tail:
                continuity_bits.append(f"Recent memory: {mem_tail}")
        if continuity_bits:
            final_parts.append(
                "[Continuity Anchor: " + " | ".join(continuity_bits) + "]"
            )
            meta["continuity_anchor_active"] = True

    final_parts.append(STYLE_CUE)
    final_parts.append(CANON_CUE)
    final_parts.append(world_facts.facts_for(adventure, actions, conn))
    final_parts.append(last_txt)
    messages.append({"role": "user", "content": "\n".join(final_parts)})

    # Dynamic stop sequence hardening (protagonista + player/user boundaries)
    stops = list(cfg.get("stop_sequences", ["\n> You", "\n> you"]))
    plot = adventure.get("plot_essentials") or ""
    if plot:
        try:
            from app.npc_chronicle import protagonist_names
            for p_name in protagonist_names(plot):
                p_clean = p_name.strip()
                if p_clean:
                    for variant in {p_clean, p_clean.title(), p_clean.capitalize()}:
                        for s in (f"\n> {variant}", f"\n{variant}:", f"\n>{variant}"):
                            if s not in stops:
                                stops.append(s)
        except Exception:
            pass

    for extra in ("\nPlayer:", "\nUser:"):
        if extra not in stops:
            stops.append(extra)

    return BuiltContext(messages=messages,
                        stop_sequences=stops,
                        meta=meta)
