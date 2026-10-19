"""Lugar, item e vontade do NPC só existem se o mundo já os estabeleceu."""

from __future__ import annotations

import json

from app import context_builder as cb
from app import db, settings_store, world_facts
from app.npc_chronicle import apply_chronicle

from conftest import add_action, make_adventure
from test_v2 import make_v2_transport, override_transport


def _facts(adv, actions, cards=None):
    return world_facts.assess(adv, actions, cards or [], []).facts_line


def test_opening_does_not_invent_a_room_or_obedient_elara(tmp_db):
    adv = make_adventure(plot_essentials="[Name: Caio]")
    with db.session() as conn:
        db.insert_card(
            conn, adv["id"], type="character", title="Elara",
            triggers=["Elara"], entry="A woman you just met at the gate.",
        )
        cards = db.list_cards(conn, adv["id"])
    actions = [
        {"seq": 0, "type": "start", "text": "You stand before the gate. Elara watches you."},
        {"seq": 1, "type": "do", "text": "\n> You levar Elara para o quarto\n"},
    ]
    found = world_facts.assess(adv, actions, cards, [])
    assert found.where == "unestablished"
    assert found.blocked_places == ["quarto"]
    assert found.resisted == ["Elara"]
    assert "Blocked place: quarto" in found.facts_line
    assert "Elara resists" in found.facts_line
    assert "Não existe: quarto" in found.player_notice
    assert "Elara recusa" in found.player_notice
    assert "bedroom" not in found.image_directive.lower()
    assert "refuses" in found.image_directive
    assert "bedroom" in found.image_negative


def test_a_common_room_is_not_the_bedroom(tmp_db):
    adv = make_adventure()
    actions = [
        {"seq": 0, "type": "start", "text": "You stand in the tavern's common room. Elara dries a cup."},
        {"seq": 1, "type": "do", "text": "\n> You take Elara to the room\n"},
    ]
    cards = [{
        "type": "character", "title": "Elara", "triggers": ["Elara"],
        "entry": "The barkeep.", "sheet": "",
    }]
    found = world_facts.assess(adv, actions, cards, [])
    assert found.blocked_places == ["room"]
    assert "Elara" in found.resisted
    assert found.where.lower().startswith("tavern") or "common room" in found.where.lower()


def test_story_action_can_create_a_room_and_consent_must_already_exist(tmp_db):
    adv = make_adventure()
    cards = [{
        "type": "character", "title": "Elara", "triggers": ["Elara"],
        "entry": "She obeys you.", "sheet": "",
    }]
    created = [
        {"seq": 0, "type": "story", "text": "There is a bedroom upstairs. Elara waits in the hall."},
        {"seq": 1, "type": "do", "text": "\n> You take Elara to the bedroom\n"},
    ]
    allowed = world_facts.assess(adv, created, cards, [])
    assert allowed.blocked_places == []
    assert allowed.resisted == []

    stranger = [{
        "type": "character", "title": "Elara", "triggers": ["Elara"],
        "entry": "A stranger.", "sheet": "",
    }]
    refused = world_facts.assess(adv, created, stranger, [])
    assert refused.blocked_places == []
    assert refused.resisted == ["Elara"]


def test_narrator_compliance_does_not_become_canon(tmp_db):
    adv = make_adventure()
    cards = [{
        "type": "character", "title": "Elara", "triggers": ["Elara"],
        "entry": "A stranger.", "sheet": "",
    }]
    actions = [
        {"seq": 0, "type": "start", "text": "You stand in the yard. Elara stands apart."},
        {"seq": 1, "type": "do", "text": "\n> You levar Elara para o quarto\n"},
        {"seq": 2, "type": "continue", "text": "Elara follows you into the bedroom."},
    ]
    found = world_facts.assess(adv, actions, cards, [])
    assert "bedroom" not in found.canon_blob.lower()
    assert "quarto" in found.invented_places or found.ban_room
    assert "Elara" in found.invented_people
    assert "Stay in" in found.image_moment("Elara follows you into the bedroom.")
    assert "bedroom" not in found.image_moment("ignored").lower()


def test_missing_sword_and_conjure_without_power(tmp_db):
    adv = make_adventure(plot_essentials="[Name: Caio]")
    actions = [
        {"seq": 0, "type": "start", "text": "You stand in the yard."},
        {"seq": 1, "type": "do", "text": "\n> You draw your sword and conjure a bedroom\n"},
    ]
    found = world_facts.assess(adv, actions, [], [])
    assert "sword" in [item.casefold() for item in found.blocked_items]
    assert found.conjure_denied is True
    assert any(world_facts.is_private_room_phrase(place) or world_facts.is_generic_room(place)
               for place in found.blocked_places)


