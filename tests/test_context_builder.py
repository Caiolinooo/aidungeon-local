"""Testes do montador de contexto (sanduíche do PLANO.md §4.2)."""

from __future__ import annotations

from app import context_builder as cb
from app import db, settings_store

from conftest import add_action, make_adventure


def _cfg(conn, **over):
    cfg = settings_store.runtime_config(conn)
    for key, value in over.items():
        if key == "budgets":
            cfg["budgets"].update(value)
        else:
            cfg[key] = value
    return cfg


def _adventure_full(**over):
    fields = {
        "title": "Faerun Test",
        "ai_instructions": "Sandbox em Faerûn com tom sombrio.",
        "plot_essentials": "[Name: Caio\nClass: Sorcerer]",
        "authors_note": "Mundo vivo; NPCs agem por conta própria.",
        "story_summary": "Caio chegou a Calimport e conheceu Thuldan.",
    }
    fields.update(over)
    return make_adventure(**fields)


def _build(adv, actions, cfg):
    with db.session() as conn:
        return cb.build_context(adv, actions, cfg, conn)


# --------------------------------------------------------------- ordem
def test_sandwich_order_exact(tmp_db):
    adv = _adventure_full()
    add_action(adv["id"], 0, "start", "You stand in Calimport.")
    add_action(adv["id"], 1, "do", "\n> You look around\n")
    add_action(adv["id"], 2, "continue", "The market is busy.")
    with db.session() as conn:
        db.insert_card(conn, adv["id"], title="Baldur's Gate",
                       entry="Porto movimentado.", pinned=True)
        db.insert_memory(conn, adv["id"], "Caio é da Zhentarim.")
        actions = db.list_actions(conn, adv["id"])
        cfg = _cfg(conn)
    built = _build(adv, actions, cfg)

    roles = [m["role"] for m in built.messages]
    assert roles == ["system", "system", "system", "system", "system",
                     "user", "system", "user"]
    m = built.messages
    assert cb.BASE_SYSTEM_PROMPT in m[0]["content"]
    assert "Sandbox em Faerûn" in m[0]["content"]          # 1 ai_instructions
    assert "Name: Caio" in m[1]["content"]                 # 2 plot_essentials
    assert "Baldur's Gate" in m[2]["content"]              # 3 story cards
    assert "Zhentarim" in m[3]["content"]                  # 4 memory bank
    assert "Calimport" in m[4]["content"]                  # 5 story_summary
    assert "You stand in Calimport." in m[5]["content"]    # 6 janela verbatim
    assert "Mundo vivo" in m[6]["content"]                 # 7 authors_note
    assert "The market is busy." in m[7]["content"]        # 8 última ação


def test_empty_blocks_are_skipped(tmp_db):
    adv = make_adventure(title="Blank")
    add_action(adv["id"], 0, "start", "Once upon a time.")
    with db.session() as conn:
        actions = db.list_actions(conn, adv["id"])
        built = _build(adv, actions, _cfg(conn))
    # sem plot/cards/memórias/summary/note: system base + user(start? não—
    # start é a última ação → bloco 8; janela vazia)
    roles = [m["role"] for m in built.messages]
    assert roles == ["system", "user"]
    assert built.messages[1]["content"] == "Once upon a time."


# ------------------------------------------------------------- formatação
def test_player_action_formatting(tmp_db):
    adv = make_adventure(title="Fmt")
    add_action(adv["id"], 0, "start", "Intro.")
    add_action(adv["id"], 1, "do", "draw my sword")              # crua → embrulha
    add_action(adv["id"], 2, "say", 'Hello, guard.')             # crua → embrulha
    add_action(adv["id"], 3, "do", "\n> You sheathe the blade\n")  # backup: já vem formatada
    add_action(adv["id"], 4, "story", "Pure narration.")
    with db.session() as conn:
        actions = db.list_actions(conn, adv["id"])
        built = _build(adv, actions, _cfg(conn))
    window = built.messages[1]["content"]  # user: janela (ações 0..3)
    assert "\n> You draw my sword\n" in window
    assert '\n> You say "Hello, guard."\n' in window
    assert "\n> You sheathe the blade\n" in window
    assert "> You \n> You" not in window  # nunca re-embrulha texto do backup
    # bloco 8 = última ação (story → prosa pura)
    assert built.messages[-1]["content"] == "Pure narration."


