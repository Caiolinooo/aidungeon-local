"""Cruzamento ponta a ponta: ficha ↔ retrato ↔ memória ↔ cena.

O teste não confere o relatório inteiro: confere que o elo quebrado aparece
no check certo e que o reparo conserta o dado persistido.
"""

from __future__ import annotations

import json

import httpx
import pytest

from app import db, deps
from conftest import add_action, make_adventure

SHEET_JSON = {
    "appearance": "cabelo flutuando como estrelas, olhos de ambar",
    "body": "magro, agil, postura elegante",
    "clothing": "vestes de seda e cristais",
    "personality": "orgulhosa e calculista",
    "humor": "responde com ironia seca",
    "speech": "fala devagar, em frases curtas",
    "goals": "manter o reino de cristal",
    "background": "nasceu entre os cristais da floresta antiga",
    "lifestyle": "vive na torre de cristal, estuda a luz",
    "habits": "toca o anel de cristal quando pensa",
    "likes": "cristais, poeira de estrelas",
    "sentiments": "desconfiada do visitante",
    "status": "alive",
    "stats": {"str": 10, "dex": 14, "con": 12, "int": 15, "wis": 11, "cha": 13},
    "notes": "rainha das fadas de cristal",
    "portrait_prompt": ("Elara wears translucent silk and crystal garments that "
                         "shimmer, her hair floating like stars, amber eyes, "
                         "bust portrait"),
}


def _mock_transport(chat_content: str, capture: list | None = None) -> httpx.MockTransport:
    """LLM de chat + serviço de embeddings."""
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/models"):
            return httpx.Response(200, json={"object": "list", "data": [{"id": "mock-model"}]})
        if path.endswith("/embeddings"):
            return httpx.Response(200, json={"data": [{"embedding": [0.1, 0.2, 0.3]}]})
        if path.endswith("/chat/completions"):
            if capture is not None:
                capture.append(json.loads(request.content.decode("utf-8")))
            return httpx.Response(200, json={
                "choices": [{"message": {"content": chat_content}}],
            })
        return httpx.Response(404, json={"error": f"no mock route: {path}"})

    return httpx.MockTransport(handler)


def _check(report: dict, check_id: str) -> dict:
    return next(c for c in report["checks"] if c["id"] == check_id)


def test_fresh_adventure_reports_every_check_ok(client):
    adv = make_adventure(title="Sem NPCs")
    resp = client.get(f"/api/adventures/{adv['id']}/consistency")
    assert resp.status_code == 200
    report = resp.json()
    assert report["ok"] is True
    assert {c["id"] for c in report["checks"]} == {
        "card_sheets", "portraits", "memories", "npc_minds",
        "canon_world", "scene_alignment",
    }
    assert all(c["status"] == "ok" for c in report["checks"])


def test_incomplete_sheet_is_flagged_and_repaired(client):
    adv = make_adventure(title="Ficha quebrada")
    with db.session() as conn:
        db.insert_card(conn, adv["id"], title="Elara", type="character",
                       sheet=json.dumps({"appearance": "cabelo de estrelas"}))
    report = client.get(f"/api/adventures/{adv['id']}/consistency").json()
    sheets = _check(report, "card_sheets")
    assert sheets["status"] == "error"
    assert "humor" in sheets["items"][0]["missing_fields"]
    assert report["ok"] is False

    transport = _mock_transport(json.dumps(SHEET_JSON))
    client.app.dependency_overrides[deps.get_http_transport] = lambda: transport
    try:
        resp = client.post(f"/api/adventures/{adv['id']}/consistency/repair",
                           json={"check": "card_sheets"})
    finally:
        client.app.dependency_overrides.pop(deps.get_http_transport, None)
    assert resp.status_code == 200
    assert resp.json()["repaired"]
    with db.session() as conn:
        card = db.get_card(conn, adv["id"],
                           resp.json()["repaired"][0]["card_id"])
    sheet = json.loads(card["sheet"])
    assert sheet["humor"] == SHEET_JSON["humor"]
    assert sheet["stats"]["str"] == 10
    # Depois do reparo a ficha está completa: a auditoria não aponta nada.
    sheets_after = _check(
        client.get(f"/api/adventures/{adv['id']}/consistency").json(),
        "card_sheets")
    assert sheets_after["status"] == "ok"
    assert sheets_after["items"] == []


def test_nude_portrait_prompt_is_flagged_against_a_clothed_sheet(client):
    adv = make_adventure(title="Retrato nu")
    sheet = {k: v for k, v in SHEET_JSON.items() if k != "portrait_prompt"}
    with db.session() as conn:
        db.insert_card(conn, adv["id"], title="Elara", type="character",
                       sheet=json.dumps(sheet),
                       portrait_prompt="Elara stands naked in the crystal forest")
    report = client.get(f"/api/adventures/{adv['id']}/consistency").json()
    portraits = _check(report, "portraits")
    assert portraits["status"] == "error"
    assert portraits["items"][0]["fix"] == "rebuild_prompt"

    transport = _mock_transport(json.dumps(SHEET_JSON))
    client.app.dependency_overrides[deps.get_http_transport] = lambda: transport
    try:
        resp = client.post(f"/api/adventures/{adv['id']}/consistency/repair",
                           json={"check": "portraits"})
    finally:
        client.app.dependency_overrides.pop(deps.get_http_transport, None)
    assert resp.status_code == 200
    with db.session() as conn:
        card = db.get_card(conn, adv["id"], resp.json()["repaired"][0]["card_id"])
    assert "crystal garments" in card["portrait_prompt"]


