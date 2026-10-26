"""Passe dos NPCs e o cânone visual usado na imagem da cena."""

from __future__ import annotations

import json

from app import db, npc_assets, npc_chronicle
from conftest import add_action, make_adventure
from test_v2 import make_v2_transport, override_transport


def test_chronicle_creates_and_updates_without_replacing_the_face(tmp_db):
    adv = make_adventure(plot_essentials="[Name: Caio; Class: Sorcerer]")
    with db.session() as conn:
        db.insert_card(
            conn, adv["id"], type="character", title="Princess Alusair",
            triggers=["Alusair"], entry="Steel Regent of Cormyr.",
            sheet=json.dumps({
                "appearance": "long red hair, green eyes",
                "clothing": "plate armor",
                "stats": {"cha": 16, "str": 14},
            }),
        )
        result = npc_chronicle.apply_chronicle(conn, adv["id"], {
            "characters": [
                {
                    "name": "Caio",
                    "summary": "The player, who must not become a card.",
                    "facts": ["looks around the room"],
                },
                {
                    "name": "Princess Alusair",
                    "existing_title": "Princess Alusair",
                    "appearance": "dark hair and a white gown",
                    "look_changed": False,
                    "clothing": "a white gown",
                    "status": "alive",
                    "facts": ["Her father the king was killed in the north."],
                    "stat_delta": {"cha": 1, "str": 9},
                },
                {
                    "name": "Vangerdahast",
                    "aliases": ["the mage"],
                    "summary": "Royal mage of Cormyr.",
                    "appearance": "bald, grey beard, blue eyes",
                    "clothing": "purple robes",
                    "status": "alive",
                    "facts": ["He warns Alusair about the northern army."],
                },
            ],
        }, adv["plot_essentials"])
        cards = {card["title"]: card for card in db.list_cards(conn, adv["id"])}
        jobs = db.list_jobs(conn, adv["id"])

    assert result == {"created": 1, "updated": 1, "bodies": 0}
    assert "Caio" not in cards
    princess = npc_assets.parse_sheet(cards["Princess Alusair"]["sheet"])
    assert princess["appearance"] == "long red hair, green eyes"
    assert princess["clothing"] == "plate armor"
    assert "king was killed" in princess["notes"]
    assert princess["stats"]["cha"] == 17
    assert princess["stats"]["str"] == 16  # delta acima de 2 é cortado
    mage = cards["Vangerdahast"]
    assert "the mage" in mage["triggers"]
    assert npc_assets.parse_sheet(mage["sheet"])["appearance"].startswith("bald")
    assert any(job["ref_id"] == mage["id"] and job["type"] == "character_portrait" for job in jobs)


def test_character_canon_overrides_the_scene_prose():
    text = "Princess Alusair reclines in a white gown, her dark hair loose."
    canon = npc_assets.character_canon([
        {
            "type": "character",
            "title": "Princess Alusair",
            "triggers": ["Alusair"],
            "sheet": json.dumps({
                "appearance": "long red hair, green eyes",
                "clothing": "plate armor",
                "status": "alive",
            }),
        },
    ], text)
    assert "long red hair" in canon
    assert "plate armor" in canon
    assert "overrides the prose" in canon


