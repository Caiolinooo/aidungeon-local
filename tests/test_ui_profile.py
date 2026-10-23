"""Testes v0.7.0 — ui-profile: manifesto de controles gerado pela IA,
estado dos widgets (PUT ui-state) e bloco [Player state] no contexto."""

from __future__ import annotations

import json

import httpx

from app import context_builder as cb
from app import db, deps, settings_store
from app.routes import ui_profile

from conftest import add_action, make_adventure


# ------------------------------------------------------------ parse/normalize
def test_parse_profile_dnd_full():
    content = json.dumps({
        "tabs": [
            {"id": "do", "label": "Act", "placeholder": "What do you attempt?"},
            {"id": "say", "label": "Speak"},
            {"id": "hack", "label": "Invalid"},          # id fora do whitelist
        ],
        "widgets": [
            {"type": "dice", "label": "Dice", "dice": [20, 6, 7, -1, 12]},
            {"type": "stats", "label": "Stats", "stats": [
                {"name": "HP", "value": 20, "max": 20, "min": 0},
                {"name": "Mana", "value": 5},
                {"value": 9},                            # sem nome → fora
            ]},
            {"type": "inventory", "label": "Bag",
             "items": [{"name": "Torch", "qty": 2}, {"name": "Rope"}]},
            {"type": "character", "label": "Sheet", "fields": [
                {"name": "Class", "value": "Rogue"}]},
            {"type": "minimap", "label": "Map"},         # tipo desconhecido → fora
        ],
    })
    profile = ui_profile._parse_profile(content)
    # tabs: sempre as 3 canônicas, na ordem fixa, com default p/ as ausentes
    assert [t["id"] for t in profile["tabs"]] == ["do", "say", "story"]
    # rótulos e placeholders são fixos no app (PT-BR); o texto do LLM é ignorado
    assert profile["tabs"][0]["label"] == "Ação"
    assert profile["tabs"][0]["placeholder"] == "O que você faz?"
    assert profile["tabs"][1]["label"] == "Fala"
    assert profile["tabs"][2]["label"] == "História"
    widgets = {w["type"]: w for w in profile["widgets"]}
    assert set(widgets) == {"dice", "stats", "inventory", "character"}
    assert widgets["dice"]["dice"] == [6, 12, 20]       # filtrados + ordenados
    assert [s["name"] for s in widgets["stats"]["stats"]] == ["HP", "Mana"]
    assert widgets["stats"]["stats"][1]["max"] is None
    assert widgets["inventory"]["items"] == [
        {"name": "Torch", "qty": 2}, {"name": "Rope", "qty": 1}]


def test_parse_profile_narrative_no_widgets():
    profile = ui_profile._parse_profile('{"tabs": [], "widgets": []}')
    assert profile["widgets"] == []
    assert [t["id"] for t in profile["tabs"]] == ["do", "say", "story"]


def test_parse_profile_rejects_garbage():
    for bad in ["not json", "[1,2,3]", "```json\n42\n```", ""]:
        try:
            ui_profile._parse_profile(bad)
            raise AssertionError(f"deveria falhar: {bad!r}")
        except (ValueError, TypeError):
            pass


def test_parse_profile_keeps_hud_and_story_actions():
    profile = ui_profile._parse_profile(json.dumps({
        "widgets": [],
        "hud": [
            {"label": "Local", "value": "Sala dos Girinos", "tone": "info"},
            {"label": "clima", "value": "Chuva"},
            {"label": "", "value": "sem rótulo"},
        ],
        "actions": [
            {"label": "Perguntar sobre a marca", "type": "say",
             "text": "Pergunto sobre a marca do pântano."},
            {"type": "invalid", "text": "Observo a porta."},
            {"label": "sem texto", "type": "do", "text": ""},
        ],
    }))
    assert profile["widgets"] == []
    assert [item["label"] for item in profile["hud"]] == ["Local", "clima"]
    assert profile["hud"][0]["tone"] == "info"
    assert profile["hud"][1]["tone"] == "neutral"
    assert profile["actions"] == [
        {"label": "Perguntar sobre a marca", "type": "say",
         "text": "Pergunto sobre a marca do pântano."},
        {"label": "Observo a porta.", "type": "do", "text": "Observo a porta."},
    ]
    state = ui_profile.initial_state(profile)
    assert state["_hud"][0]["value"] == "Sala dos Girinos"
    assert state["_actions"][0]["type"] == "say"


