"""Mapa vivo, viagem, journal e geração de locais por IA."""

from __future__ import annotations

import json

from app import db, world_map


def _location(conn, adv_id, title):
    return db.insert_card(conn, adv_id, type="location", title=title, entry=title)


def test_map_links_travel_and_journal(tmp_db):
    with db.session() as conn:
        adv = db.insert_adventure(conn, title="Costazul")
        beach = _location(conn, adv["id"], "Praia")
        square = _location(conn, adv["id"], "Praça")
        world_map.ensure_link(conn, adv["id"], beach["id"], square["id"])
        world_map.set_current_location(conn, adv["id"], beach["id"])
        result = world_map.travel(conn, adv["id"], square["id"])
        assert result["travelled"] is True
        assert result["location"]["title"] == "Praça"
        assert result["from"]["title"] == "Praia"
        assert world_map.current_location(conn, adv["id"])["id"] == square["id"]
        entries = world_map.journal_for(conn, adv["id"])
        assert any(entry["kind"] == "travel" for entry in entries)
        payload = world_map.map_payload(conn, adv["id"])
        assert payload["current"]["id"] == square["id"]
        assert len(payload["locations"]) == 2


def test_travel_rejects_unconnected_location(tmp_db):
    with db.session() as conn:
        adv = db.insert_adventure(conn, title="Costazul")
        beach = _location(conn, adv["id"], "Praia")
        mountain = _location(conn, adv["id"], "Montanha")
        world_map.set_current_location(conn, adv["id"], beach["id"])
        try:
            world_map.travel(conn, adv["id"], mountain["id"])
        except ValueError as exc:
            assert str(exc) == "location_not_connected"
        else:
            raise AssertionError("viagem sem conexão deveria falhar")


def test_location_image_is_linked_to_card(tmp_db):
    with db.session() as conn:
        adv = db.insert_adventure(conn, title="Costazul")
        card = _location(conn, adv["id"], "Praça")
        image = db.insert_image(
            conn, adventure_id=adv["id"], action_seq=-1,
            file_path="data/images/test.png", prompt="square",
            kind="location", ref_id=card["id"],
        )
        world_map.attach_location_image(conn, adv["id"], card["id"], image["id"], state_key="abc")
        latest = world_map.latest_location_image(conn, adv["id"], card["id"])
        assert latest["id"] == image["id"]
        assert latest["state_key"] == "abc"
        assert latest["url"].endswith(image["id"])


def test_generate_location_uses_reference_and_web_sources(tmp_db, monkeypatch):
    with db.session() as conn:
        adv = db.insert_adventure(conn, title="Costazul")
        current = _location(conn, adv["id"], "Praia")
        world_map.set_current_location(conn, adv["id"], current["id"])

    monkeypatch.setattr(
        "app.assistant.generate_single_card",
        lambda *args, **kwargs: {
            "card": {
                "title": "Rio das Ostras",
                "type": "location",
                "triggers": ["Rio das Ostras"],
                "entry": "Cidade litorânea com praias e praças.",
            },
            "web_sources": [{"title": "Wikipedia", "url": "https://example.com"}],
        },
    )
    monkeypatch.setattr(
        "app.web_search.research_context",
        lambda *args, **kwargs: {
            "context_block": "[Web Research] Rio das Ostras facts",
            "sources": [{"title": "Wikipedia", "url": "https://example.com"}],
        },
    )
    with db.session() as conn:
        result = world_map.generate_location(
            conn, adv["id"], name="Rio das Ostras",
            reference="cidade real no Rio de Janeiro",
            llm_cfg={}, web_search=True,
        )
        card = result["card"]
        assert card["title"] == "Rio das Ostras"
        assert result["web_sources"]
        links = world_map.links_for(conn, adv["id"], current["id"])
        assert any(link["to_card_id"] == card["id"] for link in links)
        entries = world_map.journal_for(conn, adv["id"])
        assert any(entry["kind"] == "discovery" for entry in entries)