def test_see_prompt_includes_the_character_canon(client, tmp_path):
    adv = make_adventure(image_style="ink sketch")
    with db.session() as conn:
        db.insert_card(
            conn, adv["id"], type="character", title="Princess Alusair",
            triggers=["Alusair"], entry="Steel Regent of Cormyr.",
            sheet=json.dumps({
                "appearance": "long red hair, green eyes",
                "clothing": "plate armor",
            }),
        )
        db.set_setting(conn, "image.storage_dir", json.dumps(str(tmp_path / "imgs")))
    add_action(adv["id"], 0, "continue",
               "Princess Alusair reclines in a white gown, dark hair loose.")
    capture: list = []
    override_transport(client, make_v2_transport(capture=capture))
    try:
        resp = client.post(f"/api/adventures/{adv['id']}/see", json={"action_seq": 0})
    finally:
        client.app.dependency_overrides.clear()
    assert resp.status_code == 200, resp.text
    prompt = resp.json()["prompt"]
    assert "long red hair, green eyes" in prompt
    assert "plate armor" in prompt
    assert "white gown" not in prompt.lower()
    assert "dark hair" not in prompt.lower()
    assert "reclines" not in prompt.lower()
    assert prompt.lower().index("long red hair") < prompt.lower().index("dark tower")
    assert " BREAK " in prompt
    sent = json.dumps(capture)
    assert "long red hair" in sent
    with db.session() as conn:
        image = db.get_image(conn, resp.json()["id"])
    assert "gown" in image["negative_prompt"].lower()


def test_rendered_card_carries_the_sheet_into_the_prompt(tmp_db):
    from app import context_builder as cb
    from app import settings_store
    adv = make_adventure()
    add_action(adv["id"], 0, "start", "Princess Alusair waits.")
    with db.session() as conn:
        db.insert_card(
            conn, adv["id"], type="character", title="Princess Alusair",
            triggers=["Alusair"], entry="Steel Regent.", pinned=True,
            sheet=json.dumps({
                "appearance": "long red hair",
                "status": "alive",
                "stats": {"str": 14, "cha": 16},
            }),
        )
        actions = db.list_actions(conn, adv["id"])
        built = cb.build_context(adv, actions, settings_store.runtime_config(conn), conn)
    final = built.messages[-1]["content"]
    assert "long red hair" in final
    assert "Status: alive" in final
    assert "str 14" in final
    assert cb.CANON_CUE in final


def test_status_and_face_survive_a_long_entry():
    rendered = npc_assets.card_canon_text({
        "title": "Princess Alusair",
        "entry": "x" * 800,
        "sheet": json.dumps({"status": "dead", "appearance": "long red hair"}),
    })
    assert rendered.index("Status: dead") < rendered.index("x" * 40)
    assert rendered.index("long red hair") < rendered.index("x" * 40)


def test_see_paints_the_latest_beat_not_the_old_story(client, tmp_path):
    adv = make_adventure()
    with db.session() as conn:
        db.set_setting(conn, "image.storage_dir", json.dumps(str(tmp_path / "imgs")))
    add_action(adv["id"], 0, "start", "An ancient dragon sleeps under the mountain.")
    add_action(adv["id"], 1, "do", "\n> You open the gate\n")
    add_action(adv["id"], 2, "continue", "Princess Alusair steps into the rain.")
    capture: list = []
    override_transport(client, make_v2_transport(capture=capture))
    try:
        resp = client.post(f"/api/adventures/{adv['id']}/see", json={"action_seq": 2})
    finally:
        client.app.dependency_overrides.clear()
    assert resp.status_code == 200, resp.text
    prompt = resp.json()["prompt"]
    assert "a dark tower under a blood-red sky" in prompt
    assert "steps into the rain" not in prompt
    assert "open the gate" not in prompt
    assert "ancient dragon" not in prompt.lower()
    sent = json.dumps(capture)
    assert "steps into the rain" in sent
    assert "open the gate" in sent
    assert "ancient dragon" not in sent.lower()


