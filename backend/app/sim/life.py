"""Relógio, necessidades, rotina e relação entre NPCs. Sem LLM."""

from __future__ import annotations

NEEDS = ("fome", "energia", "higiene", "social", "diversao", "bexiga")
NEED_START = 80
NEED_LABELS_LOW = {
    "fome": "faminto",
    "energia": "exausto",
    "higiene": "sujo",
    "social": "solitário",
    "diversao": "entediado",
    "bexiga": "apertado",
}
NEED_NAMES = {
    "fome": "fome",
    "energia": "energia",
    "higiene": "higiene",
    "social": "social",
    "diversao": "diversão",
    "bexiga": "bexiga",
}
SEASONS = ("Primavera", "Verão", "Outono", "Inverno")
WEEKDAYS = ("segunda", "terça", "quarta", "quinta", "sexta", "sábado", "domingo")

BASE_RATES = {
    "fome": -4.0,
    "energia": -3.0,
    "higiene": -2.0,
    "social": -2.0,
    "diversao": -2.0,
    "bexiga": -5.0,
}
ACTIVITY_RATES = {
    "eat": {"fome": 100.0, "bexiga": -8.0},
    "sleep": {"energia": 12.5, "fome": -2.0, "bexiga": -2.0},
    "rest": {"energia": 6.0},
    "wash": {"higiene": 120.0},
    "social": {"social": 40.0, "diversao": 10.0},
    "fun": {"diversao": 40.0},
    "work": {"energia": -6.0, "fome": -4.0, "diversao": -4.0},
    "bladder": {"bexiga": 600.0},
    "other": {},
}
_TOGETHER = ("social", "eat", "fun")
_VERB = {
    "eat": "comendo",
    "sleep": "dormindo",
    "rest": "descansando",
    "wash": "se lavando",
    "social": "socializando",
    "fun": "se divertindo",
    "work": "trabalhando",
    "bladder": "no banheiro",
    "other": "ocupado",
}

DEFAULT_ROUTINE: dict[int, tuple[str, str]] = {}
for _hour in range(0, 6):
    DEFAULT_ROUTINE[_hour] = ("sleep", "casa")
DEFAULT_ROUTINE[6] = ("wash", "casa")
DEFAULT_ROUTINE[7] = ("eat", "casa")
for _hour in range(8, 12):
    DEFAULT_ROUTINE[_hour] = ("work", "trabalho")
DEFAULT_ROUTINE[12] = ("eat", "taverna")
for _hour in range(13, 18):
    DEFAULT_ROUTINE[_hour] = ("work", "trabalho")
DEFAULT_ROUTINE[18] = ("social", "praça")
DEFAULT_ROUTINE[19] = ("eat", "casa")
for _hour in range(20, 22):
    DEFAULT_ROUTINE[_hour] = ("fun", "taverna")
for _hour in range(22, 24):
    DEFAULT_ROUTINE[_hour] = ("sleep", "casa")


def enable(state: dict, wage_per_hour: int = 5) -> None:
    state["life"] = {
        "enabled": True,
        "day": 1,
        "minute": 480,
        "wage_per_hour": int(wage_per_hour),
        "npcs": {},
        "relations": {},
        "relations_day": {},
    }


def disable(state: dict) -> None:
    life = state.get("life")
    if not isinstance(life, dict):
        life = {
            "enabled": False, "day": 1, "minute": 480, "wage_per_hour": 5,
            "npcs": {}, "relations": {}, "relations_day": {},
        }
        state["life"] = life
    life["enabled"] = False


def penalty(actor: dict, pack: dict | None = None) -> int:
    """Necessidades abaixo de 15, no máximo 2. Sem needs, zero."""
    needs = (actor or {}).get("needs")
    if not isinstance(needs, dict):
        return 0
    low = sum(1 for key in NEEDS if int(needs.get(key, 100) or 0) < 15)
    return min(2, low)


def mood(needs: dict | None) -> str:
    if not isinstance(needs, dict) or not needs:
        return "Ok"
    values = [int(needs.get(key, 0) or 0) for key in NEEDS]
    average = sum(values) / len(NEEDS)
    if average >= 75:
        return "Ótimo"
    if average >= 55:
        return "Bem"
    if average >= 35:
        return "Ok"
    if average >= 15:
        return "Mal"
    return "Péssimo"


