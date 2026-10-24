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

import copy
import json
import logging
import re
from typing import Any

from app import body_state, db, events, jobs, likeness, llm_client, npc_assets, settings_store, ui_runtime, world_facts

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
  "bodies": [
    {
      "target": "player or exact character name",
      "card_id": "exact id when known, else empty",
      "pain": 0,
      "expression": "short English expression, or empty",
      "current_outfit": "what they are wearing now, or empty",
      "conditions": [
        {"op": "add|update|heal|remove", "id": "existing condition id, or empty for new",
         "kind": "wound|cut|bruise|burn|fracture|scar|amputation|tattoo|mark|illness|other",
         "location": "left cheek", "severity": 1,
         "description": "one short sentence in the language of the story",
         "visual_en": "short English visual phrase for image prompts",
         "permanent": false, "visibility": "portrait|scene|hidden",
         "portrait_visible": false, "scene_visible": true}
      ]
    }
  ],
  "ui": {
    "hud": [{"label": "Local", "value": "short fact", "tone": "neutral|info|good|warn|danger"}],
    "actions": [{"label": "short button", "type": "do|say|story",
                 "text": "full action in the player's voice"}],
    "stats": [{"widget": "widget id", "name": "exact stat name", "value": 0}],
    "inventory": [{"widget": "widget id", "op": "add|remove|set", "name": "item", "qty": 1}],
    "fields": [{"widget": "widget id", "name": "exact field name", "value": "new value"}]
  },
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
- bodies is the ONLY authority on injuries, pain and expression. Read the canon
  passage and decide whether each person's body changed. Do not add pain or a
  wound only because someone was attacked; only if the passage shows it landed.
- `target` may be "player" or the exact card title. The player card is not in
  `characters`; use `bodies` for the player.
- pain is 0-10. Keep old pain unless the passage changes it; no automatic decay.
- conditions are persistent: add a new one, update an existing id, or heal/remove.
  Never duplicate a wound that already exists in the roster. Use the roster's
  condition ids when updating/healing.
- portrait_visible is true only when the injury/change is visible in a bust or
  upper-body portrait (face, neck, hands, exposed chest/arms). A wound under
  armor stays scene_visible=true and portrait_visible=false.
- scene_visible is true when a full-body/scene image would show it.
- visibility hidden means neither image shows it.
- visual_en must be a concise English phrase for the image model, e.g.
  "small fresh cut on the left cheek", "bruised knuckles", "bandaged right forearm".
- expression is a short English phrase for the face, e.g. "guarded", "furious",
  "relieved"; use it only when the passage justifies it.
- current_outfit is what they are wearing NOW after the passage, only if it
  changed. Otherwise empty; the sheet keeps the previous outfit.
- If nobody's body/expression/outfit changed, return an empty bodies array.
- ui is the live game panel. Use the "Game UI" block to get exact widget ids,
  stat/field names, limits and current values. Never invent a widget.
- hud: 2-5 short facts the player must always see now (place, time, goal,
  danger, weather). Use the language of the story. Only facts this passage or
  the established setup supports; omit a label instead of guessing.
- actions: 2-4 things the player could do right now, grounded in the scene.
  Short labels and full first-person text. They are options, never outcomes:
  do not narrate the result and do not decide how an NPC reacts.
- stats: absolute new values, only when the passage shows a clear consequence
  for that exact stat. Respect min/max from the Game UI block.
- inventory: only items actually gained, lost, used up, broken or given.
- fields: only values the passage established (objective, title, rank...).
  Use exact field names from the Game UI block; do not add new fields.
- If the UI did not change, return empty arrays. Always include the ui object.
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
        sheet["current_outfit"] = clothing[:240]
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
    elif updated:
        try:
            from app import portrait_sync
            portrait_sync.sync_card(conn, adventure_id, card["id"])
        except Exception:
            log.warning("npc_chronicle: variante de retrato falhou", exc_info=True)
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


def _snapshot_for_undo(conn, source_action_id: int | None, snapshots: dict[str, dict]) -> None:
    if not source_action_id or not snapshots:
        return
    current = db.get_action_rules(conn, source_action_id) or {}
    merged = current.get("body_before") if isinstance(current.get("body_before"), dict) else {}
    for card_id, snap in snapshots.items():
        merged.setdefault(card_id, snap)
    db.merge_action_rules(conn, source_action_id, {"body_before": merged})