def test_parse_profile_caps_lists():
    content = json.dumps({"widgets": [
        {"type": "stats", "stats": [
            {"name": f"S{i}", "value": i} for i in range(50)]},
        {"type": "character", "fields": [
            {"name": f"F{i}", "value": "x"} for i in range(50)]},
    ]})
    profile = ui_profile._parse_profile(content)
    widgets = {w["type"]: w for w in profile["widgets"]}
    assert len(widgets["stats"]["stats"]) == ui_profile._MAX_STATS
    assert len(widgets["character"]["fields"]) == ui_profile._MAX_FIELDS


# ------------------------------------------------------------- initial_state
def test_initial_state_seeds_from_profile():
    profile = ui_profile._parse_profile(json.dumps({"widgets": [
        {"id": "stats", "type": "stats",
         "stats": [{"name": "HP", "value": 20}]},
        {"id": "inventory", "type": "inventory",
         "items": [{"name": "Torch", "qty": 2}]},
        {"id": "character", "type": "character",
         "fields": [{"name": "Class", "value": "Rogue"}]},
        {"id": "dice", "type": "dice", "dice": [20]},
    ]}))
    state = ui_profile.initial_state(profile)
    assert state["stats"] == {"HP": 20}
    assert state["inventory"] == [{"name": "Torch", "qty": 2}]
    assert state["character"] == {"Class": "Rogue"}
    assert "dice" not in state  # dados são stateless


# -------------------------------------------------------- render_player_state
def test_render_player_state_compact():
    text = cb.render_player_state({
        "stats": {"HP": 14, "Mana": 3},
        "inventory": [{"name": "Torch", "qty": 2}, {"name": "Rope", "qty": 1}],
        "character": {"Class": "Rogue"},
        "dice": {},        # vazio → fora
        "empty": [],
    })
    assert text == ("stats: HP 14, Mana 3\n"
                    "inventory: Torch×2, Rope×1\n"
                    "character: Class Rogue")
    assert cb.render_player_state({}) == ""
    assert cb.render_player_state(None) == ""


# ------------------------------------------------------------------- rotas
def _make_transport(chat_content: str, capture: list | None = None):
    """MockTransport do LLM (não-stream devolve chat_content como message)."""
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
                "choices": [{"message": {"content": chat_content}}],
            })
        return httpx.Response(404, json={"error": f"no mock route: {path}"})
    return httpx.MockTransport(handler)


def _override(client, transport):
    client.app.dependency_overrides[deps.get_http_transport] = lambda: transport


DND_MANIFEST = json.dumps({
    "tabs": [{"id": "do", "label": "Act", "placeholder": "What do you attempt?"}],
    "widgets": [
        {"id": "dice", "type": "dice", "label": "Dice", "dice": [20, 6]},
        {"id": "stats", "type": "stats", "label": "Stats",
         "stats": [{"name": "HP", "value": 20, "max": 20, "min": 0}]},
        {"id": "inventory", "type": "inventory", "label": "Inventory",
         "items": [{"name": "Torch", "qty": 1}]},
    ],
})


def test_ui_profile_generate_and_get(client):
    adv = make_adventure(title="Dungeon of Doom")
    add_action(adv["id"], 0, "start", "You descend into the dungeon.")
    _override(client, _make_transport(DND_MANIFEST))
    try:
        resp = client.post(f"/api/adventures/{adv['id']}/ui-profile")
    finally:
        client.app.dependency_overrides.clear()
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["profile"]["tabs"][0]["label"] == "Ação"
    assert [w["type"] for w in body["profile"]["widgets"]] == ["dice", "stats", "inventory"]
    # estado semeado a partir do manifesto
    assert body["state"]["stats"] == {"HP": 20}
    assert body["state"]["inventory"] == [{"name": "Torch", "qty": 1}]

    # GET devolve o persistido; aventura state também carrega os campos
    got = client.get(f"/api/adventures/{adv['id']}/ui-profile").json()
    assert got == body
    state = client.get(f"/api/adventures/{adv['id']}").json()
    assert state["ui_profile"] == body["profile"]
    assert state["ui_state"] == body["state"]


