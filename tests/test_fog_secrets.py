"""Regressão: ficha na cópia, névoa na publicação, mapa e cards."""

from __future__ import annotations

import json

from app import auth as auth_core
from app import db, publications, users, world_map
from app.routes import adventures as adventure_routes

from conftest import TEST_AUTH_SECRET, TEST_AUTH_USER


def test_copy_scenario_cards_keeps_sheet_string_and_dict(tmp_db):
    with db.session() as conn:
        adv = db.insert_adventure(conn, title="Fichas", owner=TEST_AUTH_USER)
        adventure_routes._copy_scenario_cards(conn, adv["id"], [
            {"key": "s", "title": "String", "type": "character", "sheet": '{"mood":"calm"}'},
            {"key": "d", "title": "Dict", "type": "character", "sheet": {"mood": "wild"}},
            {"key": "e", "title": "Empty", "type": "character", "sheet": ""},
            {"key": "b", "title": "Blank", "type": "character", "sheet": "   "},
        ])
        cards = {row["title"]: row for row in db.list_cards(conn, adv["id"])}
    assert cards["String"]["sheet"] == '{"mood":"calm"}'
    assert json.loads(cards["Dict"]["sheet"]) == {"mood": "wild"}
    assert cards["Empty"]["sheet"] == ""
    assert cards["Blank"]["sheet"] == ""


def test_snapshot_omits_hidden_link_and_copy_preserves_fog(tmp_db):
    secret = "SEGREDO-TRAVEL-NAO-PUBLICAR"
    with db.session() as conn:
        source = db.insert_adventure(conn, title="Porto", owner=TEST_AUTH_USER)
        harbor = db.insert_card(
            conn, source["id"], title="Harbor", type="location", entry="salt",
            sheet='{"mood":"calm"}',
        )
        market = db.insert_card(
            conn, source["id"], title="Market", type="location", entry="spice",
        )
        world_map.ensure_link(
            conn, source["id"], harbor["id"], market["id"],
            travel_text="walk the docks", discovered=True,
        )
        world_map.ensure_link(
            conn, source["id"], market["id"], harbor["id"],
            travel_text=secret, discovered=False,
        )
        snap = json.loads(publications.canonical_json(
            publications._snapshot_adventure(conn, db.get_adventure(conn, source["id"])),
        ))

    blob = publications.canonical_json(snap)
    assert secret not in blob
    assert snap["links"] == [{
        "bidirectional": True,
        "discovered": True,
        "from_key": "harbor",
        "to_key": "market",
        "travel_cost": "",
        "travel_text": "walk the docks",
    }]
    harbor_card = next(card for card in snap["story_cards"] if card["title"] == "Harbor")
    assert harbor_card["sheet"] == '{"mood":"calm"}'

    with db.session() as conn:
        copy = db.insert_adventure(conn, title="Copia", owner=TEST_AUTH_USER)
        adventure_routes._copy_publication_assets(conn, copy["id"], snap)
        copied = {row["title"]: row for row in db.list_cards(conn, copy["id"])}
        rows = conn.execute(
            "SELECT travel_text, discovered FROM location_links WHERE adventure_id=?",
            (copy["id"],),
        ).fetchall()
    assert copied["Harbor"]["sheet"] == '{"mood":"calm"}'
    assert [(row["travel_text"], int(row["discovered"])) for row in rows] == [
        ("walk the docks", 1),
    ]

    closed = {
        "story_cards": [
            {"key": "a", "title": "A", "type": "location"},
            {"key": "b", "title": "B", "type": "location"},
        ],
        "links": [
            {"from_key": "a", "to_key": "b", "travel_text": "trilha oculta", "discovered": False},
            {"from_key": "b", "to_key": "a", "travel_text": "sem flag"},
        ],
    }
    with db.session() as conn:
        other = db.insert_adventure(conn, title="Névoa", owner=TEST_AUTH_USER)
        adventure_routes._copy_publication_assets(conn, other["id"], closed)
        flags = {
            row["travel_text"]: int(row["discovered"])
            for row in conn.execute(
                "SELECT travel_text, discovered FROM location_links WHERE adventure_id=?",
                (other["id"],),
            )
        }
    assert flags == {"trilha oculta": 0, "sem flag": 0}