def test_see_keeps_the_face_and_puts_the_beat_before_the_style(client, tmp_path):
    from test_v2 import PNG_BYTES

    adv = make_adventure(plot_essentials="[Name: Caio]", image_style="ink sketch")
    portrait = tmp_path / "alusair.png"
    portrait.write_bytes(PNG_BYTES)
    with db.session() as conn:
        db.set_setting(conn, "image.storage_dir", json.dumps(str(tmp_path / "imgs")))
        image = db.insert_image(
            conn, adventure_id=adv["id"], action_seq=-1,
            file_path=str(portrait), prompt="bust",
            kind="portrait", width=64, height=64,
        )
        alusair = db.insert_card(
            conn, adv["id"], type="character", title="Princess Alusair",
            triggers=["Alusair"], entry="Steel Regent.",
            portrait_prompt="long red hair, green eyes, plate armor",
        )
        db.set_card_portrait(conn, alusair["id"], image["id"], anchor_id=image["id"])
        db.insert_card(
            conn, adv["id"], type="character", title="Caio",
            triggers=["Caio"], entry="The sorcerer.",
            portrait_prompt="short black hair, leather coat",
        )
    add_action(adv["id"], 0, "start", "An ancient dragon sleeps under the mountain.")
    add_action(adv["id"], 1, "do", "\n> You open the gate\n")
    add_action(adv["id"], 2, "continue", "Princess Alusair steps into the rain.")
    capture: list = []
    override_transport(client, make_v2_transport(capture=capture))
    try:
        resp = client.post(f"/api/adventures/{adv['id']}/see", json={"action_seq": 2})
    finally:
        client.app.dependency_overrides.clear()
    assert resp.status_code == 200, resp.text
    prompt = resp.json()["prompt"]
    assert "BREAK" in prompt
    head, tail = prompt.split("BREAK", 1)
    assert "plate armor" in head
    assert "short black hair" in head
    assert "steps into the rain" not in head
    assert "open the gate" not in head
    assert "a dark tower under a blood-red sky" in head
    assert "ink sketch" in tail
    assert "ancient dragon" not in prompt.lower()
    assert head.lower().index("plate armor") < head.lower().index("dark tower")
    assert head.lower().index("short black hair") < head.lower().index("dark tower")
    img2img = [item for item in capture if isinstance(item, dict) and item.get("init_images")]
    assert img2img == []


def test_look_words_from_the_prose_do_not_override_the_card():
    from app.routes.images import contradicting_looks
    canon = "exact likeness, Princess Alusair: long red hair, green eyes, plate armor"
    cleaned, negative = contradicting_looks(
        "Princess Alusair reclines in a white gown, dark hair loose, opening the door.",
        canon,
    )
    assert "gown" not in cleaned.lower()
    assert "dark hair" not in cleaned.lower()
    assert "opening the door" in cleaned
    assert "gown" in negative.lower()
    kept, banned = contradicting_looks("long red hair and plate armor", canon)
    assert "long red hair" in kept
    assert "plate armor" in kept
    assert banned == ""
    skin_canon = "exact likeness, Selene: pale marble skin, long straight black hair"
    kept_skin, banned_skin = contradicting_looks(
        "her pale skin glowing under the moonlight, lifts her dress",
        skin_canon,
    )
    assert "pale skin" in kept_skin.lower()
    assert "pale" not in banned_skin.lower()
    assert "dress" in banned_skin.lower()
    curly, curly_ban = contradicting_looks(
        "A curly-haired woman kisses a shirtless man on the sofa.",
        skin_canon,
    )
    assert "curly" not in curly.lower()
    assert "kiss" in curly.lower()
    assert "curly" in curly_ban.lower()
    untouched, still = contradicting_looks("a white gown", "")
    assert untouched == "a white gown"
    assert still == ""
    undressed, blocked = contradicting_looks(
        "Princess Alusair stands nude in the doorway.", canon)
    assert "nude" in undressed.lower()
    assert "nude" not in blocked.lower()
    _eyes, eyes_ban = contradicting_looks("seus olhos compostos piscam", canon)
    assert "olhos" not in eyes_ban.lower()


def test_portrait_beats_a_conflicting_sheet_and_the_prose():
    card = {
        "type": "character",
        "title": "Princess Alusair",
        "triggers": ["Alusair"],
        "portrait_prompt": "long red hair, green eyes, plate armor",
        "sheet": json.dumps({"appearance": "dark hair", "clothing": "a white gown"}),
    }
    text = "Princess Alusair reclines in a white gown, her dark hair loose."
    clause = npc_assets.image_canon_clause([card], text)
    assert "plate armor" in clause
    assert "white gown" not in clause
    assert "dark hair" not in clause
    narrated = npc_assets.card_canon_text(card)
    assert "plate armor" not in narrated
    assert "Bust portrait" not in narrated
    assert "dark hair" in narrated
    assert "white gown" in narrated


