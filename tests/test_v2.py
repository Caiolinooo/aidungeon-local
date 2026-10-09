"""Testes v2 — settings/image, ação See, assistente, choices e sampler."""

from __future__ import annotations

import base64
import json

import httpx

from app import db, deps, settings_store
from conftest import add_action, make_adventure, sse_body

# PNG 1x1 válido (usado como resposta fake do sdwebui)
PNG_B64 = ("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8"
           "z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==")
PNG_BYTES = base64.b64decode(PNG_B64)


def make_v2_transport(chat_content="a dark tower under a blood-red sky",
                      capture: list | None = None,
                      fail: str | None = None) -> httpx.MockTransport:
    """MockTransport combinado: LLM (/models + /chat/completions stream e
    não-stream) + sdwebui (/sdapi/v1/txt2img)."""
    def handler(request: httpx.Request) -> httpx.Response:
        if fail == "connect":
            raise httpx.ConnectError("mock: connection refused")
        path = request.url.path
        if path.endswith("/models"):
            return httpx.Response(200, json={
                "object": "list",
                "data": [{"id": "mock-model", "object": "model"}],
            })
        if path.endswith("/chat/completions"):
            payload = json.loads(request.content.decode("utf-8"))
            if capture is not None:
                capture.append(payload)
            if payload.get("stream"):
                return httpx.Response(
                    200, headers={"content-type": "text/event-stream"},
                    text=sse_body(("You", " open")))
            return httpx.Response(200, json={
                "choices": [{"message": {"content": chat_content}}],
            })
        if path.endswith("/sdapi/v1/txt2img"):
            return httpx.Response(200, json={"images": [PNG_B64], "parameters": {}})
        return httpx.Response(404, json={"error": f"no mock route: {path}"})

    return httpx.MockTransport(handler)


def override_transport(client, transport) -> None:
    client.app.dependency_overrides[deps.get_http_transport] = lambda: transport


# ------------------------------------------------------------- settings/image
def test_image_settings_defaults(client, monkeypatch):
    monkeypatch.delenv("IMAGE_API_KEY", raising=False)
    resp = client.get("/api/settings/image")
    assert resp.status_code == 200
    data = resp.json()
    assert data["provider"] == "sdwebui"
    assert data["enabled"] is True
    assert data["openai_api_key"] is None
    assert data["style_suffix"]


def test_image_settings_put_roundtrip_and_mask(client, monkeypatch):
    monkeypatch.delenv("IMAGE_API_KEY", raising=False)
    resp = client.put("/api/settings/image",
                      json={"sdwebui_steps": 30, "negative_prompt": "ugly",
                            "openai_api_key": "sk-img-123456"})
    data = resp.json()
    assert data["sdwebui_steps"] == 30
    assert data["negative_prompt"] == "ugly"
    assert data["openai_api_key"] == "sk-…3456"
    assert "sk-img-123456" not in json.dumps(resp.json())

    again = client.get("/api/settings/image").json()  # override persiste
    assert again["sdwebui_steps"] == 30
    assert "sk-img-123456" not in json.dumps(again)

    client.put("/api/settings/image", json={"openai_api_key": ""})  # "" remove
    assert client.get("/api/settings/image").json()["openai_api_key"] is None


