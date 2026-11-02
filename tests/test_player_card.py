"""O protagonista sem ficha: nasce do plot essentials e entra na cena."""

from __future__ import annotations

import json

from app import db, jobs, scene

from conftest import make_adventure


PLOT = (
    "[Name: Caio; Role: Vampiro Ancião; Appearance: Homem de aparência "
    "imortal, pele pálida, cabelos escuros e olhos profundos, vestindo um "
    "terno escuro.]"
)


def test_protagonist_from_plot_reads_english_and_portuguese():
    english = jobs.protagonist_from_plot(PLOT)
    assert english["name"] == "Caio"
    assert english["role"] == "Vampiro Ancião"
    assert "pele pálida" in english["appearance"]

    portuguese = jobs.protagonist_from_plot(
        "Protagonista — Nome: Alusair; Papel: barda; Aparência: cabelos ruivos")
    assert portuguese["name"] == "Alusair"
    assert portuguese["role"] == "barda"
    assert portuguese["appearance"] == "cabelos ruivos"

    # O primeiro bloco é o jogador; os seguintes são NPCs.
    first = jobs.protagonist_from_plot(
        "[Name: Caio; Role: Vampiro] [Name: Donzela; Role: NPC]")
    assert first["name"] == "Caio"
    assert jobs.protagonist_from_plot("") == {}
    assert jobs.protagonist_from_plot("[Name: ; Role: nada]") == {}


def test_ensure_player_card_creates_sheet_and_portrait_job(tmp_db):
    adv = make_adventure(owner="Caio", plot_essentials=PLOT)
    with db.session() as conn:
        player = jobs.ensure_player_card(conn, db.get_adventure(conn, adv["id"]))
        cards = db.list_cards(conn, adv["id"])
        portraits = [job for job in db.list_jobs(conn, adv["id"])
                     if job["type"] == "character_portrait"]
        again = jobs.ensure_player_card(conn, db.get_adventure(conn, adv["id"]))

    assert player is not None
    assert player["title"] == "Caio"
    assert player["player_username"] == "Caio"
    assert player["use_for_character_creation"] is True
    sheet = json.loads(player["sheet"])
    assert "vampiro" in sheet["class"].casefold()
    assert "pele pálida" in sheet["appearance"].casefold()
    assert "terno escuro" in sheet["clothing"].casefold()
    assert sheet["stats"]["cha"] >= 10
    assert len([card for card in cards if card["title"] == "Caio"]) == 1
    assert again["id"] == player["id"]
    assert [job["ref_id"] for job in portraits] == [player["id"]]


def test_ensure_player_card_adopts_an_existing_same_name_card(tmp_db):
    adv = make_adventure(owner="Caio", plot_essentials=PLOT)
    with db.session() as conn:
        existing = db.insert_card(
            conn, adv["id"], type="character", title="Caio",
            entry="O vampiro da história.", sheet=json.dumps({
                "appearance": "pale skin, dark hair",
                "notes": "ficha antiga",
            }),
        )
        player = jobs.ensure_player_card(conn, db.get_adventure(conn, adv["id"]))
        cards = db.list_cards(conn, adv["id"])

    assert player["id"] == existing["id"]
    assert player["player_username"] == "Caio"
    assert player["entry"] == "O vampiro da história."
    sheet = json.loads(player["sheet"])
    assert sheet["appearance"] == "pale skin, dark hair"
    assert sheet["notes"] == "ficha antiga"
    assert sheet["class"] == "Vampiro Ancião"
    assert len([card for card in cards if card["title"] == "Caio"]) == 1


def test_second_person_narration_keeps_the_player_in_the_cast():
    player = {
        "id": "p1", "type": "character", "title": "Caio",
        "use_for_character_creation": True,
    }
    npc = {"id": "n1", "type": "character", "title": "Elara", "triggers": ["Elara"]}
    assert scene._cast([player, npc], "You see Elara by the door.") == [player, npc]
    assert scene._cast([player, npc], "Elara waits alone.") == [npc]
    assert "Donzela" in scene._mention_names({
        "title": "A Donzela da Noite", "triggers": [],
    })
