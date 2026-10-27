"""Turno com motor: rules_roll, retry, undo e recusa antes do narrador."""

from __future__ import annotations

import json

import httpx

from app import db, game_engine
from app.sim import packs, rules, state

from conftest import add_action, make_adventure, sse_body

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
VERDICT = {"possible": True, "blockers": [], "risks": [], "notes": ""}


class Bag:
    def __init__(self) -> None:
        self.rolls = [15, 6, 4]

    def randint(self, _a: int, _b: int) -> int:
        if not self.rolls:
            return 1
        return self.rolls.pop(0)

    def random(self) -> float:
        return 0.0

    def choice(self, seq):
        return seq[0]


class _Narrator:
    def __init__(self, intent_body: dict) -> None:
        self.intent_body = json.dumps(intent_body)
        self.calls: list[dict] = []

    def transport(self) -> httpx.MockTransport:
        def handler(request: httpx.Request) -> httpx.Response:
            path = request.url.path
            if path.endswith("/props"):
                return httpx.Response(200, json={"total_slots": 4})
            if path.endswith("/models"):
                return httpx.Response(200, json={"data": [{"id": "mock-model"}]})
            if path.endswith("/chat/completions"):
                payload = json.loads(request.content.decode("utf-8"))
                self.calls.append(payload)
                if payload.get("stream"):
                    return httpx.Response(
                        200, headers={"content-type": "text/event-stream"},
                        text=sse_body(("O golpe ", "acerta.")),
                    )
                system = (payload.get("messages") or [{}])[0].get("content") or ""
                body = self.intent_body if "Classifique" in system else json.dumps(VERDICT)
                return httpx.Response(200, json={
                    "choices": [{"message": {"content": body}}],
                })
            return httpx.Response(404, json={"error": path})

        return httpx.MockTransport(handler)


def _armed(monkeypatch) -> dict:
    monkeypatch.setattr("app.sim.rng.gameplay_rng", lambda _state: Bag())
    adv = make_adventure(title="Gate", owner="hero")
    add_action(adv["id"], 0, "start", "You stand in the yard.")
    sim = state.empty()
    sim["pack"] = "d20"
    rules.ensure_actor(
        sim, packs.load_pack("d20"), "hero", "Aldric",
        {"str": 16, "dex": 14, "con": 14, "int": 10, "wis": 10, "cha": 10},
    )
    with db.session() as conn:
        db.set_sim_state(conn, adv["id"], sim)
        db.insert_card(
            conn, adv["id"], title="Goblin", type="character", entry="verde",
            sheet=json.dumps({"status": "alive"}),
        )
    return adv


def _events(adventure_id: str, narrator: _Narrator, text: str) -> list[dict]:
    return list(game_engine.handle_action(
        adventure_id, "do", text, transport=narrator.transport(), user="hero",
    ))


def test_attack_emits_rules_roll_before_tokens_and_matches_state(tmp_db, monkeypatch):
    adv = _armed(monkeypatch)
    narrator = _Narrator(ATTACK)
    events = _events(adv["id"], narrator, "Ataco o goblin com minha espada")
    names = [event["event"] for event in events]
    assert "rules_roll" in names and "token" in names
    assert names.index("rules_roll") < names.index("token")
    resolution = next(event["data"] for event in events if event["event"] == "rules_roll")
    assert any("Goblin" in step["text"] for step in resolution["steps"])
    with db.session() as conn:
        stored = db.get_adventure(conn, adv["id"])["sim_state"]
    foe = stored["foes"]["goblin"]
    pv = next(change for change in resolution["changes"] if change["who"] == "Goblin")
    assert foe["hp"] == pv["to"] == 0
    assert foe["dead"] is True
    stream = next(call for call in narrator.calls if call.get("stream"))
    blob = json.dumps(stream["messages"], ensure_ascii=False)
    assert "Resolução mecânica" in blob
    assert names.index("rules_roll") < names.index("action_check") < names.index("token")
    checks = [
        call for call in narrator.calls
        if not call.get("stream")
        and "você verifica" in ((call.get("messages") or [{}])[0].get("content") or "").casefold()
    ]
    assert checks
    portrait = checks[0]["messages"][-1]["content"]
    assert '"status": "dead"' in portrait
    assert '"hp": 0' in portrait


def test_retry_reuses_the_block_without_changing_state(tmp_db, monkeypatch):
    adv = _armed(monkeypatch)
    _events(adv["id"], _Narrator(ATTACK), "Ataco o goblin com minha espada")
    with db.session() as conn:
        before = db.get_adventure(conn, adv["id"])["sim_state"]
    narrator = _Narrator(ATTACK)
    retried = list(game_engine.handle_retry(adv["id"], transport=narrator.transport()))
    assert any(event["event"] == "token" for event in retried)
    assert not any(event["event"] == "rules_roll" for event in retried)
    with db.session() as conn:
        after = db.get_adventure(conn, adv["id"])["sim_state"]
    assert after == before
    stream = next(call for call in narrator.calls if call.get("stream"))
    blob = json.dumps(stream["messages"], ensure_ascii=False)
    assert "Resolução mecânica" in blob


def test_undo_restores_state_and_card(tmp_db, monkeypatch):
    adv = _armed(monkeypatch)
    _events(adv["id"], _Narrator(ATTACK), "Ataco o goblin com minha espada")
    game_engine.handle_undo(adv["id"])
    with db.session() as conn:
        stored = db.get_adventure(conn, adv["id"])["sim_state"]
        cards = db.list_cards(conn, adv["id"])
    assert stored["foes"] == {}
    sheet = json.loads(cards[0]["sheet"])
    assert sheet["status"] == "alive"


def test_missing_item_is_refused_before_the_narrator(tmp_db, monkeypatch):
    adv = _armed(monkeypatch)
    narrator = _Narrator({
        **ATTACK,
        "kind": "use_item",
        "item": "poção de cura",
        "target": "",
        "weapon": "",
    })
    events = _events(adv["id"], narrator, "Bebo minha poção de cura")
    assert not any(event["event"] == "token" for event in events)
    assert any(event["event"] == "refused" for event in events)
    error = next(event for event in events if event["event"] == "error")
    assert error["data"]["code"] == "rules_refused"
    assert not any(call.get("stream") for call in narrator.calls)
    with db.session() as conn:
        actions = db.list_actions(conn, adv["id"])
    assert [action["type"] for action in actions] == ["start"]
