"""Criação jogável a partir de uma versão imutável do catálogo."""

from app import db, world_map
from app.routes import adventures as adventure_routes

from conftest import TEST_AUTH_USER

def test_play_publication_copies_version_links_and_scripts(client, monkeypatch):
    monkeypatch.setattr(adventure_routes, "_enqueue_creation_jobs", lambda _aid: None)
    monkeypatch.setattr(adventure_routes.consistency, "audit", lambda *_args: {})
    with db.session() as conn:
        source = db.insert_adventure(conn, title="Porto", owner=TEST_AUTH_USER)
        db.insert_action(conn, source["id"], 0, "start", "A maré sobe.")
        db.insert_action(conn, source["id"], 1, "do", "Ação privada que não é template.")
        old_port = db.insert_card(conn, source["id"], title="Porto", type="location")
        old_cave = db.insert_card(conn, source["id"], title="Caverna", type="location")
        world_map.ensure_link(conn, source["id"], old_port["id"], old_cave["id"],
                              travel_text="Seguir a trilha")
        db.set_setting(conn, f"script:{source['id']}:input_modifier", "modifier = text")

    published = client.post("/api/publications", json={
        "kind": "adventure", "source_id": source["id"],
    })
    assert published.status_code == 201
    publication = published.json()

    created = client.post("/api/adventures", json={
        "publication_id": publication["id"], "publication_version": 1,
    })
    assert created.status_code == 201, created.text
    adventure = created.json()
    assert adventure["scenario_id"] is None
    assert adventure["publication_id"] == publication["id"]
    assert adventure["publication_version"] == 1

    with db.session() as conn:
        actions = db.list_actions(conn, adventure["id"])
        assert [(row["seq"], row["text"]) for row in actions] == [(0, "A maré sobe.")]
        cards = db.list_cards(conn, adventure["id"])
        assert {row["title"] for row in cards} == {"Porto", "Caverna"}
        assert not ({row["id"] for row in cards} & {old_port["id"], old_cave["id"]})
        ids = {row["title"]: row["id"] for row in cards}
        link = world_map.links_for(conn, adventure["id"], ids["Porto"])[0]
        assert link["to_card_id"] == ids["Caverna"]
        assert link["travel_text"] == "Seguir a trilha"
        assert db.get_setting(conn, f"script:{adventure['id']}:input_modifier") == "modifier = text"
