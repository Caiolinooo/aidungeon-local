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


def _window(built) -> str:
    """As users da janela (entre a system e a user final), coladas."""
    return "".join(m["content"] for m in built.messages[1:-1])


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

    # wire format Qwen3: UMA system + uma user por ação da janela + user final
    roles = [m["role"] for m in built.messages]
    assert roles == ["system", "user", "user", "user"]
    assert roles[0] == "system" and all(r != "system" for r in roles[1:])

    # system estável: base, ai_instructions e plot (blocos 1–2)
    sys_msg = built.messages[0]["content"]
    order = [
        cb.BASE_SYSTEM_PROMPT,          # 1a base
        "Sandbox em Faerûn",            # 1b ai_instructions
        "[Plot]",                       # 2 plot_essentials
        "Name: Caio",
    ]
    positions = [sys_msg.index(fragment) for fragment in order]
    assert positions == sorted(positions)
    for volatile in ("[World info]", "[Memories]", "[Summary]"):
        assert volatile not in sys_msg

    # 6 — uma user por ação da janela (0..1; a última vai p/ o bloco 8)
    assert built.messages[1]["content"] == "You stand in Calimport."
    assert built.messages[2]["content"] == "\n> You look around\n"

    # 7+8 — user final: voláteis no topo, author's note e a última ação no fim
    final = built.messages[3]["content"]
    order = [
        "[World info]",                 # 3 story cards
        "Baldur's Gate",
        "[Memories]",                   # 4 memory bank
        "Zhentarim",
        "[Summary]",                    # 5 story_summary
        "Caio chegou a Calimport",
        "[Author's note: Mundo vivo; NPCs agem por conta própria.]",
    ]
    positions = [final.index(fragment) for fragment in order]
    assert positions == sorted(positions)
    assert final.endswith("[Author's note: Mundo vivo; NPCs agem por conta própria.]\n"
                          f"{cb.STYLE_CUE}\n"
                          f"{cb.CANON_CUE}\n"
                          "The market is busy.")


def test_empty_blocks_are_skipped(tmp_db):
    adv = make_adventure(title="Blank")
    add_action(adv["id"], 0, "start", "Once upon a time.")
    with db.session() as conn:
        actions = db.list_actions(conn, adv["id"])
        built = _build(adv, actions, _cfg(conn))
    # sem plot/cards/memórias/summary/note: system só com a base; start é a
    # última ação → vai para a user final (janela vazia)
    roles = [m["role"] for m in built.messages]
    assert roles == ["system", "user"]
    assert built.messages[0]["content"] == cb.BASE_SYSTEM_PROMPT
    assert built.messages[1]["content"] == f"{cb.STYLE_CUE}\n{cb.CANON_CUE}\nOnce upon a time."


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
    window = _window(built)  # users da janela (ações 0..3)
    assert "\n> You draw my sword\n" in window
    assert '\n> You say "Hello, guard."\n' in window
    assert "\n> You sheathe the blade\n" in window
    assert "> You \n> You" not in window  # nunca re-embrulha texto do backup
    # bloco 8 = última ação (story → prosa pura)
    assert built.messages[-1]["content"] == f"{cb.STYLE_CUE}\n{cb.CANON_CUE}\nPure narration."


def test_stop_sequences_from_settings(tmp_db):
    adv = make_adventure(title="Stop")
    add_action(adv["id"], 0, "start", "Intro.")
    with db.session() as conn:
        actions = db.list_actions(conn, adv["id"])
        built = _build(adv, actions, _cfg(conn))
    assert built.stop_sequences == ["\n> You", "\n> you", "\nPlayer:", "\nUser:"]


def test_dynamic_stop_sequences_with_protagonist_names(tmp_db):
    adv = make_adventure(title="Protagonist Stop", plot_essentials="[Name: Caio\nClass: Sorcerer]")
    add_action(adv["id"], 0, "start", "Intro.")
    with db.session() as conn:
        actions = db.list_actions(conn, adv["id"])
        built = _build(adv, actions, _cfg(conn))
    assert "\n> You" in built.stop_sequences
    assert "\n> you" in built.stop_sequences
    assert "\n> Caio" in built.stop_sequences
    assert "\nCaio:" in built.stop_sequences
    assert "\nPlayer:" in built.stop_sequences
    assert "\nUser:" in built.stop_sequences


