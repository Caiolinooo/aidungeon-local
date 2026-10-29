"""Testes do game engine — action/continue/retry/undo/edit com LLM mockado.

O LLM é mockado via httpx.MockTransport (resposta SSE fake de
/chat/completions + /models), sem nenhum servidor real.
"""

from __future__ import annotations

import threading

import pytest

from app import action_check, context_builder, db, game_engine, npc_gate, output_sanitizer
from app.world_facts import ATTEMPT_RULE
from app.errors import GameError

from conftest import add_action, make_adventure, make_llm_transport

NEUTRAL_GATE = (
    "\n[Adjudicação do GM — obedeça sem citar estas regras]\n"
    "Fato decidido: A ação segue no ambiente, sem outra pessoa para responder.\n"
    "Tom/consequência: A ação segue no ambiente, sem outra pessoa para responder.\n"
    "Não contradiga o fato decidido; não transforme recusa em obediência."
)


def _collect(events) -> list[dict]:
    return list(events)


def _tokens(events: list[dict]) -> str:
    return "".join(e["data"]["token"] for e in events if e["event"] == "token")


def _done(events: list[dict]) -> dict:
    dones = [e for e in events if e["event"] == "done"]
    assert dones, f"sem evento done: {events}"
    return dones[-1]["data"]


@pytest.fixture()
def adventure(tmp_db):
    adv = make_adventure(title="Engine", ai_instructions="Tom épico.")
    add_action(adv["id"], 0, "start", "You stand before the gate.")
    return adv


# ------------------------------------------------------------------ action
def test_action_streams_and_persists(adventure):
    transport = make_llm_transport(tokens=("You", " push", " the gate open."))
    events = _collect(game_engine.handle_action(adventure["id"], "do",
                                                "push the gate", transport=transport))
    assert _tokens(events) == "You push the gate open."
    data = _done(events)
    player = data["player_action"]
    cont = data["action"]
    assert player["type"] == "do"
    assert player["text"] == "\n> You push the gate\n"   # formato do backup
    assert player["seq"] == 1
    assert cont["type"] == "continue"
    assert cont["seq"] == 2
    assert cont["text"] == "You push the gate open."
    with db.session() as conn:
        actions = db.list_actions(conn, adventure["id"])
    assert [a["type"] for a in actions] == ["start", "do", "continue"]


def test_action_emits_bounded_phase_telemetry(adventure):
    events = _collect(game_engine.handle_action(
        adventure["id"], "do", "wait and observe the room",
        transport=make_llm_transport(tokens=("Time passes.",))))
    phases = [event["data"] for event in events if event["event"] == "turn_phase"]
    names = {item["phase"] for item in phases}
    assert names == {"intent", "action_check", "npc_gate", "narrative_wait",
                     "narrative_ttft", "correction"}
    for name in names:
        assert [item["state"] for item in phases if item["phase"] == name] == ["start", "end"]
        end = next(item for item in phases if item["phase"] == name and item["state"] == "end")
        assert isinstance(end["elapsed_ms"], int) and end["elapsed_ms"] >= 0
    wait_end_index = next(index for index, event in enumerate(events)
                          if event["event"] == "turn_phase"
                          and event["data"]["phase"] == "narrative_wait"
                          and event["data"]["state"] == "end")
    token_index = next(index for index, event in enumerate(events)
                       if event["event"] == "token")
    assert wait_end_index < token_index
    assert all(set(item) <= {"phase", "state", "elapsed_ms"} for item in phases)


def test_phase_end_is_emitted_when_work_fails(adventure, monkeypatch):
    def fail(*args, **kwargs):
        raise RuntimeError("sim failed")

    monkeypatch.setattr("app.sim.engine.run_turn", fail)
    events = _collect(game_engine.handle_action(
        adventure["id"], "do", "wait", transport=make_llm_transport()))
    intent = [event["data"] for event in events
              if event["event"] == "turn_phase" and event["data"]["phase"] == "intent"]
    assert [item["state"] for item in intent] == ["start", "end"]
    assert events[-1]["event"] == "error"