def test_memory_without_embedding_is_flagged_and_reembedded(client):
    adv = make_adventure(title="Memória sem vetor")
    with db.session() as conn:
        db.insert_memory(conn, adv["id"], "A rainha de cristal fulminou o invited.")
        db.set_setting(conn, "embeddings.base_url", json.dumps("http://embed.test"))
    report = client.get(f"/api/adventures/{adv['id']}/consistency").json()
    memories = _check(report, "memories")
    assert memories["status"] == "error"
    assert memories["items"][0]["issue"] == "sem embedding"

    transport = _mock_transport(json.dumps(SHEET_JSON))
    client.app.dependency_overrides[deps.get_http_transport] = lambda: transport
    try:
        resp = client.post(f"/api/adventures/{adv['id']}/consistency/repair",
                           json={"check": "memories"})
    finally:
        client.app.dependency_overrides.pop(deps.get_http_transport, None)
    assert resp.status_code == 200
    assert resp.json()["repaired"][0]["dim"] == 3
    with db.session() as conn:
        mem = db.list_memories(conn, adv["id"], include_vector=True)[0]
    assert mem["embedding"] is not None
    assert mem["embedding_dim"] == 3


def test_repair_refuses_a_check_that_cannot_be_repaired(client):
    adv = make_adventure(title="Cânone")
    resp = client.post(f"/api/adventures/{adv['id']}/consistency/repair",
                        json={"check": "canon_world"})
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "invalid_request"


def test_consistency_of_an_unknown_adventure_is_404(client):
    resp = client.get("/api/adventures/nao-existe/consistency")
    assert resp.status_code == 404


def test_player_sheet_is_judged_by_its_own_schema(client):
    """A ficha do jogador não é ficha de NPC: humor/ligações não são exigidos."""
    adv = make_adventure(title="Jogador")
    with db.session() as conn:
        db.upsert_player_character(
            conn, adv["id"], "caio", title="Caio",
            sheet=json.dumps({
                "appearance": "cabelo curto", "status": "alive", "class": "Ladino",
                "stats": {"str": 12, "dex": 14, "con": 13, "int": 10, "wis": 11, "cha": 9},
            }),
        )
    report = client.get(f"/api/adventures/{adv['id']}/consistency").json()
    sheets = _check(report, "card_sheets")
    assert sheets["status"] == "ok", sheets
    assert sheets["items"] == []


def test_repair_leaves_the_player_sheet_to_the_character_route(client):
    adv = make_adventure(title="Jogador sem classe")
    with db.session() as conn:
        db.upsert_player_character(
            conn, adv["id"], "caio", title="Caio",
            sheet=json.dumps({"appearance": "cabelo curto", "status": "alive",
                              "stats": {"str": 12, "dex": 14, "con": 13,
                                        "int": 10, "wis": 11, "cha": 9}}),
        )
    report = client.get(f"/api/adventures/{adv['id']}/consistency").json()
    sheets = _check(report, "card_sheets")
    assert sheets["status"] == "error"
    assert sheets["items"][0]["missing_fields"] == ["class"]

    transport = _mock_transport(json.dumps(SHEET_JSON))
    client.app.dependency_overrides[deps.get_http_transport] = lambda: transport
    try:
        resp = client.post(f"/api/adventures/{adv['id']}/consistency/repair",
                           json={"check": "card_sheets"})
    finally:
        client.app.dependency_overrides.pop(deps.get_http_transport, None)
    assert resp.status_code == 200
    body = resp.json()
    assert body["repaired"] == []
    assert body["errors"] and "character" in body["errors"][0]
    with db.session() as conn:
        card = db.player_character(conn, adv["id"], "caio")
    assert "class" not in json.loads(card["sheet"])


def test_repair_fills_what_is_deterministic_when_the_llm_fails(client):
    """LLM fora: status, classe e atributos saem; os textos vivos ficam de fora."""
    adv = make_adventure(title="Ficha sem LLM")
    with db.session() as conn:
        db.insert_card(conn, adv["id"], title="Elara", type="character",
                       entry="Elara, rainha das fadas de cristal, espera no claro.",
                       sheet=json.dumps({"appearance": "cabelo de estrelas",
                                         "status": "alive"}))

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json={"object": "list",
                                             "data": [{"id": "mock-model"}]})
        if request.url.path.endswith("/chat/completions"):
            return httpx.Response(500, json={"error": "llm fora"})
        return httpx.Response(404, json={"error": "no mock route"})

    client.app.dependency_overrides[deps.get_http_transport] = (
        lambda: httpx.MockTransport(handler))
    try:
        resp = client.post(f"/api/adventures/{adv['id']}/consistency/repair",
                           json={"check": "card_sheets"})
    finally:
        client.app.dependency_overrides.pop(deps.get_http_transport, None)
    assert resp.status_code == 200
    body = resp.json()
    assert body["repaired"] and body["repaired"][0]["partial"] is True
    assert body["errors"], "o erro do LLM precisa aparecer para o jogador"
    with db.session() as conn:
        card = db.get_card(conn, adv["id"], body["repaired"][0]["card_id"])
    sheet = json.loads(card["sheet"])
    assert sheet["class"] == "Bardo"  # inferida do texto do card
    assert set(sheet["stats"]) >= {"str", "dex", "con", "int", "wis", "cha"}
    assert not sheet.get("humor"), "texto vivo depende do LLM"
