"""Atributos: point-buy, normalização, estimativa, sorteio e ficha completa."""

from __future__ import annotations

import json
import random

from app import npc_assets, stats


class _Seq:
    def __init__(self, rolls: list[int]) -> None:
        self._rolls = list(rolls)

    def randint(self, _a: int, _b: int) -> int:
        return self._rolls.pop(0)


def test_point_buy_cost_and_total():
    assert stats.POINT_BUY_BUDGET == 27
    assert stats.point_buy_cost(8) == 0
    assert stats.point_buy_cost(10) == 2
    assert stats.point_buy_cost(15) == 7
    assert stats.point_buy_cost(7) == 0
    assert stats.point_buy_cost(16) == 0
    bought = {key: 12 for key in stats.ABILITIES}
    assert stats.point_buy_total(bought) == 24
    floor = {key: 8 for key in stats.ABILITIES}
    floor["str"] = 15
    assert stats.point_buy_total(floor) == 7
    assert stats.point_buy_total({}) == 0


def test_normalize_stats_fills_six_keeps_extras_and_drops_junk():
    out = stats.normalize_stats({"str": "x", "dex": 99, "con": None, "hp": 30, "wis": 0})
    assert list(out)[:6] == list(stats.ABILITIES)
    assert out["str"] == 10
    assert out["dex"] == 20
    assert out["con"] == 10
    assert out["int"] == 10
    assert out["wis"] == 1
    assert out["cha"] == 10
    assert out["hp"] == 30
    assert stats.normalize_stats(None) == {key: 10 for key in stats.ABILITIES}
    assert stats.normalize_stats({"cha": "8"})["cha"] == 8


def test_estimate_stats_changes_with_class():
    warrior = stats.estimate_stats(class_role="guerreiro")
    mage = stats.estimate_stats("erudito de torre", "wizard")
    rogue = stats.estimate_stats(class_role="arqueiro")
    priest = stats.estimate_stats(class_role="clérigo")
    bard = stats.estimate_stats(class_role="Bardo")
    blank = stats.estimate_stats()
    assert blank == {key: 10 for key in stats.ABILITIES}
    assert warrior["str"] > mage["str"]
    assert warrior["con"] > 10
    assert mage["int"] > warrior["int"]
    assert rogue["dex"] > 10
    assert rogue["cha"] == 10
    assert priest["wis"] > 10
    assert bard["cha"] > bard["str"]
    for pool in (warrior, mage, rogue, priest, bard):
        assert all(stats.STAT_MIN <= value <= stats.STAT_MAX for value in pool.values())


def test_roll_stats_is_4d6_drop_lowest():
    rolls = []
    for _key in stats.ABILITIES:
        rolls.extend([1, 6, 6, 6])
    dropped = stats.roll_stats(_Seq(rolls))
    assert dropped == {key: 18 for key in stats.ABILITIES}
    worst = []
    for _key in stats.ABILITIES:
        worst.extend([1, 1, 1, 1])
    assert stats.roll_stats(_Seq(worst)) == {key: 3 for key in stats.ABILITIES}
    rolled = [stats.roll_stats(random.Random(seed)) for seed in range(40)]
    values = [value for pool in rolled for value in pool.values()]
    assert min(values) >= 3
    assert max(values) <= 18
    assert 1 not in values and 2 not in values


def test_stat_modifier_and_block():
    assert stats.stat_modifier(10) == 0
    assert stats.stat_modifier(12) == 1
    assert stats.stat_modifier(15) == 2
    assert stats.stat_modifier(9) == -1
    assert stats.stat_modifier(8) == -1
    assert stats.stat_modifier(1) == -5
    block = stats.stats_block({"str": 12, "dex": 15, "con": 8, "hp": 30})
    assert block.startswith("Força 12 (+1), Destreza 15 (+2), Constituição 8 (-1)")
    assert "hp" not in block


def test_sheet_without_stats_is_estimated_and_class_reaches_canon():
    original = {"appearance": "capuz"}
    done = npc_assets.ensure_sheet_complete(original, entry="Um ladino das docas.")
    assert original == {"appearance": "capuz"}
    assert done["class"] == "Ladino"
    assert done["status"] == "alive"
    assert "ladino" in done["notes"].lower()
    assert set(stats.ABILITIES) <= set(done["stats"])
    assert done["stats"]["dex"] > done["stats"]["str"]
    kept = npc_assets._normalize_sheet(
        {"appearance": "novas rugas"},
        {"class": "Mago", "appearance": "capuz"},
    )
    assert kept["class"] == "Mago"
    rendered = npc_assets.card_canon_text({
        "title": "Nyx",
        "entry": "Nas sombras.",
        "sheet": json.dumps({"class": "Ladino", "stats": done["stats"]}),
    })
    assert "Class: Ladino" in rendered
    assert "Stats: str " in rendered


def test_put_character_keeps_class_and_fills_empty_stats(client):
    adv = client.post("/api/adventures", json={"title": "Mesa"}).json()
    saved = client.put(
        f"/api/adventures/{adv['id']}/character",
        json={"title": "Lira", "sheet": {"class": "Bardo", "stats": {}}},
    )
    assert saved.status_code == 200, saved.text
    sheet = json.loads(saved.json()["card"]["sheet"])
    assert sheet["class"] == "Bardo"
    assert set(stats.ABILITIES) <= set(sheet["stats"])
    assert all(stats.STAT_MIN <= sheet["stats"][key] <= stats.STAT_MAX for key in stats.ABILITIES)
    assert sheet["stats"]["cha"] > sheet["stats"]["str"]

    with_extra = client.put(
        f"/api/adventures/{adv['id']}/character",
        json={"title": "Lira", "sheet": {"class": "Guerreiro", "stats": {"hp": 30, "str": "nope"}}},
    )
    assert with_extra.status_code == 200, with_extra.text
    extra = json.loads(with_extra.json()["card"]["sheet"])
    assert extra["class"] == "Guerreiro"
    assert extra["stats"]["hp"] == 30
    assert extra["stats"]["str"] > extra["stats"]["int"]


def test_build_sheet_merges_one_gap_fill_and_survives_failure(monkeypatch):
    calls: list[dict] = []

    first = json.dumps({
        "appearance": "red hair",
        "personality": "calm",
        "stats": {"str": 12, "dex": 12, "con": 12, "int": 12, "wis": 12, "cha": 12},
        "status": "alive",
        "notes": "keeps a key",
        "portrait_prompt": "red hair, bust",
    })

    def fake(_cfg, messages, **kwargs):
        calls.append({"messages": messages, **kwargs})
        if len(calls) in (1, 3):
            return first
        if len(calls) == 2:
            return json.dumps({
                "class": "Mago",
                "humor": "dry",
                "appearance": "SHOULD NOT APPLY",
            })
        raise RuntimeError("llm down")

    monkeypatch.setattr("app.llm_client.chat_completion", fake)
    built = npc_assets.build_sheet({}, title="Ada", entry="Wizard of the docks.")
    assert built["appearance"] == "red hair"
    assert built["class"] == "Mago"
    assert built["humor"] == "dry"
    assert built["portrait_prompt"] == "red hair, bust"
    assert calls[1]["temperature"] == 0.5
    assert calls[1]["max_tokens"] == 600
    assert "class" in calls[1]["messages"][0]["content"]

    broken = npc_assets.build_sheet({}, title="Ada", entry="Wizard of the docks.")
    assert broken["appearance"] == "red hair"
    assert broken["notes"] == "keeps a key"
    assert "class" not in broken