def test_action_check_and_gate_overlap_before_narrator(adventure, monkeypatch):
    barrier = threading.Barrier(2)
    verdict = action_check.Verdict(
        possible=False, blockers=["a porta já está trancada"], notes="fato conhecido")
    adjudication = {
        "decision": "partial", "outcome": "O guarda recua e bloqueia a passagem.",
        "reactions": [], "initiative": None,
    }

    def check(*args, **kwargs):
        barrier.wait(timeout=2)
        return verdict

    gate_world_checks: list[dict] = []

    def gate(*args, **kwargs):
        gate_world_checks.append(kwargs["world_check"])
        barrier.wait(timeout=2)
        return adjudication

    monkeypatch.setattr(action_check, "hard_blockers", lambda state: ["a porta já está trancada"])
    monkeypatch.setattr(action_check, "check_action", check)
    monkeypatch.setattr(npc_gate, "decide", gate)
    captured: list[dict] = []
    events = _collect(game_engine.handle_action(
        adventure["id"], "do", "wait and observe the room",
        transport=make_llm_transport(tokens=("O guarda mantém a porta fechada.",),
                                     capture=captured)))

    assert not barrier.broken
    assert gate_world_checks == [{"action_blockers": ["a porta já está trancada"]}]
    assert _done(events)["action_check"] == verdict.to_dict()
    narrator = next(call for call in captured if call.get("stream"))
    prompt = narrator["messages"][-1]["content"]
    assert action_check.verdict_block(verdict) in prompt
    assert npc_gate.cue_block(adjudication, []) in prompt


def test_say_never_calls_action_check_llm(adventure, monkeypatch):
    monkeypatch.setattr(npc_gate, "decide", lambda *args, **kwargs: {
        "decision": "allow", "outcome": "", "reactions": [], "initiative": None,
    })
    captured: list[dict] = []
    events = _collect(game_engine.handle_action(
        adventure["id"], "say", "Ladrão, precisamos sair agora",
        transport=make_llm_transport(tokens=("O ladrão concorda.",), capture=captured)))

    assert _tokens(events) == "O ladrão concorda."
    assert any(call.get("stream") for call in captured)
    assert all(call.get("stream") for call in captured)


def test_release_generation_lock_resumes_portrait_preparation(adventure, monkeypatch):
    calls = []
    monkeypatch.setattr("app.jobs.resume_portrait_prepare", lambda: calls.append(True))
    assert game_engine.acquire_generation_lock(adventure["id"])
    game_engine.release_generation_lock(adventure["id"])
    assert calls == [True]


def test_action_say_formatting(adventure):
    transport = make_llm_transport(tokens=("The guard nods.",))
    events = _collect(game_engine.handle_action(adventure["id"], "say",
                                                "Open up.", transport=transport))
    assert _done(events)["player_action"]["text"] == '\n> You say "Open up."\n'


def test_payload_carries_sampler_stop_and_thinking_flag(adventure):
    captured: list[dict] = []
    transport = make_llm_transport(tokens=("Ok.",), model_id="gguf-mock",
                                   capture=captured)
    _collect(game_engine.handle_action(adventure["id"], "do", "wait",
                                       transport=transport))
    streams = [call for call in captured if call.get("stream")]
    assert len(streams) == 1
    payload = streams[0]
    assert payload["model"] == "gguf-mock"                 # descoberto via /models
    assert payload["stream"] is True
    assert payload["stop"] == ["\n> You", "\n> you", "\nPlayer:", "\nUser:",
                               *context_builder.CONTEXT_STOPS]
    assert payload["max_tokens"] == 300
    assert payload["temperature"] == 0.85
    assert payload["top_p"] == 1.0
    assert payload["min_p"] == 0.08
    assert payload["presence_penalty"] == 0.15
    assert payload["frequency_penalty"] == 0.10
    assert payload["dry_multiplier"] == 0.8
    assert payload["dry_base"] == 1.75
    assert payload["dry_sequence_breakers"] == ["\n", ":", "\"", "*"]
    assert payload["chat_template_kwargs"] == {"enable_thinking": False}
    # última mensagem = pistas de estilo e cânone + última ação do jogador
    assert payload["messages"][-1]["role"] == "user"
    assert payload["messages"][-1]["content"] == (
        f"{context_builder.STYLE_CUE}\n{context_builder.CANON_CUE}\n"
        f"{context_builder.LORE_CUE}\n"
        f"[Facts: where=unestablished; places=none; items=none. {ATTEMPT_RULE}]\n"
        "\n> You wait\n" + NEUTRAL_GATE
    )


