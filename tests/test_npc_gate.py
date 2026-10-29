"""O adjudicador é uma chamada LLM separada e sem pisos/frases prontas."""

from __future__ import annotations

import json

from app import db, npc_assets, npc_gate


def _card(title, sheet, entry=""):
    return {
        "type": "character",
        "title": title,
        "entry": entry,
        "sheet": json.dumps(sheet),
    }


def _capture(monkeypatch, response: str):
    calls = []

    def fake(cfg, messages, **kwargs):
        calls.append((cfg, messages, kwargs))
        return response

    monkeypatch.setattr("app.llm_client.chat_completion", fake)
    return calls


def test_ensure_saved_is_idempotent_and_creates_body_state(tmp_db):
    with db.session() as conn:
        adv = db.insert_adventure(conn, title="Adjudicação")
    with db.session() as conn:
        elara = db.insert_card(
            conn, adv["id"], title="Elara", type="character",
            entry="Uma inimiga antiga, desconfiada de todos.",
        )
        first = npc_gate.ensure_saved(conn, adv["id"])
        again = npc_gate.ensure_saved(conn, adv["id"])
        sheet = npc_assets.parse_sheet(
            db.get_card(conn, adv["id"], elara["id"])["sheet"])
    assert first == 1
    assert again == 0
    assert sheet["alignment"] == "oposto"
    assert sheet["body_state"]["pain"] == 0
    assert sheet["body_state"]["conditions"] == []


def test_decide_sends_every_person_body_and_memories(tmp_db, monkeypatch):
    with db.session() as conn:
        adv = db.insert_adventure(conn, title="Adjudicação")
        elara = db.insert_card(
            conn, adv["id"], title="Elara", type="character",
            entry="Guarda ferida na clareira.",
            sheet=json.dumps({
                "alignment": "desconfiado",
                "body_state": {"v": 1, "pain": 6, "conditions": [{
                    "id": "c1", "kind": "cut", "location": "left cheek",
                    "severity": 3, "description": "corte no rosto",
                    "visual_en": "small cut on the left cheek", "active": True,
                    "portrait_visible": True, "scene_visible": True,
                }]},
                "expression": "guarded",
            }),
        )
        db.insert_card(
            conn, adv["id"], title="Bram", type="character",
            entry="Mercador que observa.",
        )
        db.insert_action(conn, adv["id"], 0, "start", "Elara segura a faca.")
        npc_life = __import__("app.npc_life", fromlist=["add_memory"])
        npc_life.add_memory(conn, adv["id"], elara["id"], "event",
                            "O jogador já tentou passar por ela.")
    calls = _capture(monkeypatch, json.dumps({
        "decision": "partial",
        "outcome": "Elara barra a passagem, mas deixa o jogador falar.",
        "reactions": [{"npc": "Elara", "card_id": elara["id"],
                       "reaction": "Ela ergue a faca.", "line": "Fale rápido."}],
        "initiative": None,
        "cue": "Mostre a guarda ferida e desconfiada.",
    }))
    result = npc_gate.decide(
        adv["id"], "Eu empurro Elara e passo.", {}, None,
        world_check={"possible": False, "blockers": ["parede"]})
    assert result["decision"] == "partial"
    assert result["reactions"][0]["npc"] == "Elara"
    assert result["cue"]
    payload = json.loads(calls[0][1][1]["content"])
    assert payload["action"] == "Eu empurro Elara e passo."
    assert payload["world_check"]["possible"] is False
    people = {item["name"]: item for item in payload["people"]}
    assert set(people) == {"Elara", "Bram"}
    assert people["Elara"]["body"]["pain"] == 6
    assert "small cut" in people["Elara"]["body"]["visual_portrait"]
    assert people["Elara"]["memories"]


