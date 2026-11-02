"""Lugar, item e vontade do NPC só existem se o mundo já os estabeleceu."""

from __future__ import annotations

import json
import time

from app import context_builder as cb
from app import db, image_client, jobs, settings_store, world_facts
from app.npc_chronicle import apply_chronicle

from conftest import add_action, make_adventure
from test_scene import _face, _reference_image_nodes, pipeline_transport
from test_v2 import PNG_BYTES, make_v2_transport, override_transport


def _stub_painter(monkeypatch, tmp_path) -> None:
    """O teste olha o cânone do prompt. O executor só grava o PNG."""

    def fake_generate(cfg, prompt, *, adventure_id, image_id, **kwargs):
        directory = tmp_path / "imgs" / adventure_id
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{image_id}.png"
        path.write_bytes(PNG_BYTES)
        return {
            "file_path": str(path),
            "width": int(kwargs.get("width") or 1024),
            "height": int(kwargs.get("height") or 1024),
            "provider": "comfyui",
        }

    monkeypatch.setattr(image_client, "generate_image", fake_generate)


def _facts(adv, actions, cards=None):
    return world_facts.assess(adv, actions, cards or [], []).facts_line


def test_elara_is_a_woman_and_caio_is_a_man():
    from app import npc_assets
    assert npc_assets.sex_label("Elara", "Elara recusa. Ela fica no portão.") == "adult woman"
    assert npc_assets.sex_label("Caio", "Caio observa. Ele espera.") == "adult man"
    assert npc_assets.sex_label("Elara", "You place Elara on the bed as Caio watches her.") == ""
    assert npc_assets.sex_label(
        "Elara", "You place Elara on the bed.",
        card={"title": "Elara", "entry": "Uma mulher.", "sheet": ""},
    ) == "adult woman"
    assert npc_assets.sex_label("Elara", "Elara vê o rei ao longe.") == ""
    assert npc_assets.sex_label("Caio", "You place Elara on the bed as Caio watches her.") == ""
    assert npc_assets.sex_label(
        "Caio", "Caio watches as Lexi removes her dress. Ela hesita. Ela respira.",
        card={"title": "Caio", "entry": "", "sheet": "",
              "portrait_prompt": "A handsome vampire with pale skin"},
    ) == "adult man"
    assert npc_assets.nude_names(
        [{"title": "Caio"}, {"title": "Lexi"}],
        "Caio watches as Lexi, completely naked, kneels.",
    ) == ["Lexi"]
    assert npc_assets.nude_names(
        [{"title": "Caio"}, {"title": "A Donzela da Noite", "aliases": ["Lexi"]}],
        "lexi completamente nua na minha frente",
    ) == ["A Donzela da Noite"]
    assert npc_assets.nude_names(
        [{"title": "Zendaya"}, {"title": "Clara"}],
        "Zendaya and Clara are naked together.",
    ) == ["Zendaya", "Clara"]
    assert npc_assets.nude_names(
        [{"title": "Caio"}, {"title": "Lexi"}], "Caio sees Lexi naked.",
    ) == ["Lexi"]
    assert npc_assets.nude_names(
        [{"title": "Zendaya"}, {"title": "Elara"}],
        "Zendaya is naked. Elara is not naked.",
    ) == ["Zendaya"]
    assert npc_assets.nude_names(
        [{"title": "Thalita Massoto"}], "Thalita está pelada.",
    ) == ["Thalita Massoto"]


