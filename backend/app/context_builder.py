"""Montador de contexto — o "sanduíche" do PLANO.md §4.2.

Ordem lógica EXATA dos blocos (budgets de config/settings.json):

    1. instrução base de narrador em 2ª pessoa + ai_instructions  (≤ 700)
    2. plot_essentials                                            (≤ 400)
    3. story cards (pinned + disparados)                          (≤ 1200)
    4. memory bank (top-k)                                        (≤ 600)
    5. story_summary                                              (≤ 400)
    6. user:   janela recente verbatim, UMA mensagem por ação (dinâmica,
              trunca POR AÇÃO INTEIRA, e só em múltiplos de WINDOW_DROP_CHUNK)
    7. authors_note                                               (≤ 300)
    8. user:   última ação formatada                              (≤ 200)
    reserva de resposta                                           (512)

FORMATO NO WIRE (chat template Qwen3 do llama.cpp — prefixo estável p/ slot
pinado): o template exige a mensagem system NO INÍCIO e rejeita (HTTP 500
"System message must be at the beginning") qualquer system após a primeira
posição. A system da posição 0 leva SÓ os blocos estáveis, concatenados com
"\\n\\n":
    bloco 1  sem cabeçalho (é a diretiva central do narrador)
    bloco 2  "[Plot]"        — quem é você, mundo, regras (Plot Essentials)
    bloco 2b "[Players]"     — mesa compartilhada: jogador = personagem (≤ 400)
Os blocos voláteis vão no TOPO da mensagem user final, dentro de um
elemento <lore>…</lore>, para cards/memórias/resumo/estado não invalidarem
o prefixo cacheado:
    bloco 3  "[World info]"  — story cards disparados/pinned (lorebook)
    bloco 4  "[Memories]"    — memory bank (resumos de eventos antigos)
    bloco 5  "[Summary]"     — story summary rolante
    bloco 5b "[Player state]"
    âncoras  "[Active Lore: …]" e "[Continuity Anchor: …]"
Depois do </lore> vêm a author's note (bloco 7, no formato clássico
"[Author's note: {texto}]"), STYLE_CUE, CANON_CUE, LORE_CUE (nunca repetir
as notas) e a última ação. Os marcadores do contexto (CONTEXT_STOPS) entram
nas stop sequences: o narrador que começa a copiar um bloco para ali.
Budgets, escala dinâmica e a contabilidade de extra_canon_tokens não mudam
— só a posição. A janela recente descarta ações inteiras e, quando
descarta, sobe o corte para um múltiplo de WINDOW_DROP_CHUNK. Ações "see"
(prompt SDXL) não entram na narração.

Cada ação da janela é uma user própria, com o texto que teria na janela
corrida. O Qwen3.6 é híbrido: o llama-server só reaproveita o prefixo a
partir de um checkpoint, e o checkpoint útil é o do início da última user.
Com uma user por ação, o prompt do turno seguinte repete byte a byte tudo
o que vinha antes da user final, e esse checkpoint continua valendo. Uma
janela numa user só termina com <|im_end|> logo depois do texto, que o
turno seguinte troca pela ação nova, e o servidor reprocessa a janela
inteira. Nada volátil pode entrar antes da user final.

Formatação das ações do jogador (byte a byte como no backup):
    do   → "\\n> You {text}\\n"   (multiplayer+ator: "\\n> {actor}: {text}\\n")
    say  → '\\n> You say "{text}"\\n'  (multiplayer+ator: '\\n> {actor} says "{text}"\\n')
    story/start/continue → prosa pura (verbatim)
    see → fora do narrador (o prompt de imagem não é prosa)

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
from typing import Any, Callable

from app import npc_bonds
from app import world_facts

BASE_SYSTEM_PROMPT = (
    "You are the narrator and environment in an interactive text adventure. You control NPCs,\n"
    "sensory consequences, and the immediate world. The player controls only their own character.\n"
    "NPCs, places, and objects stay under your control.\n"
    "Continue the story in its own language, tense, and grammatical person. If the story\n"
    "addresses the player as \"you\", stay in second person. If it narrates a named protagonist,\n"
    "keep that name and person. Do not switch to English. Do not write image captions,\n"
    "portrait prompts, or camera directions. Write vivid prose with dialogue, sensory detail\n"
    "and consequences for the player's actions.\n\n"
    "Player agency invariants (highest priority):\n"
    "- Never write internal thoughts, feelings, decisions, spoken dialogue, or future actions for You.\n"
    "- Describe only the immediate sensory consequences of the player's action and NPC reactions.\n"
    "- Conclude each response with the scene active and unresolved, inviting the player's next move.\n"
    "- If an NPC asks a question or initiates a conflict, stop immediately to let the player answer.\n"
    "- Never break the fourth wall. The <lore> notes and the bracketed notes are private\n"
    "  reference: never repeat, quote or continue them, and never write tags or headers.\n\n"
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
    "- The Plot block and the <lore> notes (World info, Memories, Summary) define the\n"
    "  world, genre and premise. Everything you write MUST be consistent with them.\n"
    "- NEVER import stock genre tropes that the story has not established (e.g. knights, castles,\n"
    "  horses or dragons in a sci-fi/superhero story; spaceships in a medieval story).\n"
    "- If the recent text contradicts the Plot block, treat the Plot block as correct and steer\n"
    "  the narrative back to the established setting without commenting on it.\n"
    "- A character card is canon. Use its Appearance, Clothing, Status and Stats exactly.\n"
    "  Those fields come from the character sheet, not from a portrait prompt.\n"
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
    "[Style: complete grammatical sentences in the story's language and person, "
    "articles and prepositions kept, two or three sentences per paragraph, "
    "a blank line between paragraphs. Do not copy clipped or run-on prose, "
    "and do not write image prompts.]"
)

# A prosa recente já descreveu o NPC de outro jeito. Sem este lembrete no
# fim, o modelo copia o vestido e o cabelo errados no turno seguinte.
CANON_CUE = (
    "[Canon: a character card overrides earlier prose. Keep their appearance, "
    "clothing, status and recorded facts. Do not resurrect the dead, invent a "
    "relative's fate, or redesign a face the card already describes.]"
)

LORE_OPEN = "<lore>"
LORE_CLOSE = "</lore>"

# Vai no fim, colado na última ação: a regra do system perde para o exemplo
# do próprio contexto, e um pedido fora da história faz o narrador devolver
# um card no formato do [World info].
LORE_CUE = (
    "[Private: the <lore> notes and every bracketed note are for you alone. "
    "Use them silently; never repeat, quote or continue them or their tags. "
    "Reply only with the next part of the story.]"
)

# Marcadores do contexto. Viram stop sequences: a geração para no primeiro
# que o narrador escrever. O output_sanitizer limpa as variantes que escapam.
CONTEXT_STOPS = (
    LORE_OPEN, LORE_CLOSE, "[World info]", "[Memories]", "[Summary]",
    "[Player state]", "[Players]", "[Plot]", "[NPC minds]", "[Author's note", "[Active Lore",
    "[Continuity Anchor", "[Style:", "[Canon:", "[Private:", "[Web Research",
)

# A borda esquerda da janela só anda em múltiplos disto, para o prefixo
# cacheado (slot pinado) não ser reprocessado a cada turno.
WINDOW_DROP_CHUNK = 8


def narrative_actions(actions: list[dict]) -> list[dict]:
    """Tira ações `see` (prompt SDXL). Não são prosa e não entram na janela."""
    return [a for a in actions if a.get("type") != "see"]


def narrative_window(
    actions: list[dict],
    window_budget: int,
    estimator: Callable[[str], int] | None = None,
) -> tuple[list[dict], list[dict], list[dict], int]:
    """Lista narrativa e a janela recente com descarte em blocos.

    Devolve `(narrative, kept, evicted, tokens)`. `narrative` é `actions`
    sem `see`. A última ação narrativa é o turno atual: não está em `kept`
    nem em `evicted`. `kept` é o sufixo de `narrative[:-1]` que cabe no
    budget, depois de subir o descarte para múltiplo de WINDOW_DROP_CHUNK.
    `evicted` é o prefixo que saiu — o que o summarizer arquiva. O mesmo
    par (kept, evicted) vale para o prompt e para a memória.
    """
    estimate = estimator or estimate_tokens
    narrative = narrative_actions(actions)
    pool = narrative[:-1] if narrative else []
    chosen: list[dict] = []
    used = 0
    for action in reversed(pool):
        cost = estimate(format_action_text(action["type"], action["text"],
                                           actor=action.get("actor") or ""))
        if used + cost > window_budget:
            break
        chosen.append(action)
        used += cost
    chosen.reverse()
    dropped = len(pool) - len(chosen)
    if dropped:
        snapped = ((dropped + WINDOW_DROP_CHUNK - 1) // WINDOW_DROP_CHUNK) * WINDOW_DROP_CHUNK
        keep = len(pool) - snapped
        chosen = chosen[-keep:] if keep > 0 else []
        used = sum(
            estimate(format_action_text(a["type"], a["text"], actor=a.get("actor") or ""))
            for a in chosen
        )
    evicted = pool[: len(pool) - len(chosen)]
    return narrative, chosen, evicted, used


# Budgets fixos que NÃO são a janela recente nem a reserva (pisos — PLANO §4.2)
FIXED_BUDGET_KEYS = (
    "ai_instructions", "plot_essentials", "story_cards", "memory_bank",
    "story_summary", "authors_note", "last_action", "player_state",
    "npc_minds",
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
def format_action_text(type_: str, text: str, *, actor: str = "",
                       multiplayer: bool = False) -> str:
    """Renderiza a ação no formato do modelo. Idempotente p/ textos do backup.

    Com vários jogadores e ator gravado, do/say citam o nome em vez de "You".
    Texto que já começa com ">" (backup) não é reembrulhado.
    """
    if type_ in ("do", "say"):
        if text.lstrip().startswith(">"):
            return text  # já formatado (ex.: importado do backup)
        inner = text.strip()
        who = actor.strip()
        if multiplayer and who:
            if type_ == "do":
                return f"\n> {who}: {inner}\n"
            return f'\n> {who} says "{inner}"\n'
        if type_ == "do":
            return f"\n> You {inner}\n"
        return f'\n> You say "{inner}"\n'
    return text  # start/continue/story: prosa pura, verbatim


def story_text(actions: list[dict]) -> str:
    """A história como o narrador lê: ações formatadas, sem `see`."""
    return "".join(
        format_action_text(a["type"], a["text"], actor=a.get("actor") or "")
        for a in narrative_actions(actions)
    )


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


def _minds_block(conn: sqlite3.Connection, adventure_id: str, cards: list[dict],
                 budget_tokens: int) -> str:
    """Humor e memórias de quem já entrou no bloco de cards."""
    from app import npc_assets, npc_life
    lines: list[str] = []
    present = [card for card in cards if (card.get("type") or "") == "character"]
    if not present:
        return ""
    memories = npc_life.recent_for_cards(
        conn, adventure_id, [card["id"] for card in present], per_card=3,
    )
    by_card: dict[str, list[dict]] = {}
    for row in memories:
        by_card.setdefault(row.get("card_id") or "", []).append(row)
    for card in present:
        sheet = npc_assets.parse_sheet(card.get("sheet") or "")
        life = sheet.get("life") if isinstance(sheet.get("life"), dict) else {}
        bits = [
            " ".join(str(life.get(key) or "").split())
            for key in ("mood", "activity")
        ]
        bits = [bit for bit in bits if bit]
        name = " ".join(str(card.get("title") or "").split()) or "NPC"
        head = f"{name} — {'; '.join(bits)}" if bits else name
        block = [head]
        for row in by_card.get(card.get("id") or "", [])[:3]:
            text = " ".join(str(row.get("text") or "").split())[:240]
            if text:
                block.append(f"- {text}")
        if len(block) > 1 or bits:
            lines.append("\n".join(block))
    return _fit_budget("\n".join(lines), budget_tokens)


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
def _is_multiplayer(conn: sqlite3.Connection, adventure: dict) -> bool:
    """Mais de um participante, ou turno ligado (a mesa já é compartilhada)."""
    from app import db
    if adventure.get("turn_mode"):
        return True
    return len(db.adventure_participants(conn, adventure)) > 1


def _players_block(conn: sqlite3.Connection, adventure: dict) -> str:
    """Uma linha por participante: nome = título + resumo curto da ficha."""
    from app import db, npc_assets
    lines: list[str] = []
    cards = {
        card["player_username"]: card
        for card in db.list_player_characters(conn, adventure["id"])
    }
    for username in db.adventure_participants(conn, adventure):
        card = cards.get(username)
        if not card:
            lines.append(f"{username} = (sem personagem)")
            continue
        title = (card.get("title") or "").strip() or "(sem personagem)"
        sheet = npc_assets.parse_sheet(card.get("sheet") or "")
        bits: list[str] = []
        appearance = " ".join(str(sheet.get("appearance") or "").split())
        if appearance:
            bits.append(appearance[:120])
        personality = " ".join(str(sheet.get("personality") or "").split())
        if personality:
            bits.append(personality[:120])
        sentiments = " ".join(str(sheet.get("sentiments") or "").split())
        if sentiments:
            bits.append(sentiments[:120])
        status = str(sheet.get("status") or "").strip()
        if status:
            bits.append(status)
        stats = sheet.get("stats")
        if isinstance(stats, dict) and stats:
            bits.append(" ".join(f"{key} {stats[key]}" for key in stats))
        extra = "; ".join(bits)
        lines.append(f"{username} = {title}" + (f" — {extra}" if extra else ""))
    return "\n".join(lines)


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
    do template Qwen3: UMA system na posição 0 (blocos estáveis 1–2), uma
    user por ação da janela e a user final. Blocos voláteis (cards,
    memórias, resumo, player state, âncoras) ficam no <lore> do topo da
    user final."""
    # Prompt de imagem, não prosa: fora da janela, da varredura de
    # cards/memórias e da escolha da última ação. O summarizer usa o mesmo filtro.
    actions = narrative_actions(actions)
    budgets = cfg["budgets"]
    scale = context_scale_factor(cfg)
    meta: dict[str, Any] = {"budgets": {}, "context_scale_factor": scale}

    # ---- system único (posição 0): só blocos estáveis 1–2 -----------------
    system_parts: list[str] = []
    volatile_parts: list[str] = []

    # 1 — narrador + ai_instructions (sem cabeçalho)
    block1 = BASE_SYSTEM_PROMPT
    ai = (adventure.get("ai_instructions") or "").strip()
    if ai:
        block1 += "\n\n" + _fit_budget(ai, int(budgets.get("ai_instructions", 700)))
    system_parts.append(block1)

    # 2 — plot_essentials
    plot = (adventure.get("plot_essentials") or "").strip()
    if plot:
        system_parts.append("[Plot]\n" + _fit_budget(plot, int(budgets.get("plot_essentials", 1600))))

    # Mesa compartilhada: quem joga e a ficha de cada um. Estável (só muda
    # quando alguém edita o personagem), então fica na system, não no <lore>.
    multiplayer = _is_multiplayer(conn, adventure)
    if multiplayer:
        players = _players_block(conn, adventure)
        if players:
            system_parts.append("[Players]\n" + _fit_budget(players, 400))

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
    recent_tail = story_text(actions)[-scan_chars:]

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
        volatile_parts.append("[World info]\n" + cards_block)
        minds = _minds_block(
            conn, adventure["id"], cards, int(budgets.get("npc_minds", 300)),
        )
        if minds:
            volatile_parts.append("[NPC minds]\n" + minds)
        meta["cards_included"] = [c.get("id") for c in cards]
        extra_canon_tokens += max(0, estimate_tokens(cards_block) - base_cards_budget)

    # 4 — memory bank (top-k, ordem cronológica)
    if mems:
        mems_block = "\n\n".join(m["text"] for m in mems)
        volatile_parts.append("[Memories]\n" + mems_block)
        meta["memories_included"] = [m.get("id") for m in mems]
        extra_canon_tokens += max(0, estimate_tokens(mems_block) - base_mem_budget)

    # 5 — story_summary
    if summary:
        sum_block = _fit_budget(summary, sum_budget)
        volatile_parts.append("[Summary]\n" + sum_block)
        extra_canon_tokens += max(0, estimate_tokens(sum_block) - base_sum_budget)

    # 5b — estado do painel dinâmico (v0.7.0): HP/inventário/ficha que o
    # jogador edita nos widgets. Sem ele a IA narra dano/tesouro no escuro.
    player_state = render_player_state(adventure.get("ui_state") or {})
    if player_state:
        volatile_parts.append("[Player state]\n" + _fit_budget(
            player_state, int(budgets.get("player_state", 200))))

    messages: list[dict] = [{"role": "system", "content": "\n\n".join(system_parts)}]

    # 6 — user por ação: janela recente verbatim (trunca POR AÇÃO INTEIRA)
    base_window_budget = recent_window_budget(cfg)
    window_budget = max(0, base_window_budget - extra_canon_tokens)
    meta["budgets"]["recent_window"] = base_window_budget
    # Mesma seleção do summarizer: see já saiu, descarte em WINDOW_DROP_CHUNK.
    _narrative, window_kept, _evicted, used = narrative_window(actions, window_budget)
    chosen = [
        format_action_text(a["type"], a["text"], actor=a.get("actor") or "",
                           multiplayer=multiplayer)
        for a in window_kept
    ]
    messages.extend({"role": "user", "content": text} for text in chosen if text.strip())
    meta["window_action_count"] = len(chosen)
    meta["window_tokens_est"] = used

    # 7+8 — user(final): <lore> com voláteis e âncoras, author's note, lembretes, última ação
    note = (adventure.get("authors_note") or "").strip()
    if note:
        note = _fit_budget(note, int(budgets.get("authors_note", 300)))
    if actions:
        last = actions[-1]
        last_txt = _fit_budget(
            format_action_text(last["type"], last["text"],
                               actor=last.get("actor") or "", multiplayer=multiplayer),
            int(budgets.get("last_action", 200)),
        )
    else:
        last_txt = "Begin the story."
    lore_parts: list[str] = list(volatile_parts)

    # Active entity recency anchor: cards disparados pelo turno imediato
    anchored_titles: set[str] = set()
    if actions and cards:
        immediate_norm = " ".join(last_txt.split()).casefold()
        for c in cards:
            if _card_matches_immediate_turn(c, immediate_norm):
                snippet = _card_snippet(c)
                title = c.get("title") or ""
                if title and snippet:
                    lore_parts.append(f"[Active Lore: {title} — {snippet}]")
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
                        lore_parts.append(f"[Active Lore: {title} — {snippet}]")
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
            lore_parts.append(
                "[Continuity Anchor: " + " | ".join(continuity_bits) + "]"
            )
            meta["continuity_anchor_active"] = True

    final_parts: list[str] = []
    if lore_parts:
        final_parts.append("\n".join([LORE_OPEN, *lore_parts, LORE_CLOSE]))
    if note:
        final_parts.append(f"[Author's note: {note}]")
    final_parts.extend((STYLE_CUE, CANON_CUE, LORE_CUE))
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

    for extra in ("\nPlayer:", "\nUser:", *CONTEXT_STOPS):
        if extra not in stops:
            stops.append(extra)

    # Momento do NPC: volátil, só na user final. A janela (uma user por
    # ação já gravada) não recebe este bloco — senão o prefixo cacheado muda.
    scene_text = "\n".join(
        (action.get("text") or "") for action in narrative_actions(actions)[-3:]
    )
    moment = npc_bonds.moment_for(conn, adventure["id"], scene_text)
    if moment and messages:
        messages[-1]["content"] = npc_bonds.insert_into_final(
            messages[-1]["content"], moment)
        if npc_bonds.MOMENT_MARKER not in stops:
            stops.append(npc_bonds.MOMENT_MARKER)

    return BuiltContext(messages=messages,
                        stop_sequences=stops,
                        meta=meta)


def with_reference(content: str, block: str) -> str:
    """Bloco de referência montado fora daqui (pesquisa web) no topo do <lore>
    da user final; sem <lore>, abre um."""
    head = LORE_OPEN + "\n"
    if content.startswith(head):
        return head + block + "\n" + content[len(head):]
    return f"{head}{block}\n{LORE_CLOSE}\n{content}"