def test_no_floor_model_may_allow_a_harm_attempt(tmp_db, monkeypatch):
    with db.session() as conn:
        adv = db.insert_adventure(conn, title="Sem piso")
        db.insert_card(conn, adv["id"], title="Elara", type="character",
                       entry="Uma criança frágil.")
        db.insert_action(conn, adv["id"], 0, "start", "Elara está aqui.")
    _capture(monkeypatch, json.dumps({
        "decision": "allow",
        "outcome": "Elara cambaleia e cai.",
        "reactions": [{"npc": "Elara", "card_id": "", "reaction": "Ela cai."}],
        "cue": "Ela cai, ferida.",
    }))
    result = npc_gate.decide(adv["id"], "Eu bato nela.", {}, None)
    assert result["decision"] == "allow"
    assert result["outcome"] == "Elara cambaleia e cai."
    assert "não deixa" not in result["cue"]


def test_llm_failure_returns_none_without_canned_text(tmp_db, monkeypatch):
    with db.session() as conn:
        adv = db.insert_adventure(conn, title="Falha")
        db.insert_card(conn, adv["id"], title="Elara", type="character",
                       entry="Guarda.")
        db.insert_action(conn, adv["id"], 0, "start", "Elara está aqui.")

    def down(*args, **kwargs):
        raise RuntimeError("offline")

    monkeypatch.setattr("app.llm_client.chat_completion", down)
    assert npc_gate.decide(adv["id"], "Eu passo.", {}, None) is None


def test_own_beat_uses_llm_and_returns_initiative(tmp_db, monkeypatch):
    with db.session() as conn:
        adv = db.insert_adventure(conn, title="Iniciativa")
        elara = db.insert_card(conn, adv["id"], title="Elara", type="character",
                               entry="Guarda inquieta.")
        db.insert_action(conn, adv["id"], 0, "start", "Elara está aqui.")
    calls = _capture(monkeypatch, json.dumps({
        "decision": "own_beat",
        "outcome": "Elara decide falar.",
        "reactions": [],
        "initiative": {"npc": "Elara", "card_id": elara["id"],
                       "line": "Elara bate a lança no chão."},
        "cue": "Deixe Elara agir.",
    }))
    result = npc_gate.own_beat(adv["id"], {}, None)
    assert calls[0][2]["role"] == "turn_gate"
    assert result["decision"] == "own_beat"
    assert result["initiative"]["npc"] == "Elara"


def test_remember_stores_only_ai_reactions(tmp_db, monkeypatch):
    with db.session() as conn:
        adv = db.insert_adventure(conn, title="Memória")
        elara = db.insert_card(conn, adv["id"], title="Elara", type="character",
                               entry="Guarda.")
    result = {
        "decision": "refuse",
        "reactions": [{"npc": "Elara", "card_id": elara["id"],
                       "reaction": "Ela não abaixa a lança.", "line": ""}],
    }
    with db.session() as conn:
        saved = npc_gate.remember(conn, adv["id"], result)
        memories = __import__("app.npc_life", fromlist=["list_memories"]).list_memories(
            conn, adv["id"], elara["id"])
    assert saved == 1
    assert memories[0]["text"] == "Ela não abaixa a lança."


def test_no_living_npc_skips_llm_with_neutral_allow(tmp_db, monkeypatch):
    with db.session() as conn:
        adv = db.insert_adventure(conn, title="Vazio")
        db.insert_card(conn, adv["id"], title="Jogador", type="character",
                       player_username="caio", entry="O protagonista.")
        db.insert_card(
            conn, adv["id"], title="Fantasma", type="character",
            entry="Já se foi.",
            sheet=json.dumps({"status": "dead"}),
        )
        db.insert_card(conn, adv["id"], title="Taverna", type="location",
                       entry="Salão vazio.")
    calls = _capture(monkeypatch, "não deveria ser chamado")
    result = npc_gate.decide(adv["id"], "Eu abro a porta.", {}, None)
    assert calls == []
    assert result["decision"] == "allow"
    assert result["reactions"] == []
    assert result["initiative"] is None
    assert result["outcome"]
    assert result["cue"] == result["outcome"]
    assert npc_gate.own_beat(adv["id"], {}, None) is None
    assert calls == []