def test_active_entity_recency_anchor_in_final_user_turn(tmp_db):
    adv = make_adventure(title="Active Lore Test")
    add_action(adv["id"], 0, "start", "You walk the desert path.")
    add_action(adv["id"], 1, "do", "greet Princess Alusair with a deep bow")
    with db.session() as conn:
        db.insert_card(conn, adv["id"], title="Princess Alusair",
                       triggers=["Alusair"], entry="Steel Regent of Cormyr.")
        actions = db.list_actions(conn, adv["id"])
        built = cb.build_context(adv, actions, _cfg(conn), conn)
    final_content = built.messages[-1]["content"]
    assert "[Active Lore: Princess Alusair — Steel Regent of Cormyr]" in final_content


# ---------------------------------------------------------------- budgets
def test_block_budget_truncation(tmp_db):
    adv = _adventure_full(plot_essentials="x" * 10000,
                          authors_note="y" * 5000,
                          ai_instructions="z" * 9000)
    add_action(adv["id"], 0, "start", "Intro.")
    with db.session() as conn:
        actions = db.list_actions(conn, adv["id"])
        built = _build(adv, actions, _cfg(conn))
    sys_msg = built.messages[0]["content"]
    assert "z" * 2800 in sys_msg and "z" * 2801 not in sys_msg  # instr ≤ 700 tk
    assert "x" * 6400 in sys_msg and "x" * 6401 not in sys_msg  # plot ≤ 1600 tk
    # author's note truncada a 300 tk dentro da user final
    final = built.messages[-1]["content"]
    assert final.startswith("[Summary]\n")
    assert "[Author's note: " + "y" * 1200 + "]\n" in final
    assert "y" * 1201 not in final
    assert final.endswith("Intro.")


def test_recent_window_dynamic_rule(tmp_db):
    adv = make_adventure(title="Rule")
    add_action(adv["id"], 0, "start", "Intro.")
    with db.session() as conn:
        actions = db.list_actions(conn, adv["id"])
        built = _build(adv, actions, _cfg(conn))
    expected = 65536 - (700 + 1600 + 1600 + 800 + 800 + 300 + 200 + 200) - 1024
    assert built.meta["budgets"]["recent_window"] == expected == 58312


def test_long_context_dynamic_scaling_and_continuity_anchor(tmp_db):
    adv = _adventure_full(
        plot_essentials="You are Sir Roland, Knight of Cormyr.",
        story_summary="Sir Roland recovered the Obsidian Key in the ruins of Myth Drannor.",
    )
    # 10 turns so len(chosen) >= 8 triggers the Long-Horizon Continuity Anchor
    for i in range(10):
        txt = f"Turn {i}: Lady Alusair studies the ancient map in the war tent." if i == 7 else f"Turn {i}: The march continues."
        add_action(adv["id"], i, "continue" if i > 0 else "start", txt)
    add_action(adv["id"], 10, "do", "ask her what the runes mean")

    with db.session() as conn:
        db.insert_card(conn, adv["id"], title="Lady Alusair",
                       triggers=["Alusair"], entry="Steel Regent of Cormyr. Wears silver plate.")
        actions = db.list_actions(conn, adv["id"])
        cfg = _cfg(conn, context_size=131072)
        built = cb.build_context(adv, actions, cfg, conn)

    assert built.meta["context_scale_factor"] == 3.0
    assert built.meta.get("continuity_anchor_active") is True
    final_msg = built.messages[-1]["content"]
    # Even though turn 10 only says "ask her what the runes mean", Alusair from turn 7 (within last 3 turns) is anchored!
    assert "[Active Lore: Lady Alusair — Steel Regent of Cormyr]" in final_msg
    assert "[Continuity Anchor:" in final_msg
    assert "Sir Roland" in final_msg
    assert "Obsidian Key" in final_msg


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
    assert built.messages[-1]["content"].endswith("CURRENT last action.")
    assert cb.STYLE_CUE in built.messages[-1]["content"]


