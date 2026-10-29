"""Checagem de possibilidade antes do narrador.

O LLM auxiliar é um MockTransport: narrativa em SSE, veredito em JSON.
Bloqueio do world_facts não chega a abrir essa chamada.
"""

from __future__ import annotations

import json

import httpx

import pytest

from app import action_check, db, game_engine, llm_client, settings_store
from app import events as events_bus
from conftest import add_action, make_adventure, sse_body

VERDICT = {
    "possible": True,
    "blockers": [],
    "risks": ["a guarda pode ouvir"],
    "notes": "nada impede esperar junto ao portao",
    "cost": "ruido na trilha",
    "consequence": "a ronda muda de posto",
}


class _Narrator:
    def __init__(self, aux: str | None = None, *, fail_aux: bool = False,
                 tokens: tuple[str, ...] = ("The gate", " holds.")):
        self.aux = json.dumps(VERDICT) if aux is None else aux
        self.fail_aux = fail_aux
        self.tokens = tokens
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
                        text=sse_body(self.tokens),
                    )
                if self.fail_aux:
                    raise httpx.ConnectError("mock: aux down")
                return httpx.Response(200, json={
                    "choices": [{"message": {"content": self.aux}}],
                })
            return httpx.Response(404, json={"error": path})

        return httpx.MockTransport(handler)

    def aux_calls(self) -> list[dict]:
        return [call for call in self.calls if not call.get("stream")]

    def narrator(self) -> dict:
        return next(call for call in self.calls if call.get("stream"))


def _play(adventure_id: str, narrator: _Narrator, text: str, type_: str = "do",
          user: str = "") -> list[dict]:
    return list(game_engine.handle_action(
        adventure_id, type_, text, transport=narrator.transport(), user=user,
    ))


def _yard() -> dict:
    adv = make_adventure(plot_essentials="[Name: Caio]")
    add_action(adv["id"], 0, "start", "You stand in the yard.")
    return adv


def _rich(user: str = "hero") -> dict:
    adv = make_adventure(title="Gate", character_rules="sem magia de fogo")
    add_action(adv["id"], 0, "start", "You stand before the gate. Mira waits beside you.")
    npc_sheet = {
        "status": "alive",
        "class": "ranger",
        "life": {"mood": "wary", "activity": "watching the road"},
        "bonds": {"relationship": 55, "trust": 12, "fear": 5, "desire": 0, "boldness": 40},
    }
    with db.session() as conn:
        db.upsert_player_character(
            conn, adv["id"], user, title="Caio",
            sheet=json.dumps({
                "status": "alive",
                "class": "ladino",
                "appearance": "scar over the brow",
                "clothing": "grey cloak",
                "stats": {"dex": 16},
            }),
        )
        npc = db.insert_card(
            conn, adv["id"], title="Mira", type="character", entry="A scout.",
            sheet=json.dumps(npc_sheet),
        )
        db.insert_npc_memory(
            conn, adventure_id=adv["id"], card_id=npc["id"], kind="event",
            text="She hid the spare key.", mood="wary",
        )
        db.insert_memory(conn, adv["id"], "The gate key is hidden under the loose stone.")
        db.update_adventure_ui_state(conn, adv["id"], {
            "inventory": [{"name": "Torch", "qty": 1}],
            "dice": {"last": 12},
            "character": {"scar": "brow"},
        })
    return adv


def test_settings_block_stays_out_of_runtime_config(tmp_db):
    settings_store.load_file_settings(force=True)
    with db.session() as conn:
        cfg = settings_store.runtime_config(conn)
    assert "action_check" not in cfg
    block = settings_store.load_file_settings()["action_check"]
    assert block == {
        "enabled": True,
        "mode": "assist",
        "max_tokens": 300,
        "timeout_seconds": 20,
        "min_words": 4,
    }


def test_world_facts_block_skips_aux_models_without_npcs(tmp_db):
    narrator = _Narrator()
    events = _play(_yard()["id"], narrator, "draw your sword and conjure a bedroom")
    assert narrator.aux_calls() == []
    assert len(narrator.calls) == 1
    checks = [event for event in events if event["event"] == "action_check"]
    assert len(checks) == 1
    data = checks[0]["data"]
    assert set(data) == {"possible", "blockers", "risks", "notes", "cost", "consequence"}
    assert data["possible"] is False
    assert data["cost"] == ""
    assert data["consequence"] == ""
    assert data["risks"] == []
    blob = " ".join(data["blockers"]).casefold()
    assert "sword" in blob
    assert "bedroom" in blob or "quarto" in blob
    kinds = [event["event"] for event in events]
    assert kinds.index("action_check") < kinds.index("token")
    done = next(event for event in events if event["event"] == "done")
    assert done["data"]["action_check"] == data
    assert "player_action" in done["data"] and "action" in done["data"]
    content = narrator.narrator()["messages"][-1]["content"]
    assert content.startswith("<lore>\n[Verificacao de mundo]\n")
    assert "A acao do jogador e NAO E POSSIVEL." in content
    assert "Custo esperado: \n" in content
    assert "Consequência: \n" in content
    assert "Nao mencione esta verificacao ao jogador." in content