def test_dead_only_roster_still_skips_llm(tmp_db, monkeypatch):
    with db.session() as conn:
        adv = db.insert_adventure(conn, title="Cemitério")
        db.insert_card(
            conn, adv["id"], title="Elara", type="character",
            entry="Guarda caída.",
            sheet=json.dumps({"status": "missing"}),
        )
    calls = _capture(monkeypatch, "não deveria ser chamado")
    result = npc_gate.decide(adv["id"], "Eu procuro Elara.", {}, None)
    assert calls == []
    assert result["decision"] == "allow"
    assert result["reactions"] == []


def test_living_npc_still_calls_llm_with_token_cap(tmp_db, monkeypatch):
    with db.session() as conn:
        adv = db.insert_adventure(conn, title="Com gente")
        elara = db.insert_card(conn, adv["id"], title="Elara", type="character",
                               entry="Guarda na porta.")
    calls = _capture(monkeypatch, json.dumps({
        "decision": "refuse",
        "outcome": "Elara barra a porta.",
        "reactions": [{"npc": "Elara", "card_id": elara["id"],
                       "reaction": "Ela cruza a lança.", "line": "Não."}],
        "initiative": None,
        "cue": "Mostre a recusa.",
    }))
    result = npc_gate.decide(adv["id"], "Eu entro.", {}, None)
    assert result["decision"] == "refuse"
    assert result["reactions"][0]["npc"] == "Elara"
    assert len(calls) == 1
    assert calls[0][2]["role"] == "turn_gate"


def test_cue_block_never_invents_player_prose():
    cue = npc_gate.cue_block({
        "decision": "refuse",
        "outcome": "Elara não deixa passar.",
        "reactions": [{"npc": "Elara", "reaction": "Ela ergue a lança.", "line": "Volte."}],
        "initiative": None,
        "cue": "Mostre a recusa.",
    })
    assert "Elara não deixa passar." in cue
    assert "NÃO tem o efeito" in cue or "NÃO" in cue
    assert "Fale" not in cue or "Fala possível" in cue


def test_decide_parses_visuals_and_rejects_bad_relation_targets(tmp_db, monkeypatch):
    with db.session() as conn:
        adv = db.insert_adventure(conn, title="Vínculo")
        elara = db.insert_card(conn, adv["id"], title="Elara", type="character",
                               entry="Guarda.")
        player = db.insert_card(
            conn, adv["id"], title="Caio", type="character",
            player_username="caio", entry="Protagonista.")
    _capture(monkeypatch, json.dumps({
        "decision": "partial",
        "outcome": "Elara apanha e não cai.",
        "reactions": [{
            "npc": "Elara", "card_id": elara["id"],
            "reaction": "Ela segura o rosto.", "line": "",
            "emotion": "fear", "pose": "hand on cheek",
        }],
        "initiative": None,
        "relation_updates": [
            {"card_id": elara["id"], "target": "player",
             "delta": {"fear": 50, "trust": -4, "affinity": 9},
             "reason": "apanhou", "evidence": "o soco no rosto"},
            {"card_id": "ninguem", "target": "player",
             "delta": {"trust": -8}, "reason": "golpe", "evidence": "o soco"},
            {"card_id": elara["id"], "target": player["id"],
             "delta": {"trust": -8}, "reason": "golpe", "evidence": "o soco"},
            {"card_id": player["id"], "target": "player",
             "delta": {"fear": 8}, "reason": "golpe", "evidence": "o soco"},
        ],
        "cue": "Mostre o golpe.",
    }))
    result = npc_gate.decide(adv["id"], "Eu soco Elara.", {}, None, user="caio")
    assert result["reactions"][0]["emotion"] == "fear"
    assert result["reactions"][0]["pose"] == "hand on cheek"
    assert result["initiative"] is None
    assert len(result["relation_updates"]) == 1
    update = result["relation_updates"][0]
    assert update["card_id"] == elara["id"]
    assert update["target"] == "player"
    assert update["delta"] == {"fear": 20, "trust": -4}
    assert "affinity" not in update["delta"]