def test_nude_names_combines_moment_and_order_without_crossing_people():
    from app import npc_assets

    cards = [{"title": "Caio"}, {"title": "Lexi"}, {"title": "Clara"}]
    assert npc_assets.nude_names(
        [{"title": "Elara"}],
        "Elara desliza o vestido de seda e fica nua, a pele na luz da lua.",
    ) == ["Elara"]
    moment = "Caio is naked. Lexi is naked."
    assert npc_assets.nude_names(cards, moment, "wide shot") == ["Caio", "Lexi"]
    assert npc_assets.nude_names(cards, moment, "Caio is clothed") == ["Lexi"]
    assert npc_assets.nude_names(cards, moment, "Clara was naked") == [
        "Caio", "Lexi", "Clara",
    ]
    assert npc_assets.nude_names(cards, moment, "Caio is not naked") == ["Lexi"]
    assert npc_assets.nude_names(
        cards, "Caio observa enquanto Lexi fica nua.",
    ) == ["Lexi"]
    assert npc_assets.nude_names(
        cards, "Caio não está nu; Lexi está nua.",
    ) == ["Lexi"]
    assert npc_assets.nude_names(
        cards, "Caio was naked and Lexi is still naked.",
    ) == ["Caio", "Lexi"]
    assert npc_assets.nude_names(
        cards, "Lexi is naked, no underwear.",
    ) == ["Lexi"]
    assert npc_assets.nude_names(
        [{"title": "Thalita Massoto"}, {"title": "Thalita Smith"}],
        "Thalita está nua.",
    ) == []
    assert npc_assets.nude_names(
        [{"title": "Thalita"}, {"title": "Thalita Massoto"}],
        "Thalita está nua.",
    ) == ["Thalita"]
    assert npc_assets.nude_names(
        [{"title": "Elara"}], "Elara is naked.",
        "Elara waits and is not naked",
    ) == []
    assert npc_assets.nude_names(
        [{"title": "Elara"}], "Elara is clothed and then is naked.",
    ) == ["Elara"]
    assert npc_assets.nude_names(
        [{"title": "Elara"}], "", "Elara is clothed; Caio is naked",
    ) == []


def test_negative_without_nudity_ban_handles_nested_weights():
    from app import npc_assets

    assert npc_assets.negative_without_nudity_ban(
        "((nude:1.2)), naked:1.4, text, (blurry:1.3)",
    ) == "text, (blurry:1.3)"


def test_see_calls_elara_a_woman_before_the_act(client, tmp_path, monkeypatch):
    adv = make_adventure(plot_essentials="[Name: Caio]")
    effects = {"world_effects": [
        {"kind": "physical", "target": "Elara", "duration": "scene",
         "summary": "A saia subiu.", "visual_en": "skirt lifted by invisible touch"},
        {"kind": "social", "target": "Elara", "duration": "permanent",
         "summary": "Ela virou Lexi.", "visual_en": "name Lexi floating in air"},
        {"kind": "reality", "target": "world", "duration": "permanent",
         "summary": "Chuva permanente.", "visual_en": "rain over the apartment"},
    ]}
    with db.session() as conn:
        db.update_adventure_ui_state(conn, adv["id"], effects)
        db.set_setting(conn, "image.storage_dir", json.dumps(str(tmp_path / "imgs")))
        db.set_setting(conn, "image.negative_prompt", json.dumps("nude, text, watermark"))
        card = db.insert_card(
            conn, adv["id"], type="character", title="Elara",
            triggers=["Elara"], entry="Uma mulher de cabelos negros.",
        )
    _face(adv["id"], card["id"], PNG_BYTES)
    add_action(adv["id"], 0, "start", "Elara está no quarto. Ela observa. Caio está vestido. Ele espera.")
    add_action(adv["id"], 1, "continue", "Elara está vestida e observa Caio.")
    # O ato chega ao CLIP pela frase do modelo (prosa crua não entra mais).
    _stub_painter(monkeypatch, tmp_path)
    override_transport(client, make_v2_transport("Caio places Elara on the bed."))
    try:
        resp = client.post(
            f"/api/adventures/{adv['id']}/see",
            json={"action_seq": 1, "extra_prompt": "Elara completely naked"},
        )
    finally:
        client.app.dependency_overrides.clear()
    assert resp.status_code == 200, resp.text
    prompt = resp.json()["prompt"]
    assert prompt.startswith("Player instruction, highest priority: Elara completely naked")
    assert "fully nude" in prompt
    assert "bare skin" in prompt
    assert "completely unclothed" in prompt
    assert prompt.index("fully nude") < prompt.index("same continuous scene")
    assert "no stockings" not in prompt
    head = prompt.split("BREAK", 1)[0]
    assert "elara is an adult woman" in head.lower()
    with db.session() as conn:
        negative = db.get_image(conn, resp.json()["id"])["negative_prompt"].lower()
    assert "masculine body" not in negative
    assert "nude" not in negative
    assert "underwear" in negative
    assert "text" in negative
    assert "remove o vestido" not in negative
    assert "skirt lifted" not in prompt
    assert "floating in air" not in prompt
    assert "rain over the apartment" not in prompt