def test_valid_llm_verdict_reaches_the_narrator_and_the_event(tmp_db):
    llm_client.invalidate_slot_cache()
    narrator = _Narrator()
    events = _play(_rich()["id"], narrator, "wait quietly by the gate", user="hero")
    aux = narrator.aux_calls()
    checks = [call for call in aux if call.get("max_tokens") == 300]
    assert len(checks) == 1
    aux = checks
    assert aux[0]["temperature"] == 0.15
    assert aux[0]["id_slot"] == 2
    portrait = aux[0]["messages"][-1]["content"]
    for piece in ("Mira", "wary", "spare key", "ladino", "scar over the brow",
                  "Torch", "loose stone", "sem magia de fogo"):
        assert piece in portrait, piece
    checks = [event for event in events if event["event"] == "action_check"]
    assert len(checks) == 1
    assert checks[0]["data"] == VERDICT
    kinds = [event["event"] for event in events]
    assert kinds.index("action_check") < kinds.index("token")
    done = next(event for event in events if event["event"] == "done")
    assert done["data"]["action_check"] == VERDICT
    content = narrator.narrator()["messages"][-1]["content"]
    assert "<lore>\n[Verificacao de mundo]\n" in content
    assert "A acao do jogador e POSSIVEL." in content
    assert "A acao do jogador e NAO E POSSIVEL." not in content
    assert "a guarda pode ouvir" in content
    assert "nada impede esperar junto ao portao" in content
    assert "Custo esperado: ruido na trilha" in content
    assert "Consequência: a ronda muda de posto" in content


def test_garbage_llm_json_does_not_break_the_turn(tmp_db):
    narrator = _Narrator(aux="lixo sem json")
    events = _play(_yard()["id"], narrator, "wait quietly by the gate")
    assert [event["event"] for event in events if event["event"] == "action_check"] == []
    done = next(event for event in events if event["event"] == "done")
    assert "action_check" not in done["data"]
    assert done["data"]["action"]["text"] == "The gate holds."
    assert "[Verificacao de mundo]" not in narrator.narrator()["messages"][-1]["content"]


def test_llm_failure_does_not_break_the_turn(tmp_db):
    narrator = _Narrator(fail_aux=True)
    events = _play(_rich()["id"], narrator, "wait quietly by the gate")
    assert events[-1]["event"] == "done"
    assert "action_check" not in events[-1]["data"]
    assert [event["event"] for event in events if event["event"] == "action_check"] == []


def test_disabled_check_makes_no_call(tmp_db, monkeypatch):
    settings_store.load_file_settings(force=True)
    monkeypatch.setitem(settings_store._file_cache, "action_check", {
        "enabled": False, "mode": "assist", "max_tokens": 300,
        "timeout_seconds": 20, "min_words": 1,
    })
    narrator = _Narrator()
    events = _play(_rich()["id"], narrator, "draw your sword and conjure a bedroom")
    assert len(narrator.aux_calls()) == 1
    assert [event["event"] for event in events if event["event"] == "action_check"] == []
    assert events[-1]["event"] == "done"
    assert "[Verificacao de mundo]" not in narrator.narrator()["messages"][-1]["content"]


def test_short_story_and_continue_are_not_checked(tmp_db):
    adv = _rich()
    narrator = _Narrator()
    short = _play(adv["id"], narrator, "knock")
    assert len(narrator.aux_calls()) == 1
    assert [event["event"] for event in short if event["event"] == "action_check"] == []

    narrator.calls.clear()
    story = list(game_engine.handle_action(
        adv["id"], "story", "rain falls on the empty yard tonight",
        transport=narrator.transport(),
    ))
    assert narrator.aux_calls() == []
    assert [event["event"] for event in story if event["event"] == "action_check"] == []

    narrator.calls.clear()
    continued = list(game_engine.handle_continue(adv["id"], transport=narrator.transport()))
    assert len(narrator.aux_calls()) == 1
    assert [event["event"] for event in continued if event["event"] == "action_check"] == []


def test_collect_failure_still_narrates(tmp_db, monkeypatch):
    def boom(*_args, **_kwargs):
        raise RuntimeError("sem retrato")

    monkeypatch.setattr(action_check, "collect_state", boom)
    narrator = _Narrator()
    events = _play(_rich()["id"], narrator, "wait quietly by the gate")
    assert events[-1]["event"] == "done"
    assert len(narrator.aux_calls()) == 1
    assert [event["event"] for event in events if event["event"] == "action_check"] == []


