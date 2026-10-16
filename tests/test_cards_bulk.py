"""Testes para Story Cards: filtros de busca, operações em lote (bulk) e DEFECT-CARDS-LLM-LEAK."""

from __future__ import annotations

import json
import httpx
import pytest

from app import db, deps
from conftest import make_adventure


def make_card(adventure_id: str, *, title: str = "Card", card_type: str = "other",
              triggers: list[str] | None = None, entry: str = "Entry",
              pinned: bool = False) -> dict:
    with db.session() as conn:
        return db.insert_card(
            conn,
            adventure_id,
            title=title,
            type=card_type,
            triggers=triggers or [],
            entry=entry,
            pinned=pinned,
        )


# =============================================================================
# 1. Query parameter filtering: q, type, pinned
# =============================================================================

def test_list_cards_filter_by_type(client):
    adv = make_adventure(title="Filter Adventure")
    c1 = make_card(adv["id"], title="Lord Malakor", card_type="character")
    c2 = make_card(adv["id"], title="Dark Citadel", card_type="location")
    c3 = make_card(adv["id"], title="Vorpal Blade", card_type="item")

    resp = client.get(f"/api/adventures/{adv['id']}/cards?type=character")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 1
    assert data[0]["id"] == c1["id"]

    resp = client.get(f"/api/adventures/{adv['id']}/cards?type=location")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 1
    assert data[0]["id"] == c2["id"]


def test_list_cards_filter_by_pinned(client):
    adv = make_adventure(title="Pinned Adventure")
    c_pinned = make_card(adv["id"], title="Pinned Lore", pinned=True)
    c_unpinned = make_card(adv["id"], title="Normal Lore", pinned=False)

    resp_pinned = client.get(f"/api/adventures/{adv['id']}/cards?pinned=true")
    assert resp_pinned.status_code == 200
    pinned_cards = resp_pinned.json()
    assert len(pinned_cards) == 1
    assert pinned_cards[0]["id"] == c_pinned["id"]

    resp_unpinned = client.get(f"/api/adventures/{adv['id']}/cards?pinned=false")
    assert resp_unpinned.status_code == 200
    unpinned_cards = resp_unpinned.json()
    assert len(unpinned_cards) == 1
    assert unpinned_cards[0]["id"] == c_unpinned["id"]


def test_list_cards_filter_by_q_title_triggers_entry(client):
    adv = make_adventure(title="Search Adventure")
    c1 = make_card(adv["id"], title="Archmage Ignis", triggers=["fire", "pyro"], entry="A master of flames.")
    c2 = make_card(adv["id"], title="Frost Queen", triggers=["ice", "blizzard"], entry="Ruler of the frozen wastes.")
    c3 = make_card(adv["id"], title="Ancient Library", triggers=["books", "tomes"], entry="Contains the secret flame archives.")

    # Match in title (case-insensitive)
    resp = client.get(f"/api/adventures/{adv['id']}/cards?q=ignis")
    assert resp.status_code == 200
    titles = [c["title"] for c in resp.json()]
    assert titles == ["Archmage Ignis"]

    # Match in triggers
    resp = client.get(f"/api/adventures/{adv['id']}/cards?q=blizzard")
    assert resp.status_code == 200
    titles = [c["title"] for c in resp.json()]
    assert titles == ["Frost Queen"]

    # Match in entry (flame matches Ignis and Library)
    resp = client.get(f"/api/adventures/{adv['id']}/cards?q=flame")
    assert resp.status_code == 200
    titles = {c["title"] for c in resp.json()}
    assert titles == {"Archmage Ignis", "Ancient Library"}

    # No match
    resp = client.get(f"/api/adventures/{adv['id']}/cards?q=nonexistentxyz")
    assert resp.status_code == 200
    assert resp.json() == []


def test_list_cards_combined_filters(client):
    adv = make_adventure(title="Combo Adventure")
    c1 = make_card(adv["id"], title="Red Dragon", card_type="character", entry="Fierce beast", pinned=True)
    c2 = make_card(adv["id"], title="Blue Dragon", card_type="character", entry="Ice drake", pinned=False)
    c3 = make_card(adv["id"], title="Dragon Cave", card_type="location", entry="Home of dragons", pinned=True)

    resp = client.get(f"/api/adventures/{adv['id']}/cards?q=dragon&type=character&pinned=true")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 1
    assert data[0]["id"] == c1["id"]


# =============================================================================
# 2. Bulk operations: delete, pin, unpin, retype
# =============================================================================

def test_bulk_pin_and_unpin(client):
    adv = make_adventure()
    c1 = make_card(adv["id"], title="C1", pinned=False)
    c2 = make_card(adv["id"], title="C2", pinned=False)
    c3 = make_card(adv["id"], title="C3", pinned=False)

    # Pin c1 and c2
    resp = client.post(
        f"/api/adventures/{adv['id']}/cards/bulk",
        json={"action": "pin", "card_ids": [c1["id"], c2["id"]]},
    )
    assert resp.status_code == 200
    assert resp.json() == {"updated": 2, "action": "pin"}

    with db.session() as conn:
        assert db.get_card(conn, adv["id"], c1["id"])["pinned"] is True
        assert db.get_card(conn, adv["id"], c2["id"])["pinned"] is True
        assert db.get_card(conn, adv["id"], c3["id"])["pinned"] is False

    # Unpin c1
    resp = client.post(
        f"/api/adventures/{adv['id']}/cards/bulk",
        json={"action": "unpin", "card_ids": [c1["id"]]},
    )
    assert resp.status_code == 200
    assert resp.json() == {"updated": 1, "action": "unpin"}

    with db.session() as conn:
        assert db.get_card(conn, adv["id"], c1["id"])["pinned"] is False
        assert db.get_card(conn, adv["id"], c2["id"])["pinned"] is True