def test_item_card_and_yield_allow_the_attempt(tmp_db):
    adv = make_adventure()
    cards = [
        {"type": "item", "title": "Sword", "triggers": ["sword"], "entry": "Caio carries it.", "sheet": ""},
        {"type": "character", "title": "Elara", "triggers": ["Elara"],
         "entry": "She obeys you.", "sheet": ""},
    ]
    actions = [
        {"seq": 0, "type": "start", "text": "Elara waits by the fire."},
        {"seq": 1, "type": "do", "text": "\n> You draw your sword\n"},
    ]
    found = world_facts.assess(adv, actions, cards, [])
    assert found.blocked_items == []
    assert found.resisted == []


def test_pronoun_order_uses_the_person_already_in_the_scene(tmp_db):
    adv = make_adventure()
    cards = [{
        "type": "character", "title": "Elara", "triggers": ["Elara"],
        "entry": "A stranger.", "sheet": "",
    }]
    actions = [
        {"seq": 0, "type": "start", "text": "You stand in the yard. Elara watches you."},
        {"seq": 1, "type": "do", "text": "\n> You leva ela para o quarto\n"},
    ]
    found = world_facts.assess(adv, actions, cards, [])
    assert found.blocked_places == ["quarto"]
    assert found.resisted == ["Elara"]
    assert found.absent == []


def test_see_ignores_a_narration_that_obeyed_the_blocked_order(client, tmp_path):
    adv = make_adventure(plot_essentials="[Name: Caio]")
    with db.session() as conn:
        db.set_setting(conn, "image.storage_dir", json.dumps(str(tmp_path / "imgs")))
        db.insert_card(
            conn, adv["id"], type="location", title="The Yard",
            triggers=["yard"], entry="Packed earth and a barred gate.",
        )
        db.insert_card(
            conn, adv["id"], type="character", title="Elara",
            triggers=["Elara"], entry="A woman you just met.",
        )
    add_action(adv["id"], 0, "start", "You stand in the yard. Elara watches you.")
    add_action(adv["id"], 1, "do", "\n> You levar Elara para o quarto\n")
    add_action(adv["id"], 2, "continue", "Elara follows you into the bedroom and closes the door.")
    override_transport(client, make_v2_transport())
    try:
        resp = client.post(f"/api/adventures/{adv['id']}/see", json={"action_seq": 2})
    finally:
        client.app.dependency_overrides.clear()
    assert resp.status_code == 200, resp.text
    prompt = resp.json()["prompt"].lower()
    assert "yard" in prompt
    assert "bedroom" not in prompt
    assert "quarto" not in prompt
    assert "does not exist" in prompt or "refus" in prompt


def test_facts_block_reaches_the_narrator(tmp_db):
    adv = make_adventure(plot_essentials="[Name: Caio]")
    add_action(adv["id"], 0, "start", "You stand before the gate. Elara watches you.")
    add_action(adv["id"], 1, "do", "\n> You levar Elara para o quarto\n")
    with db.session() as conn:
        db.insert_card(
            conn, adv["id"], type="character", title="Elara",
            triggers=["Elara"], entry="A stranger at the gate.",
        )
        actions = db.list_actions(conn, adv["id"])
        built = cb.build_context(adv, actions, settings_store.runtime_config(conn), conn)
    final = built.messages[-1]["content"]
    assert "Blocked place: quarto" in final
    assert "Elara resists" in final
    assert "absolute agency" not in built.messages[0]["content"]
    assert "A do or say is an attempt" in built.messages[0]["content"]


