"""Regras no prompt, terceira pessoa e trava de qualidade do narrador."""

from app import context_builder as cb
from app import db, output_sanitizer, settings_store

from conftest import add_action, make_adventure


def test_quality_flags_impersonation_mechanics_and_language():
    assert output_sanitizer.quality_flags("You decide to open the door.") == ["impersonation"]
    assert output_sanitizer.quality_flags("The dice roll came up 17.") == ["mechanics_leak"]
    clean = "A porta range quando você a empurra e o corredor não está vazio."
    assert output_sanitizer.quality_flags(clean, expected_language="pt") == []
    english = (
        "The guard looks at the door and steps back with a sword in his hand."
    )
    assert output_sanitizer.quality_flags(english, expected_language="pt") == ["language"]


def test_rules_and_third_person_reach_the_narrator(tmp_db):
    adv = make_adventure(
        title="Mesa",
        plot_essentials="Name: Lia",
        character_rules="Ataque: 1d20 + modificador contra a CA.",
        third_person=True,
    )
    add_action(adv["id"], 0, "start", "Lia está na porta.")
    with db.session() as conn:
        actions = db.list_actions(conn, adv["id"])
        cfg = settings_store.runtime_config(conn)
        built = cb.build_context(adv, actions, cfg, conn)
    system = built.messages[0]["content"]
    assert "[Plot]" in system
    assert system.index("[Plot]") < system.index("[Rules]")
    assert "1d20 + modificador" in system
    assert 'Narrate in the third person. Refer to the protagonist by name; never as "you".' in system
    assert system.index("Narrate in the third person") < system.index("[Plot]")


def test_empty_rules_and_second_person_stay_quiet(tmp_db):
    adv = make_adventure(title="Solo", plot_essentials="Name: Caio", third_person=False)
    add_action(adv["id"], 0, "start", "You stand at the gate.")
    with db.session() as conn:
        actions = db.list_actions(conn, adv["id"])
        cfg = settings_store.runtime_config(conn)
        built = cb.build_context(adv, actions, cfg, conn)
    system = built.messages[0]["content"]
    assert "[Rules]" not in system
    assert "third person" not in system
    assert "one visible action" in system


def test_merge_action_rules_keeps_resolution(tmp_db):
    adv = make_adventure(title="Undo")
    with db.session() as conn:
        action = db.insert_action(conn, adv["id"], 0, "do", "abre a porta")
        db.set_action_rules(conn, action["id"], {
            "before": {"hp": 10}, "cards": [], "resolution": {"steps": ["ok"]},
        })
        db.merge_action_rules(conn, action["id"], {"quality_flags": ["language"]})
        stored = db.get_action_rules(conn, action["id"])
        row = conn.execute("SELECT * FROM actions WHERE id=?", (action["id"],)).fetchone()
    assert stored["before"] == {"hp": 10}
    assert stored["resolution"]["steps"] == ["ok"]
    assert stored["quality_flags"] == ["language"]
    assert db.action_to_dict(row)["quality_flags"] == ["language"]
    assert db.action_to_dict(row)["rules"]["steps"] == ["ok"]
