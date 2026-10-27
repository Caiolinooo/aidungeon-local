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
        assert all(int(link["discovered"]) == 1 for link in links)
        entries = world_map.journal_for(conn, adv["id"])
        assert any(entry["kind"] == "discovery" for entry in entries)
        payload = world_map.map_payload(conn, adv["id"])
        assert any(loc["id"] == card["id"] for loc in payload["locations"])


SECRET_TITLE = "Lugar Oculto Zyx"
SECRET_ENTRY = "SEGREDO-ENTRADA-NAO-VAZAR"
SECRET_JOURNAL = "SEGREDO-JOURNAL-NAO-VAZAR"
SECRET_IMAGE = "SEGREDO-IMAGEM-NAO-VAZAR"
SECRET_TRAVEL = "SEGREDO-TRAVEL-NAO-VAZAR"
SECRET_COORD = "SEGREDO-COORD-99"
PEAK_TITLE = "Pico Sem Nome Xy"
PEAK_ENTRY = "SEGREDO-PICO-NAO-VAZAR"
PEAK_TRAVEL = "trilha-interna-nao-vazar"
CONCEALED = (
    SECRET_TITLE, SECRET_ENTRY, SECRET_JOURNAL, SECRET_IMAGE, SECRET_TRAVEL,
    SECRET_COORD, PEAK_TITLE, PEAK_ENTRY, PEAK_TRAVEL,
)


def _blob(payload) -> str:
    return json.dumps(payload, ensure_ascii=False)


def _loc_ids(payload) -> set[str]:
    return {loc["id"] for loc in payload["locations"]}


def _assert_absent(payload, *needles: str) -> None:
    blob = _blob(payload)
    for needle in needles:
        assert needle not in blob, needle


def _assert_fog_link(link: dict) -> None:
    assert link.get("hidden") is True
    assert link.get("discovered") == 0
    assert "to_card_id" not in link
    for banned in (
        "to_title", "to_entry", "title", "entry", "travel_text",
        "coordinates", "image", "hierarchy",
    ):
        assert banned not in link


def _seed_fog(conn) -> dict:
    adv = db.insert_adventure(conn, title="Neblina")
    beach = db.insert_card(conn, adv["id"], type="location", title="Praia", entry="Areia quente.")
    cave = db.insert_card(
        conn, adv["id"], type="location", title=SECRET_TITLE, entry=SECRET_ENTRY,
        sheet=json.dumps({"coordinates": {"lat": 418273, "note": SECRET_COORD}}),
    )
    peak = db.insert_card(
        conn, adv["id"], type="location", title=PEAK_TITLE, entry=PEAK_ENTRY,
    )
    world_map.set_current_location(conn, adv["id"], beach["id"])
    world_map.ensure_link(
        conn, adv["id"], beach["id"], cave["id"],
        travel_text=SECRET_TRAVEL, discovered=False,
    )
    world_map.ensure_link(
        conn, adv["id"], cave["id"], peak["id"],
        travel_text=PEAK_TRAVEL, discovered=False,
    )
    world_map.add_journal(
        conn, adv["id"], kind="discovery", title=SECRET_TITLE, text=SECRET_JOURNAL,
        entity_type="location", entity_id=cave["id"],
    )
    world_map.add_journal(
        conn, adv["id"], kind="note", title="Nota da praia", text="mar calmo",
        entity_type="location", entity_id=beach["id"],
        related=[{"type": "location", "id": cave["id"], "title": SECRET_TITLE}],
    )
    image = db.insert_image(
        conn, adventure_id=adv["id"], action_seq=-1,
        file_path="data/images/cave.png", prompt=SECRET_IMAGE,
        kind="location", ref_id=cave["id"],
    )
    world_map.attach_location_image(
        conn, adv["id"], cave["id"], image["id"], state_key="fog",
    )
    return {
        "adv": adv["id"],
        "beach": beach["id"],
        "cave": cave["id"],
        "peak": peak["id"],
        "image": image["id"],
    }


def test_map_payload_omits_undiscovered_location_journal_and_image(tmp_db):
    with db.session() as conn:
        ids = _seed_fog(conn)
        link = conn.execute(
            "SELECT discovered FROM location_links WHERE from_card_id=? AND to_card_id=?",
            (ids["beach"], ids["cave"]),
        ).fetchone()
        assert int(link["discovered"]) == 0
        payload = world_map.map_payload(conn, ids["adv"])
        _assert_absent(payload, *CONCEALED, ids["image"])
        assert _loc_ids(payload) == {ids["beach"]}
        assert payload["current"]["id"] == ids["beach"]
        assert "Areia quente." in _blob(payload)
        assert "mar calmo" in _blob(payload)
        fog = [link for loc in payload["locations"] for link in loc["links"] if link.get("hidden")]
        assert len(fog) == 1
        assert ids["cave"] not in json.dumps(fog)
        _assert_fog_link(fog[0])
        assert world_map.location_visible(conn, ids["adv"], ids["beach"])
        assert not world_map.location_visible(conn, ids["adv"], ids["cave"])
        assert not world_map.location_visible(conn, ids["adv"], ids["peak"])
    with db.session() as conn:
        payload = world_map.map_payload(conn, ids["adv"])
        _assert_absent(payload, *CONCEALED, ids["image"])
        assert _loc_ids(payload) == {ids["beach"]}