def test_portrait_blocks_a_new_outfit_and_the_dead_stay_dead(tmp_db):
    adv = make_adventure(plot_essentials="[Name: Caio]")
    with db.session() as conn:
        db.insert_card(
            conn, adv["id"], type="character", title="Princess Alusair",
            triggers=["Alusair"], entry="Steel Regent.",
            portrait_prompt="long red hair, green eyes, plate armor",
            sheet=json.dumps({"status": "dead", "stats": {"str": 14}}),
        )
        kept = npc_chronicle.apply_chronicle(conn, adv["id"], {
            "characters": [{
                "name": "Princess Alusair",
                "existing_title": "Princess Alusair",
                "appearance": "dark hair",
                "clothing": "a white gown",
                "status": "alive",
                "facts": ["She is mentioned in the hall."],
                "stat_delta": {"str": -1},
            }],
        }, adv["plot_essentials"])
        card = next(c for c in db.list_cards(conn, adv["id"]) if c["title"] == "Princess Alusair")
        sheet = npc_assets.parse_sheet(card["sheet"])
        assert kept == {"created": 0, "updated": 1, "bodies": 0}
        assert "gown" not in json.dumps(sheet).lower()
        assert sheet["status"] == "dead"
        assert "mentioned in the hall" in sheet["notes"]
        assert sheet["stats"]["str"] == 13

        npc_chronicle.apply_chronicle(conn, adv["id"], {
            "characters": [{
                "name": "Princess Alusair",
                "status": "alive",
                "look_changed": True,
                "appearance": "long red hair, a scar across the cheek",
                "facts": ["Alusair was brought back, scarred but breathing."],
            }],
        }, adv["plot_essentials"])
        card = next(c for c in db.list_cards(conn, adv["id"]) if c["title"] == "Princess Alusair")
        sheet = npc_assets.parse_sheet(card["sheet"])
        assert sheet["status"] == "alive"
        assert "scar" in sheet["appearance"]
        assert "scar" in card["portrait_prompt"]


def test_blank_adventure_grows_place_character_and_plot(tmp_db):
    adv = make_adventure()
    with db.session() as conn:
        result = npc_chronicle.apply_chronicle(conn, adv["id"], {
            "plot_essentials": "[Name: Caio; Place: the royal hall; Tone: tense]",
            "characters": [{
                "name": "Princess Alusair",
                "summary": "Princess of the realm.",
                "facts": ["She receives the visitor in the hall."],
            }],
            "places": [{
                "name": "Royal Hall",
                "aliases": ["the hall"],
                "summary": "The throne room of the palace.",
                "facts": ["The meeting happens here."],
            }],
            "items": [{
                "name": "Sealed Scroll",
                "summary": "A letter for the regent.",
                "facts": ["Caio still carries it."],
            }],
        }, "")
        cards = {card["title"]: card for card in db.list_cards(conn, adv["id"])}
        plot = db.get_adventure(conn, adv["id"])["plot_essentials"]
        kept = npc_chronicle.apply_chronicle(conn, adv["id"], {
            "plot_essentials": "[Name: Someone Else]",
            "places": [{
                "name": "Royal Hall",
                "existing_title": "Royal Hall",
                "facts": ["Banners hang from the beams."],
            }],
        }, plot)
        hall = next(c for c in db.list_cards(conn, adv["id"]) if c["title"] == "Royal Hall")
        plot_after = db.get_adventure(conn, adv["id"])["plot_essentials"]

    assert result["created"] == 3
    assert cards["Princess Alusair"]["type"] == "character"
    assert cards["Royal Hall"]["type"] == "location"
    assert "the hall" in cards["Royal Hall"]["triggers"]
    assert cards["Sealed Scroll"]["type"] == "item"
    assert cards["Sealed Scroll"]["sheet"] == ""
    assert "Caio" in plot
    assert kept == {"created": 0, "updated": 1, "bodies": 0}
    assert "Banners" in hall["entry"]
    assert plot_after == plot


