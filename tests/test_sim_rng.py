"""RNG de gameplay: mesma seed, contador persistido e undo coerente."""

from __future__ import annotations

from pathlib import Path

from app import db
from app.sim import dice, engine, packs, rng as sim_rng, rules, state

from conftest import add_action, make_adventure

ROOT = Path(__file__).resolve().parents[1]
SEED = 99

ATTACK = {
    "kind": "attack",
    "ability": "str",
    "skill": "",
    "target": "Goblin",
    "toughness": "weak",
    "hostile": False,
    "weapon": "espada",
    "item": "",
    "gold": 0,
    "difficulty": "medium",
    "advantage": "",
    "minutes": 5,
    "activity": "other",
    "shock": "none",
    "heal": False,
}


def test_gameplay_modules_do_not_call_system_random():
    blobs = []
    for rel in (
        "backend/app/sim/dice.py",
        "backend/app/sim/engine.py",
        "backend/app/sim/runtime.py",
        "backend/app/routes/sim.py",
    ):
        blobs.append((ROOT / rel).read_text(encoding="utf-8"))
    assert all("SystemRandom" not in blob for blob in blobs)


def test_same_seed_repeats_the_same_sequence():
    left = {"rng": {"seed": SEED, "counter": 0}}
    right = {"rng": {"seed": SEED, "counter": 0}}
    left_rng = sim_rng.gameplay_rng(left)
    right_rng = sim_rng.gameplay_rng(right)
    assert [left_rng.randint(1, 20) for _ in range(6)] == [right_rng.randint(1, 20) for _ in range(6)]
    assert left["rng"]["counter"] == right["rng"]["counter"] == 6
    assert left["rng"]["seed"] == right["rng"]["seed"] == SEED


def test_roll_endpoint_persists_seed_and_counter(client):
    from conftest import TEST_AUTH_USER
    adv = make_adventure(title="Dados", owner=TEST_AUTH_USER)
    with db.session() as conn:
        sim = state.empty()
        sim["rng"] = {"seed": SEED, "counter": 0}
        db.set_sim_state(conn, adv["id"], sim)
    first = client.post(f"/api/adventures/{adv['id']}/sim/roll", json={"expr": "1d20"})
    second = client.post(
        f"/api/adventures/{adv['id']}/sim/roll",
        json={"expr": "d20", "mode": "adv"},
    )
    assert first.status_code == 200, first.text
    assert second.status_code == 200, second.text
    probe = {"rng": {"seed": SEED, "counter": 0}}
    rng = sim_rng.gameplay_rng(probe)
    rolled = dice.roll("1d20", rng)
    chosen, rolls = dice.roll_d20(rng, "adv")
    assert first.json()["rolls"] == rolled.rolls
    assert first.json()["total"] == rolled.total
    assert second.json()["rolls"] == rolls
    assert second.json()["total"] == chosen
    with db.session() as conn:
        stored = db.get_adventure(conn, adv["id"])["sim_state"]
    assert stored["rng"]["seed"] == SEED
    assert stored["rng"]["counter"] == probe["rng"]["counter"] == 3


def test_seed_is_created_once(client):
    from conftest import TEST_AUTH_USER
    adv = make_adventure(title="Semente", owner=TEST_AUTH_USER)
    assert client.post(f"/api/adventures/{adv['id']}/sim/roll", json={"expr": "1d6"}).status_code == 200
    with db.session() as conn:
        first = db.get_adventure(conn, adv["id"])["sim_state"]["rng"]
    assert client.post(f"/api/adventures/{adv['id']}/sim/roll", json={"expr": "1d6"}).status_code == 200
    with db.session() as conn:
        second = db.get_adventure(conn, adv["id"])["sim_state"]["rng"]
    assert second["seed"] == first["seed"]
    assert first["counter"] == 1
    assert second["counter"] == 2


def test_turn_undo_restores_the_counter(tmp_db, monkeypatch):
    monkeypatch.setattr("app.sim.intent.classify", lambda *args, **kwargs: dict(ATTACK))
    adv = make_adventure(title="Turno", owner="hero")
    action = add_action(adv["id"], 0, "do", "Ataco o goblin com a espada")
    sim = state.empty()
    sim["pack"] = "d20"
    sim["rng"] = {"seed": SEED, "counter": 0}
    rules.ensure_actor(
        sim, packs.load_pack("d20"), "hero", "Aldric",
        {"str": 16, "dex": 14, "con": 14, "int": 10, "wis": 10, "cha": 10},
    )
    with db.session() as conn:
        db.set_sim_state(conn, adv["id"], sim)
        adventure = db.get_adventure(conn, adv["id"])
    cfg = {"sim": {"enabled": True, "min_words": 3}}
    first = engine.run_turn(adv["id"], adventure, action, [action], "hero", {}, cfg, None)
    assert first and first.get("resolution")
    with db.session() as conn:
        advanced = db.get_adventure(conn, adv["id"])["sim_state"]["rng"]
    assert advanced["seed"] == SEED
    assert advanced["counter"] > 0
    with db.session() as conn:
        assert engine.revert(conn, adv["id"], action["id"]) is True
        restored = db.get_adventure(conn, adv["id"])["sim_state"]
    assert restored["rng"]["seed"] == SEED
    assert restored["rng"]["counter"] == 0
    with db.session() as conn:
        adventure = db.get_adventure(conn, adv["id"])
    second = engine.run_turn(adv["id"], adventure, action, [action], "hero", {}, cfg, None)
    assert second["resolution"]["steps"] == first["resolution"]["steps"]
    with db.session() as conn:
        replayed = db.get_adventure(conn, adv["id"])["sim_state"]["rng"]
    assert replayed["counter"] == advanced["counter"]
    assert replayed["seed"] == SEED