def test_reveal_location_updates_payload_and_persists(tmp_db):
    with db.session() as conn:
        ids = _seed_fog(conn)
        try:
            world_map.reveal_location(conn, ids["adv"], ids["peak"])
        except ValueError as exc:
            assert str(exc) == "location_not_connected"
        else:
            raise AssertionError("revelar o local além da névoa deveria falhar")
        _assert_absent(world_map.map_payload(conn, ids["adv"]), *CONCEALED)
        revealed = world_map.reveal_location(conn, ids["adv"], ids["cave"])
        assert revealed["id"] == ids["cave"]
        payload = world_map.map_payload(conn, ids["adv"])
        assert payload["current"]["id"] == ids["beach"]
        assert ids["cave"] in _loc_ids(payload)
        assert ids["peak"] not in _loc_ids(payload)
        cave = next(loc for loc in payload["locations"] if loc["id"] == ids["cave"])
        assert cave["entry"] == SECRET_ENTRY
        assert cave["image"]["id"] == ids["image"]
        assert cave["image"]["prompt"] == SECRET_IMAGE
        assert cave["hierarchy"]["coordinates"]["lat"] == 418273
        assert any(entry["text"] == SECRET_JOURNAL for entry in payload["journal"])
        _assert_absent(payload, PEAK_TITLE, PEAK_ENTRY, PEAK_TRAVEL)
        fog = [link for link in cave["links"] if link.get("hidden")]
        assert len(fog) == 1
        assert ids["peak"] not in json.dumps(fog)
        _assert_fog_link(fog[0])
        again = world_map.reveal_location(conn, ids["adv"], ids["cave"])
        assert again["id"] == ids["cave"]
    with db.session() as conn:
        payload = world_map.map_payload(conn, ids["adv"])
        assert ids["cave"] in _loc_ids(payload)
        assert ids["peak"] not in _loc_ids(payload)
        assert payload["current"]["id"] == ids["beach"]
        row = conn.execute(
            "SELECT discovered FROM location_links WHERE from_card_id=? AND to_card_id=?",
            (ids["beach"], ids["cave"]),
        ).fetchone()
        assert int(row["discovered"]) == 1
        far = conn.execute(
            "SELECT discovered FROM location_links WHERE from_card_id=? AND to_card_id=?",
            (ids["cave"], ids["peak"]),
        ).fetchone()
        assert int(far["discovered"]) == 0


def test_travel_reveals_undiscovered_exit_and_keeps_far_fog(tmp_db):
    with db.session() as conn:
        ids = _seed_fog(conn)
        try:
            world_map.travel(conn, ids["adv"], ids["peak"])
        except ValueError as exc:
            assert str(exc) == "location_not_connected"
        else:
            raise AssertionError("viajar duas saídas adiante deveria falhar")
        _assert_absent(world_map.map_payload(conn, ids["adv"]), *CONCEALED)
        result = world_map.travel(conn, ids["adv"], ids["cave"])
        assert result["travelled"] is True
        assert result["location"]["id"] == ids["cave"]
        assert result["from"]["id"] == ids["beach"]
        _assert_absent(result, PEAK_TITLE, PEAK_ENTRY, PEAK_TRAVEL)
        fog = [link for link in result["links"] if link.get("hidden")]
        assert len(fog) == 1
        assert ids["peak"] not in json.dumps(fog)
        _assert_fog_link(fog[0])
        payload = world_map.map_payload(conn, ids["adv"])
        assert payload["current"]["id"] == ids["cave"]
        assert {ids["beach"], ids["cave"]} <= _loc_ids(payload)
        assert ids["peak"] not in _loc_ids(payload)
        assert SECRET_ENTRY in _blob(payload)
        _assert_absent(payload, PEAK_TITLE, PEAK_ENTRY, PEAK_TRAVEL)
        back = world_map.travel(conn, ids["adv"], ids["beach"])
        assert back["travelled"] is True
        payload = world_map.map_payload(conn, ids["adv"])
        assert payload["current"]["id"] == ids["beach"]
        assert ids["cave"] in _loc_ids(payload)
        assert ids["peak"] not in _loc_ids(payload)
    with db.session() as conn:
        payload = world_map.map_payload(conn, ids["adv"])
        assert payload["current"]["id"] == ids["beach"]
        assert ids["cave"] in _loc_ids(payload)
        assert ids["peak"] not in _loc_ids(payload)
        _assert_absent(payload, PEAK_TITLE, PEAK_ENTRY, PEAK_TRAVEL)