def test_see_paints_the_refusal_not_the_kiss(client, tmp_path):
    from test_v2 import PNG_BYTES

    adv = make_adventure(plot_essentials="[Name: Caio]")
    portrait = tmp_path / "alusair.png"
    portrait.write_bytes(PNG_BYTES)
    with db.session() as conn:
        db.set_setting(conn, "image.storage_dir", json.dumps(str(tmp_path / "imgs")))
        image = db.insert_image(
            conn, adventure_id=adv["id"], action_seq=-1,
            file_path=str(portrait), prompt="bust",
            kind="portrait", width=64, height=64,
        )
        alusair = db.insert_card(
            conn, adv["id"], type="character", title="Princess Alusair",
            triggers=["Alusair", "princesa"], entry="Steel Regent.",
            portrait_prompt="long red hair, green eyes, plate armor",
        )
        db.set_card_portrait(conn, alusair["id"], image["id"], anchor_id=image["id"])
    add_action(adv["id"], 0, "start", "You stand with Princess Alusair in the hall.")
    add_action(adv["id"], 1, "do", "\n> You kiss Princess Alusair\n")
    capture: list = []
    override_transport(
        client,
        make_v2_transport("Caio kisses Princess Alusair in the hall.", capture=capture),
    )
    try:
        resp = client.post(f"/api/adventures/{adv['id']}/see", json={"action_seq": 1})
    finally:
        client.app.dependency_overrides.clear()
    assert resp.status_code == 200, resp.text
    prompt = resp.json()["prompt"]
    head = prompt.split("BREAK", 1)[0]
    assert "refus" in head.lower()
    assert "kiss" not in prompt.lower()
    assert "alusair" in head.lower()
    assert "caio" in head.lower()
    assert head.lower().index("refus") < head.lower().index("long red hair")
    assert not any(isinstance(item, dict) and item.get("init_images") for item in capture)
    with db.session() as conn:
        saved = db.get_image(conn, resp.json()["id"])
    assert "kiss" in saved["negative_prompt"].lower()


def test_see_does_not_paint_an_undress_she_did_not_accept(client, tmp_path):
    adv = make_adventure(plot_essentials="[Name: Caio]")
    with db.session() as conn:
        db.set_setting(conn, "image.storage_dir", json.dumps(str(tmp_path / "imgs")))
        db.insert_card(
            conn, adv["id"], type="character", title="Princess Alusair",
            triggers=["Alusair", "princesa"], entry="Steel Regent.",
            portrait_prompt="long red hair, green eyes, plate armor",
        )
    add_action(adv["id"], 0, "start", "You stand with Princess Alusair in the hall.")
    add_action(adv["id"], 1, "do", "\n> You undress Princess Alusair until she is naked\n")
    capture: list = []
    override_transport(client, make_v2_transport(capture=capture))
    try:
        resp = client.post(f"/api/adventures/{adv['id']}/see", json={"action_seq": 1})
    finally:
        client.app.dependency_overrides.clear()
    assert resp.status_code == 200, resp.text
    prompt = resp.json()["prompt"].lower()
    assert "naked" not in prompt
    assert "clothes off" not in prompt
    assert "refus" in prompt
    assert "plate armor" in prompt
    assert "long red hair" in prompt
    with db.session() as conn:
        saved = db.get_image(conn, resp.json()["id"])
    negative = saved["negative_prompt"].lower()
    assert "naked" in negative or "nude" in negative
    sent = json.dumps(capture)
    assert "Do not censor adult nudity" not in sent