def test_generation_concurrent_conflict(adventure):
    # gerador não consumido → lock seguro → segunda geração = 409
    gen = game_engine.handle_action(adventure["id"], "do", "hold",
                                    transport=make_llm_transport())
    with pytest.raises(GameError) as exc:
        game_engine.handle_continue(adventure["id"], transport=make_llm_transport())
    assert exc.value.status == 409
    assert exc.value.code == "generation_in_progress"
    _collect(gen)  # consome e libera o lock
    # depois de liberado, volta a funcionar
    events = _collect(game_engine.handle_continue(adventure["id"],
                                                  transport=make_llm_transport()))
    assert _done(events)["action"]["type"] == "continue"


def test_action_adventure_not_found(tmp_db):
    with pytest.raises(GameError) as exc:
        game_engine.handle_action("nope", "do", "x", transport=make_llm_transport())
    assert exc.value.status == 404


# ---------------------------------------------------------------- continue
def test_continue_without_player_action(adventure):
    events = _collect(game_engine.handle_continue(
        adventure["id"], transport=make_llm_transport(tokens=("The gate creaks.",))))
    data = _done(events)
    assert "player_action" not in data
    assert data["action"]["type"] == "continue"
    assert data["action"]["seq"] == 1


# ------------------------------------------------------------------- retry
def test_retry_replaces_last_continue_same_seq_new_id(adventure):
    t1 = make_llm_transport(tokens=("First answer.",))
    first = _done(_collect(game_engine.handle_action(adventure["id"], "do",
                                                     "knock", transport=t1)))
    old = first["action"]
    t2 = make_llm_transport(tokens=("Second answer.",))
    events = _collect(game_engine.handle_retry(adventure["id"], transport=t2))
    new = _done(events)["action"]
    assert new["type"] == "continue"
    assert new["seq"] == old["seq"]          # mesmo seq
    assert new["id"] != old["id"]            # novo id
    assert new["text"] == "Second answer."
    with db.session() as conn:
        actions = db.list_actions(conn, adventure["id"])
    assert [a["seq"] for a in actions] == [0, 1, 2]
    assert sum(1 for a in actions if a["type"] == "continue") == 1


def test_retry_conflict_when_last_is_not_continue(adventure):
    add_action(adventure["id"], 1, "do", "\n> You wait\n")  # órfã
    with pytest.raises(GameError) as exc:
        game_engine.handle_retry(adventure["id"], transport=make_llm_transport())
    assert exc.value.status == 409
    assert exc.value.code == "not_regenerable"


def test_retry_conflict_on_opening_only(adventure):
    with pytest.raises(GameError) as exc:
        game_engine.handle_retry(adventure["id"], transport=make_llm_transport())
    assert exc.value.status == 409


def test_retry_failure_preserves_old_answer(adventure):
    _done(_collect(game_engine.handle_action(
        adventure["id"], "do", "knock",
        transport=make_llm_transport(tokens=("Original.",)))))
    with db.session() as conn:
        before = db.list_actions(conn, adventure["id"])
    events = _collect(game_engine.handle_retry(
        adventure["id"], transport=make_llm_transport(fail="connect")))
    assert events[-1]["event"] == "error"
    assert "unreachable" in events[-1]["data"]["message"]
    with db.session() as conn:
        after = db.list_actions(conn, adventure["id"])
    assert [a["id"] for a in after] == [a["id"] for a in before]
    assert after[-1]["text"] == "Original."


# -------------------------------------------------------------------- undo
def test_undo_removes_ai_and_originating_player_action(adventure):
    _collect(game_engine.handle_action(adventure["id"], "do", "knock",
                                       transport=make_llm_transport()))
    result = game_engine.handle_undo(adventure["id"])
    assert [a["type"] for a in result["removed"]] == ["do", "continue"]
    assert result["last_action"]["type"] == "start"
    assert result["action_count"] == 1


def test_undo_never_removes_start(adventure):
    with pytest.raises(GameError) as exc:
        game_engine.handle_undo(adventure["id"])
    assert exc.value.status == 409
    assert exc.value.code == "nothing_to_undo"


def test_undo_skips_see_recorded_mid_turn(adventure):
    add_action(adventure["id"], 1, "do", "\n> You knock\n")
    add_action(adventure["id"], 2, "see", "A heavy oak gate in the rain")
    add_action(adventure["id"], 3, "continue", "The gate opens.")
    result = game_engine.handle_undo(adventure["id"])
    assert [a["type"] for a in result["removed"]] == ["do", "continue"]
    assert result["last_action"]["type"] == "see"
    with db.session() as conn:
        actions = db.list_actions(conn, adventure["id"])
    assert [(a["seq"], a["type"]) for a in actions] == [(0, "start"), (2, "see")]