def test_verdict_reaches_the_event_bus_with_the_action_id(tmp_db):
    """Os outros jogadores veem a checagem da acao (com o id para amarrar)."""
    narrator = _Narrator()
    adv = _rich()
    with events_bus.subscribe(adv["id"]) as sub:
        _play(adv["id"], narrator, "wait quietly by the gate", user="hero")
        published = []
        while True:
            event = sub.get(0.1)
            if event is None:
                break
            published.append(event)
    checks = [e for e in published if e.type == "action_check"]
    assert len(checks) == 1
    data = checks[0].data
    assert data["possible"] is True
    assert data["risks"] == VERDICT["risks"]
    assert data["action_id"] is not None
    assert data["seq"] is not None


def test_check_reads_world_after_sim_resolution(tmp_db, monkeypatch):
    def resolve(adventure_id, adventure, player_action, actions, user,
                llm_cfg, cfg, transport, cancel=None):
        with db.session() as conn:
            for card in db.list_cards(conn, adventure_id):
                if card.get("title") != "Mira":
                    continue
                sheet = json.loads(card["sheet"])
                sheet["status"] = "dead"
                db.update_card(conn, adventure_id, card["id"], {
                    "sheet": json.dumps(sheet),
                })
            row = db.get_adventure(conn, adventure_id) or {}
            sim = dict(row.get("sim_state") or {})
            sim["pack"] = "d20"
            sim["foes"] = {"mira": {"name": "Mira", "hp": 0, "hp_max": 8, "dead": True}}
            db.set_sim_state(conn, adventure_id, sim)
        return {"resolution": {
            "changes": [{"who": "Mira", "what": "PV", "from": 8, "to": 0}],
            "block": "",
        }}

    monkeypatch.setattr("app.sim.engine.run_turn", resolve)
    narrator = _Narrator()
    events = _play(_rich()["id"], narrator, "wait quietly by the gate", user="hero")
    kinds = [event["event"] for event in events]
    assert kinds.index("rules_roll") < kinds.index("action_check") < kinds.index("token")
    checks = [
        call for call in narrator.aux_calls()
        if "você verifica" in (call["messages"][0]["content"] or "").casefold()
    ]
    assert len(checks) == 1
    portrait = checks[0]["messages"][-1]["content"]
    assert '"status": "dead"' in portrait
    assert '"hp": 0' in portrait
    assert '"what": "PV"' in portrait


def _capture_check(monkeypatch):
    calls = []

    def fake(cfg, messages, **kwargs):
        calls.append((messages, kwargs))
        return json.dumps(VERDICT)

    monkeypatch.setattr(action_check.llm_client, "chat_completion", fake)
    return calls


def _bounded_state() -> dict:
    canon = "Cânone essencial " * 200
    return {
        "where": "gate",
        "canon_blob": canon,
        "character_rules": "regra " * 400,
        "blocked_items": ["lanterna antiga", "corda"],
        "blocked_places": ["quarto selado"],
        "resisted": ["Mira"],
        "invented_people": ["Fantasma Novo"],
        "memories": [f"MEM-{index} " + ("fato " * 80) for index in range(6)],
        "recent_actions": [
            {"type": "do", "text": f"ACAO-{index} " + ("passo " * 80)}
            for index in range(10)
        ],
        "absent": [
            {"name": f"Ausente {index}", "status": "missing", "lore": "segredo", "stats": {"str": 99}}
            for index in range(19)
        ] + ["SoNome", {"name": "Extra", "status": "alive", "lore": "nao"}],
        "present_npcs": [{
            "name": "Mira",
            "status": "alive",
            "class": "ranger",
            "stats": {"dex": 16, "hp": 20},
            "canon": canon,
            "bonds": {"trust": 3},
            "mood_line": "Mira — wary",
            "memory": "She hid the spare key. " + ("detalhe " * 80),
        }],
        "player": {
            "name": "Caio",
            "class": "ladino",
            "appearance": "scar over the brow",
            "status": "alive",
            "clothing": "grey cloak",
            "stats": {"dex": 16, "hp": 11},
            "canon": canon,
            "inventory": [{"name": f"item-{index}", "detail": "x" * 80} for index in range(40)],
            "dice": {"last": 17, "history": [1, 2, 17], "note": "ignore"},
            "character": {"scar": "brow"},
            "ui_state": {"note": "z" * 2000, "flag": True},
        },
        "sim": {
            "marker": "SIM-COMPLETO",
            "actors": [{"name": "Caio", "hp": 11, "dead": False}],
            "log": [f"evt-{index}" for index in range(30)],
        },
    }


