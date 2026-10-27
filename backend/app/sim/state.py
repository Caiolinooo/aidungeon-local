"""Estado simulado da aventura: actors, inimigos e o gancho do Modo Vida."""

from __future__ import annotations

import copy
import unicodedata

_ABILITY_ORDER = ("str", "dex", "con", "int", "wis", "cha")


def empty() -> dict:
    return {
        "v": 1,
        "pack": "",
        "actors": {},
        "foes": {},
        "life": None,
        "rng": None,
        "quests": [],
        "triggers": [],
    }


def load(adventure: dict) -> dict:
    """`sim_state` da aventura por cima de `empty()`. Chaves desconhecidas permanecem."""
    base = empty()
    raw = (adventure or {}).get("sim_state")
    if not isinstance(raw, dict):
        return base
    for key, value in raw.items():
        base[key] = value
    if not isinstance(base.get("actors"), dict):
        base["actors"] = {}
    if not isinstance(base.get("foes"), dict):
        base["foes"] = {}
    if not isinstance(base.get("pack"), str):
        base["pack"] = ""
    if not isinstance(base.get("quests"), list):
        base["quests"] = []
    if not isinstance(base.get("triggers"), list):
        base["triggers"] = []
    if base.get("rng") is not None and not isinstance(base.get("rng"), dict):
        base["rng"] = None
    return base


def fold(text: str) -> str:
    """Minúsculas sem acento. Mesma regra de `stats._fold` (privado lá)."""
    decomposed = unicodedata.normalize("NFKD", text or "")
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch)).lower()


def actor_key(user: str) -> str:
    return user or "_solo"


def snapshot(state: dict) -> dict:
    return copy.deepcopy(state)


def _actor_line(actor: dict, currency: str) -> str:
    name = (actor.get("name") or "Jogador").strip() or "Jogador"
    bits: list[str] = []
    if actor.get("hp") is not None and actor.get("hp_max") is not None:
        bits.append(f"PV {actor['hp']}/{actor['hp_max']}")
    if actor.get("ac") is not None:
        bits.append(f"CA {actor['ac']}")
    bits.append(f"nível {int(actor.get('level') or 1)} (XP {int(actor.get('xp') or 0)})")
    resources = actor.get("resources") if isinstance(actor.get("resources"), dict) else {}
    if resources.get("gold") is not None:
        bits.append(f"{currency} {int(resources.get('gold') or 0)}")
    line = f"{name}: " + ", ".join(bits) + "."
    inventory = actor.get("inventory") if isinstance(actor.get("inventory"), list) else []
    if inventory:
        items = ", ".join(
            f"{row.get('name')}×{int(row.get('qty') or 1)}"
            for row in inventory if isinstance(row, dict) and row.get("name")
        )
        if items:
            line += f" Inventário: {items}."
    if actor.get("down"):
        line += " Condição: caído."
    san = resources.get("san")
    if san is not None:
        line += f" SAN {int(san)}."
    return line


def render_block(adventure: dict, conn, actions: list[dict]) -> str:
    """Texto pt-BR para o <lore>. Vazio se não há pack nem Modo Vida."""
    state = load(adventure)
    life = state.get("life") if isinstance(state.get("life"), dict) else None
    life_on = bool(life and life.get("enabled"))
    pack_id = state.get("pack") or ""
    if not pack_id and not life_on:
        return ""
    from app.sim import packs
    pack = packs.load_pack(pack_id) if pack_id else None
    currency = (pack or {}).get("currency") or "Ouro"
    lines = ["[Estado simulado]"]
    actors = state.get("actors") if isinstance(state.get("actors"), dict) else {}
    for actor in actors.values():
        if isinstance(actor, dict):
            lines.append(_actor_line(actor, currency))
    foes = state.get("foes") if isinstance(state.get("foes"), dict) else {}
    alive = [foe for foe in foes.values() if isinstance(foe, dict) and not foe.get("dead")]
    if alive:
        bits = ", ".join(f"{foe.get('name')} {foe.get('hp')}/{foe.get('hp_max')} PV" for foe in alive)
        lines.append(f"Inimigos: {bits}.")
    if life_on:
        from app import db, npc_bonds
        from app.sim import life as life_mod
        cards = db.list_cards(conn, adventure.get("id") or "")
        recent = "\n".join((action.get("text") or "") for action in (actions or [])[-3:])
        scene = npc_bonds.characters_in_text(cards, recent)[:4]
        block = life_mod.render(state, "", scene)
        if block:
            lines.append(block)
    return "\n".join(lines)


def abilities_of(raw: dict | None) -> dict:
    """As seis habilidades d20, na ordem do pack, sem extras."""
    source = raw if isinstance(raw, dict) else {}
    return {key: source[key] for key in _ABILITY_ORDER if key in source}
