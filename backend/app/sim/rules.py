"""Resolução determinística. Sem LLM. O rng entra de fora para o teste repetir."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from app import stats
from app.sim import dice
from app.sim.state import fold

_ABILITY_SHORT = {
    "str": "FOR", "dex": "DES", "con": "CON", "int": "INT", "wis": "SAB", "cha": "CAR",
    "for": "FOR", "des": "DES", "tam": "TAM", "app": "APP", "pod": "POD", "edu": "EDU",
}
_D100_FROM = {
    "for": "str", "con": "con", "des": "dex", "int": "int",
    "pod": "wis", "app": "cha", "edu": "int",
}
_KINDS_WITHOUT_PACK = ("buy", "take", "use_item", "none")


@dataclass
class Resolution:
    steps: list[dict] = field(default_factory=list)
    changes: list[dict] = field(default_factory=list)
    refusal: str | None = None
    card_status: dict[str, str] = field(default_factory=dict)


def ensure_actor(state: dict, pack: dict | None, user: str, name: str,
                 sheet_stats: dict | None, legacy_inventory: list | None = None) -> dict:
    """Cria o actor uma vez. Edição posterior não é refeita."""
    from app.sim.state import actor_key
    key = actor_key(user)
    actors = state.setdefault("actors", {})
    current = actors.get(key)
    if isinstance(current, dict):
        return current
    actor: dict = {
        "name": (name or "").strip() or "Jogador",
        "hp": None,
        "hp_max": None,
        "ac": None,
        "abilities": {},
        "skills": {},
        "proficient": [],
        "level": 1,
        "xp": 0,
        "resources": {"gold": 0, "san": None},
        "inventory": _clean_inventory(legacy_inventory),
        "down": False,
        "needs": None,
    }
    resolution = (pack or {}).get("resolution")
    if resolution == "d20":
        normalized = stats.normalize_stats(sheet_stats or {})
        actor["abilities"] = {key_: normalized[key_] for key_ in stats.ABILITIES}
        hp_max = max(1, int(pack["hp"]["base"]) + stats.stat_modifier(actor["abilities"]["con"]))
        actor["hp_max"] = hp_max
        actor["hp"] = hp_max
        actor["ac"] = int(pack["ac"]["base"]) + stats.stat_modifier(actor["abilities"]["dex"])
        actor["resources"]["gold"] = int(pack.get("start_gold") or 0)
    elif resolution == "d100":
        base = stats.normalize_stats(sheet_stats or {})
        scale = int(pack.get("ability_scale") or 5)
        abilities = {}
        for key_ in pack.get("abilities") or []:
            abilities[key_] = _d100_score(key_, base, scale)
        actor["abilities"] = abilities
        hp_max = max(1, (int(abilities.get("con") or 0) + int(abilities.get("tam") or 0)) // 10)
        actor["hp_max"] = hp_max
        actor["hp"] = hp_max
        actor["resources"]["gold"] = int(pack.get("start_gold") or 0)
        san_cfg = pack.get("san") or {}
        if isinstance(san_cfg, dict) and san_cfg.get("start"):
            actor["resources"]["san"] = int(abilities.get(san_cfg["start"]) or 0)
    actors[key] = actor
    return actor


def _d100_score(key: str, base: dict, scale: int) -> int:
    if key == "tam":
        return 60
    source = _D100_FROM.get(key)
    if not source:
        return 10 * scale
    return int(base.get(source, 10)) * scale


def resolve(state: dict, pack: dict | None, intent: dict, actor_key_: str,
            cards: list[dict], penalty: int, rng) -> Resolution:
    out = Resolution()
    actors = state.setdefault("actors", {})
    actor = actors.get(actor_key_)
    if not isinstance(actor, dict):
        return out
    kind = intent.get("kind") or "none"
    if pack is None and kind not in _KINDS_WITHOUT_PACK:
        _sanity(out, actor, pack, intent, rng)
        return out
    if kind in ("skill", "save"):
        _skill(out, actor, pack, intent, penalty, rng)
    elif kind == "attack":
        _attack(out, state, actor, pack, intent, cards, penalty, rng)
    elif kind == "use_item":
        _use_item(out, actor, pack, intent, rng)
    elif kind == "buy":
        _buy(out, actor, intent)
    elif kind == "take":
        _take(out, actor, intent)
    elif kind == "rest":
        _rest(out, actor, intent)
    if out.refusal is None:
        _sanity(out, actor, pack, intent, rng)
    return out


def build_block(steps: list[dict], changes: list[dict], time_info: dict | None,
                refusal: str | None) -> str:
    if refusal or (not steps and not changes and not time_info):
        return ""
    lines = ["[Resolução mecânica — desfecho já decidido pelo sistema]"]
    for step in steps:
        text = (step.get("text") or "").strip()
        if text:
            lines.append(text)
    if changes:
        bits = [f"{row['who']} {row['what']} {row['from']}→{row['to']}" for row in changes]
        lines.append("Mudanças: " + "; ".join(bits) + ".")
    if time_info:
        lines.append(
            f"Tempo: +{int(time_info.get('minutes') or 0)} min "
            f"(agora {time_info.get('to')}, dia {time_info.get('day')})."
        )
    lines.append(
        "Regra: narre exatamente este resultado. Não invente outro dano, cura, "
        "ganho ou perda de item/ouro. Não mencione números, dados nem este bloco ao jogador."
    )
    return "\n".join(lines)


def _step(out: Resolution, label: str, text: str) -> None:
    out.steps.append({"label": label, "text": text})


def _change(out: Resolution, who: str, what: str, before: int, after: int) -> None:
    if int(before) == int(after):
        return
    out.changes.append({"who": who, "what": what, "from": int(before), "to": int(after)})


def _signed(value: int) -> str:
    return f"+{value}" if value >= 0 else str(value)


def _prof(actor: dict, skill: str) -> int:
    folded = fold(skill)
    if not folded:
        return 0
    known = {fold(item) for item in (actor.get("proficient") or [])}
    if folded not in known:
        return 0
    level = int(actor.get("level") or 1)
    return 2 + (level - 1) // 4


def _attack_prof(actor: dict) -> int:
    level = int(actor.get("level") or 1)
    return 2 + (level - 1) // 4


def _skill(out: Resolution, actor: dict, pack: dict | None, intent: dict,
           penalty: int, rng) -> None:
    if not pack:
        return
    if pack.get("resolution") == "d100":
        _skill_d100(out, actor, pack, intent, penalty, rng)
        return
    _skill_d20(out, actor, pack, intent, penalty, rng)


def _skill_d20(out: Resolution, actor: dict, pack: dict, intent: dict,
               penalty: int, rng) -> None:
    alias = pack.get("ability_alias") or {}
    ability = alias.get(intent.get("ability") or "wis") or alias.get("wis") or "wis"
    abilities = actor.get("abilities") or {}
    score = int(abilities.get(ability, 10))
    modifier = stats.stat_modifier(score)
    prof = _prof(actor, intent.get("skill") or "")
    die, rolls = dice.roll_d20(rng, intent.get("advantage") or "")
    total = die + modifier + prof - int(penalty or 0)
    table = pack.get("dc") or {}
    target = int(table.get(intent.get("difficulty") or "medium", 15))
    if total >= target + 10:
        label = "sucesso excepcional"
    elif total <= target - 10:
        label = "fracasso desastroso"
    elif total >= target:
        label = "SUCESSO"
    else:
        label = "FALHA"
    skill = (intent.get("skill") or "").strip() or ability
    short = _ABILITY_SHORT.get(ability, ability.upper())
    net = modifier + prof - int(penalty or 0)
    dice_txt = str(die) if len(rolls) == 1 else "/".join(str(n) for n in rolls)
    net_txt = "" if net == 0 else f" {_signed(net)}"
    _step(out, "skill",
          f"{skill} ({short}): d20 {dice_txt}{net_txt} = {total} vs CD {target} — {label}")


def _d100_base(actor: dict, pack: dict, skill: str, ability: str) -> int:
    folded = fold(skill)
    actor_skills = {fold(key): int(value) for key, value in (actor.get("skills") or {}).items()}
    if folded and folded in actor_skills:
        return actor_skills[folded]
    pack_skills = {fold(key): int(value) for key, value in (pack.get("skills") or {}).items()}
    if folded and folded in pack_skills:
        return pack_skills[folded]
    if ability:
        alias = (pack.get("ability_alias") or {}).get(ability) or ability
        abilities = actor.get("abilities") or {}
        if alias in abilities:
            return int(abilities[alias])
    return int(pack.get("skill_default") if pack.get("skill_default") is not None else 20)


def _d100_target(base: int, penalty: int, pack: dict, difficulty: str) -> int:
    mult = float((pack.get("difficulty") or {}).get(difficulty, 1.0) or 1.0)
    scaled = max(1, base - int(penalty or 0) * 10) * mult
    return max(1, int(scaled))


def _d100_level(roll: int, alvo: int, base: int) -> str:
    if roll == 1:
        return "CRÍTICO"
    if roll == 100 or (base < 50 and roll >= 96):
        return "DESASTRE"
    if roll <= alvo // 5:
        return "EXTREMO"
    if roll <= alvo // 2:
        return "DIFÍCIL"
    if roll <= alvo:
        return "SUCESSO"
    return "FALHA"


def _skill_d100(out: Resolution, actor: dict, pack: dict, intent: dict,
                penalty: int, rng) -> None:
    skill = (intent.get("skill") or "").strip() or "Perícia"
    base = _d100_base(actor, pack, intent.get("skill") or "", intent.get("ability") or "")
    alvo = _d100_target(base, penalty, pack, intent.get("difficulty") or "medium")
    rolled = dice.roll_d100(rng)
    label = _d100_level(rolled, alvo, base)
    _step(out, "skill", f"{skill}: d100 {rolled} vs {alvo} — {label}")


def _attack(out: Resolution, state: dict, actor: dict, pack: dict | None,
            intent: dict, cards: list[dict], penalty: int, rng) -> None:
    if not pack:
        return
    if actor.get("down"):
        out.refusal = "Seu personagem está caído e não pode atacar."
        return
    foe = _find_or_create_foe(state, pack, intent, cards)
    if foe.get("dead"):
        out.refusal = f"{foe.get('name') or 'O alvo'} já está morto."
        return
    weapon = _weapon(pack, intent.get("weapon") or "")
    if pack.get("resolution") == "d100":
        hit, crit = _attack_d100(out, actor, pack, intent, weapon, penalty, rng)
    else:
        hit, crit = _attack_d20(out, actor, pack, weapon, foe, penalty, intent, rng)
    if not hit:
        if intent.get("hostile"):
            _counter(out, actor, pack, foe, rng)
        return
    damage = _weapon_damage(actor, weapon, pack, crit, rng)
    before = int(foe.get("hp") or 0)
    foe["hp"] = before - damage
    _change(out, foe.get("name") or "Inimigo", "PV", before, max(0, int(foe["hp"])))
    _step(out, "damage", f"Dano {damage}. {foe.get('name')} PV {before}→{max(0, int(foe['hp']))}.")
    if int(foe["hp"]) <= 0:
        foe["hp"] = 0
        foe["dead"] = True
        _on_kill(out, actor, pack, foe, rng)
        return
    if intent.get("hostile"):
        _counter(out, actor, pack, foe, rng)


def _attack_d20(out, actor, pack, weapon, foe, penalty, intent, rng) -> tuple[bool, bool]:
    ability = weapon.get("ability") or "str"
    modifier = stats.stat_modifier(int((actor.get("abilities") or {}).get(ability, 10)))
    prof = _attack_prof(actor)
    die, rolls = dice.roll_d20(rng, intent.get("advantage") or "")
    net = modifier + prof - int(penalty or 0)
    total = die + net
    ac = int(foe.get("ac") if foe.get("ac") is not None else 10)
    dice_txt = str(die) if len(rolls) == 1 else "/".join(str(n) for n in rolls)
    net_txt = "" if net == 0 else f" {_signed(net)}"
    name = weapon.get("_name") or "desarmado"
    if die == 1:
        _step(out, "attack", f"Ataque ({name}): d20 {dice_txt}{net_txt} = {total} vs CA {ac} — ERRO")
        return False, False
    if die == 20 or total >= ac:
        label = "CRÍTICO" if die == 20 else "ACERTO"
        _step(out, "attack", f"Ataque ({name}): d20 {dice_txt}{net_txt} = {total} vs CA {ac} — {label}")
        return True, die == 20
    _step(out, "attack", f"Ataque ({name}): d20 {dice_txt}{net_txt} = {total} vs CA {ac} — ERRO")
    return False, False


def _attack_d100(out, actor, pack, intent, weapon, penalty, rng) -> tuple[bool, bool]:
    skill = weapon.get("skill") or intent.get("skill") or "luta"
    base = _d100_base(actor, pack, skill, "")
    alvo = _d100_target(base, penalty, pack, intent.get("difficulty") or "medium")
    rolled = dice.roll_d100(rng)
    label = _d100_level(rolled, alvo, base)
    name = weapon.get("_name") or skill
    _step(out, "attack", f"Ataque ({name}): d100 {rolled} vs {alvo} — {label}")
    if label in ("DESASTRE", "FALHA"):
        return False, False
    return True, label in ("CRÍTICO", "EXTREMO")


def _weapon_damage(actor: dict, weapon: dict, pack: dict, crit: bool, rng) -> int:
    """d20: dados + modificador da habilidade; crítico rola os dados de novo.
    d100 extremo/crítico: máximo dos dados mais uma rolagem extra."""
    expr = weapon.get("dice") or "1d3"
    rolled = dice.roll(expr, rng)
    if pack.get("resolution") == "d100":
        if crit:
            return max(1, dice.roll_max(expr) + rolled.total)
        return max(1, rolled.total)
    ability = weapon.get("ability") or "str"
    score = int((actor.get("abilities") or {}).get(ability, 10))
    total = rolled.total + stats.stat_modifier(score)
    if crit:
        extra = dice.roll(expr, rng)
        total += extra.total - extra.modifier
    return max(1, total)


def _weapon(pack: dict, text: str) -> dict:
    """A maior chave do pack contida no texto da arma. Senão, desarmado.

    `_score` é preenchido pelo ataque d20 antes do dano — aqui só a ficha da arma.
    """
    hay = fold(text)
    weapons = pack.get("weapons") or {}
    best_key = ""
    for key in weapons:
        folded = fold(key)
        if folded and folded in hay and len(folded) > len(fold(best_key)):
            best_key = key
    chosen = best_key or "desarmado"
    data = dict(weapons.get(chosen) or weapons.get("desarmado") or {"dice": "1d3", "ability": "str"})
    data["_name"] = chosen
    return data


def _find_or_create_foe(state: dict, pack: dict, intent: dict, cards: list[dict]) -> dict:
    target = (intent.get("target") or "").strip()
    slug = re.sub(r"\s+", "-", fold(target) or "inimigo") or "inimigo"
    foes = state.setdefault("foes", {})
    if not isinstance(foes, dict):
        foes = {}
        state["foes"] = foes
    current = foes.get(slug)
    if isinstance(current, dict):
        return current
    band = intent.get("toughness") if intent.get("toughness") in ("weak", "normal", "tough", "boss") else "normal"
    stats_row = (pack.get("toughness") or {}).get(band) or (pack.get("toughness") or {}).get("normal") or {}
    card = _matching_card(cards, target)
    foe = {
        "name": target or "Inimigo",
        "toughness": band,
        "hp": int(stats_row.get("hp") or 1),
        "hp_max": int(stats_row.get("hp") or 1),
        "ac": stats_row.get("ac"),
        "atk": int(stats_row.get("atk") or 0),
        "dmg": stats_row.get("dmg") or "1d4",
        "xp": int(stats_row.get("xp") or 0),
        "gold": stats_row.get("gold") or "",
        "card_id": card.get("id") if card else None,
        "dead": False,
    }
    foes[slug] = foe
    _prune_foes(foes)
    return foe


def _prune_foes(foes: dict) -> None:
    while len(foes) > 20:
        dead_key = next((key for key, foe in foes.items() if isinstance(foe, dict) and foe.get("dead")), None)
        if dead_key is None:
            break
        del foes[dead_key]


def _matching_card(cards: list[dict], target: str) -> dict | None:
    want = fold(target)
    if not want:
        return None
    for card in cards or []:
        if (card.get("type") or "") != "character":
            continue
        if fold(card.get("title") or "") == want:
            return card
    return None


def _on_kill(out: Resolution, actor: dict, pack: dict, foe: dict, rng) -> None:
    _step(out, "kill", f"{foe.get('name')} morre.")
    xp = int(foe.get("xp") or 0)
    if xp:
        before = int(actor.get("xp") or 0)
        actor["xp"] = before + xp
        _change(out, actor.get("name") or "Jogador", "XP", before, actor["xp"])
        _step(out, "xp", f"XP {before}→{actor['xp']}.")
        _level_up(out, actor, pack)
    gold_expr = foe.get("gold") or ""
    if gold_expr and pack.get("resolution") == "d20":
        gained = max(0, dice.roll(str(gold_expr), rng).total)
        resources = actor.setdefault("resources", {"gold": 0, "san": None})
        before = int(resources.get("gold") or 0)
        resources["gold"] = before + gained
        _change(out, actor.get("name") or "Jogador", "Ouro", before, resources["gold"])
        _step(out, "gold", f"Ouro {before}→{resources['gold']}.")
    card_id = foe.get("card_id")
    if card_id:
        out.card_status[str(card_id)] = "dead"


def _level_up(out: Resolution, actor: dict, pack: dict) -> None:
    levels = pack.get("xp_levels")
    if not isinstance(levels, list):
        return
    while int(actor.get("level") or 1) < len(levels):
        level = int(actor.get("level") or 1)
        if int(actor.get("xp") or 0) < int(levels[level]):
            break
        actor["level"] = level + 1
        per = int((pack.get("hp") or {}).get("per_level") or 0)
        con = int((actor.get("abilities") or {}).get("con", 10))
        gain = max(1, per + stats.stat_modifier(con))
        before_max = int(actor.get("hp_max") or 0)
        before_hp = int(actor.get("hp") or 0)
        actor["hp_max"] = before_max + gain
        actor["hp"] = before_hp + gain
        who = actor.get("name") or "Jogador"
        _change(out, who, "PV máx", before_max, actor["hp_max"])
        _change(out, who, "PV", before_hp, actor["hp"])
        _step(out, "level", f"Nível {actor['level']}!")


def _counter(out: Resolution, actor: dict, pack: dict, foe: dict, rng) -> None:
    if actor.get("hp") is None:
        return
    if pack.get("resolution") == "d100":
        rolled = dice.roll_d100(rng)
        hit = rolled <= int(foe.get("atk") or 0)
        crit = rolled == 1 and hit
        _step(out, "counter",
              f"Contra-ataque: d100 {rolled} vs {int(foe.get('atk') or 0)} — "
              f"{'ACERTO' if hit else 'ERRO'}")
    else:
        die, _rolls = dice.roll_d20(rng, "")
        total = die + int(foe.get("atk") or 0)
        ac = int(actor.get("ac") if actor.get("ac") is not None else 10)
        if die == 1:
            hit, crit = False, False
        else:
            hit = die == 20 or total >= ac
            crit = die == 20 and hit
        label = "CRÍTICO" if crit else ("ACERTO" if hit else "ERRO")
        _step(out, "counter", f"Contra-ataque: d20 {die} +{int(foe.get('atk') or 0)} = {total} vs CA {ac} — {label}")
    if not hit:
        return
    expr = str(foe.get("dmg") or "1d4")
    rolled = dice.roll(expr, rng)
    damage = rolled.total
    if crit:
        damage += dice.roll(expr, rng).total - rolled.modifier
    damage = max(1, damage)
    before = int(actor.get("hp") or 0)
    actor["hp"] = max(0, before - damage)
    who = actor.get("name") or "Jogador"
    _change(out, who, "PV", before, actor["hp"])
    _step(out, "counter-damage", f"Dano {damage}. {who} PV {before}→{actor['hp']}.")
    if actor["hp"] == 0:
        actor["down"] = True
        _step(out, "down", "Você caiu (0 PV).")


def _use_item(out: Resolution, actor: dict, pack: dict | None, intent: dict, rng) -> None:
    item = (intent.get("item") or "").strip()
    row = _find_item(actor.get("inventory") or [], item)
    if row is None:
        shown = item or "isso"
        out.refusal = (
            f"Você não tem '{shown}' no inventário "
            "(a aba Vida/Regras permite adicioná-lo)."
        )
        return
    consumable = _consumable(pack, row.get("name") or item)
    heal = (consumable or {}).get("heal") if isinstance(consumable, dict) else None
    if heal and actor.get("hp") is not None and actor.get("hp_max") is not None:
        gained = max(0, dice.roll(str(heal), rng).total)
        before = int(actor["hp"])
        actor["hp"] = min(int(actor["hp_max"]), before + gained)
        actor["down"] = False
        _change(out, actor.get("name") or "Jogador", "PV", before, actor["hp"])
        _step(out, "heal", f"{row.get('name')}: cura {gained}. PV {before}→{actor['hp']}.")
        _consume(actor, row)
        return
    _step(out, "item", f"Você usa {row.get('name')}.")


def _buy(out: Resolution, actor: dict, intent: dict) -> None:
    item = (intent.get("item") or "").strip()
    if not item:
        return
    resources = actor.setdefault("resources", {"gold": 0, "san": None})
    have = int(resources.get("gold") or 0)
    cost = int(intent.get("gold") or 0)
    if cost > have:
        out.refusal = f"Ouro insuficiente: precisa de {cost}, tem {have}."
        return
    resources["gold"] = have - cost
    _change(out, actor.get("name") or "Jogador", "Ouro", have, resources["gold"])
    _add_item(actor, item, 1)
    _step(out, "buy", f"Compra {item} por {cost}. Ouro {have}→{resources['gold']}.")


def _take(out: Resolution, actor: dict, intent: dict) -> None:
    item = (intent.get("item") or "").strip()
    if not item:
        return
    _add_item(actor, item, 1)
    _step(out, "take", f"Você pega {item}.")


def _rest(out: Resolution, actor: dict, intent: dict) -> None:
    if actor.get("hp") is None or actor.get("hp_max") is None:
        return
    minutes = int(intent.get("minutes") or 0)
    before = int(actor["hp"])
    hp_max = int(actor["hp_max"])
    if minutes >= 360:
        actor["hp"] = hp_max
        actor["down"] = False
        _step(out, "rest", f"Descanso longo. PV {before}→{actor['hp']}.")
    elif minutes >= 60:
        actor["hp"] = min(hp_max, before + hp_max // 4)
        _step(out, "rest", f"Descanso. PV {before}→{actor['hp']}.")
    else:
        return
    _change(out, actor.get("name") or "Jogador", "PV", before, actor["hp"])


def _sanity(out: Resolution, actor: dict, pack: dict | None, intent: dict, rng) -> None:
    shock = intent.get("shock") or "none"
    if shock == "none":
        return
    resources = actor.get("resources") if isinstance(actor.get("resources"), dict) else None
    if not resources or resources.get("san") is None:
        return
    if not pack:
        return
    table = (pack.get("sanity_loss") or {}).get(shock)
    if not isinstance(table, (list, tuple)) or len(table) < 2:
        return
    san = int(resources.get("san") or 0)
    rolled = dice.roll_d100(rng)
    passed = rolled <= san
    loss = _loss_amount(table[0] if passed else table[1], rng)
    resources["san"] = max(0, san - loss)
    who = actor.get("name") or "Jogador"
    _change(out, who, "SAN", san, resources["san"])
    label = "SUCESSO" if passed else "FALHA"
    _step(out, "san", f"Sanidade: d100 {rolled} vs {san} — {label}. Perda {loss}.")
    if loss >= 5:
        _step(out, "insanity", "Insanidade temporária.")
    if resources["san"] == 0:
        _step(out, "madness", "Loucura indefinida.")


def _loss_amount(expr, rng) -> int:
    if expr in (0, "0", "", None):
        return 0
    if isinstance(expr, int):
        return max(0, expr)
    text = str(expr).strip()
    if text.isdigit():
        return int(text)
    return max(0, dice.roll(text, rng).total)


def _find_item(inventory: list, name: str) -> dict | None:
    needle = fold(name)
    if not needle:
        return None
    best = None
    best_len = -1
    for row in inventory:
        if not isinstance(row, dict):
            continue
        hay = fold(row.get("name") or "")
        if not hay:
            continue
        if needle in hay or hay in needle:
            if len(hay) > best_len:
                best = row
                best_len = len(hay)
    return best


def _consumable(pack: dict | None, name: str) -> dict | None:
    if not pack:
        return None
    needle = fold(name)
    for key, value in (pack.get("consumables") or {}).items():
        folded = fold(key)
        if folded and (folded in needle or needle in folded):
            return value if isinstance(value, dict) else None
    return None


def _consume(actor: dict, row: dict) -> None:
    row["qty"] = int(row.get("qty") or 1) - 1
    if row["qty"] <= 0:
        inventory = actor.get("inventory") or []
        if row in inventory:
            inventory.remove(row)


def _add_item(actor: dict, name: str, qty: int) -> None:
    inventory = actor.setdefault("inventory", [])
    found = _find_item(inventory, name)
    if found is not None and fold(found.get("name") or "") == fold(name):
        found["qty"] = min(999, int(found.get("qty") or 0) + qty)
        return
    inventory.append({"name": name[:80], "qty": max(1, min(999, qty))})


def _clean_inventory(rows: list | None) -> list:
    clean = []
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        name = str(row.get("name") or "").strip()[:80]
        if not name:
            continue
        try:
            qty = int(row.get("qty") or 1)
        except (TypeError, ValueError):
            qty = 1
        clean.append({"name": name, "qty": max(1, min(999, qty))})
        if len(clean) >= 40:
            break
    return clean