def test_see_locks_the_pale_vampire_before_the_nude_beat(client, tmp_path):
    """O card da vampira pálida de cabelo preto liso vem antes do beijo.

    A frase hostil descreve outra mulher. Ela não pode liderar o prompt,
    nem mandar a pele pálida para o negative.
    """
    adv = make_adventure(plot_essentials="[Name: Caio]")
    with db.session() as conn:
        db.set_setting(conn, "image.storage_dir", json.dumps(str(tmp_path / "imgs")))
        db.insert_card(
            conn, adv["id"], type="character", title="Selene",
            triggers=["Selene", "vampira"], entry="Vampira gótica.",
            portrait_prompt=(
                "Selene is a pale, elegant gothic vampiress with long black hair "
                "and deep, enigmatic eyes, wearing a black velvet dress, velvet choker, "
                "and platform boots, captured in a bust portrait against a neutral background."
            ),
            sheet=json.dumps({
                "appearance": (
                    "Pele pálida como mármore, lábios vermelhos vibrantes, "
                    "cabelos pretos longos e lisos, olhos escuros profundos"
                ),
                "body": "Estrutura fina mas atlética, postura elegante e fluida",
                "clothing": "Vestido de veludo preto, colar de veludo no pescoço, botas de plataforma",
            }),
        )
    add_action(adv["id"], 0, "say", '\n> You say "por que n tira suas roupas amor?"\n')
    add_action(
        adv["id"], 1, "continue",
        "Selene hesita e ri. Ela desliza o vestido preto e fica nua, "
        "lábios vermelhos, e se aproxima do sofá.",
    )
    capture: list = []
    override_transport(client, make_v2_transport(
        capture=capture,
        chat_content=(
            "A curly-haired woman kisses a shirtless man on the sofa, "
            "her pale skin glowing under the moonlight."
        ),
    ))
    try:
        resp = client.post(f"/api/adventures/{adv['id']}/see", json={"action_seq": 1})
    finally:
        client.app.dependency_overrides.clear()
    assert resp.status_code == 200, resp.text
    prompt = resp.json()["prompt"]
    head, tail = prompt.split("BREAK", 1)
    folded = head.lower()
    assert folded.index("black hair") < folded.index("kiss")
    assert folded.index("pale") < folded.index("kiss")
    assert "straight" in folded
    assert "selene" in folded
    assert "curly" not in folded
    assert "velvet" not in folded
    assert "bust" not in folded
    assert "dress" not in folded
    assert "desliza" not in prompt.lower()
    assert "pele pálida" not in prompt.lower()
    assert "vestido" not in head.lower()
    assert "athletic" in folded or "slim" in folded
    assert not any(isinstance(item, dict) and item.get("init_images") for item in capture)
    with db.session() as conn:
        saved = db.get_image(conn, resp.json()["id"])
    negative = saved["negative_prompt"].lower()
    assert "curly" in negative
    assert "pale" not in negative
    assert "different person" in negative
    assert "wrong skin tone" in negative
    assert "wrong hair color" in negative
    assert "extra unnamed person" in negative
    assert "nude" not in negative
    assert "naked" not in negative
    assert tail


def test_identity_bits_use_the_vampire_face_not_the_dress():
    card = {
        "title": "Selene",
        "portrait_prompt": (
            "Selene is a pale, elegant gothic vampiress with long black hair "
            "and deep, enigmatic eyes, wearing a black velvet dress, velvet choker, "
            "and platform boots, captured in a bust portrait against a neutral background."
        ),
        "sheet": json.dumps({
            "appearance": (
                "Pele pálida como mármore, lábios vermelhos vibrantes, "
                "cabelos pretos longos e lisos, olhos escuros profundos"
            ),
            "body": "Estrutura fina mas atlética, postura elegante e fluida",
            "clothing": "Vestido de veludo preto",
        }),
    }
    bits = " ".join(npc_assets.identity_bits(card)).lower()
    assert "pale" in bits and "marble" in bits
    assert "long straight black hair" in bits
    assert "slim athletic build" in bits
    assert "dress" not in bits
    assert "bust" not in bits
    assert "choker" not in bits
    assert "pele" not in bits
    assert "cabelos" not in bits


