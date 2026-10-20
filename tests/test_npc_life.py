"""Mente do NPC: cânone, memória do turno, dia e o bloco do narrador."""

from __future__ import annotations

import json

from app import context_builder as cb
from app import db, npc_assets, npc_life, settings_store
from conftest import add_action, make_adventure


def _character(adventure_id: str, **sheet: object) -> dict:
    with db.session() as conn:
        return db.insert_card(
            conn, adventure_id, type="character", title="Ada",
            triggers=["Ada"], entry="Sky captain.", pinned=True,
            sheet=json.dumps(sheet, ensure_ascii=False) if sheet else "",
        )


def test_card_canon_text_includes_the_living_fields():
    rendered = npc_assets.card_canon_text({
        "title": "Ada",
        "entry": "Sky captain.",
        "sheet": json.dumps({
            "humor": "dry jokes",
            "lifestyle": "sleeps on the bridge",
            "sentiments": "wary of the player",
            "life": {"mood": "wary", "activity": "mending a sail"},
        }),
    })
    assert "Humor: dry jokes" in rendered
    assert "Lifestyle: sleeps on the bridge" in rendered
    assert "Sentiments: wary of the player" in rendered
    assert "Mood: wary" in rendered
    assert "Doing: mending a sail" in rendered
    assert rendered.index("Mood: wary") < rendered.index("Humor: dry jokes")
    assert rendered.index("Humor: dry jokes") < rendered.index("Ada: Sky captain.")


def test_run_mind_stores_memory_mood_and_trust_and_keeps_humor(tmp_db, monkeypatch):
    adv = make_adventure()
    card = _character(
        adv["id"], humor="dry jokes",
        bonds={"relationship": 40, "trust": 10, "fear": 15, "desire": 15, "boldness": 40},
    )
    add_action(adv["id"], 0, "story", "Ada hides the compass from you.")

    def fake(_cfg, _messages, **_kwargs):
        return json.dumps({"characters": [{
            "name": "Ada",
            "memory": "Ada hid the compass.",
            "mood": "wary",
            "activity": "watching the door",
            "sentiments": "does not trust the player",
            "bonds": {"trust": 77},
        }]})

    monkeypatch.setattr("app.llm_client.chat_completion", fake)
    result = npc_life.run_mind(adv["id"])
    assert result["updated"] == 1
    with db.session() as conn:
        memories = db.list_npc_memories(conn, adv["id"], card["id"])
        stored = json.loads(db.get_card(conn, adv["id"], card["id"])["sheet"])
    assert len(memories) == 1
    assert memories[0]["kind"] == "event"
    assert memories[0]["text"] == "Ada hid the compass."
    assert stored["life"]["mood"] == "wary"
    assert stored["bonds"]["trust"] == 77
    assert stored["humor"] == "dry jokes"


def test_tick_day_advances_and_reflects_after_six_memories(tmp_db, monkeypatch):
    adv = make_adventure()
    card = _character(adv["id"], humor="dry jokes")
    with db.session() as conn:
        for index in range(6):
            db.insert_npc_memory(
                conn, adventure_id=adv["id"], card_id=card["id"],
                kind="event", text=f"Ada counted crate {index}.",
            )

    def fake(_cfg, _messages, **_kwargs):
        return json.dumps({
            "event": "Ada walked the docks after dark.",
            "mood": "tired",
            "activity": "sleeping in the hammock",
            "reflection": "The player keeps secrets.",
        })

    monkeypatch.setattr("app.llm_client.chat_completion", fake)
    result = npc_life.tick_day(adv["id"])
    assert result["day"] == 1
    with db.session() as conn:
        stored_adv = db.get_adventure(conn, adv["id"])
        memories = db.list_npc_memories(conn, adv["id"], card["id"])
    assert stored_adv["life_day"] == 1
    assert any(row["kind"] == "reflection" and "secrets" in row["text"] for row in memories)


def test_build_context_includes_the_mind_of_a_present_character(tmp_db):
    adv = make_adventure()
    add_action(adv["id"], 0, "start", "Ada waits on the deck.")
    card = _character(adv["id"], life={"mood": "wary", "activity": "coiling rope"})
    with db.session() as conn:
        db.insert_npc_memory(
            conn, adventure_id=adv["id"], card_id=card["id"],
            kind="event", text="Ada hid the key.",
        )
        actions = db.list_actions(conn, adv["id"])
        cfg = settings_store.runtime_config(conn)
        fresh = db.get_adventure(conn, adv["id"])
        built = cb.build_context(fresh, actions, cfg, conn)
    rendered = "\n".join(message["content"] for message in built.messages)
    assert "[NPC minds]" in rendered
    assert "Ada — wary; coiling rope" in rendered
    assert "Ada hid the key." in rendered