def test_non_owner_misses_unlinked_and_fog_cards_owner_keeps_them(client):
    with db.session() as conn:
        adv = db.insert_adventure(conn, title="Mesa", owner=TEST_AUTH_USER)
        beach = db.insert_card(conn, adv["id"], type="location", title="Praia", entry="areia")
        tower = db.insert_card(
            conn, adv["id"], type="location", title="TORRE-SOLTA", entry="TORRE-ENTRY",
        )
        cave = db.insert_card(
            conn, adv["id"], type="location", title="CAVE-SECRET", entry="CAVE-ENTRY",
        )
        npc = db.insert_card(
            conn, adv["id"], type="character", title="NPC-VISIVEL", entry="fala",
        )
        world_map.set_current_location(conn, adv["id"], beach["id"])
        world_map.ensure_link(
            conn, adv["id"], beach["id"], cave["id"],
            travel_text="TRILHA-SECRETA", discovered=False,
        )
        users.create_user(conn, "mira", "mira-pass")
        db.share_adventure(conn, adv["id"], "mira")
        db.set_dm_mode(conn, adv["id"], True)
        ids = {
            "adv": adv["id"], "beach": beach["id"], "tower": tower["id"],
            "cave": cave["id"], "npc": npc["id"],
        }
        assert world_map.location_visible(conn, ids["adv"], ids["tower"])
        assert world_map.location_visible(conn, ids["adv"], ids["tower"], viewer=TEST_AUTH_USER)
        assert not world_map.location_visible(conn, ids["adv"], ids["tower"], viewer="mira")
        assert world_map.location_visible(conn, ids["adv"], ids["beach"], viewer="mira")
        assert not world_map.location_visible(conn, ids["adv"], ids["cave"], viewer=TEST_AUTH_USER)
        assert not world_map.location_visible(conn, ids["adv"], ids["cave"], viewer="mira")

    owner_map = client.get(f"/api/adventures/{ids['adv']}/map")
    assert owner_map.status_code == 200, owner_map.text
    owner_titles = {loc["title"] for loc in owner_map.json()["locations"]}
    assert owner_titles == {"Praia", "TORRE-SOLTA"}
    assert "CAVE-ENTRY" not in owner_map.text
    assert "TRILHA-SECRETA" not in owner_map.text
    assert ids["cave"] not in owner_map.text
    fog = [
        link for loc in owner_map.json()["locations"] for link in loc["links"]
        if link.get("hidden")
    ]
    assert len(fog) == 1
    assert "to_card_id" not in fog[0]
    assert "entry" not in fog[0]
    assert "title" not in fog[0]

    owner_cards = client.get(f"/api/adventures/{ids['adv']}/cards")
    assert {card["title"] for card in owner_cards.json()} == {
        "Praia", "TORRE-SOLTA", "CAVE-SECRET", "NPC-VISIVEL",
    }
    assert client.get(f"/api/adventures/{ids['adv']}/cards/{ids['cave']}").status_code == 200
    assert client.get(f"/api/adventures/{ids['adv']}/cards/{ids['tower']}").status_code == 200

    client.headers["Authorization"] = (
        f"Bearer {auth_core.make_token('mira', TEST_AUTH_SECRET, ttl_h=1)}"
    )
    mira_map = client.get(f"/api/adventures/{ids['adv']}/map")
    assert mira_map.status_code == 200, mira_map.text
    assert {loc["title"] for loc in mira_map.json()["locations"]} == {"Praia"}
    assert "TORRE-ENTRY" not in mira_map.text
    assert "CAVE-ENTRY" not in mira_map.text
    assert "TRILHA-SECRETA" not in mira_map.text
    assert ids["cave"] not in mira_map.text
    assert ids["tower"] not in mira_map.text

    mira_cards = client.get(f"/api/adventures/{ids['adv']}/cards")
    assert {card["title"] for card in mira_cards.json()} == {"Praia", "NPC-VISIVEL"}
    hidden_tower = client.get(f"/api/adventures/{ids['adv']}/cards/{ids['tower']}")
    hidden_cave = client.get(f"/api/adventures/{ids['adv']}/cards/{ids['cave']}")
    assert hidden_tower.status_code == 404
    assert hidden_cave.status_code == 404
    assert "TORRE-ENTRY" not in hidden_tower.text
    assert "CAVE-ENTRY" not in hidden_cave.text
    visible_npc = client.get(f"/api/adventures/{ids['adv']}/cards/{ids['npc']}")
    assert visible_npc.status_code == 200
    assert visible_npc.json()["title"] == "NPC-VISIVEL"
    visible_beach = client.get(f"/api/adventures/{ids['adv']}/cards/{ids['beach']}")
    assert visible_beach.status_code == 200
