"""Testes do game engine — action/continue/retry/undo/edit com LLM mockado.

O LLM é mockado via httpx.MockTransport (resposta SSE fake de
/chat/completions + /models), sem nenhum servidor real.
"""

from __future__ import annotations

import pytest

from app import context_builder, db, game_engine
from app.errors import GameError

from conftest import add_action, make_adventure, make_llm_transport


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
    assert len(captured) == 1
    payload = captured[0]
    assert payload["model"] == "gguf-mock"                 # descoberto via /models
    assert payload["stream"] is True
    assert payload["stop"] == ["\n> You", "\n> you"]
    assert payload["max_tokens"] == 300
    assert payload["temperature"] == 0.85
    assert payload["chat_template_kwargs"] == {"enable_thinking": False}
    # última mensagem = pistas de estilo e cânone + última ação do jogador
    assert payload["messages"][-1]["role"] == "user"
    assert payload["messages"][-1]["content"] == (
        f"{context_builder.STYLE_CUE}\n{context_builder.CANON_CUE}\n\n> You wait\n"
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