def test_existing_world_stays_visible_when_fog_link_is_added(tmp_db):
    with db.session() as conn:
        adv = db.insert_adventure(conn, title="Costazul")
        beach = _location(conn, adv["id"], "Praia")
        square = _location(conn, adv["id"], "Praça")
        port = db.insert_card(
            conn, adv["id"], type="location", title="Porto", entry="Cais antigo.",
            sheet=json.dumps({"coordinates": {"lat": 22001, "lng": 43}}),
        )
        world_map.ensure_link(conn, adv["id"], beach["id"], square["id"])
        world_map.set_current_location(conn, adv["id"], beach["id"])
        image = db.insert_image(
            conn, adventure_id=adv["id"], action_seq=-1,
            file_path="data/images/square.png", prompt="praca-visivel",
            kind="location", ref_id=square["id"],
        )
        world_map.attach_location_image(
            conn, adv["id"], square["id"], image["id"], state_key="known",
        )
        world_map.add_journal(
            conn, adv["id"], kind="travel", title="Na praça", text="chegou na praça",
            entity_type="location", entity_id=square["id"],
        )
        payload = world_map.map_payload(conn, adv["id"])
        assert {loc["title"] for loc in payload["locations"]} == {"Praia", "Praça", "Porto"}
        square_node = next(loc for loc in payload["locations"] if loc["id"] == square["id"])
        assert square_node["image"]["id"] == image["id"]
        assert square_node["entry"] == "Praça"
        port_node = next(loc for loc in payload["locations"] if loc["id"] == port["id"])
        assert port_node["hierarchy"]["coordinates"]["lat"] == 22001
        assert any(entry["text"] == "chegou na praça" for entry in payload["journal"])
        world_map.ensure_link(
            conn, adv["id"], beach["id"], square["id"], discovered=False,
        )
        kept = conn.execute(
            "SELECT discovered FROM location_links WHERE from_card_id=? AND to_card_id=?",
            (beach["id"], square["id"]),
        ).fetchone()
        assert int(kept["discovered"]) == 1
        secret = db.insert_card(
            conn, adv["id"], type="location", title=SECRET_TITLE, entry=SECRET_ENTRY,
            sheet=json.dumps({"coordinates": {"lat": 418273, "note": SECRET_COORD}}),
        )
        world_map.ensure_link(
            conn, adv["id"], port["id"], secret["id"],
            travel_text=SECRET_TRAVEL, discovered=False,
        )
        payload = world_map.map_payload(conn, adv["id"])
        assert {loc["title"] for loc in payload["locations"]} == {"Praia", "Praça", "Porto"}
        _assert_absent(payload, SECRET_TITLE, SECRET_ENTRY, SECRET_TRAVEL, SECRET_COORD)
        assert "praca-visivel" in _blob(payload)
        assert "chegou na praça" in _blob(payload)
        port_node = next(loc for loc in payload["locations"] if loc["id"] == port["id"])
        assert port_node["hierarchy"]["coordinates"]["lat"] == 22001
        fog = [link for link in port_node["links"] if link.get("hidden")]
        assert len(fog) == 1
        assert secret["id"] not in json.dumps(fog)
        _assert_fog_link(fog[0])


def test_map_routes_omit_fog_until_reveal_and_travel(client):
    with db.session() as conn:
        ids = _seed_fog(conn)
    hidden = client.get(f"/api/adventures/{ids['adv']}/map")
    assert hidden.status_code == 200
    for needle in (*CONCEALED, ids["image"], ids["cave"], ids["peak"]):
        assert needle not in hidden.text
    assert _loc_ids(hidden.json()) == {ids["beach"]}
    journal = client.get(f"/api/adventures/{ids['adv']}/journal")
    assert journal.status_code == 200
    assert SECRET_JOURNAL not in journal.text
    assert SECRET_TITLE not in journal.text
    assert "mar calmo" in journal.text
    denied = client.post(
        f"/api/adventures/{ids['adv']}/map/locations/{ids['cave']}/image",
        json={"reference": SECRET_ENTRY},
    )
    assert denied.status_code == 404
    assert SECRET_ENTRY not in denied.text
    assert SECRET_IMAGE not in denied.text
    revealed = client.post(f"/api/adventures/{ids['adv']}/map/reveal/{ids['cave']}")
    assert revealed.status_code == 200
    assert ids["cave"] in _loc_ids(revealed.json())
    assert ids["peak"] not in _loc_ids(revealed.json())
    assert SECRET_ENTRY in revealed.text
    assert PEAK_TITLE not in revealed.text
    reloaded = client.get(f"/api/adventures/{ids['adv']}/map")
    assert ids["cave"] in _loc_ids(reloaded.json())
    assert PEAK_ENTRY not in reloaded.text
    travelled = client.post(f"/api/adventures/{ids['adv']}/map/travel/{ids['cave']}")
    assert travelled.status_code == 200
    assert travelled.json()["travelled"] is True
    assert PEAK_TITLE not in travelled.text
    onward = client.post(f"/api/adventures/{ids['adv']}/map/travel/{ids['peak']}")
    assert onward.status_code == 200
    final = client.get(f"/api/adventures/{ids['adv']}/map")
    final_ids = _loc_ids(final.json())
    assert {ids["beach"], ids["cave"], ids["peak"]} <= final_ids
    assert final.json()["current"]["id"] == ids["peak"]
    assert PEAK_ENTRY in final.text