def test_image_sentence_drops_the_rest_of_the_dump():
    from app.routes.images import clip_words, one_image_sentence

    dumped = (
        "Alex thrusts a combat knife. Um grande centro comercial à meia-noite. "
        "As prateleiras estão vazias."
    )
    assert one_image_sentence(dumped) == "Alex thrusts a combat knife."
    echoed = "> You ataque o zumbi O corredor cheirava à podridão."
    assert not one_image_sentence(echoed).startswith(">")
    assert clip_words("alpha beta gamma", 15) == "alpha beta"


def test_portrait_prompt_is_not_narrator_appearance():
    rendered = npc_assets.card_canon_text({
        "title": "Alex",
        "entry": "Ex-firefighter.",
        "portrait_prompt": "Bust portrait of a rugged 28-year-old ex-firefighter",
    })
    assert "Bust portrait" not in rendered
    assert rendered == "Alex: Ex-firefighter."



def test_bodies_update_player_and_npc_and_snapshot_for_undo(tmp_db):
    adv = make_adventure(plot_essentials="[Name: Caio]")
    with db.session() as conn:
        player = db.insert_card(
            conn, adv["id"], title="Caio", type="character",
            player_username="hero", entry="O protagonista.",
            sheet=json.dumps({"body_state": {"v": 1, "pain": 0, "conditions": []}}),
        )
        elara = db.insert_card(
            conn, adv["id"], title="Elara", type="character",
            entry="Guarda.", sheet=json.dumps({"expression": "wary"}),
        )
        action = db.insert_action(conn, adv["id"], 1, "continue", "Elara ataca.")
        result = npc_chronicle.apply_chronicle(conn, adv["id"], {
            "bodies": [
                {
                    "target": "player", "card_id": player["id"],
                    "pain": 4, "expression": "hurt",
                    "current_outfit": "rasgada",
                    "conditions": [{
                        "op": "add", "kind": "cut", "location": "left cheek",
                        "severity": 3, "description": "corte no rosto",
                        "visual_en": "small cut on the left cheek",
                        "visibility": "portrait",
                    }],
                },
                {
                    "target": "Elara", "card_id": elara["id"],
                    "expression": "furious",
                    "conditions": [{
                        "op": "add", "kind": "bruise", "location": "arm",
                        "visibility": "scene", "description": "hematoma no braço",
                    }],
                },
            ],
        }, adv["plot_essentials"],
            source_action_id=action["id"], source_seq=1)
        assert result["bodies"] == 2
        player_sheet = npc_assets.parse_sheet(db.get_card(conn, adv["id"], player["id"])["sheet"])
        elara_sheet = npc_assets.parse_sheet(db.get_card(conn, adv["id"], elara["id"])["sheet"])
        assert player_sheet["body_state"]["pain"] == 4
        assert player_sheet["expression"] == "hurt"
        assert player_sheet["current_outfit"] == "rasgada"
        cut = player_sheet["body_state"]["conditions"][0]
        assert cut["source_seq"] == 1
        assert cut["portrait_visible"] is True
        assert elara_sheet["expression"] == "furious"
        assert elara_sheet["body_state"]["conditions"][0]["scene_visible"] is True
        before = db.get_action_rules(conn, action["id"])["body_before"]
        assert set(before) == {player["id"], elara["id"]}

        from app import game_engine
        assert game_engine._restore_body_from_action(conn, adv["id"], action) == 2
        player_sheet = npc_assets.parse_sheet(db.get_card(conn, adv["id"], player["id"])["sheet"])
        assert player_sheet["body_state"]["pain"] == 0
        assert player_sheet["body_state"]["conditions"] == []