def test_explicit_see_sync_and_async_send_sanitized_negative(client, tmp_path):
    adv = make_adventure()
    with db.session() as conn:
        db.set_setting(conn, "image.provider", json.dumps("comfyui"))
        db.set_setting(conn, "image.storage_dir", json.dumps(str(tmp_path / "imgs")))
        db.set_setting(
            conn, "image.negative_prompt",
            json.dumps("(nude:1.2), naked:1.4, text, watermark"),
        )
        card = db.insert_card(
            conn, adv["id"], type="character", title="Elara",
            triggers=["Elara"], entry="A woman with black hair.",
            portrait_prompt="black hair, green eyes",
        )
    _face(adv["id"], card["id"], PNG_BYTES)
    add_action(adv["id"], 0, "start", "Elara waits in the room.")
    capture: dict = {}
    transport = pipeline_transport("Elara waits naked in the room.", capture)

    def wait_pending(job_id: str) -> None:
        deadline = time.monotonic() + 5
        while True:
            with db.session() as conn:
                status = db.get_job(conn, job_id)["status"]
            if status == "pending":
                return
            assert status != "error"
            assert time.monotonic() < deadline
            time.sleep(0.01)

    override_transport(client, transport)
    try:
        sync = client.post(
            f"/api/adventures/{adv['id']}/see",
            json={"action_seq": 0, "extra_prompt": "Elara is naked, no underwear"},
        )
        assert sync.status_code == 200, sync.text
        queued = client.post(
            f"/api/adventures/{adv['id']}/see?async=1&variation=1",
            json={"action_seq": 0, "extra_prompt": "Elara is naked, no underwear"},
        )
        assert queued.status_code == 202, queued.text
        wait_pending(queued.json()["job_id"])
        assert jobs.process_pending(transport) == 1

        with db.session() as conn:
            db.set_setting(conn, "image.provider", json.dumps("openai_compatible"))
        switched = client.post(
            f"/api/adventures/{adv['id']}/see?async=1&variation=2",
            json={"action_seq": 0, "extra_prompt": "Elara is naked, no underwear"},
        )
        assert switched.status_code == 202, switched.text
        wait_pending(switched.json()["job_id"])
        with db.session() as conn:
            db.set_setting(conn, "image.provider", json.dumps("comfyui"))
        assert jobs.process_pending(transport) == 1
    finally:
        client.app.dependency_overrides.clear()

    assert len(capture["graphs"]) == 3
    for graph in capture["graphs"]:
        qwen = next((node["inputs"] for node in graph.values()
                     if node["class_type"] == "TextEncodeQwenImage21"), None)
        if qwen is not None:
            negative = qwen["negative_prompt"].lower()
            assert "nude" not in negative
            assert "naked" not in negative
            assert "text" in negative
            assert "watermark" in negative
        else:
            zero = next(node for node in graph.values()
                        if node["class_type"] == "ConditioningZeroOut")
            assert zero["inputs"]["conditioning"] == ["4", 0]
    assert _reference_image_nodes(capture["graphs"][-1])