def _apply_bodies(conn, adventure_id: str, payload: dict, *, source_action_id, source_seq) -> int:
    items = payload.get("bodies")
    if not isinstance(items, list):
        return 0
    cards = db.list_cards(conn, adventure_id)
    by_id = {card.get("id"): card for card in cards}
    by_name = {(card.get("title") or "").casefold(): card for card in cards if card.get("title")}
    player_cards = [card for card in cards if card.get("player_username")]
    snapshots: dict[str, dict] = {}
    changed = 0
    for raw in items[:24]:
        if not isinstance(raw, dict):
            continue
        target = " ".join(str(raw.get("target") or "").split())
        card = by_id.get(raw.get("card_id")) or by_name.get(target.casefold())
        if card is None and target.casefold() in ("player", "jogador", "you"):
            card = player_cards[0] if len(player_cards) == 1 else None
        if card is None:
            continue
        sheet = npc_assets.parse_sheet(card.get("sheet") or "")
        before = body_state.snapshot(sheet)
        updated = body_state.apply_to_sheet(
            sheet, raw, source_action_id=source_action_id, source_seq=source_seq)
        if updated == sheet:
            continue
        if card["id"] not in snapshots:
            snapshots[card["id"]] = before
        db.update_card(conn, adventure_id, card["id"], {
            "sheet": json.dumps(updated, ensure_ascii=False),
        })
        changed += 1
        try:
            from app import portrait_sync
            portrait_sync.sync_card(conn, adventure_id, card["id"])
        except Exception:
            log.warning("npc_chronicle: retrato não sincronizado", exc_info=True)
    _snapshot_for_undo(conn, source_action_id, snapshots)
    return changed


def _apply_ui(conn, adventure_id: str, payload: dict, *,
              source_action_id: int | None,
              source_seq: int | None) -> bool:
    """Aplica HUD/ações/valores do painel. Guarda o antes para o undo."""
    update = payload.get("ui")
    if not isinstance(update, dict):
        return False
    adventure = db.get_adventure(conn, adventure_id) or {}
    profile = adventure.get("ui_profile") if isinstance(adventure.get("ui_profile"), dict) else None
    state = adventure.get("ui_state") if isinstance(adventure.get("ui_state"), dict) else {}
    if profile is None and not state:
        return False
    updated = ui_runtime.apply(profile, state, update, seq=source_seq)
    if updated == state:
        return False
    if source_action_id:
        rules = db.get_action_rules(conn, source_action_id) or {}
        if "ui_before" not in rules:
            db.merge_action_rules(conn, source_action_id,
                                  {"ui_before": copy.deepcopy(state)})
    db.update_adventure_ui_state(conn, adventure_id, updated)
    return True