def test_undo_removes_orphan_player_action(adventure):
    add_action(adventure["id"], 1, "say", '\n> You say "hi"\n')  # sem resposta da IA
    result = game_engine.handle_undo(adventure["id"])
    assert [a["type"] for a in result["removed"]] == ["say"]
    assert result["last_action"]["type"] == "start"


# -------------------------------------------------------------------- edit
def test_edit_marks_edited_at(adventure):
    _collect(game_engine.handle_action(adventure["id"], "do", "knock",
                                       transport=make_llm_transport()))
    with db.session() as conn:
        cont = db.list_actions(conn, adventure["id"])[-1]
    assert cont["edited_at"] is None
    updated = game_engine.handle_edit(cont["id"], "Edited by hand.")
    assert updated["text"] == "Edited by hand."
    assert updated["edited_at"] is not None


def test_edit_not_found(tmp_db):
    with pytest.raises(GameError) as exc:
        game_engine.handle_edit(999, "x")
    assert exc.value.status == 404


# ------------------------------------------------- fallback defensivo LLM
def test_reasoning_only_response_becomes_config_error(adventure):
    transport = make_llm_transport(tokens=("pensando...",), reasoning_only=True)
    events = _collect(game_engine.handle_action(adventure["id"], "do", "knock",
                                                transport=transport))
    assert events[-1]["event"] == "error"
    assert "content vazio" in events[-1]["data"]["message"]
    # ação do jogador persistida; nenhuma continue gravada
    with db.session() as conn:
        actions = db.list_actions(conn, adventure["id"])
    assert [a["type"] for a in actions] == ["start", "do"]


def test_retry_temperature_stepping(adventure):
    # Initial generation
    _collect(game_engine.handle_action(
        adventure["id"], "do", "explore the ruins",
        transport=make_llm_transport(tokens=("First output.",)),
    ))

    # Retry 1: temperature should step from 0.85 to 0.90
    cap1: list[dict] = []
    t1 = make_llm_transport(tokens=("Variation 1.",), capture=cap1)
    _collect(game_engine.handle_retry(adventure["id"], transport=t1))
    assert len(cap1) == 1
    assert round(cap1[0]["temperature"], 2) == 0.90

    # Retry 2: temperature should step to 0.95
    cap2: list[dict] = []
    t2 = make_llm_transport(tokens=("Variation 2.",), capture=cap2)
    _collect(game_engine.handle_retry(adventure["id"], transport=t2))
    assert len(cap2) == 1
    assert round(cap2[0]["temperature"], 2) == 0.95

    # Retry with explicit retry_count=6: capped at 1.15 (0.85 + 0.05 * 6 = 1.15)
    cap6: list[dict] = []
    t6 = make_llm_transport(tokens=("Variation 6.",), capture=cap6)
    _collect(game_engine.handle_retry(adventure["id"], transport=t6, retry_count=6))
    assert len(cap6) == 1
    assert round(cap6[0]["temperature"], 2) == 1.15

    # Retry with explicit retry_count=10: capped at max_retry_temperature (1.15)
    cap10: list[dict] = []
    t10 = make_llm_transport(tokens=("Variation 10.",), capture=cap10)
    _collect(game_engine.handle_retry(adventure["id"], transport=t10, retry_count=10))
    assert len(cap10) == 1
    assert round(cap10[0]["temperature"], 2) == 1.15


def test_token_limit_keeps_the_last_complete_sentence(adventure):
    transport = make_llm_transport(
        tokens=("The door opens. He stumbles and",),
        finish_reason="length",
    )
    events = _collect(game_engine.handle_action(
        adventure["id"], "do", "push", transport=transport))
    assert "stumbles" in _tokens(events)
    assert _done(events)["action"]["text"] == "The door opens."


def test_a_finished_reply_keeps_a_trailing_fragment(adventure):
    transport = make_llm_transport(tokens=("The door opens. He stumbles and",))
    events = _collect(game_engine.handle_action(
        adventure["id"], "do", "push", transport=transport))
    assert _done(events)["action"]["text"] == "The door opens. He stumbles and"


def test_cut_without_punctuation_keeps_the_fragment():
    assert output_sanitizer.cut_incomplete_sentence("apesar") == "apesar"
    assert output_sanitizer.cut_incomplete_sentence("Done.") == "Done."