def test_bulk_retype(client):
    adv = make_adventure()
    c1 = make_card(adv["id"], title="C1", card_type="other")
    c2 = make_card(adv["id"], title="C2", card_type="other")

    resp = client.post(
        f"/api/adventures/{adv['id']}/cards/bulk",
        json={"action": "retype", "card_ids": [c1["id"], c2["id"]], "new_type": "faction"},
    )
    assert resp.status_code == 200
    assert resp.json() == {"updated": 2, "action": "retype"}

    with db.session() as conn:
        assert db.get_card(conn, adv["id"], c1["id"])["type"] == "faction"
        assert db.get_card(conn, adv["id"], c2["id"])["type"] == "faction"


def test_bulk_retype_missing_new_type_400(client):
    adv = make_adventure()
    c1 = make_card(adv["id"], title="C1")

    # Missing new_type
    resp = client.post(
        f"/api/adventures/{adv['id']}/cards/bulk",
        json={"action": "retype", "card_ids": [c1["id"]]},
    )
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "invalid_payload"

    # Blank new_type
    resp = client.post(
        f"/api/adventures/{adv['id']}/cards/bulk",
        json={"action": "retype", "card_ids": [c1["id"]], "new_type": "   "},
    )
    assert resp.status_code == 400


def test_bulk_delete(client):
    adv = make_adventure()
    c1 = make_card(adv["id"], title="C1")
    c2 = make_card(adv["id"], title="C2")
    c3 = make_card(adv["id"], title="C3")

    resp = client.post(
        f"/api/adventures/{adv['id']}/cards/bulk",
        json={"action": "delete", "card_ids": [c1["id"], c3["id"]]},
    )
    assert resp.status_code == 200
    assert resp.json() == {"updated": 2, "action": "delete"}

    with db.session() as conn:
        assert db.get_card(conn, adv["id"], c1["id"]) is None
        assert db.get_card(conn, adv["id"], c2["id"]) is not None
        assert db.get_card(conn, adv["id"], c3["id"]) is None


def test_bulk_validation_errors(client):
    adv = make_adventure()
    # Empty card_ids
    resp = client.post(
        f"/api/adventures/{adv['id']}/cards/bulk",
        json={"action": "pin", "card_ids": []},
    )
    assert resp.status_code == 422

    # Invalid action
    resp = client.post(
        f"/api/adventures/{adv['id']}/cards/bulk",
        json={"action": "explode", "card_ids": ["c1"]},
    )
    assert resp.status_code == 422


# =============================================================================
# 3. Cross-adventure isolation in bulk ops
# =============================================================================

def test_bulk_cross_adventure_isolation(client):
    adv1 = make_adventure(title="Adv 1")
    adv2 = make_adventure(title="Adv 2")

    c_adv1 = make_card(adv1["id"], title="Adv1 Card", card_type="item", pinned=False)
    c_adv2 = make_card(adv2["id"], title="Adv2 Card", card_type="item", pinned=False)

    # Calling bulk delete on adv1 with c_adv2's id should NOT affect adv2
    resp = client.post(
        f"/api/adventures/{adv1['id']}/cards/bulk",
        json={"action": "delete", "card_ids": [c_adv1["id"], c_adv2["id"]]},
    )
    assert resp.status_code == 200
    assert resp.json() == {"updated": 1, "action": "delete"}

    with db.session() as conn:
        assert db.get_card(conn, adv1["id"], c_adv1["id"]) is None
        assert db.get_card(conn, adv2["id"], c_adv2["id"]) is not None

    # Calling bulk pin on adv1 with c_adv2 should NOT pin card in adv2
    resp = client.post(
        f"/api/adventures/{adv1['id']}/cards/bulk",
        json={"action": "pin", "card_ids": [c_adv2["id"]]},
    )
    assert resp.status_code == 200
    assert resp.json() == {"updated": 0, "action": "pin"}

    with db.session() as conn:
        assert db.get_card(conn, adv2["id"], c_adv2["id"])["pinned"] is False


def test_bulk_nonexistent_adventure_404(client):
    resp = client.post(
        "/api/adventures/nonexistent-id/cards/bulk",
        json={"action": "delete", "card_ids": ["c1"]},
    )
    assert resp.status_code == 404


# =============================================================================
# 4. DEFECT-CARDS-LLM-LEAK fix verification
# =============================================================================

def test_enrich_card_llm_unreachable_503(client):
    """Enrich card when LLM connection fails returns HTTP 503 llm_unreachable."""
    adv = make_adventure()
    c = make_card(adv["id"], title="NPC", entry="An NPC awaiting enrichment.")

    def fail_transport(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("Connection refused by mock LLM")

    transport = httpx.MockTransport(fail_transport)
    client.app.dependency_overrides[deps.get_http_transport] = lambda: transport
    try:
        resp = client.post(f"/api/adventures/{adv['id']}/cards/{c['id']}/enrich")
        assert resp.status_code == 503
        err = resp.json()["error"]
        assert err["code"] == "llm_unreachable"
    finally:
        client.app.dependency_overrides.pop(deps.get_http_transport, None)