def test_see_does_not_borrow_nudity_from_unresolved_player(client, tmp_path, monkeypatch):
    adv = make_adventure(plot_essentials="[Name: Caio]")
    with db.session() as conn:
        db.set_setting(conn, "image.storage_dir", json.dumps(str(tmp_path / "imgs")))
        card = db.insert_card(
            conn, adv["id"], type="character", title="Elara",
            triggers=["Elara"], entry="A woman with black hair.",
            portrait_prompt="black hair, green eyes",
        )
    _face(adv["id"], card["id"], PNG_BYTES)
    add_action(adv["id"], 0, "start", "Elara waits with you.")
    _stub_painter(monkeypatch, tmp_path)
    override_transport(client, make_v2_transport("Elara waits."))
    try:
        response = client.post(
            f"/api/adventures/{adv['id']}/see",
            json={"action_seq": 0, "extra_prompt": "Elara is clothed; Caio is naked"},
        )
    finally:
        client.app.dependency_overrides.clear()
    assert response.status_code == 200, response.text
    prompt = response.json()["prompt"]
    assert "fully nude only for" not in prompt
    assert "fully clothed, keep their current outfits: Elara" in prompt


def test_previous_nudity_does_not_reactivate_explicit_outside_minor_guard(
        client, tmp_path, monkeypatch):
    adv = make_adventure()
    with db.session() as conn:
        db.set_setting(conn, "image.storage_dir", json.dumps(str(tmp_path / "imgs")))
        lia = db.insert_card(
            conn, adv["id"], type="character", title="Lia", triggers=["Lia"],
            entry="A young woman.", portrait_prompt="short black hair",
            sheet=json.dumps({"minor": True, "age": 16}),
        )
        ana = db.insert_card(
            conn, adv["id"], type="character", title="Ana", triggers=["Ana"],
            entry="An adult woman.", portrait_prompt="long red hair",
        )
    _face(adv["id"], lia["id"], PNG_BYTES)
    _face(adv["id"], ana["id"], PNG_BYTES)
    add_action(adv["id"], 0, "start", "Lia and Ana are naked.")
    add_action(adv["id"], 1, "do", "Lia waits.")
    add_action(adv["id"], 2, "do", "Ana waves.")
    _stub_painter(monkeypatch, tmp_path)
    override_transport(client, make_v2_transport("Lia waits while Ana waves."))
    try:
        response = client.post(
            f"/api/adventures/{adv['id']}/see", json={"action_seq": 2},
        )
    finally:
        client.app.dependency_overrides.clear()
    assert response.status_code == 200, response.text
    with db.session() as conn:
        image = db.get_image(conn, response.json()["id"])
    assert "nude" in image["negative_prompt"].lower()
    assert "fully clothed" in response.json()["prompt"].lower()