def test_leaked_context_after_prose_is_not_saved(adventure):
    transport = make_llm_transport(tokens=(
        "Clara smiles at you.", "\n\n[world info]\nClara: devoted.",))
    events = _collect(game_engine.handle_action(
        adventure["id"], "story", "Clara enters.", transport=transport))
    assert _done(events)["action"]["text"] == "Clara smiles at you."


def test_reply_made_only_of_context_notes_saves_nothing(adventure):
    transport = make_llm_transport(tokens=(
        "[World info]\nAppearance: red dress.\n\n", "[Continuity Anchor: Clara]\nClara waits."))
    events = _collect(game_engine.handle_action(
        adventure["id"], "story", "Clara enters the suite.", transport=transport))
    assert events[-1] == {"event": "error", "data": {
        "message": "the narrator returned no story text; nothing was saved, try again"}}
    with db.session() as conn:
        actions = db.list_actions(conn, adventure["id"])
    assert [a["type"] for a in actions] == ["start", "story"]
    assert game_engine.acquire_generation_lock(adventure["id"])
    game_engine.release_generation_lock(adventure["id"])


def test_failed_retry_keeps_the_old_reply(adventure):
    add_action(adventure["id"], 1, "continue", "The gate creaks.")
    transport = make_llm_transport(tokens=("<lore>\n[Summary]\nResumo.",))
    events = _collect(game_engine.handle_retry(adventure["id"], transport=transport))
    assert events[-1]["event"] == "error"
    with db.session() as conn:
        actions = db.list_actions(conn, adventure["id"])
    assert actions[-1]["text"] == "The gate creaks."


def test_web_research_goes_inside_the_lore(adventure, monkeypatch):
    block = "[Web Research & Canon Facts]\n- Gate: old iron."
    monkeypatch.setattr(game_engine.web_search_mod, "research_context",
                        lambda *args, **kwargs: {"sources": [{"title": "Gate"}],
                                                 "context_block": block})
    captured: list[dict] = []
    transport = make_llm_transport(tokens=("The gate is old.",), capture=captured)
    events = _collect(game_engine.handle_action(
        adventure["id"], "do", "inspect the gate", transport=transport, web_search=True))
    assert _done(events)["web_sources"] == [{"title": "Gate"}]
    final = next(call for call in captured if call.get("stream"))["messages"][-1]["content"]
    assert final.startswith(f"<lore>\n{block}\n</lore>\n")
    from app.world_facts import ATTEMPT_RULE
    assert final.endswith(
        f"{context_builder.LORE_CUE}\n"
        f"[Facts: where=unestablished; places=none; items=none. {ATTEMPT_RULE}]\n"
        "\n> You inspect the gate\n" + NEUTRAL_GATE
    )


def test_compliance_follows_refuse_and_partial():
    obeys = "Ela obedece e sai com o jogador."
    refuses = "Ela recusa e se afasta."
    assert game_engine._compliance_flag(obeys, {"decision": "refuse"}) == ["compliance"]
    assert game_engine._compliance_flag(obeys, {"decision": "partial"}) == ["compliance"]
    assert game_engine._compliance_flag(refuses, {"decision": "refuse"}) == []
    assert game_engine._compliance_flag(obeys, {"decision": "allow"}) == []
    assert game_engine._compliance_flag(obeys, None) == []