def test_hard_blockers_and_allow_llm_never_touch_the_network(tmp_db, monkeypatch):
    def boom(*args, **kwargs):
        raise AssertionError("network")

    monkeypatch.setattr(action_check.llm_client, "chat_completion", boom)
    state = {
        "blocked_items": ["lanterna antiga"],
        "blocked_places": ["quarto selado"],
        "resisted": ["Mira"],
        "invented_people": ["Fantasma Novo"],
    }
    expected = [
        "Sem item: lanterna antiga.",
        "Não existe: quarto selado.",
        "Mira recusa.",
        "Fantasma Novo não está estabelecido no mundo.",
    ]
    assert action_check.hard_blockers(state) == expected
    blocked = action_check.check_action(
        "abro a porta", "do", state, llm_cfg={}, transport=None, cfg={},
        allow_llm=False,
    )
    assert blocked.possible is False
    assert blocked.blockers == expected
    assert blocked.risks == []
    assert blocked.cost == "" and blocked.consequence == ""
    supplied = action_check.check_action(
        "abro a porta", "do", state, llm_cfg={}, transport=None, cfg={},
        blockers=["motivo externo"],
    )
    assert supplied.possible is False
    assert supplied.blockers == ["motivo externo"]
    empty = action_check.check_action(
        "eu falo baixo", "say", state, llm_cfg={}, transport=None, cfg={},
        allow_llm=False, blockers=[],
    )
    assert empty.to_dict() == {
        "possible": True, "blockers": [], "risks": [], "notes": "",
        "cost": "", "consequence": "",
    }


def test_check_prompt_bounds_keep_present_stats_sim_and_blockers(monkeypatch):
    calls = _capture_check(monkeypatch)
    state = _bounded_state()
    original_memory = state["present_npcs"][0]["memory"]
    original_sim = json.loads(json.dumps(state["sim"]))
    verdict = action_check.check_action(
        "exameno o portao", "do", state, llm_cfg={}, transport=None, cfg={},
        blockers=[],
    )
    assert verdict.possible is True
    assert calls[0][1]["role"] == "turn_check"
    assert len(state["memories"]) == 6
    assert isinstance(state["player"]["inventory"], list)
    assert state["player"]["ui_state"]["flag"] is True
    assert state["sim"] == original_sim
    world = json.loads(calls[0][0][1]["content"])["world"]
    assert world["player"]["name"] == "Caio"
    assert world["player"]["stats"] == {"dex": 16, "hp": 11}
    assert world["player"]["appearance"] == "scar over the brow"
    assert world["player"]["dice"] == {"last": 17}
    assert isinstance(world["player"]["ui_state"], str)
    assert len(world["player"]["ui_state"]) <= 1200
    assert isinstance(world["player"]["inventory"], str)
    assert len(world["player"]["inventory"]) <= 1200
    assert len(world["player"]["canon"]) <= 1200
    assert world["player"]["canon"].startswith("Cânone essencial")
    assert len(world["canon_blob"]) <= 1200
    assert len(world["character_rules"]) <= 1200
    assert world["character_rules"].startswith("regra")
    assert len(state["character_rules"]) > 1200
    mira = world["present_npcs"][0]
    assert mira["stats"] == {"dex": 16, "hp": 20}
    assert mira["bonds"] == {"trust": 3}
    assert mira["mood_line"] == "Mira — wary"
    assert mira["memory"] == original_memory
    assert len(mira["canon"]) <= 1200
    assert world["sim"] == original_sim
    assert world["blocked_items"] == ["lanterna antiga", "corda"]
    assert world["blocked_places"] == ["quarto selado"]
    assert world["resisted"] == ["Mira"]
    assert world["invented_people"] == ["Fantasma Novo"]
    assert [item[:5] for item in world["memories"]] == [f"MEM-{index}" for index in range(4)]
    assert all(len(item) <= 160 for item in world["memories"])
    assert "MEM-4" not in json.dumps(world["memories"])
    assert [item["text"][:6] for item in world["recent_actions"]] == [f"ACAO-{index}" for index in range(4, 10)]
    assert all(len(item["text"]) <= 240 for item in world["recent_actions"])
    assert "ACAO-0" not in json.dumps(world["recent_actions"])
    assert len(world["absent"]) == 20
    assert world["absent"][0] == {"name": "Ausente 0", "status": "missing"}
    assert world["absent"][-1] == {"name": "SoNome", "status": ""}
    assert all(set(item) == {"name", "status"} for item in world["absent"])
    assert "segredo" not in json.dumps(world["absent"])
    assert "Extra" not in json.dumps(world["absent"])


def test_cancelled_check_still_propagates(monkeypatch):
    def cancel(*args, **kwargs):
        raise llm_client.LlmCancelled("stop")

    monkeypatch.setattr(action_check.llm_client, "chat_completion", cancel)
    with pytest.raises(llm_client.LlmCancelled):
        action_check.check_action(
            "olho", "do", {"player": {"name": "Caio"}},
            llm_cfg={}, transport=None, cfg={}, blockers=[],
        )