def test_fifth_person_cannot_reenter_see_without_frame_validation(
        client, tmp_path, monkeypatch):
    _stub_painter(monkeypatch, tmp_path)
    override_transport(client, make_v2_transport("The group waits."))
    try:
        adult = make_adventure(title="Quinto adulto")
        with db.session() as conn:
            db.set_setting(conn, "image.storage_dir", json.dumps(str(tmp_path / "imgs")))
            cards = []
            for name in ("Ada", "Elara", "Lexi", "Bela", "Caio"):
                cards.append(db.insert_card(
                    conn, adult["id"], type="character", title=name, triggers=[name],
                    entry="An adult.", portrait_prompt=f"{name} portrait",
                ))
        for card in cards:
            _face(adult["id"], card["id"], PNG_BYTES)
        add_action(adult["id"], 0, "start",
                   "Ada, Elara, Lexi and Bela are naked. Caio is clothed.")
        response = client.post(f"/api/adventures/{adult['id']}/see", json={"action_seq": 0})
        assert response.status_code == 200, response.text
        prompt = response.json()["prompt"]
        assert "Exactly 4 people" in prompt
        assert "Exactly 5 people" not in prompt
        assert "fully clothed" in prompt

        minor = make_adventure(title="Quinto menor")
        with db.session() as conn:
            db.set_setting(conn, "image.storage_dir", json.dumps(str(tmp_path / "imgs")))
            cards = []
            for name in ("Ada", "Elara", "Lexi", "Bela", "Minor"):
                cards.append(db.insert_card(
                    conn, minor["id"], type="character", title=name, triggers=[name],
                    entry="A person.", portrait_prompt=f"{name} portrait",
                    sheet=json.dumps({"minor": True, "age": 16}) if name == "Minor" else "{}",
                ))
        for card in cards:
            _face(minor["id"], card["id"], PNG_BYTES)
        add_action(minor["id"], 0, "start", "Ada, Elara, Lexi, Bela and Minor are naked.")
        response = client.post(
            f"/api/adventures/{minor['id']}/see?async=1", json={"action_seq": 0})
        if response.status_code == 202:
            deadline = time.monotonic() + 5
            while True:
                with db.session() as conn:
                    job = db.get_job(conn, response.json()["job_id"])
                    status = job["status"]
                if status == "pending":
                    break
                assert status != "error"
                assert time.monotonic() < deadline
                time.sleep(0.01)
            assert "Minor" not in job["payload"]["prompt"]
            assert "Exactly 4 people" in job["payload"]["prompt"]
        else:
            assert response.status_code == 422
            assert response.json()["error"]["code"] == "minor"
    finally:
        client.app.dependency_overrides.clear()


def test_dead_minor_cannot_reenter_after_frame_decision(client, tmp_path, monkeypatch):
    adv = make_adventure(title="Morto e menor")
    with db.session() as conn:
        db.set_setting(conn, "image.storage_dir", json.dumps(str(tmp_path / "imgs")))
        ada = db.insert_card(
            conn, adv["id"], type="character", title="Ada", triggers=["Ada"],
            entry="An adult.", portrait_prompt="Ada portrait",
        )
        minor = db.insert_card(
            conn, adv["id"], type="character", title="Minor", triggers=["Minor"],
            entry="A dead person.", portrait_prompt="Minor portrait",
            sheet=json.dumps({"status": "dead", "minor": True, "age": 16}),
        )
    _face(adv["id"], ada["id"], PNG_BYTES)
    _face(adv["id"], minor["id"], PNG_BYTES)
    add_action(adv["id"], 0, "start", "Ada is naked. Minor is naked.")
    _stub_painter(monkeypatch, tmp_path)
    override_transport(client, make_v2_transport("Ada waits."))
    try:
        response = client.post(f"/api/adventures/{adv['id']}/see", json={"action_seq": 0})
    finally:
        client.app.dependency_overrides.clear()
    assert response.status_code == 200, response.text
    prompt = response.json()["prompt"]
    assert "Exactly 1 people" in prompt
    assert "Minor" not in prompt


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


def test_first_person_kiss_and_grab_are_resisted(tmp_db):
    adv = make_adventure()
    cards = [{
        "type": "character", "title": "Elara", "triggers": ["Elara"],
        "entry": "A stranger.", "sheet": "",
    }]
    kiss = [
        {"seq": 0, "type": "start", "text": "You stand in the yard. Elara watches you."},
        {"seq": 1, "type": "do", "text": "\n> You beijo Elara\n"},
    ]
    found = world_facts.assess(adv, kiss, cards, [])
    assert found.resisted == ["Elara"]
    grab = [
        {"seq": 0, "type": "start", "text": "You stand in the yard. Elara watches you."},
        {"seq": 1, "type": "do", "text": "\n> You agarro ela\n"},
    ]
    found = world_facts.assess(adv, grab, cards, [])
    assert found.resisted == ["Elara"]
    hit = [
        {"seq": 0, "type": "start", "text": "You stand in the yard. Elara watches you."},
        {"seq": 1, "type": "do", "text": "\n> You dar um cascudo na Elara\n"},
    ]
    found = world_facts.assess(adv, hit, cards, [])
    assert found.resisted == ["Elara"]
    assert world_facts.narration_lacks_refusal(
        "O crânio de Elara encontra a neve e ela se cala.")
    assert not world_facts.narration_lacks_refusal(
        "Elara desvia e dá um passo para trás.")


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