# ---------------------------------------------------------------------- /see
def test_see_flow_creates_image_and_action(client, tmp_path):
    adv = make_adventure(image_style="ink sketch")
    add_action(adv["id"], 0, "start", "You stand before the dark tower.")
    add_action(adv["id"], 1, "continue", "Thunder cracks overhead.")
    with db.session() as conn:
        db.set_setting(conn, "image.storage_dir", json.dumps(str(tmp_path / "imgs")))

    capture: list = []
    override_transport(client, make_v2_transport(capture=capture))
    try:
        resp = client.post(f"/api/adventures/{adv['id']}/see",
                           json={"action_seq": 1, "extra_prompt": "dramatic angle"})
    finally:
        client.app.dependency_overrides.clear()

    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["provider"] == "sdwebui"
    assert data["url"] == f"/api/images/{data['id']}"
    # prompt final: image_style + resumo do LLM + style_suffix + extra_prompt
    assert data["prompt"].startswith("ink sketch")
    assert "a dark tower under a blood-red sky" in data["prompt"]
    assert "dramatic angle" in data["prompt"]
    # action type=see persistida com imagem inline
    action = data["action"]
    assert action["type"] == "see"
    assert action["image"]["id"] == data["id"]
    assert action["image"]["url"] == data["url"]

    with db.session() as conn:
        image = db.get_image(conn, data["id"])
        assert image is not None
        assert image["action_seq"] == action["seq"]
        see_actions = [a for a in db.list_actions(conn, adv["id"])
                       if a["type"] == "see"]
        assert len(see_actions) == 1
        assert see_actions[0]["image"]["id"] == data["id"]

    # GET do arquivo devolve os bytes do PNG
    resp = client.get(f"/api/images/{data['id']}")
    assert resp.status_code == 200
    assert resp.content == PNG_BYTES

    # o LLM do prompt_builder recebeu a cena (sem a action see)
    chat_calls = [c for c in capture if "messages" in c]
    assert chat_calls and "dark tower" in json.dumps(chat_calls[0]["messages"])


def test_see_unknown_adventure_404(client):
    resp = client.post("/api/adventures/nope/see", json={"action_seq": 0})
    assert resp.status_code == 404


def test_see_disabled_returns_503(client):
    adv = make_adventure()
    add_action(adv["id"], 0, "start", "You wake up.")
    with db.session() as conn:
        db.set_setting(conn, "image.enabled", json.dumps(False))
    resp = client.post(f"/api/adventures/{adv['id']}/see", json={"action_seq": 0})
    assert resp.status_code == 503
    assert resp.json()["error"]["code"] == "image_disabled"


def test_get_image_missing_404(client):
    assert client.get("/api/images/nope").status_code == 404


# ---------------------------------------------------------------- assistant
DRAFT = {
    "title": "The Leviathan Hunt",
    "description": "A steampunk pirate chases a mythic beast.",
    "ai_instructions": "Swashbuckling tone, sky-whale imagery.",
    "plot_essentials": "[Name: Ada Vane; Class: Sky Captain]",
    "authors_note": "Keep it pulpy.",
    "opening_text": "You grip the railing as the Leviathan breaches the clouds.",
    "tags": ["steampunk", "pirates", "hunt"],
    "story_cards": [
        {"title": "Ada Vane", "type": "character",
         "triggers": ["Ada", "captain"], "entry": "Captain of the Kestrel."},
        {"title": "The Leviathan", "triggers": ["Leviathan"],
         "entry": "A sky-whale of legend."},
        {"title": "Port Sorrow", "type": "location",
         "triggers": ["Port Sorrow"], "entry": "Last free port."},
    ],
}


def test_assistant_scenario_parses_fenced_json(client):
    fenced = f"```json\n{json.dumps(DRAFT)}\n```"
    override_transport(client, make_v2_transport(fenced))
    try:
        resp = client.post("/api/assistant/scenario",
                           json={"brief": "steampunk pirate hunting a leviathan"})
    finally:
        client.app.dependency_overrides.clear()
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["title"] == "The Leviathan Hunt"
    assert data["tags"] == ["steampunk", "pirates", "hunt"]
    assert len(data["story_cards"]) == 3
    # defaults: type ausente → 'other'; triggers sempre lista
    assert data["story_cards"][1]["type"] == "other"
    assert data["story_cards"][0]["triggers"] == ["Ada", "captain"]


def test_assistant_scenario_malformed_json_503(client):
    override_transport(client, make_v2_transport("sorry, I cannot help with that"))
    try:
        resp = client.post("/api/assistant/scenario",
                           json={"brief": "steampunk pirate hunting a leviathan"})
    finally:
        client.app.dependency_overrides.clear()
    assert resp.status_code == 503
    assert resp.json()["error"]["code"] == "generation_failed"


def test_assistant_field_regenerates_one_field(client):
    override_transport(client, make_v2_transport('{"value": "The Sky Whale Saga"}'))
    try:
        resp = client.post("/api/assistant/field",
                           json={"field": "title", "brief": "sky whales",
                                 "current": "Old Title", "draft": DRAFT})
    finally:
        client.app.dependency_overrides.clear()
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"field": "title", "value": "The Sky Whale Saga"}