def test_stop_sequences_from_settings(tmp_db):
    adv = make_adventure(title="Stop")
    add_action(adv["id"], 0, "start", "Intro.")
    with db.session() as conn:
        actions = db.list_actions(conn, adv["id"])
        built = _build(adv, actions, _cfg(conn))
    assert built.stop_sequences == ["\n> You", "\n> you"]


# ---------------------------------------------------------------- budgets
def test_block_budget_truncation(tmp_db):
    adv = _adventure_full(plot_essentials="x" * 10000,
                          authors_note="y" * 5000,
                          ai_instructions="z" * 9000)
    add_action(adv["id"], 0, "start", "Intro.")
    with db.session() as conn:
        actions = db.list_actions(conn, adv["id"])
        built = _build(adv, actions, _cfg(conn))
    plot_block = built.messages[1]["content"]
    assert cb.estimate_tokens(plot_block) <= 400 + 1
    # sem cards/memórias e janela vazia: [base, plot, summary, note, user]
    note_block = built.messages[3]["content"]
    assert cb.estimate_tokens(note_block) <= 300 + 1
    instr_block = built.messages[0]["content"]
    assert cb.estimate_tokens(instr_block) <= \
        cb.estimate_tokens(cb.BASE_SYSTEM_PROMPT) + 700 + 2


def test_recent_window_dynamic_rule(tmp_db):
    adv = make_adventure(title="Rule")
    add_action(adv["id"], 0, "start", "Intro.")
    with db.session() as conn:
        actions = db.list_actions(conn, adv["id"])
        built = _build(adv, actions, _cfg(conn))
    budgets = settings_store.load_file_settings()["context"]["budgets"]
    expected = 8192 - (700 + 400 + 1200 + 600 + 400 + 300 + 200) - 512
    assert built.meta["budgets"]["recent_window"] == expected == 3880


def test_window_truncates_by_whole_action(tmp_db):
    adv = make_adventure(title="Trunc")
    big_old = "OLDACTION " + "a" * 2000      # ~503 tokens — não cabe
    newest = "NEWESTACTION fits fine."
    add_action(adv["id"], 0, "start", big_old)
    add_action(adv["id"], 1, "continue", newest)
    add_action(adv["id"], 2, "continue", "CURRENT last action.")
    with db.session() as conn:
        actions = db.list_actions(conn, adv["id"])
        # janela minúscula: só a ação mais recente cabe inteira
        built = _build(adv, actions, _cfg(conn, context_size=1500))
    # regra dinâmica: max(0, 1500 - 3800 - 512) = 0 → janela vazia
    assert built.meta["budgets"]["recent_window"] == 0
    assert built.meta["window_action_count"] == 0
    # janela vazia: nenhum fragmento da ação antiga vaza para o contexto
    full = "".join(m["content"] for m in built.messages)
    assert "OLDACTION" not in full
    assert "NEWESTACTION" not in full  # ação 1 ficou fora (janela 0); última vai ao bloco 8
    assert built.messages[-1]["content"].startswith("CURRENT last action.")


def test_window_keeps_newest_whole_actions_first(tmp_db):
    adv = make_adventure(title="Trunc2")
    add_action(adv["id"], 0, "start", "S" + "0" * 800)     # ~201 tk
    add_action(adv["id"], 1, "continue", "M" + "1" * 400)  # ~101 tk
    add_action(adv["id"], 2, "continue", "CURRENT.")
    with db.session() as conn:
        actions = db.list_actions(conn, adv["id"])
        # budget da janela = 250 tokens: cabe a ação 1 inteira (~101), não a 0
        cfg = _cfg(conn, context_size=3800 + 512 + 250)
        built = _build(adv, actions, cfg)
    assert built.meta["budgets"]["recent_window"] == 250
    assert built.meta["window_action_count"] == 1
    window = built.messages[1]["content"]
    assert window.startswith("M") and len(window) == 401   # inteira, sem corte