def apply_chronicle(conn, adventure_id: str, payload: dict, plot: str, *,
                    source_action_id: int | None = None,
                    source_seq: int | None = None) -> dict:
    """Aplica o JSON do passe. Não chama o LLM."""
    if not isinstance(payload, dict):
        return {"created": 0, "updated": 0, "bodies": 0}
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

    body_count = _apply_bodies(
        conn, adventure_id, payload,
        source_action_id=source_action_id, source_seq=source_seq,
    )
    _apply_ui(
        conn, adventure_id, payload,
        source_action_id=source_action_id, source_seq=source_seq,
    )
    if incoming:
        db.update_adventure_memory(conn, adventure_id, plot_essentials=incoming)
    return {"created": created, "updated": updated, "bodies": body_count}


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
        ui_profile = adventure.get("ui_profile") if isinstance(adventure.get("ui_profile"), dict) else None
        ui_state = adventure.get("ui_state") if isinstance(adventure.get("ui_state"), dict) else {}
        assessment = world_facts.assess(adventure, actions, all_cards, memories)
    non_see = [action["text"] for action in actions if action["type"] != "see" and action.get("text")]
    _canon, attempt = world_facts.chronicle_passage(actions)
    text = (assessment.canon_blob or _canon).strip()
    if not text and not attempt:
        return {"created": 0, "updated": 0}
    # Gate barato: turno sem sinal de entidade não chama o LLM. Com painel
    # dinâmico o passe roda sempre, porque o HUD e as ações mudam com a cena.
    latest_turn = "\n".join(non_see[-2:]).strip()
    ui_active = bool(
        (ui_profile or {}).get("widgets")
        or (ui_profile or {}).get("hud")
        or (ui_profile or {}).get("actions")
        or ui_state.get(ui_runtime.HUD_KEY)
        or ui_state.get(ui_runtime.ACTIONS_KEY)
    )
    if not ui_active and not _turn_has_entity_signal(latest_turn, cards, plot):
        return {"created": 0, "updated": 0}
    roster = []
    for card in cards:
        sheet = npc_assets.parse_sheet(card.get("sheet") or "")
        state = body_state.body_state_of(sheet)
        roster.append({
            "card_id": card.get("id") or "",
            "title": card.get("title") or "",
            "entry": (card.get("entry") or "")[:300],
            "status": sheet.get("status") or "",
            "appearance": (sheet.get("appearance") or "")[:200],
            "current_outfit": (sheet.get("current_outfit") or sheet.get("clothing") or "")[:160],
            "expression": (sheet.get("expression") or "")[:80],
            "pain": state.get("pain"),
            "conditions": state.get("conditions") or [],
            "notes": (sheet.get("notes") or "")[:240],
            "stats": sheet.get("stats") or {},
        })
    players = []
    for card in all_cards:
        if not card.get("player_username"):
            continue
        sheet = npc_assets.parse_sheet(card.get("sheet") or "")
        state = body_state.body_state_of(sheet)
        players.append({
            "card_id": card.get("id") or "",
            "title": card.get("title") or card.get("player_username") or "Jogador",
            "player_username": card.get("player_username") or "",
            "current_outfit": (sheet.get("current_outfit") or sheet.get("clothing") or "")[:160],
            "expression": (sheet.get("expression") or "")[:80],
            "pain": state.get("pain"),
            "conditions": state.get("conditions") or [],
        })
    user = (
        f"{assessment.facts_line}\n\n"
        f"Protagonist (do not create a card): {plot[:800]}\n\n"
        f"Existing characters (roster — use these exact card ids/condition ids):\n"
        f"{json.dumps(roster, ensure_ascii=False)[:7000]}\n\n"
        f"Player characters (use bodies target='player' or exact title):\n"
        f"{json.dumps(players, ensure_ascii=False)[:2500]}\n\n"
        f"Canon passage (only this establishes facts):\n{text or '(none)'}\n\n"
        f"Player attempt (NOT canon):\n{attempt or '(none)'}"
    )
    ui_context = ui_runtime.prompt_context(ui_profile, ui_state)
    if ui_context:
        user += (
            "\n\nGame UI (exact widget ids, names, limits and current values; "
            "only report changes the passage supports):\n" + ui_context
        )
    content = llm_client.chat_completion(
        llm_cfg,
        [{"role": "system", "content": _SYSTEM}, {"role": "user", "content": user}],
        role="chronicle", temperature=0.2, max_tokens=2100, transport=transport,
        response_format={"type": "json_schema", "json_schema": {
            "name": "npc_chronicle", "strict": True,
            "schema": {
                "type": "object",
                "required": ["plot_essentials", "characters", "bodies", "ui", "places", "factions", "items"],
                "properties": {
                    "plot_essentials": {"type": "string"},
                    "characters": {"type": "array", "items": {"type": "object"}},
                    "bodies": {"type": "array", "items": {"type": "object"}},
                    "ui": {"type": "object"},
                    "places": {"type": "array", "items": {"type": "object"}},
                    "factions": {"type": "array", "items": {"type": "object"}},
                    "items": {"type": "array", "items": {"type": "object"}},
                },
            },
        }},
    )
    try:
        payload = npc_assets._extract_json(content)
    except (ValueError, TypeError) as exc:
        log.warning("npc_chronicle: JSON ilegível (%s)", exc)
        return {"created": 0, "updated": 0}
    payload = world_facts.filter_chronicle_payload(payload, assessment)
    _confirm_real_people(payload, transport)
    latest = next((action for action in reversed(actions) if action.get("type") == "continue"), None)
    with db.session() as conn:
        result = apply_chronicle(
            conn, adventure_id, payload, plot,
            source_action_id=(latest or {}).get("id"),
            source_seq=(latest or {}).get("seq"),
        )
        updated = db.get_adventure(conn, adventure_id) or {}
        ui_after = updated.get("ui_state") or {}
    if ui_profile is not None or ui_after:
        events.publish(adventure_id, "ui_changed", {
            "ui_state": ui_after,
            "seq": (latest or {}).get("seq"),
        })
    log.info("npc_chronicle: aventura %s → %s", adventure_id, result)
    return result