def test_see_ignores_a_narration_that_obeyed_the_blocked_order(client, tmp_path, monkeypatch):
    adv = make_adventure(plot_essentials="[Name: Caio]")
    with db.session() as conn:
        db.set_setting(conn, "image.storage_dir", json.dumps(str(tmp_path / "imgs")))
        db.insert_card(
            conn, adv["id"], type="location", title="The Yard",
            triggers=["yard"], entry="Packed earth and a barred gate.",
        )
        card = db.insert_card(
            conn, adv["id"], type="character", title="Elara",
            triggers=["Elara"], entry="A woman you just met.",
        )
    _face(adv["id"], card["id"], b"\x89PNG\r\nelara-yard")
    add_action(adv["id"], 0, "start", "You stand in the yard. Elara watches you.")
    add_action(adv["id"], 1, "do", "\n> You levar Elara para o quarto\n")
    add_action(adv["id"], 2, "continue", "Elara follows you into the bedroom and closes the door.")
    _stub_painter(monkeypatch, tmp_path)
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


def test_see_paints_the_refusal_in_the_yard_not_a_bedroom(client, tmp_path, monkeypatch):
    adv = make_adventure(plot_essentials="[Name: Caio]")
    with db.session() as conn:
        db.set_setting(conn, "image.storage_dir", json.dumps(str(tmp_path / "imgs")))
        yard = db.insert_card(
            conn, adv["id"], type="location", title="The Yard",
            triggers=["yard"], entry="Packed earth and a barred gate.",
        )
        card = db.insert_card(
            conn, adv["id"], type="character", title="Elara",
            triggers=["Elara"], entry="A woman you just met.",
            portrait_prompt="black hair, green cloak",
        )
        caio = db.insert_card(
            conn, adv["id"], type="character", title="Caio",
            triggers=["Caio"], entry="The man in the yard.",
            portrait_prompt="short black hair",
            use_for_character_creation=True,
        )
    _face(adv["id"], yard["id"], b"\x89PNG\r\nyard-anchor")
    _face(adv["id"], card["id"], b"\x89PNG\r\nelara-cloak")
    _face(adv["id"], caio["id"], b"\x89PNG\r\ncaio-anchor")
    add_action(adv["id"], 0, "start", "You stand in the yard. Elara watches you.")
    add_action(adv["id"], 1, "do", "\n> You levar Elara para o quarto\n")
    capture: list = []
    _stub_painter(monkeypatch, tmp_path)
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


def test_see_keeps_nudity_the_narrator_already_established(client, tmp_path, monkeypatch):
    adv = make_adventure(plot_essentials="[Name: Caio]")
    with db.session() as conn:
        db.set_setting(conn, "image.storage_dir", json.dumps(str(tmp_path / "imgs")))
        card = db.insert_card(
            conn, adv["id"], type="character", title="Princess Alusair",
            triggers=["Alusair"], entry="Steel Regent.",
            portrait_prompt="long red hair, green eyes, plate armor",
        )
    _face(adv["id"], card["id"], PNG_BYTES)
    add_action(adv["id"], 0, "start", "Princess Alusair is naked on the bed.")
    capture: list = []
    # A nudez chega ao CLIP pela frase do modelo; prova também que scrub e
    # contradicting_looks não a removem.
    _stub_painter(monkeypatch, tmp_path)
    override_transport(client, make_v2_transport("Princess Alusair lies naked on the bed.", capture=capture))
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
        card = db.insert_card(
            conn, adv["id"], type="character", title="Elara",
            triggers=["Elara"], entry="A stranger.",
        )
    _face(adv["id"], card["id"], b"\x89PNG\r\nelara-notice")
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
