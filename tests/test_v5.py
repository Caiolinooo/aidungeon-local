"""Testes v0.5.0 — ficha estruturada do NPC (sheet): persistência, enrich via
LLM, prompt de retrato enriquecido e cores VRM extraídas do retrato."""

from __future__ import annotations

import json

import httpx

from app import db, deps, jobs
from conftest import make_adventure
from test_v3 import PNG_B64, make_assets_transport, make_npc, use_tmp_storage

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

COLORS_JSON = json.dumps({
    "hair": "#C0C0D8", "eyes": "#7A5AF8", "skin": "#E8B89A",
    "top": "#2F3A4A", "bottom": "#1A1A22",
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


def give_portrait(client, adventure_id: str, card_id: str) -> None:
    """Gera um retrato real (mock) para o card via fila de jobs."""
    resp = client.post(f"/api/adventures/{adventure_id}/cards/{card_id}/portrait")
    assert resp.status_code == 202, resp.text
    assert jobs.process_pending(transport=make_assets_transport()) == 1


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
    assert json.loads(resp.json()["sheet"]) == new_sheet

    listed = client.get(f"/api/adventures/{adv['id']}/cards").json()
    assert json.loads(listed[0]["sheet"])["notes"].endswith("paperwork")

    # PATCH sem sheet preserva a ficha existente
    resp = client.patch(f"/api/adventures/{adv['id']}/cards/{card['id']}",
                        json={"entry": "Sky captain of the Kestrel."})
    assert json.loads(resp.json()["sheet"]) == new_sheet


def test_adventure_from_scenario_preserves_sheet(client):
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
    assert json.loads(cards[0]["sheet"]) == SHEET


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


# ------------------------------------------------------- vrm from portrait
def test_vrm_from_portrait_merges_colors_preserving_model(client, tmp_path):
    use_tmp_storage(tmp_path)
    adv = make_adventure()
    npc = make_npc(adv["id"], title="Ada Vane", entry="Sky captain.",
                   vrm_id=json.dumps({"model": "custom-mesh", "emotion": "happy"}))
    give_portrait(client, adv["id"], npc["id"])

    override_transport(client, make_chat_transport(COLORS_JSON))
    try:
        resp = client.post(
            f"/api/adventures/{adv['id']}/cards/{npc['id']}/vrm-from-portrait")
    finally:
        clear_override(client)
    assert resp.status_code == 200, resp.text
    cfg = json.loads(resp.json()["vrm_id"])
    assert cfg["hair"] == "#c0c0d8"         # normalizado p/ minúsculas
    assert cfg["eyes"] == "#7a5af8"
    assert cfg["skin"] == "#e8b89a"
    assert cfg["model"] == "custom-mesh"    # preserva config anterior
    assert cfg["emotion"] == "happy"


def test_vrm_from_portrait_without_portrait_409(client):
    adv = make_adventure()
    npc = make_npc(adv["id"], title="Ada Vane", entry="Sky captain.")
    resp = client.post(
        f"/api/adventures/{adv['id']}/cards/{npc['id']}/vrm-from-portrait")
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "no_portrait"


def test_vrm_from_portrait_too_few_colors_503(client, tmp_path):
    use_tmp_storage(tmp_path)
    adv = make_adventure()
    npc = make_npc(adv["id"], title="Ada Vane", entry="Sky captain.")
    give_portrait(client, adv["id"], npc["id"])

    two_colors = json.dumps({"hair": "#A1B2C3", "eyes": "blue", "skin": "?"})
    override_transport(client, make_chat_transport(two_colors))
    try:
        resp = client.post(
            f"/api/adventures/{adv['id']}/cards/{npc['id']}/vrm-from-portrait")
    finally:
        clear_override(client)
    assert resp.status_code == 503
    assert resp.json()["error"]["code"] == "generation_failed"


def test_vrm_from_portrait_sends_image_to_vision(client, tmp_path):
    use_tmp_storage(tmp_path)
    adv = make_adventure()
    npc = make_npc(adv["id"], title="Ada Vane", entry="Sky captain.")
    give_portrait(client, adv["id"], npc["id"])

    capture: list = []
    override_transport(client, make_chat_transport(COLORS_JSON, capture))
    try:
        resp = client.post(
            f"/api/adventures/{adv['id']}/cards/{npc['id']}/vrm-from-portrait")
    finally:
        clear_override(client)
    assert resp.status_code == 200, resp.text
    content = capture[0]["messages"][1]["content"]
    assert isinstance(content, list)
    image_part = next(p for p in content if p["type"] == "image_url")
    assert image_part["image_url"]["url"].startswith("data:image/png;base64,")


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