def relation_label(value: int) -> str:
    if value < -50:
        return "inimigos"
    if value < -10:
        return "desafeto"
    if value < 10:
        return "conhecidos"
    if value < 50:
        return "amigos"
    return "próximos"


def clock(minute: int) -> str:
    minute = int(minute) % 1440
    return f"{minute // 60:02d}:{minute % 60:02d}"


def weekday_season(day: int) -> tuple[str, str]:
    day = max(1, int(day))
    return WEEKDAYS[(day - 1) % 7], SEASONS[((day - 1) // 30) % 4]


def _rates(activity: str) -> dict[str, float]:
    rates = dict(BASE_RATES)
    replacement = ACTIVITY_RATES.get(activity) or ACTIVITY_RATES["other"]
    rates.update(replacement)
    return rates


def _blank_needs() -> dict[str, int]:
    return {key: NEED_START for key in NEEDS}


def _ensure_player_needs(actor: dict | None) -> dict[str, float] | None:
    if not isinstance(actor, dict):
        return None
    needs = actor.get("needs")
    if not isinstance(needs, dict):
        needs = _blank_needs()
        actor["needs"] = needs
    return {key: float(needs.get(key, NEED_START) or 0) for key in NEEDS}


def _sync_npcs(life: dict, cards: list[dict]) -> dict:
    bucket = life.setdefault("npcs", {})
    if not isinstance(bucket, dict):
        bucket = {}
        life["npcs"] = bucket
    for card in cards or []:
        if (card.get("type") or "") != "character":
            continue
        if card.get("player_username"):
            continue
        card_id = card.get("id")
        if not card_id:
            continue
        current = bucket.get(card_id)
        if not isinstance(current, dict):
            current = {
                "name": card.get("title") or "NPC",
                "needs": _blank_needs(),
                "activity": "",
                "place": "",
            }
            bucket[card_id] = current
        elif not isinstance(current.get("needs"), dict):
            current["needs"] = _blank_needs()
        current["name"] = card.get("title") or current.get("name") or "NPC"
    return bucket


def advance(state: dict, minutes: int, activity: str, actor_key_: str,
            cards: list[dict], rng, ctx_npc_chance: float = 0.25) -> dict:
    """Avança o relógio em fatias de no máximo uma hora. Devolve o salto."""
    life = state.get("life")
    if not isinstance(life, dict):
        enable(state)
        life = state["life"]
    actors = state.get("actors") if isinstance(state.get("actors"), dict) else {}
    actor = actors.get(actor_key_) if isinstance(actors.get(actor_key_), dict) else None
    player_needs = _ensure_player_needs(actor)
    player_delta = {key: 0.0 for key in NEEDS}
    npcs = _sync_npcs(life, cards)
    npc_needs = {
        card_id: {key: float((npc.get("needs") or {}).get(key, NEED_START) or 0) for key in NEEDS}
        for card_id, npc in npcs.items() if isinstance(npc, dict)
    }
    npc_delta = {card_id: {key: 0.0 for key in NEEDS} for card_id in npc_needs}
    start_minute = int(life.get("minute") or 0) % 1440
    life["minute"] = start_minute
    life["day"] = max(1, int(life.get("day") or 1))
    if activity not in ACTIVITY_RATES:
        activity = "other"
    remaining = max(0, int(minutes))
    worked = 0
    crossed = False
    chance = float(ctx_npc_chance)
    while remaining > 0:
        minute = int(life["minute"])
        until_hour = 60 - (minute % 60)
        slice_min = min(60, remaining, until_hour or 60)
        hour = (minute // 60) % 24
        if player_needs is not None:
            rates = _rates(activity)
            for key in NEEDS:
                player_delta[key] += rates[key] * (slice_min / 60.0)
            if activity == "work":
                worked += slice_min
        for card_id, npc in npcs.items():
            if not isinstance(npc, dict) or card_id not in npc_delta:
                continue
            npc_activity, place = DEFAULT_ROUTINE.get(hour, ("other", "rua"))
            npc["activity"] = npc_activity
            npc["place"] = place
            rates = _rates(npc_activity)
            for key in NEEDS:
                npc_delta[card_id][key] += rates[key] * (slice_min / 60.0)
        _relations(life, npcs, slice_min, chance, rng)
        life["minute"] = minute + slice_min
        while int(life["minute"]) >= 1440:
            life["minute"] = int(life["minute"]) - 1440
            life["day"] = int(life["day"]) + 1
            life["relations_day"] = {}
            crossed = True
        remaining -= slice_min
    reactions: list[str] = []
    if player_needs is not None and actor is not None:
        final_needs = _clamp_needs(player_needs, player_delta)
        reactions = _react(life, player_needs, final_needs)
        actor["needs"] = final_needs
        if worked:
            wage = int(life.get("wage_per_hour") or 0)
            resources = actor.setdefault("resources", {"gold": 0, "san": None})
            resources["gold"] = int(resources.get("gold") or 0) + (worked * wage) // 60
    for card_id, start in npc_needs.items():
        npc = npcs.get(card_id)
        if isinstance(npc, dict):
            npc["needs"] = _clamp_needs(start, npc_delta[card_id])
    return {
        "minutes": int(minutes),
        "from": clock(start_minute),
        "to": clock(int(life["minute"])),
        "day": int(life["day"]),
        "crossed_midnight": crossed,
        "reactions": reactions,
    }


def _clamp_needs(start: dict[str, float], delta: dict[str, float]) -> dict[str, int]:
    stored: dict[str, int] = {}
    for key in NEEDS:
        value = start.get(key, NEED_START) + delta.get(key, 0.0)
        stored[key] = int(max(0, min(100, round(value))))
    return stored


_WARN: dict[str, str] = {
    "fome": "A fome aperta.",
    "energia": "O cansaço pesa.",
    "higiene": "O cheiro começa a incomodar.",
    "social": "A solidão pesa.",
    "diversao": "O tédio domina.",
    "bexiga": "A bexiga reclama.",
}
_CRIT: dict[str, str] = {
    "fome": "Fome crítica — forças se esvaindo.",
    "energia": "Exaustão crítica — difícil ficar de pé.",
    "higiene": "Imundo — todos notam.",
    "social": "Isolamento crítico — angústia.",
    "diversao": "Tédio crítico — nada tem graça.",
    "bexiga": "Bexiga no limite — precisa de banheiro já.",
}


def _react(life: dict, start: dict[str, float], final: dict[str, int]) -> list[str]:
    """Reações ao cruzar limiares de necessidade. Consequências em 0 (Sims-like):
    bexiga 0 → acidente (higiene despenca, alívio forçado); energia 0 → desmaio
    (dorme onde está). As outras só avisam — a penalidade nos testes já morde."""
    fired: list[str] = []
    for key in NEEDS:
        before = float(start.get(key, NEED_START) or 0)
        after = int(final.get(key, 0))
        if after == 0 and before > 0:
            if key == "bexiga":
                final["higiene"] = max(0, int(final.get("higiene", 0)) - 40)
                final["bexiga"] = 20
                fired.append("Não aguentou: acidente! A higiene despenca.")
            elif key == "energia":
                final["energia"] = 25
                fired.append("Desabou de exaustão e dormiu onde estava.")
            else:
                fired.append(_CRIT[key])
        elif after < 15 and before >= 15:
            fired.append(_CRIT[key])
        elif after < 25 and before >= 25:
            fired.append(_WARN[key])
    if fired:
        bucket = life.setdefault("reactions", [])
        if not isinstance(bucket, list):
            bucket = []
            life["reactions"] = bucket
        day = int(life.get("day") or 1)
        at = clock(int(life.get("minute") or 0))
        for text in fired:
            bucket.append({"day": day, "clock": at, "text": text})
        del bucket[:-10]
    return fired


def _relations(life: dict, npcs: dict, slice_min: int, chance: float, rng) -> None:
    groups: dict[tuple[str, str], list[str]] = {}
    for card_id, npc in npcs.items():
        if not isinstance(npc, dict):
            continue
        activity = npc.get("activity") or ""
        place = npc.get("place") or ""
        if activity not in _TOGETHER or not place:
            continue
        groups.setdefault((place, activity), []).append(str(card_id))
    relations = life.setdefault("relations", {})
    per_day = life.setdefault("relations_day", {})
    if not isinstance(relations, dict):
        relations = {}
        life["relations"] = relations
    if not isinstance(per_day, dict):
        per_day = {}
        life["relations_day"] = per_day
    window = slice_min / 60.0
    for ids in groups.values():
        ids.sort()
        for index, left in enumerate(ids):
            for right in ids[index + 1:]:
                key = f"{left}|{right}"
                if rng.random() >= chance * window:
                    continue
                added = int(per_day.get(key) or 0)
                if added >= 6:
                    continue
                gain = min(2, 6 - added)
                current = int(relations.get(key) or 0)
                relations[key] = max(-100, min(100, current + gain))
                per_day[key] = added + gain


def _need_bits(needs: dict, only_low: bool = False) -> str:
    bits = []
    for key in NEEDS:
        value = int(needs.get(key, 0) or 0)
        label = NEED_LABELS_LOW[key] if value < 25 else ""
        if only_low and not label:
            continue
        shown = f"{NEED_NAMES[key]} {value}"
        if label:
            shown += f" ({label})"
        bits.append(shown)
    return ", ".join(bits)


def render(state: dict, user: str, scene_cards: list[dict]) -> str:
    life = state.get("life") if isinstance(state.get("life"), dict) else None
    if not life or not life.get("enabled"):
        return ""
    from app.sim import packs
    from app.sim.state import actor_key
    day = max(1, int(life.get("day") or 1))
    week, season = weekday_season(day)
    lines = [f"[Vida] Dia {day} ({week}, {season}), {clock(int(life.get('minute') or 0))}."]
    pack = packs.load_pack(state.get("pack") or "")
    currency = (pack or {}).get("currency") or "Moedas"
    actors = state.get("actors") if isinstance(state.get("actors"), dict) else {}
    if user:
        chosen = [actors[actor_key(user)]] if actor_key(user) in actors else []
    else:
        chosen = [actor for actor in actors.values() if isinstance(actor, dict)]
    for actor in chosen:
        if not isinstance(actor, dict):
            continue
        needs = actor.get("needs") if isinstance(actor.get("needs"), dict) else None
        if not needs:
            continue
        gold = int((actor.get("resources") or {}).get("gold") or 0)
        lines.append(
            f"{actor.get('name') or 'Jogador'}: {_need_bits(needs)}; "
            f"humor {mood(needs)}; {gold} {currency}."
        )
    scene_ids = []
    npc_bits = []
    bucket = life.get("npcs") if isinstance(life.get("npcs"), dict) else {}
    for card in scene_cards or []:
        card_id = card.get("id")
        npc = bucket.get(card_id) if card_id else None
        if not isinstance(npc, dict):
            continue
        scene_ids.append(str(card_id))
        verb = _VERB.get(npc.get("activity") or "", "ocupado")
        place = npc.get("place") or "rua"
        low = _need_bits(npc.get("needs") or {}, only_low=True)
        piece = f"{npc.get('name') or card.get('title') or 'NPC'}: {verb} ({place})"
        if low:
            piece += f", {low}"
        npc_bits.append(piece)
    if npc_bits:
        lines.append(". ".join(npc_bits) + ".")
    relations = life.get("relations") if isinstance(life.get("relations"), dict) else {}
    rel_bits = []
    seen: set[str] = set()
    for key, value in relations.items():
        if not isinstance(key, str) or "|" not in key:
            continue
        left, right = key.split("|", 1)
        if left not in scene_ids or right not in scene_ids:
            continue
        if key in seen:
            continue
        seen.add(key)
        name_a = (bucket.get(left) or {}).get("name") or left
        name_b = (bucket.get(right) or {}).get("name") or right
        rel_bits.append(f"{name_a}–{name_b}: {relation_label(int(value or 0))}")
    if rel_bits:
        lines.append("Relações: " + ". ".join(rel_bits) + ".")
    recent = life.get("reactions") if isinstance(life.get("reactions"), list) else []
    bits = [str(item.get("text") or "") for item in recent[-2:] if isinstance(item, dict)]
    bits = [bit for bit in bits if bit]
    if bits:
        lines.append("Recente: " + " ".join(bits))
    lines.append("Use estes fatos com naturalidade; não cite números.")
    return "\n".join(lines)