def test_turn_persists_reaction_once_retry_and_story_do_not_reapply(adventure, monkeypatch):
    import json

    from app import npc_bonds, npc_gate

    with db.session() as conn:
        npc = db.insert_card(
            conn, adventure["id"], title="Mira", type="character", entry="A scout.",
            sheet=json.dumps({
                "status": "alive",
                "bonds": {
                    "relationship": 40, "trust": 40, "distrust": 30,
                    "fear": 15, "desire": 15, "boldness": 40,
                },
            }),
        )
    card_id = npc["id"]
    gate = {
        "decision": "refuse",
        "outcome": "Mira recusa.",
        "reactions": [{
            "npc": "Mira",
            "card_id": card_id,
            "reaction": "recua",
            "line": "Não.",
            "emotion": "anger",
            "pose": "step back",
        }],
        "initiative": None,
        "cue": "Mostre a recusa.",
        "relation_updates": [{
            "card_id": card_id,
            "target": "player",
            "delta": {"trust": -4, "hidden_score": 9},
            "reason": "Mira recusa o golpe",
            "evidence": "Ela recusa.",
            "secret": "motivação oculta: quer o trono",
        }],
    }
    monkeypatch.setattr(npc_gate, "decide", lambda *args, **kwargs: gate)
    seen = {}
    real_cue = npc_gate.cue_block

    def cue(result, roster=None):
        seen["roster"] = roster
        return real_cue(result, roster)

    monkeypatch.setattr(npc_gate, "cue_block", cue)
    synced: list[str] = []
    monkeypatch.setattr(
        "app.portrait_sync.sync_card",
        lambda conn, adventure_id, card_id, force=False: synced.append(card_id),
    )
    applied: list[dict] = []
    real_apply = npc_bonds.apply_adjudication

    def spy(session, adventure_id, adjudication, seq, player_text="", actor_name=""):
        applied.append({
            "seq": seq, "player_text": player_text, "actor_name": actor_name,
            "decision": (adjudication or {}).get("decision"),
        })
        return real_apply(
            session, adventure_id, adjudication, seq,
            player_text=player_text, actor_name=actor_name,
        )

    monkeypatch.setattr(npc_bonds, "apply_adjudication", spy)
    spawned: list[str] = []
    real_spawn = game_engine._spawn_post_turn

    def spawn(adventure_id, transport=None, user=""):
        spawned.append(user)
        return real_spawn(adventure_id, transport, user)

    monkeypatch.setattr(game_engine, "_spawn_post_turn", spawn)

    events = _collect(game_engine.handle_action(
        adventure["id"], "do", "I attack Mira", transport=make_llm_transport(
            tokens=("Mira recusa e se afasta.",)), user="hero"))
    kinds = [event["event"] for event in events]
    assert kinds.index("adjudication") < kinds.index("token")
    visible = next(event["data"] for event in events if event["event"] == "adjudication")
    assert set(visible) == {"decision", "outcome", "reactions", "initiative"}
    assert visible["decision"] == "refuse"
    assert visible["reactions"][0]["emotion"] == "anger"
    assert visible["reactions"][0]["pose"] == "step back"
    assert "relation_changes" not in visible
    done = _done(events)["adjudication"]
    assert done["relation_changes"][0]["delta"] == {"trust": -4}
    assert done["relation_changes"][0]["evidence"] == "Ela recusa."
    assert done["relation_changes"][0]["target"] == "player"
    blob = json.dumps(done, ensure_ascii=False)
    assert "secret" not in blob and "motivação oculta" not in blob and "hidden_score" not in blob
    assert _done(events)["action"]["adjudication"] == done
    with db.session() as conn:
        saved = db.get_action(conn, _done(events)["action"]["id"])
        rules = db.get_action_rules(conn, saved["id"])
    assert saved["adjudication"] == done
    assert "cue" not in rules["adjudication"]
    assert seen["roster"][0]["name"] == "Mira"
    assert applied == [{
        "seq": 2, "player_text": "I attack Mira", "actor_name": "hero",
        "decision": "refuse",
    }]
    assert synced == [card_id]
    assert spawned == ["hero"]
    with db.session() as conn:
        sheet = json.loads(db.get_card(conn, adventure["id"], card_id)["sheet"])
    assert sheet["bonds"]["trust"] == 36
    assert sheet["visual_state"]["emotion"] == "anger"
    assert sheet["visual_state"]["seq"] == 2
    assert sheet["expression"] == "angry"

    retried = _collect(game_engine.handle_retry(
        adventure["id"], transport=make_llm_transport(tokens=("Mira recusa outra vez.",))))
    assert [event["event"] for event in retried if event["event"] == "adjudication"] == []
    assert _done(retried)["adjudication"] == done
    assert _done(retried)["action"]["adjudication"] == done
    assert len(applied) == 1
    assert spawned == ["hero"]
    with db.session() as conn:
        again = json.loads(db.get_card(conn, adventure["id"], card_id)["sheet"])
    assert again["bonds"]["trust"] == 36

    story = _collect(game_engine.handle_action(
        adventure["id"], "story", "rain falls on the empty yard tonight",
        transport=make_llm_transport(tokens=("The yard stays quiet.",)), user="hero"))
    assert [event["event"] for event in story if event["event"] == "adjudication"] == []
    assert "adjudication" not in _done(story)
    assert len(applied) == 1
    assert spawned == ["hero", "hero"]
    with db.session() as conn:
        after_story = json.loads(db.get_card(conn, adventure["id"], card_id)["sheet"])
    assert after_story["bonds"]["trust"] == 36