def test_ui_profile_regenerate_preserves_player_state(client):
    adv = make_adventure()
    _override(client, _make_transport(DND_MANIFEST))
    try:
        client.post(f"/api/adventures/{adv['id']}/ui-profile")
        # jogador tomou dano: HP 20 → 7
        client.put(f"/api/adventures/{adv['id']}/ui-state",
                   json={"state": {"stats": {"HP": 7}}})
        resp = client.post(f"/api/adventures/{adv['id']}/ui-profile")
    finally:
        client.app.dependency_overrides.clear()
    assert resp.json()["state"]["stats"] == {"HP": 7}


def test_ui_state_put_roundtrip_and_limits(client):
    adv = make_adventure()
    resp = client.put(f"/api/adventures/{adv['id']}/ui-state",
                      json={"state": {"inventory": [{"name": "Key", "qty": 1}]}})
    assert resp.status_code == 200
    assert client.get(f"/api/adventures/{adv['id']}/ui-profile").json()["state"] == {
        "inventory": [{"name": "Key", "qty": 1}]}
    too_big = {"state": {"blob": "x" * (ui_profile._MAX_STATE_BYTES + 1)}}
    assert client.put(
        f"/api/adventures/{adv['id']}/ui-state", json=too_big).status_code == 413


def test_ui_profile_unknown_adventure_404(client):
    assert client.get("/api/adventures/nope/ui-profile").status_code == 404
    assert client.post("/api/adventures/nope/ui-profile").status_code == 404
    assert client.put("/api/adventures/nope/ui-state",
                      json={"state": {}}).status_code == 404


def test_ui_profile_disabled_returns_null(client, monkeypatch):
    adv = make_adventure()
    settings_store.load_file_settings()  # garante cache populado
    monkeypatch.setitem(settings_store._file_cache, "ui_profile", {"enabled": False})
    resp = client.post(f"/api/adventures/{adv['id']}/ui-profile")
    assert resp.status_code == 200
    assert resp.json()["profile"] is None


def test_ui_profile_unparseable_llm_503(client):
    adv = make_adventure()
    _override(client, _make_transport("sorry, I cannot help"))
    try:
        resp = client.post(f"/api/adventures/{adv['id']}/ui-profile")
    finally:
        client.app.dependency_overrides.clear()
    assert resp.status_code == 503


# ------------------------------------------------- contexto: [Player state]
def test_context_includes_player_state_block(tmp_db):
    adv = make_adventure(title="Dungeon")
    add_action(adv["id"], 0, "start", "You descend.")
    with db.session() as conn:
        db.update_adventure_ui_state(conn, adv["id"], {
            "stats": {"HP": 7},
            "inventory": [{"name": "Torch", "qty": 2}],
        })
        adv = db.get_adventure(conn, adv["id"])
        actions = db.list_actions(conn, adv["id"])
        cfg = settings_store.runtime_config(conn)
        built = cb.build_context(adv, actions, cfg, conn)
    sys_msg = built.messages[0]["content"]
    final = built.messages[-1]["content"]
    assert "[Player state]" not in sys_msg
    assert "[Player state]" in final
    assert "stats: HP 7" in final
    assert "inventory: Torch×2" in final


def test_context_without_ui_state_has_no_block(tmp_db):
    adv = make_adventure(title="Quiet Story")
    add_action(adv["id"], 0, "start", "Rain falls.")
    with db.session() as conn:
        adv = db.get_adventure(conn, adv["id"])
        actions = db.list_actions(conn, adv["id"])
        cfg = settings_store.runtime_config(conn)
        built = cb.build_context(adv, actions, cfg, conn)
    assert all("[Player state]" not in m["content"] for m in built.messages)