# ------------------------------------------------------------------ choices
CHOICES = [
    {"type": "do", "text": "Draw your sword"},
    {"type": "say", "text": "Ask the innkeeper about the tower"},
    {"type": "story", "text": "A storm rolls in from the sea"},
]


def test_choices_returns_parsed_array(client):
    adv = make_adventure()
    add_action(adv["id"], 0, "start", "You stand before the dark tower.")
    override_transport(client, make_v2_transport(json.dumps(CHOICES)))
    try:
        resp = client.post(f"/api/adventures/{adv['id']}/choices")
    finally:
        client.app.dependency_overrides.clear()
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"choices": CHOICES}


def test_choices_disabled_returns_empty(client, monkeypatch):
    adv = make_adventure()
    settings_store.load_file_settings()  # garante cache populado
    monkeypatch.setitem(settings_store._file_cache, "choices", {"enabled": False})
    resp = client.post(f"/api/adventures/{adv['id']}/choices")
    assert resp.status_code == 200
    assert resp.json() == {"choices": []}


def test_choices_unknown_adventure_404(client):
    assert client.post("/api/adventures/nope/choices").status_code == 404


# ------------------------------------------------------------------ sampler
def test_sampler_override_roundtrip_affects_runtime_config(client):
    resp = client.put("/api/settings/llm", json={
        "temperature": 0.5, "top_p": 0.7, "repetition_penalty": 1.2})
    data = resp.json()
    assert data["temperature"] == 0.5
    assert data["top_p"] == 0.7
    assert data["repetition_penalty"] == 1.2

    assert client.get("/api/settings/llm").json()["temperature"] == 0.5
    with db.session() as conn:
        assert db.get_setting(conn, "sampler.temperature") is not None
        cfg = settings_store.runtime_config(conn)
    assert cfg["sampler"]["temperature"] == 0.5
    assert cfg["sampler"]["top_p"] == 0.7
    assert cfg["sampler"]["repetition_penalty"] == 1.2


def test_sampler_validation_error(client):
    assert client.put("/api/settings/llm", json={"temperature": 3.0}).status_code == 422


# ------------------------------------------------ scenario story_cards (v2)
def test_scenario_story_cards_inherited_by_adventure(client):
    resp = client.post("/api/scenarios", json={
        "title": "Draft Scenario",
        "opening_text": "You wake aboard the Kestrel.",
        "story_cards": [{"title": "Ada Vane", "type": "character",
                         "triggers": ["Ada"], "entry": "Sky captain."}],
    })
    assert resp.status_code == 201, resp.text
    scenario = resp.json()
    assert scenario["story_cards"][0]["title"] == "Ada Vane"

    assert client.get(f"/api/scenarios/{scenario['id']}").json()[
        "story_cards"][0]["triggers"] == ["Ada"]

    adv = client.post("/api/adventures", json={"scenario_id": scenario["id"]}).json()
    assert adv["story_cards"][0]["title"] == "Ada Vane"
    assert adv["story_cards"][0]["type"] == "character"


# ------------------------------------------------------------ migration runner
def test_init_db_sets_latest_user_version(tmp_db):
    with db.connect() as conn:
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        tables = {r["name"] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        adv_cols = {r["name"] for r in conn.execute("PRAGMA table_info(adventures)")}
        scn_cols = {r["name"] for r in conn.execute("PRAGMA table_info(scenarios)")}
        card_cols = {r["name"] for r in conn.execute("PRAGMA table_info(story_cards)")}
        img_cols = {r["name"] for r in conn.execute("PRAGMA table_info(images)")}
    latest = max(int(p.name.split("_")[0]) for p in db.MIGRATIONS_DIR.glob("*.sql"))
    assert version == latest
    assert "images" in tables
    assert "jobs" in tables
    assert "image_style" in adv_cols
    assert {"portrait_image_id", "portrait_anchor_id", "portrait_prompt", "voice", "vrm_id", "sheet"} <= card_cols
    assert {"kind", "ref_id"} <= img_cols
    assert "story_cards" in scn_cols
    db.init_db()  # idempotente: segunda chamada não falha
