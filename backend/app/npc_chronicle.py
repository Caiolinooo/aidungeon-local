"""Registro vivo dos NPCs.

Depois de cada resposta da IA, um passe lê o trecho e os cards que já existem.
Quem entrou na história ganha um card. Lugar, facção e item também, só com o
que o texto já estabeleceu. Morte, parentesco, decisão ou ferimento vão para
a ficha, e um delta pequeno mexe no atributo quando o texto justifica.

A aparência já gravada não muda, a menos que o passe marque look_changed
(cicatriz, transformação). Roupa pode mudar. O protagonista do Plot não vira card.
Aventura sem plot ganha plot_essentials neste passe, a partir da abertura.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from app import db, jobs, likeness, llm_client, npc_assets, settings_store, world_facts

log = logging.getLogger("aidungeon.npc_chronicle")

_STATUSES = ("alive", "dead", "missing", "injured")

_SYSTEM = """You keep the registry of an interactive fiction game.
Read the recent passage and the existing cards. Return STRICT JSON only:
{
  "plot_essentials": "only when the protagonist block is empty: who the player is, where they are, and the tone, using only the passage. Otherwise an empty string",
  "characters": [
    {
      "name": "canonical name",
      "aliases": ["short name"],
      "existing_title": "exact title of a card already listed, or empty if this person is new",
      "summary": "who they are, 1-2 sentences, only facts the story has established",
      "facts": ["one new fact from THIS passage"],
      "status": "alive, dead, missing, injured, or unknown",
      "appearance": "face, hair, eyes, marks — empty if neither the passage nor the card describes them",
      "clothing": "what they wear now, empty if unknown",
      "personality": "traits actually shown, empty if unknown",
      "look_changed": false,
      "real": false,
      "stat_delta": {"str": 0, "dex": 0, "con": 0, "int": 0, "wis": 0, "cha": 0}
    }
  ],
  "places": [{"name": "place", "aliases": ["short name"], "existing_title": "", "summary": "what the passage established", "facts": ["one new fact"]}],
  "factions": [{"name": "group", "aliases": [], "existing_title": "", "summary": "what the passage established", "facts": []}],
  "items": [{"name": "item", "aliases": [], "existing_title": "", "summary": "what the passage established", "facts": []}]
}
Rules:
- Include someone only if they act, speak, or are discussed in the passage.
- Include a place, faction, or item only when the canon passage establishes it. Do not invent a sheet for them.
- The player attempt is not canon. Do not create a place, item, or person that appears only there.
- Do not record that an NPC complied, left, undressed, or changed location unless the canon passage shows it.
- If the Facts block says a place was not established, do not record that place.
- Never include the protagonist as a character.
- Never invent appearance, relatives, death, or numbers.
- look_changed is true only when the body or face itself changes (scar, curse, aging), not when clothes change.
- If the card already has an appearance or a portrait, leave appearance and clothing empty unless look_changed is true.
- status is dead, missing or injured only when the passage says so. Otherwise repeat the card's status, or "alive".
- Do not set status back to alive after dead or missing unless the passage clearly brings that person back.
- A death, a relative's death, a wound, a new title, allegiance, or a decision that changes their life is a fact and must be recorded.
- stat_delta is an integer from -2 to 2 and only for a clear consequence in the passage (a wound, training, a hard choice). Otherwise every value is 0.
- facts are short and new. Do not repeat a fact already on the card.
- real is true only when the passage treats them as a living public figure who is themselves, never for a fictional character.
- If nobody qualifies, return empty arrays.
Output ONLY the JSON object."""

_WORLD_KEYS = (
    ("location", "places"),
    ("faction", "factions"),
    ("item", "items"),
)


def protagonist_names(plot: str) -> set[str]:
    """Nomes do protagonista no Plot Essentials. Não viram NPC."""
    found: set[str] = set()
    for match in re.finditer(r"(?i)\bname:\s*([^\]\n;]+)", plot or ""):
        name = " ".join(match.group(1).split()).strip()
        if name:
            found.add(name.casefold())
    return found


def _is_player(name: str, protagonists: set[str]) -> bool:
    folded = " ".join((name or "").split()).casefold()
    if not folded or folded in {"you", "player", "the player"}:
        return True
    return folded in protagonists


def _match_card(cards: list[dict], item: dict) -> dict | None:
    names = []
    for raw in (item.get("existing_title"), item.get("name"), *(item.get("aliases") or [])):
        folded = " ".join(str(raw or "").split()).casefold()
        if len(folded) >= 3:
            names.append(folded)
    if not names:
        return None
    for card in cards:
        title = " ".join((card.get("title") or "").split()).casefold()
        triggers = [" ".join(str(t).split()).casefold() for t in (card.get("triggers") or [])]
        if title and title in names:
            return card
        if any(name in triggers for name in names):
            return card
    return None


def _facts(item: dict) -> list[str]:
    raw = item.get("facts") or []
    if isinstance(raw, str):
        raw = [raw]
    out = []
    for fact in raw:
        text = " ".join(str(fact).split()).strip()
        if len(text) >= 8:
            out.append(text[:400])
    return out


def _append_unique(base: str, facts: list[str], limit: int = 1600) -> str:
    out = (base or "").strip()
    for fact in facts:
        if fact.casefold() in out.casefold():
            continue
        out = f"{out} {fact}".strip()
    return out[:limit]


_RETURN_WORDS = (
    "return", "returned", "returns", "alive again", "wakes", "woke", "awake",
    "revived", "resurrect", "brought back", "not dead", "ressuscit", "voltou",
)


def _look_locked(card: dict, sheet: dict) -> bool:
    """Rosto já gravado não aceita o visual que a prosa inventou neste turno."""
    if any(sheet.get(key) for key in ("appearance", "body", "clothing")):
        return True
    if " ".join(str(card.get("portrait_prompt") or "").split()):
        return True
    return bool(card.get("portrait_image_id"))


def _status_allowed(previous: str, status: str, facts: list[str]) -> bool:
    """Um 'alive' solto não ressuscita. Voltar à vida precisa estar no fato."""
    if previous in ("dead", "missing") and status == "alive":
        blob = " ".join(facts).casefold()
        return any(word in blob for word in _RETURN_WORDS)
    return True


def _stat_delta(raw: Any) -> dict[str, int]:
    if not isinstance(raw, dict):
        return {}
    delta = {}
    for key in npc_assets.STATS_KEYS:
        try:
            value = int(raw.get(key) or 0)
        except (TypeError, ValueError):
            continue
        value = max(-2, min(2, value))
        if value:
            delta[key] = value
    return delta


def _apply_stats(existing: dict, delta: dict[str, int]) -> dict | None:
    if not delta:
        return existing or None
    stats = dict(existing or {})
    for key, value in delta.items():
        try:
            current = int(stats.get(key, 10))
        except (TypeError, ValueError):
            current = 10
        stats[key] = max(1, min(20, current + value))
    return stats


def _stamp_identity(sheet: dict, item: dict) -> None:
    """Copia pessoa real ou menor já confirmados. Não apaga uma marca antiga."""
    if item.get("minor") is True:
        sheet["minor"] = True
        return
    if item.get("real_person") is not True:
        return
    sheet["real_person"] = True
    name = " ".join(str(item.get("real_name") or item.get("name") or "").split())
    wiki = " ".join(str(item.get("wiki_title") or "").split())
    if name:
        sheet["real_name"] = name[:120]
    if wiki:
        sheet["wiki_title"] = wiki[:160]


def _confirm_real_people(payload: dict, transport: Any) -> None:
    """Wikipedia confirma `real` antes de gravar. Falha de rede não marca."""
    items = payload.get("characters")
    if not isinstance(items, list):
        return
    for item in items:
        if not isinstance(item, dict) or item.get("real") is not True:
            continue
        name = " ".join(str(item.get("name") or "").split())
        if len(name) < 2:
            continue
        try:
            page = likeness.lookup_person(name, transport=transport)
        except Exception:
            log.warning("likeness: wikipedia falhou para %s", name, exc_info=True)
            continue
        if page is None or page.fictional or page.disambiguation:
            continue
        if page.minor:
            item["minor"] = True
            continue
        if page.is_real_adult:
            item["real_person"] = True
            item["real_name"] = page.title
            item["wiki_title"] = page.title


def apply_character(conn, adventure_id: str, cards: list[dict], item: dict,
                    protagonists: set[str]) -> str | None:
    """Grava um item do passe. Devolve 'created', 'updated' ou None."""
    if not isinstance(item, dict):
        return None
    name = " ".join(str(item.get("name") or "").split()).strip()
    if len(name) < 2 or _is_player(name, protagonists):
        return None
    card = _match_card(cards, item)
    if card is not None:
        npc_assets.ensure_portrait_prompt(conn, card)

    facts = _facts(item)
    summary = " ".join(str(item.get("summary") or "").split()).strip()
    appearance = " ".join(str(item.get("appearance") or "").split()).strip()
    clothing = " ".join(str(item.get("clothing") or "").split()).strip()
    personality = " ".join(str(item.get("personality") or "").split()).strip()
    status = str(item.get("status") or "").strip().lower()
    if status not in _STATUSES:
        status = ""
    look_changed = bool(item.get("look_changed"))
    delta = _stat_delta(item.get("stat_delta"))

    if card is None:
        if not (summary or facts or appearance):
            return None
        sheet: dict[str, Any] = {}
        if appearance:
            sheet["appearance"] = appearance[:500]
        if clothing:
            sheet["clothing"] = clothing[:300]
        if personality:
            sheet["personality"] = personality[:300]
        if status:
            sheet["status"] = status
        notes = _append_unique("", facts)
        if notes:
            sheet["notes"] = notes
        stats = _apply_stats({}, delta)
        if stats:
            sheet["stats"] = stats
        _stamp_identity(sheet, item)
        triggers = []
        for raw in (name, *(item.get("aliases") or [])):
            trigger = " ".join(str(raw).split()).strip()
            if len(trigger) >= 3 and trigger not in triggers and not _is_player(trigger, protagonists):
                triggers.append(trigger)
        created = db.insert_card(
            conn, adventure_id, title=name[:80], type="character",
            triggers=triggers[:8], entry=_append_unique(summary, facts)[:1600],
            sheet=json.dumps(sheet, ensure_ascii=False) if sheet else "",
        )
        cards.append(created)
        if npc_assets.has_visual(created):
            jobs.enqueue_portrait(conn, adventure_id, created["id"])
        return "created"

    sheet = npc_assets.parse_sheet(card.get("sheet") or "")
    # Roupa e rosto já gravados vencem a prosa. O modelo descreve a princesa
    # de vestido mesmo com a ficha em armadura; isso não reescreve o cânone.
    # Retrato existente também trava: a prosa não inventa outra cara.
    # look_changed cobre cicatriz, maldição ou uma troca de visual explícita.
    locked = _look_locked(card, sheet)
    if appearance and (look_changed or not locked):
        sheet["appearance"] = appearance[:500]
    if clothing and (look_changed or not locked):
        sheet["clothing"] = clothing[:300]
    portrait_prompt = ""
    if look_changed and (sheet.get("appearance") or sheet.get("clothing")):
        portrait_prompt = ", ".join(
            part for part in (sheet.get("appearance"), sheet.get("body"), sheet.get("clothing"))
            if part
        )[:500]
    if personality and personality.casefold() not in (sheet.get("personality") or "").casefold():
        sheet["personality"] = _append_unique(sheet.get("personality") or "", [personality], 400)
    if status and _status_allowed(sheet.get("status") or "", status, facts):
        sheet["status"] = status
    if facts:
        sheet["notes"] = _append_unique(sheet.get("notes") or "", facts, 800)
    stats = _apply_stats(sheet.get("stats") or {}, delta)
    if stats:
        sheet["stats"] = stats
    _stamp_identity(sheet, item)
    entry = card.get("entry") or ""
    if not entry and summary:
        entry = summary
    entry = _append_unique(entry, facts)
    triggers = list(card.get("triggers") or [])
    for raw in (name, *(item.get("aliases") or [])):
        trigger = " ".join(str(raw).split()).strip()
        if len(trigger) >= 3 and trigger not in triggers:
            triggers.append(trigger)
    fields: dict[str, Any] = {
        "entry": entry,
        "triggers": triggers[:8],
        "sheet": json.dumps(sheet, ensure_ascii=False),
    }
    if portrait_prompt:
        fields["portrait_prompt"] = portrait_prompt
    updated = db.update_card(conn, adventure_id, card["id"], fields)
    if updated:
        card.update(updated)
    if not card.get("portrait_image_id") and npc_assets.has_visual(updated or card):
        jobs.enqueue_portrait(conn, adventure_id, card["id"])
    return "updated"


def _world_triggers(item: dict, protagonists: set[str]) -> list[str]:
    triggers = []
    for raw in (item.get("name"), *(item.get("aliases") or [])):
        trigger = " ".join(str(raw or "").split()).strip()
        if len(trigger) >= 3 and trigger not in triggers and not _is_player(trigger, protagonists):
            triggers.append(trigger)
    return triggers[:8]


def apply_world(conn, adventure_id: str, cards: list[dict], item: dict,
                type_: str, protagonists: set[str]) -> str | None:
    """Card de lugar, facção ou item. Só título, gatilhos e entry — sem ficha inventada."""
    if not isinstance(item, dict) or type_ not in ("location", "faction", "item"):
        return None
    name = " ".join(str(item.get("name") or "").split()).strip()
    if len(name) < 2 or _is_player(name, protagonists):
        return None
    facts = _facts(item)
    summary = " ".join(str(item.get("summary") or "").split()).strip()
    card = _match_card(cards, item)
    if card is None:
        if not (summary or facts):
            return None
        created = db.insert_card(
            conn, adventure_id, title=name[:80], type=type_,
            triggers=_world_triggers(item, protagonists),
            entry=_append_unique(summary, facts)[:1600],
        )
        cards.append(created)
        return "created"
    entry = _append_unique(card.get("entry") or summary, facts)
    triggers = list(card.get("triggers") or [])
    for trigger in _world_triggers(item, protagonists):
        if trigger not in triggers:
            triggers.append(trigger)
    updated = db.update_card(conn, adventure_id, card["id"], {
        "entry": entry[:1600],
        "triggers": triggers[:8],
    })
    if updated:
        card.update(updated)
    return "updated"


def _incoming_plot(payload: dict, plot: str) -> str:
    """Frase de plot só quando a aventura ainda não tem um."""
    if (plot or "").strip() or not isinstance(payload, dict):
        return ""
    line = " ".join(str(payload.get("plot_essentials") or "").split())
    return line[:800] if len(line) >= 12 else ""


def apply_chronicle(conn, adventure_id: str, payload: dict, plot: str) -> dict:
    """Aplica o JSON do passe. Não chama o LLM."""
    if not isinstance(payload, dict):
        return {"created": 0, "updated": 0}
    incoming = _incoming_plot(payload, plot)
    protagonists = protagonist_names(plot) | protagonist_names(incoming)
    all_cards = db.list_cards(conn, adventure_id)
    created = updated = 0

    characters = [card for card in all_cards if card.get("type") == "character"]
    items = payload.get("characters")
    if isinstance(items, list):
        for item in items[:8]:
            kind = apply_character(conn, adventure_id, characters, item, protagonists)
            if kind == "created":
                created += 1
            elif kind == "updated":
                updated += 1

    for type_, key in _WORLD_KEYS:
        pool = [card for card in all_cards if card.get("type") == type_]
        world_items = payload.get(key)
        if not isinstance(world_items, list):
            continue
        for item in world_items[:8]:
            kind = apply_world(conn, adventure_id, pool, item, type_, protagonists)
            if kind == "created":
                created += 1
            elif kind == "updated":
                updated += 1

    if incoming:
        db.update_adventure_memory(conn, adventure_id, plot_essentials=incoming)
    return {"created": created, "updated": updated}


def _turn_has_entity_signal(recent_text: str, cards: list[dict], plot: str) -> bool:
    """Evita gastar chamada LLM quando o trecho recente não cita nenhum card
    existente, não tem diálogo nem nomes próprios novos, e o plot já existe."""
    if not (plot or "").strip():
        return True
    folded = recent_text.casefold()
    for card in cards:
        title = (card.get("title") or "").strip().casefold()
        if title and len(title) >= 3 and title in folded:
            return True
        for trig in card.get("triggers") or []:
            t = str(trig or "").strip().casefold()
            if len(t) >= 3 and t in folded:
                return True
    # Diálogo (travessão ou aspas) ou nome próprio capitalizado no meio de frase
    if any(ch in recent_text for ch in ("—", "–", '"', "“", "«")):
        return True
    if re.search(r"(?<!^)(?<![.!?]\s)\b[A-ZÀ-Ý][a-zà-ÿ]{2,}\b", recent_text):
        return True
    return False


def update_from_turn(adventure_id: str, transport: Any = None) -> dict:
    """Lê o trecho recente, pede o JSON e grava. Falha de JSON não propaga."""
    with db.session() as conn:
        adventure = db.get_adventure(conn, adventure_id)
        if not adventure:
            return {"created": 0, "updated": 0}
        actions = db.list_actions(conn, adventure_id)
        all_cards = db.list_cards(conn, adventure_id)
        memories = db.list_memories(conn, adventure_id)
        cards = [card for card in all_cards if card.get("type") == "character"]
        llm_cfg = settings_store.effective_llm_settings(conn)
        plot = adventure.get("plot_essentials") or ""
        assessment = world_facts.assess(adventure, actions, all_cards, memories)
    non_see = [action["text"] for action in actions if action["type"] != "see" and action.get("text")]
    _canon, attempt = world_facts.chronicle_passage(actions)
    text = (assessment.canon_blob or _canon).strip()
    if not text and not attempt:
        return {"created": 0, "updated": 0}
    # Gate barato: turno sem sinal de entidade não chama o LLM.
    latest_turn = "\n".join(non_see[-2:]).strip()
    if not _turn_has_entity_signal(latest_turn, cards, plot):
        return {"created": 0, "updated": 0}
    roster = []
    for card in cards:
        sheet = npc_assets.parse_sheet(card.get("sheet") or "")
        roster.append({
            "title": card.get("title") or "",
            "entry": (card.get("entry") or "")[:300],
            "status": sheet.get("status") or "",
            "appearance": (sheet.get("appearance") or "")[:200],
            "notes": (sheet.get("notes") or "")[:240],
            "stats": sheet.get("stats") or {},
        })
    user = (
        f"{assessment.facts_line}\n\n"
        f"Protagonist (do not create a card): {plot[:500]}\n\n"
        f"Existing characters:\n{json.dumps(roster, ensure_ascii=False)[:4000]}\n\n"
        f"Canon passage (only this establishes facts):\n{text or '(none)'}\n\n"
        f"Player attempt (NOT canon):\n{attempt or '(none)'}"
    )
    content = llm_client.chat_completion(
        llm_cfg,
        [{"role": "system", "content": _SYSTEM}, {"role": "user", "content": user}],
        role="chronicle", temperature=0.2, max_tokens=550, transport=transport,
    )
    try:
        payload = npc_assets._extract_json(content)
    except (ValueError, TypeError) as exc:
        log.warning("npc_chronicle: JSON ilegível (%s)", exc)
        return {"created": 0, "updated": 0}
    payload = world_facts.filter_chronicle_payload(payload, assessment)
    _confirm_real_people(payload, transport)
    with db.session() as conn:
        result = apply_chronicle(conn, adventure_id, payload, plot)
    log.info("npc_chronicle: aventura %s → %s", adventure_id, result)
    return result