def test_window_keeps_newest_whole_actions_first(tmp_db):
    adv = make_adventure(title="Trunc2")
    add_action(adv["id"], 0, "start", "S" + "0" * 800)     # ~201 tk
    for seq in range(1, 11):                               # 10 × ~26 tk
        add_action(adv["id"], seq, "continue", f"M{seq:02d}" + "1" * 97)
    add_action(adv["id"], 11, "continue", "CURRENT.")
    with db.session() as conn:
        actions = db.list_actions(conn, adv["id"])
        # budget da janela = 250 tokens: cabem as 10 curtas (~260 não; ~234
        # sem a mais antiga), não a ação 0. O descarte sobe para múltiplo de
        # WINDOW_DROP_CHUNK (8): saem as ações 0..7 e ficam 8..10, inteiras.
        # (6200 = budgets fixos, incl. player_state=200 do v0.7.0)
        cfg = _cfg(conn, context_size=6200 + 1024 + 250)
        built = _build(adv, actions, cfg)
    assert built.meta["budgets"]["recent_window"] == 250
    assert cb.WINDOW_DROP_CHUNK == 8
    assert built.meta["window_action_count"] == 3
    assert len(built.messages) == 5
    window = _window(built)
    assert window.startswith("M08") and "S000" not in window
    assert window.count("M") == 3 and len(window) == 3 * 100   # inteiras, sem corte


def test_next_turn_repeats_every_message_before_the_final_user(tmp_db):
    """O checkpoint do llama-server fica no início da última user. O turno
    seguinte só reaproveita o cache se repetir tudo o que vinha antes dela."""
    adv = _adventure_full()
    turns = [
        ("start", "You stand in Calimport."),
        ("do", "look for Thuldan"),
        ("continue", "Thuldan waves from the docks."),
        ("say", "Any news?"),
        ("continue", "He shakes his head."),
        ("see", "people present: Thuldan, docks at dusk"),
        ("do", "walk to the market"),
        ("continue", "The market is loud."),
    ]
    for seq, (type_, text) in enumerate(turns):
        add_action(adv["id"], seq, type_, text)
    with db.session() as conn:
        db.insert_card(conn, adv["id"], title="Thuldan", triggers=["Thuldan"],
                       entry="Contrabandista halfling.")
        db.insert_memory(conn, adv["id"], "Caio deve um favor a Thuldan.")
        actions = db.list_actions(conn, adv["id"])
        cfg = _cfg(conn)
        built = [cb.build_context(adv, actions[:n], cfg, conn)
                 for n in range(2, len(actions) + 1)]
    for before, now in zip(built, built[1:]):
        shared = before.messages[:-1]
        assert now.messages[:len(shared)] == shared
        assert now.messages[len(shared)]["role"] == "user"


def test_card_triggers_from_memory_outside_the_window(tmp_db):
    adv = make_adventure(story_summary="")
    add_action(adv["id"], 0, "start", "You walk the road.")
    add_action(adv["id"], 1, "continue", "The rain keeps falling.")
    with db.session() as conn:
        db.insert_card(conn, adv["id"], title="Princess Alusair",
                       triggers=["Alusair"], entry="Steel Regent of Cormyr.")
        db.insert_memory(conn, adv["id"],
                         "Princess Alusair swore an oath in the hall.",
                         seq_start=0, seq_end=0)
        actions = db.list_actions(conn, adv["id"])
        built = cb.build_context(adv, actions, _cfg(conn), conn)
    final = built.messages[-1]["content"]
    assert "Alusair" not in "You walk the road. The rain keeps falling."
    assert "Steel Regent of Cormyr." in final
    assert "swore an oath" in final


def test_see_prompt_stays_out_of_the_narrator(tmp_db):
    adv = make_adventure(title="See")
    add_action(adv["id"], 0, "start", "The aisle is dark.")
    add_action(adv["id"], 1, "continue", "A runner leaps.")
    add_action(adv["id"], 2, "see",
               "people present: Alex, exact likeness, Bust portrait BREAK night")
    with db.session() as conn:
        actions = db.list_actions(conn, adv["id"])
        built = _build(adv, actions, _cfg(conn))
    full = "".join(message["content"] for message in built.messages)
    assert "Bust portrait" not in full
    assert "people present" not in full
    assert "BREAK" not in full
    assert built.messages[-1]["content"].endswith("A runner leaps.")
    assert "The aisle is dark." in built.messages[1]["content"]