def test_own_beat_keeps_reactions_without_initiative(tmp_db, monkeypatch):
    with db.session() as conn:
        adv = db.insert_adventure(conn, title="Sem iniciativa")
        elara = db.insert_card(conn, adv["id"], title="Elara", type="character",
                               entry="Guarda inquieta.")
        db.insert_action(conn, adv["id"], 0, "start", "Elara está aqui.")
    _capture(monkeypatch, json.dumps({
        "decision": "allow",
        "outcome": "Elara observa.",
        "reactions": [{
            "npc": "Elara", "card_id": elara["id"],
            "reaction": "Ela aperta a lança.", "line": "",
            "emotion": "anger",
        }],
        "initiative": None,
        "cue": "Deixe Elara presente.",
    }))
    result = npc_gate.own_beat(adv["id"], {}, None)
    assert result is not None
    assert result["decision"] == "allow"
    assert result["initiative"] is None
    assert result["reactions"][0]["npc"] == "Elara"
    assert result["reactions"][0]["emotion"] == "anger"


def test_remember_one_memory_per_npc_without_the_cue(tmp_db):
    with db.session() as conn:
        adv = db.insert_adventure(conn, title="Memória única")
        elara = db.insert_card(conn, adv["id"], title="Elara", type="character",
                               entry="Guarda.")
        bram = db.insert_card(conn, adv["id"], title="Bram", type="character",
                              entry="Mercador.")
    cue = "Não revele o plano secreto do porto"
    result = {
        "decision": "allow",
        "cue": cue,
        "reactions": [
            {"npc": "Elara", "card_id": elara["id"],
             "reaction": "Ela ergue a lança. " + cue, "line": "Pare."},
            {"npc": "Elara", "card_id": elara["id"],
             "reaction": "Ela ergue a lança. " + cue, "line": "Pare."},
            {"npc": "Bram", "card_id": bram["id"],
             "reaction": "Bram recua.", "line": ""},
        ],
        "initiative": {"npc": "Elara", "card_id": elara["id"],
                       "line": "Ela fecha o portão."},
    }
    with db.session() as conn:
        saved = npc_gate.remember(conn, adv["id"], result)
        elara_mem = __import__("app.npc_life", fromlist=["list_memories"]).list_memories(
            conn, adv["id"], elara["id"])
        bram_mem = __import__("app.npc_life", fromlist=["list_memories"]).list_memories(
            conn, adv["id"], bram["id"])
    assert saved == 2
    assert len(elara_mem) == 1
    assert "lança" in elara_mem[0]["text"]
    assert "portão" in elara_mem[0]["text"]
    assert "secreto" not in elara_mem[0]["text"]
    assert cue not in elara_mem[0]["text"]
    assert bram_mem[0]["text"] == "Bram recua."


def test_gate_retries_once_with_legacy_budget_when_json_is_truncated(monkeypatch):
    responses = iter([
        '{"decision":"allow","reactions":[',
        json.dumps({"decision": "allow", "outcome": "Segue.",
                    "reactions": [], "initiative": None, "cue": "Segue."}),
    ])
    budgets = []

    def fake_chat(*args, **kwargs):
        budgets.append(kwargs["max_tokens"])
        return next(responses)

    monkeypatch.setattr(npc_gate.llm_client, "chat_completion", fake_chat)
    result = npc_gate._call({}, {"action": "seguir"}, None)
    assert result["decision"] == "allow"
    assert budgets == [npc_gate.GATE_MAX_TOKENS, 900]
    assert budgets[1] > budgets[0]


