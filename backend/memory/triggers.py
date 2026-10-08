"""Gatilhos de story cards (PLANO.md §4.3 — "Gatilho de Story Card").

Regras:
- Scan case-insensitive dos últimos `settings.story_cards.scan_chars` (default
  1000) caracteres de `recent_text`, com espaços normalizados.
- Um card dispara se QUALQUER trigger aparecer como substring do trecho
  (triggers vêm do split/trim de `keys` no import — aqui tolerantes a lista,
  string JSON ou "a, b ,").
- Cards `pinned` SEMPRE entram.
- Orçamento por card: `settings.story_cards.budget_por_card` (default 150
  tokens, estimador chars/4); teto total do bloco: `budget_tokens`.
- Entrada injetável: "{title}: {entry}" truncada ao orçamento do card
  (ver `render_card_text`).
- Ordenação: pinned primeiro, depois por título.

Assinatura consumida por app.context_builder (import tardio):
    select_cards(conn, adventure_id, recent_text, budget_tokens=...) -> list[dict]
Os parâmetros keyword-only `settings`/`estimator` são injetáveis (testes);
omitidos, os valores vêm de settings_store.runtime_config(conn) e chars/4.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any, Callable

try:  # layout normal: backend/ no sys.path
    from app import db
except ImportError:  # pragma: no cover - pacote backend.memory
    from backend.app import db  # type: ignore

DEFAULT_SCAN_CHARS = 1000
DEFAULT_PER_CARD_BUDGET = 150
DEFAULT_BLOCK_BUDGET = 1200

Estimator = Callable[[str], int]


def _default_estimator(text: str) -> int:
    """Estimador chars/4 (mesma regra de app.context_builder.estimate_tokens)."""
    return (len(text) + 3) // 4


def _cards_settings(conn: sqlite3.Connection, settings: dict | None) -> dict:
    """Aceita o bloco story_cards, o runtime_config completo ou None (carrega)."""
    if isinstance(settings, dict):
        if isinstance(settings.get("story_cards"), dict):
            return settings["story_cards"]
        return settings
    try:
        from app import settings_store
    except ImportError:  # pragma: no cover
        from backend.app import settings_store  # type: ignore
    try:
        return settings_store.runtime_config(conn).get("story_cards", {})
    except Exception:  # defesa — nunca derruba a montagem de contexto
        return {}


def _normalize(text: Any) -> str:
    """Casefold + colapsa espaços (scan case-insensitive, espaços normalizados)."""
    return " ".join(str(text or "").split()).casefold()


def parse_triggers(raw: Any) -> list[str]:
    """Triggers do card: lista, string JSON ou 'a, b ,' → split/trim, sem vazios."""
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            raw = raw.split(",")
    if not isinstance(raw, (list, tuple)):
        return []
    out: list[str] = []
    for item in raw:
        trigger = " ".join(str(item).split()).strip()
        if trigger:
            out.append(trigger)
    return out


def render_card_text(card: dict, budget_tokens: int,
                     estimator: Estimator | None = None) -> str:
    """Entrada injetável do card: "{title}: {entry}" truncada ao orçamento."""
    estimator = estimator or _default_estimator
    title = (card.get("title") or "").strip()
    entry = (card.get("entry") or "").strip()
    piece = f"{title}: {entry}" if title and entry else (title or entry)
    if budget_tokens <= 0:
        return ""
    if estimator(piece) <= budget_tokens:
        return piece
    return piece[: budget_tokens * 4]  # corte duro (estimador chars/4)


def card_matches(card: dict, haystack_normalized: str) -> bool:
    """True se QUALQUER trigger do card é substring do trecho normalizado."""
    return any(_normalize(t) in haystack_normalized
               for t in parse_triggers(card.get("triggers")))


def select_cards(conn: sqlite3.Connection, adventure_id: str, recent_text: str,
                 budget_tokens: int = DEFAULT_BLOCK_BUDGET, *,
                 settings: dict | None = None,
                 estimator: Estimator | None = None) -> list[dict]:
    """Cards do bloco 3: pinned + disparados, ordenados, dentro dos budgets."""
    estimator = estimator or _default_estimator
    cfg = _cards_settings(conn, settings)
    scan_chars = int(cfg.get("scan_chars", DEFAULT_SCAN_CHARS))
    per_card = int(cfg.get("budget_por_card", DEFAULT_PER_CARD_BUDGET))

    haystack = _normalize((recent_text or "")[-scan_chars:])
    rows = conn.execute(
        "SELECT * FROM story_cards WHERE adventure_id=?", (adventure_id,),
    ).fetchall()

    candidates: list[dict] = []
    for row in rows:
        card = db.card_to_dict(row)
        # parse do valor CRU da coluna: tolera string solta "a, b ," (card_to_dict
        # devolveria [] para JSON inválido e os triggers se perderiam)
        card["triggers"] = parse_triggers(row["triggers"])
        if card["pinned"] or (haystack and card_matches(card, haystack)):
            candidates.append(card)

    # pinned primeiro, depois por título (ordem também define prioridade de budget)
    candidates.sort(key=lambda c: (not c["pinned"], (c["title"] or "").casefold()))

    selected: list[dict] = []
    used = 0
    for card in candidates:
        piece = render_card_text(card, per_card, estimator)
        if not piece:
            continue  # card sem título nem entry: nada a injetar
        cost = estimator(piece)
        if used + cost > budget_tokens:
            continue  # não cabe no restante do bloco — tenta os próximos
        selected.append(card)
        used += cost
    return selected
