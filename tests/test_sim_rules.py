"""Resolução d20/d100 com rng scriptado."""

from __future__ import annotations

import json

from app.sim import packs, rules, state


class Script:
    def __init__(self, rolls: list[int]) -> None:
        self.rolls = list(rolls)

    def randint(self, _a: int, _b: int) -> int:
        return self.rolls.pop(0)

    def random(self) -> float:
        return 0.0

    def choice(self, seq):
        return seq[0]


def _actor(pack_id: str = "d20", **stats) -> tuple[dict, dict, str]:
    pack = packs.load_pack(pack_id)
    sim = state.empty()
    sim["pack"] = pack_id
    base = {"str": 16, "dex": 14, "con": 14, "int": 10, "wis": 10, "cha": 10}
    base.update(stats)
    rules.ensure_actor(sim, pack, "hero", "Aldric", base)
    return sim, pack, "hero"


def _intent(**over) -> dict:
    raw = {
        "kind": "none", "ability": "", "skill": "", "target": "",
        "toughness": "normal", "hostile": False, "weapon": "", "item": "",
        "gold": 0, "difficulty": "medium", "advantage": "", "minutes": 5,
        "activity": "other", "shock": "none", "heal": False,
    }
    raw.update(over)
    return raw


def test_d20_attack_kills_weak_foe_and_pays_xp_and_gold():
    sim, pack, key = _actor()
    card = {"id": "gob", "type": "character", "title": "Goblin"}
    out = rules.resolve(sim, pack, _intent(
        kind="attack", target="Goblin", toughness="weak", weapon="espada",
    ), key, [card], 0, Script([15, 6, 4]))
    assert out.refusal is None
    foe = sim["foes"]["goblin"]
    assert foe["dead"] is True
    assert foe["hp"] == 0
    assert out.card_status == {"gob": "dead"}
    actor = sim["actors"][key]
    assert actor["xp"] == 25
    assert actor["resources"]["gold"] == 15 + 4
    assert any(change["what"] == "PV" and change["to"] == 0 for change in out.changes)


def test_natural_20_rolls_weapon_dice_twice():
    sim, pack, key = _actor()
    out = rules.resolve(sim, pack, _intent(
        kind="attack", target="Orc", toughness="normal", weapon="espada",
    ), key, [], 0, Script([20, 3, 4]))
    foe = sim["foes"]["orc"]
    assert foe["dead"] is False
    # 1d8 3 + 1d8 4 + mod FOR 16 (+3) = 10; 18-10 = 8
    assert foe["hp"] == 8
    assert any("CRÍTICO" in step["text"] for step in out.steps)


def test_natural_1_misses_and_counter_can_drop_the_actor():
    sim, pack, key = _actor()
    sim["actors"][key]["hp"] = 4
    out = rules.resolve(sim, pack, _intent(
        kind="attack", target="Goblin", toughness="weak", weapon="espada", hostile=True,
    ), key, [], 0, Script([1, 18, 4]))
    assert sim["foes"]["goblin"]["hp"] == 6
    assert sim["actors"][key]["hp"] == 0
    assert sim["actors"][key]["down"] is True
    assert any(step["text"] == "Você caiu (0 PV)." for step in out.steps)


def test_down_actor_cannot_attack():
    sim, pack, key = _actor()
    sim["actors"][key]["down"] = True
    out = rules.resolve(sim, pack, _intent(kind="attack", target="Goblin"), key, [], 0, Script([]))
    assert out.refusal == "Seu personagem está caído e não pode atacar."


def test_buy_without_gold_is_refused():
    sim, pack, key = _actor()
    sim["actors"][key]["resources"]["gold"] = 0
    out = rules.resolve(sim, pack, _intent(kind="buy", item="corda", gold=5), key, [], 0, Script([]))
    assert out.refusal == "Ouro insuficiente: precisa de 5, tem 0."
    assert sim["actors"][key]["inventory"] == []


def test_missing_item_is_refused():
    sim, pack, key = _actor()
    out = rules.resolve(sim, pack, _intent(kind="use_item", item="poção de cura"), key, [], 0, Script([]))
    assert out.refusal == (
        "Você não tem 'poção de cura' no inventário "
        "(a aba Vida/Regras permite adicioná-lo)."
    )


def test_potion_heals_and_is_consumed():
    sim, pack, key = _actor()
    actor = sim["actors"][key]
    actor["hp"] = 4
    actor["inventory"] = [{"name": "poção de cura", "qty": 1}]
    out = rules.resolve(sim, pack, _intent(kind="use_item", item="poção"), key, [], 0, Script([4, 3]))
    assert actor["hp"] == 12  # 4 + (4+3+2) limitado ao máximo 12
    assert actor["inventory"] == []
    assert any(change["what"] == "PV" and change["from"] == 4 for change in out.changes)


def test_d100_degrees_crit_and_fumble():
    sim, pack, key = _actor("d100")
    actor = sim["actors"][key]
    actor["skills"] = {"furtividade": 40}

    def level(roll: int) -> str:
        out = rules.resolve(sim, pack, _intent(
            kind="skill", skill="Furtividade", difficulty="medium",
        ), key, [], 0, Script([roll]))
        return out.steps[-1]["text"]

    assert "EXTREMO" in level(8)
    assert "DIFÍCIL" in level(20)
    assert level(34).endswith("SUCESSO")
    assert "CRÍTICO" in level(1)
    assert "DESASTRE" in level(100)


def test_sanity_loss_follows_the_pair():
    sim, pack, key = _actor("d100")
    actor = sim["actors"][key]
    actor["resources"]["san"] = 40
    failed = rules.resolve(sim, pack, _intent(kind="none", shock="minor"), key, [], 0, Script([80, 2]))
    assert actor["resources"]["san"] == 38
    assert any(change["what"] == "SAN" and change["to"] == 38 for change in failed.changes)
    passed = rules.resolve(sim, pack, _intent(kind="none", shock="minor"), key, [], 0, Script([10]))
    assert actor["resources"]["san"] == 38
    assert any("Perda 0" in step["text"] for step in passed.steps)


def test_dead_foe_is_refused():
    sim, pack, key = _actor()
    sim["foes"]["goblin"] = {
        "name": "Goblin", "hp": 0, "hp_max": 6, "ac": 10, "atk": 2,
        "dmg": "1d4", "dead": True, "card_id": None,
    }
    out = rules.resolve(sim, pack, _intent(kind="attack", target="Goblin"), key, [], 0, Script([]))
    assert out.refusal == "Goblin já está morto."
    assert json.dumps(out.card_status) == "{}"