def test_gate_payload_keeps_present_sheet_and_bounds_the_rest(tmp_db, monkeypatch):
    personality = "desconfiada da ponte " * 30
    long_memory = "MEMORIA-LONGA " + ("lembranca " * 40)
    opening = "COMECO-UNICO "
    ending = "Elara permanece FIM-UNICO"
    scene = opening + ("cena " * 800) + ending
    with db.session() as conn:
        adv = db.insert_adventure(conn, title="Limite")
        elara = db.insert_card(
            conn, adv["id"], title="Elara", type="character",
            entry="Guarda na clareira.",
            sheet=json.dumps({
                "status": "alive",
                "class": "ranger",
                "personality": personality,
                "body_state": {"v": 1, "pain": 6, "conditions": []},
                "expression": "guarded",
                "bonds": {"trust": 12},
            }),
        )
        conn.execute(
            "UPDATE story_cards SET created_at=? WHERE id=?",
            ("2026-01-01T00:00:00.000Z", elara["id"]),
        )
        for index in range(22):
            card = db.insert_card(
                conn, adv["id"], title=f"Longe {index:02d}", type="character",
                entry="SEGREDO-AUSENTE nao entra no prompt",
                sheet=json.dumps({"status": "alive", "class": "mercador"}),
            )
            conn.execute(
                "UPDATE story_cards SET created_at=? WHERE id=?",
                (f"2026-01-01T00:00:01.{index:03d}Z", card["id"]),
            )
        db.insert_action(conn, adv["id"], 0, "story", scene)
        for index in range(5):
            row = db.insert_npc_memory(
                conn, adventure_id=adv["id"], card_id=elara["id"], kind="event",
                text=f"MEMORIA-{index}-CURTA",
            )
            conn.execute(
                "UPDATE npc_memories SET created_at=? WHERE id=?",
                (f"2026-01-02T00:00:00.{index:03d}Z", row["id"]),
            )
        row = db.insert_npc_memory(
            conn, adventure_id=adv["id"], card_id=elara["id"], kind="event",
            text=long_memory,
        )
        conn.execute(
            "UPDATE npc_memories SET created_at=? WHERE id=?",
            ("2026-01-02T00:00:00.010Z", row["id"]),
        )
    calls = _capture(monkeypatch, json.dumps({
        "decision": "allow",
        "outcome": "Elara deixa a conversa seguir.",
        "reactions": [],
        "initiative": None,
        "relation_updates": [],
        "cue": "A conversa segue.",
    }))
    result = npc_gate.decide(
        adv["id"], "Eu cumprimento Elara.", {}, None,
        world_check={"possible": False, "blockers": ["parede de gelo"]},
    )
    assert result["decision"] == "allow"
    assert calls[0][2]["role"] == "turn_gate"
    payload = json.loads(calls[0][1][1]["content"])
    assert payload["world_check"]["blockers"] == ["parede de gelo"]
    assert len(scene) > 1600
    assert len(payload["scene_tail"]) <= 1600
    assert payload["scene_tail"].endswith(ending)
    assert "COMECO-UNICO" not in payload["scene_tail"]
    people = payload["people"]
    assert [item["name"] for item in people] == ["Elara", *[f"Longe {i:02d}" for i in range(20)]]
    present = people[0]
    assert present["present"] is True
    assert present["card_id"] == elara["id"]
    assert present["profile"]["class"] == "ranger"
    assert present["profile"]["personality"] == personality
    assert present["body"]["pain"] == 6
    assert present["bonds"]["trust"] == 12
    assert "progression" in present and "power" in present
    memories = present["memories"]
    assert len(memories) == 4
    assert len(long_memory) > 160
    assert memories[0]["text"] == npc_assets._clip(long_memory, 160)
    assert len(memories[0]["text"]) <= 160
    assert long_memory.startswith(memories[0]["text"])
    blob = json.dumps(payload, ensure_ascii=False)
    assert "MEMORIA-2-CURTA" in blob
    assert "MEMORIA-0-CURTA" not in blob
    assert "MEMORIA-1-CURTA" not in blob
    assert long_memory not in blob
    assert "SEGREDO-AUSENTE" not in blob
    for person in people[1:]:
        assert set(person) == {"name", "status", "present"}
        assert person["status"] == "alive"
        assert person["present"] is False
