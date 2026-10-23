"""Testes v0.5.0 — ficha estruturada do NPC (sheet): persistência, enrich via
LLM e prompt de retrato enriquecido."""

from __future__ import annotations

import json

import httpx

from app import db, deps, jobs
from conftest import make_adventure
from test_v3 import make_assets_transport, make_npc, use_tmp_storage

SHEET = {
    "appearance": "Silver hair, violet eyes, scar over the left brow",
    "body": "Tall, wiry build",
    "clothing": "Sky-captain coat",
    "personality": "Sardonic, loyal",
    "stats": {"str": 11, "dex": 16, "con": 12, "int": 14, "wis": 13, "cha": 15},
    "notes": "Fears storms",
}

ENRICH_JSON = json.dumps({
    "appearance": "Ash-blond braid, storm-grey eyes",
    "body": "Compact, athletic build",
    "clothing": "Weathered flight jacket",
    "personality": "Dry humor, fiercely protective",
    "stats": {"str": 12, "dex": 99, "con": 11, "int": 13, "wis": 14, "cha": -5},
    "notes": "Collects compass needles",
    "unknown_field": "must be dropped",
    "portrait_prompt": "ash-blond sky captain, bust portrait, storm light",
})


def make_chat_transport(content: str, capture: list | None = None) -> httpx.MockTransport:
    """Somente o LLM (/models + /chat/completions em JSON, não-SSE)."""
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/models"):
            return httpx.Response(200, json={
                "object": "list",
                "data": [{"id": "mock-model", "object": "model"}],
            })
        if path.endswith("/chat/completions"):
            if capture is not None:
                capture.append(json.loads(request.content.decode("utf-8")))
            return httpx.Response(200, json={
                "choices": [{"message": {"content": content}}],
            })
        return httpx.Response(404, json={"error": f"no mock route: {path}"})

    return httpx.MockTransport(handler)


def override_transport(client, transport) -> None:
    client.app.dependency_overrides[deps.get_http_transport] = lambda: transport


def clear_override(client) -> None:
    client.app.dependency_overrides.pop(deps.get_http_transport, None)


# ---------------------------------------------------------- persistência
def test_sheet_roundtrip_via_api(client):
    adv = make_adventure()
    resp = client.post(f"/api/adventures/{adv['id']}/cards", json={
        "title": "Ada Vane", "type": "character", "entry": "Sky captain.",
        "sheet": SHEET,
    })
    assert resp.status_code == 201, resp.text
    card = resp.json()
    assert json.loads(card["sheet"]) == SHEET

    new_sheet = {**SHEET, "notes": "Fears storms and paperwork"}
    resp = client.patch(f"/api/adventures/{adv['id']}/cards/{card['id']}",
                        json={"sheet": new_sheet})
    assert resp.status_code == 200, resp.text
    updated = json.loads(resp.json()["sheet"])
    assert updated.items() >= new_sheet.items()
    assert updated["portrait_state_key"]

    listed = client.get(f"/api/adventures/{adv['id']}/cards").json()
    assert json.loads(listed[0]["sheet"])["notes"].endswith("paperwork")

    # PATCH sem sheet preserva a ficha existente
    resp = client.patch(f"/api/adventures/{adv['id']}/cards/{card['id']}",
                        json={"entry": "Sky captain of the Kestrel."})
    assert json.loads(resp.json()["sheet"]).items() >= new_sheet.items()


def test_adventure_from_scenario_preserves_sheet(client):
    """A ficha do scenario sobrevive à cópia e nasce com o que é determinístico."""
    scenario = client.post("/api/scenarios", json={
        "title": "Draft", "opening_text": "You wake aboard the Kestrel.",
        "story_cards": [
            {"title": "Ada Vane", "type": "character", "entry": "Captain.",
             "sheet": SHEET},
        ],
    }).json()
    adv = client.post("/api/adventures",
                      json={"scenario_id": scenario["id"]}).json()
    cards = client.get(f"/api/adventures/{adv['id']}/cards").json()
    sheet = json.loads(cards[0]["sheet"])
    for key, value in SHEET.items():
        assert sheet[key] == value, key
    # status e classe são preenchidos na criação (C9): campo preenchível
    # nasce preenchido ou estimado.
    assert sheet["status"] == "alive"
    assert sheet["class"] == "Captain"


# ----------------------------------------------------------------- enrich
def test_enrich_fills_sheet_and_portrait_prompt(client):
    adv = make_adventure(title="Skyward", description="Airships over the storm sea.")
    npc = make_npc(adv["id"], title="Ada Vane", entry="Sky captain.")
    capture: list = []
    override_transport(client, make_chat_transport(ENRICH_JSON, capture))
    try:
        resp = client.post(f"/api/adventures/{adv['id']}/cards/{npc['id']}/enrich")
    finally:
        clear_override(client)
    assert resp.status_code == 200, resp.text
    card = resp.json()
    sheet = json.loads(card["sheet"])
    assert sheet["appearance"] == "Ash-blond braid, storm-grey eyes"
    assert sheet["stats"]["dex"] == 20      # clamp: 99 → 20
    assert sheet["stats"]["cha"] == 1       # clamp: -5 → 1
    assert "unknown_field" not in sheet     # campos fora do contrato caem fora
    assert card["portrait_prompt"] == "ash-blond sky captain, bust portrait, storm light"
    # LLM recebeu o contexto da aventura + descrição do NPC
    user_msg = capture[0]["messages"][1]["content"]
    assert "Skyward" in user_msg and "Sky captain." in user_msg


def test_enrich_unparseable_llm_response_503(client):
    adv = make_adventure()
    npc = make_npc(adv["id"], title="Ada Vane", entry="Sky captain.")
    override_transport(client, make_chat_transport("sure! here you go: not json"))
    try:
        resp = client.post(f"/api/adventures/{adv['id']}/cards/{npc['id']}/enrich")
    finally:
        clear_override(client)
    assert resp.status_code == 503
    assert resp.json()["error"]["code"] == "generation_failed"
    # nada foi persistido
    with db.session() as conn:
        assert db.get_card(conn, adv["id"], npc["id"])["sheet"] == ""


def test_enrich_unknown_card_404(client):
    adv = make_adventure()
    resp = client.post(f"/api/adventures/{adv['id']}/cards/nope/enrich")
    assert resp.status_code == 404


# ------------------------------------------- prompt de retrato usa a ficha
def test_portrait_prompt_uses_sheet(client, tmp_path):
    use_tmp_storage(tmp_path)
    adv = make_adventure()
    npc = make_npc(adv["id"], title="Ada Vane", entry="Sky captain.",
                   sheet=json.dumps(SHEET))
    client.post(f"/api/adventures/{adv['id']}/cards/{npc['id']}/portrait")

    capture: list = []
    jobs.process_pending(
        transport=make_assets_transport(capture=capture))
    chat_payloads = [p for p in capture if "messages" in p]
    user_msg = chat_payloads[0]["messages"][1]["content"]
    assert "Character sheet:" in user_msg
    assert "Silver hair, violet eyes" in user_msg
    assert "Tall, wiry build" in user_msg