def test_see_paints_the_refusal_in_the_yard_not_a_bedroom(client, tmp_path):
    adv = make_adventure(plot_essentials="[Name: Caio]")
    with db.session() as conn:
        db.set_setting(conn, "image.storage_dir", json.dumps(str(tmp_path / "imgs")))
        db.insert_card(
            conn, adv["id"], type="location", title="The Yard",
            triggers=["yard"], entry="Packed earth and a barred gate.",
        )
        db.insert_card(
            conn, adv["id"], type="character", title="Elara",
            triggers=["Elara"], entry="A woman you just met.",
            portrait_prompt="black hair, green cloak",
        )
    add_action(adv["id"], 0, "start", "You stand in the yard. Elara watches you.")
    add_action(adv["id"], 1, "do", "\n> You levar Elara para o quarto\n")
    capture: list = []
    override_transport(client, make_v2_transport(capture=capture))
    try:
        resp = client.post(f"/api/adventures/{adv['id']}/see", json={"action_seq": 1})
    finally:
        client.app.dependency_overrides.clear()
    assert resp.status_code == 200, resp.text
    prompt = resp.json()["prompt"].lower()
    head = prompt.split("break", 1)[0]
    assert "yard" in head
    assert "refus" in head
    assert "quarto" not in prompt
    assert "bedroom" not in prompt
    assert "elara" in head
    sent = json.dumps(capture).lower()
    assert "levar elara para o quarto" not in sent
    assert "refus" in sent
    with db.session() as conn:
        image = db.get_image(conn, resp.json()["id"])
    assert "bedroom" in image["negative_prompt"].lower()


def test_see_keeps_nudity_the_narrator_already_established(client, tmp_path):
    adv = make_adventure(plot_essentials="[Name: Caio]")
    with db.session() as conn:
        db.set_setting(conn, "image.storage_dir", json.dumps(str(tmp_path / "imgs")))
        db.insert_card(
            conn, adv["id"], type="character", title="Princess Alusair",
            triggers=["Alusair"], entry="Steel Regent.",
            portrait_prompt="long red hair, green eyes, plate armor",
        )
    add_action(adv["id"], 0, "start", "Princess Alusair is naked on the bed.")
    capture: list = []
    override_transport(client, make_v2_transport(capture=capture))
    try:
        resp = client.post(f"/api/adventures/{adv['id']}/see", json={"action_seq": 0})
    finally:
        client.app.dependency_overrides.clear()
    assert resp.status_code == 200, resp.text
    prompt = resp.json()["prompt"].lower()
    assert "naked" in prompt
    assert "clothes off" in prompt
    assert "plate armor" not in prompt
    sent = json.dumps(capture)
    assert "Do not censor adult nudity" in sent


def test_scene_notice_names_the_missing_room(client):
    adv = make_adventure()
    with db.session() as conn:
        db.insert_card(
            conn, adv["id"], type="location", title="The Yard",
            triggers=["yard"], entry="Open ground.",
        )
        db.insert_card(
            conn, adv["id"], type="character", title="Elara",
            triggers=["Elara"], entry="A stranger.",
        )
    add_action(adv["id"], 0, "start", "You stand in the yard. Elara watches the gate.")
    add_action(adv["id"], 1, "do", "\n> You levar Elara para o quarto\n")
    resp = client.post(f"/api/adventures/{adv['id']}/scene", json={})
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["location"] == "The Yard"
    assert "quarto" in data["notice"]
    assert "Elara recusa" in data["notice"]
    with db.session() as conn:
        job = next(item for item in db.list_jobs(conn, adv["id"]) if item["type"] == "scene_backdrop")
    assert "quarto" not in job["payload"]["prompt"].lower()
    assert "bedroom" not in job["payload"]["prompt"].lower()


def test_chronicle_drops_a_room_the_attempt_invented(tmp_db):
    adv = make_adventure()
    actions = [
        {"seq": 0, "type": "start", "text": "You stand in the yard. Elara watches you."},
        {"seq": 1, "type": "do", "text": "\n> You levar Elara para o quarto\n"},
        {"seq": 2, "type": "continue", "text": "Elara follows you into the bedroom."},
    ]
    cards = [{
        "type": "character", "title": "Elara", "triggers": ["Elara"],
        "entry": "A stranger.", "sheet": "",
    }]
    found = world_facts.assess(adv, actions, cards, [])
    payload = world_facts.filter_chronicle_payload({
        "places": [{"name": "Bedroom", "aliases": ["quarto"], "summary": "The room."}],
        "items": [{"name": "Sword", "aliases": [], "summary": "He drew it."}],
        "characters": [{
            "name": "Elara",
            "existing_title": "Elara",
            "facts": ["She follows him into the bedroom."],
        }],
    }, found)
    assert payload["places"] == []
    assert payload["characters"][0]["facts"] == []
    with db.session() as conn:
        db.insert_card(
            conn, adv["id"], type="character", title="Elara",
            triggers=["Elara"], entry="A stranger.",
        )
        result = apply_chronicle(conn, adv["id"], payload, "")
        titles = [card["title"] for card in db.list_cards(conn, adv["id"])]
    assert "Bedroom" not in titles
    assert "Sword" in titles
