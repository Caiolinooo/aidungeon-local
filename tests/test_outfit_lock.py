"""Roupa padrão travada: o mesmo traje em todo mestre, ficha e giro."""

from __future__ import annotations

import json

import httpx

from app import db, jobs, outfit_lock
from conftest import make_adventure
from test_scene import pipeline_transport
from test_turnaround import _character, _mock_generation, _write_video
from test_v3 import use_tmp_storage

OUTFIT = {
    "top": "plain black cotton crew-neck t-shirt",
    "bottom": "black slim straight-leg trousers",
    "footwear": "white low-top canvas sneakers",
    "accessories": "large round thin-framed glasses",
}


def _transport(reply: str, calls: list | None = None) -> httpx.MockTransport:
    """LLM da roupa responde `reply`; o resto (tradução, Comfy) é o pipeline."""
    base = pipeline_transport()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/chat/completions"):
            payload = json.loads(request.content.decode("utf-8"))
            system = payload["messages"][0]["content"]
            if system.startswith("You fix a fictional character's single default outfit"):
                if calls is not None:
                    calls.append(payload)
                return httpx.Response(200, json={
                    "choices": [{"message": {"content": reply}}]})
        return base.handle_request(request)

    return httpx.MockTransport(handler)


def _set_sheet(adv_id: str, card_id: str, **fields) -> dict:
    with db.session() as conn:
        card = db.get_card(conn, adv_id, card_id)
        sheet = json.loads(card.get("sheet") or "{}")
        sheet.update(fields)
        return db.update_card(conn, adv_id, card_id, {"sheet": json.dumps(sheet)})


def test_card_without_outfit_text_gets_one_stored_outfit(tmp_db, tmp_path):
    adv = make_adventure()
    card, _ = _character(adv["id"], tmp_path)          # entry "A traveller", sem roupa
    assert outfit_lock.basis(card) == ""
    calls: list = []
    transport = _transport(json.dumps(OUTFIT), calls)
    with db.session() as conn:
        first = outfit_lock.ensure(conn, card, transport=transport)
    with db.session() as conn:
        stored = db.get_card(conn, adv["id"], card["id"])
        again = outfit_lock.ensure(conn, stored, transport=transport)
    assert {key: first[key] for key in outfit_lock.SLOTS} == OUTFIT
    assert first["source"] == "llm"
    assert json.loads(stored["sheet"])["outfit_lock"]["footwear"] == OUTFIT["footwear"]
    assert again == first
    assert len(calls) == 1                             # a segunda rodada não pergunta de novo
    assert calls[0]["temperature"] == 0


def test_llm_failure_still_locks_a_neutral_outfit(tmp_db, tmp_path):
    adv = make_adventure()
    card, _ = _character(adv["id"], tmp_path)
    with db.session() as conn:
        lock = outfit_lock.ensure(conn, card, transport=_transport("sure, a nice outfit"))
    assert lock["source"] == "fallback"
    assert lock["bottom"] == "plain dark straight-leg trousers"
    assert lock["footwear"] == "plain dark closed shoes"
    with db.session() as conn:
        stored = db.get_card(conn, adv["id"], card["id"])
    assert outfit_lock.current(stored)["footwear"] == "plain dark closed shoes"


def test_only_clothing_text_changes_the_lock(tmp_db, tmp_path):
    adv = make_adventure()
    card, _ = _character(adv["id"], tmp_path)
    card = _set_sheet(adv["id"], card["id"], clothing="black top with a round neckline")
    with db.session() as conn:
        card = db.update_card(conn, adv["id"], card["id"], {
            "entry": "Name: Renata\nHair: long black curls\nAttire: Casual, simple black top."})
    assert outfit_lock.basis(card) == (
        "black top with a round neckline | Attire: Casual, simple black top.")
    with db.session() as conn:
        outfit_lock.ensure(conn, card, transport=_transport(json.dumps(OUTFIT)))
        card = db.get_card(conn, adv["id"], card["id"])
        card = db.update_card(conn, adv["id"], card["id"], {
            "entry": card["entry"] + "\nVibe: golden hour."})
    assert outfit_lock.current(card) is not None           # texto alheio à roupa
    card = _set_sheet(adv["id"], card["id"], clothing="red leather jacket")
    assert outfit_lock.current(card) is None               # roupa nova, trava nova


def test_every_turnaround_run_paints_the_same_outfit_verbatim(tmp_db, tmp_path, monkeypatch):
    """Duas rodadas: mestre, as quatro vistas e o giro levam a mesma frase de
    roupa, fora da tradução (o LLM não reescreve a trava). A roupa do momento
    não entra."""
    use_tmp_storage(tmp_path)
    adv = make_adventure()
    card, anchor = _character(adv["id"], tmp_path)
    _set_sheet(adv["id"], card["id"], current_outfit="torn swimsuit after the shipwreck")
    video = tmp_path / "source.mp4"
    _write_video(video)
    capture: dict = {}
    _mock_generation(monkeypatch, tmp_path, video, capture)
    transport = _transport(json.dumps(OUTFIT))
    sentence = outfit_lock.full_text(OUTFIT)
    for run in range(2):
        with db.session() as conn:
            jobs.enqueue_turnaround(conn, adv["id"], card["id"], anchor["id"], force=True)
        jobs.process_pending(transport)
        paints = capture["image_calls"][5 * run:5 * run + 5]
        assert len(paints) == 5                             # mestre + 4 vistas
        for call in paints[:4]:                             # mestre, frente, perfil, costas
            assert sentence in call["prompt"]
            assert "swimsuit" not in call["prompt"]
        # close-up: só o que o busto mostra, literal da trava
        assert outfit_lock.visible_text(OUTFIT) in paints[4]["prompt"]
        assert "swimsuit" not in paints[4]["prompt"]
        assert sentence in capture["prompt"]               # giro do Wan
    prompts = [call["prompt"] for call in capture["image_calls"]]
    assert prompts[:5] == prompts[5:10]


def test_default_portrait_wears_the_lock_but_story_outfit_wins(tmp_db, tmp_path, monkeypatch):
    use_tmp_storage(tmp_path)
    adv = make_adventure()
    card, _ = _character(adv["id"], tmp_path)
    video = tmp_path / "source.mp4"
    _write_video(video)
    capture: dict = {}
    _mock_generation(monkeypatch, tmp_path, video, capture)
    with db.session() as conn:
        outfit_lock.ensure(conn, card, transport=_transport(json.dumps(OUTFIT)))
        db.update_card(conn, adv["id"], card["id"], {"portrait_prompt": "a traveller, bust"})
        jobs.enqueue_portrait(conn, adv["id"], card["id"], replace=True)
    jobs.process_pending(pipeline_transport())
    text = capture["image_calls"][-1]["prompt"]
    assert "Default outfit, keep exactly: top: plain black cotton crew-neck t-shirt" in text
    assert "large round thin-framed glasses" in text
    assert "white low-top canvas sneakers" not in text      # busto não puxa os pés

    _set_sheet(adv["id"], card["id"], current_outfit="red pirate coat")
    with db.session() as conn:
        jobs.enqueue_portrait(conn, adv["id"], card["id"], replace=True)
    jobs.process_pending(pipeline_transport())
    assert "Default outfit" not in capture["image_calls"][-1]["prompt"]


def test_a_top_never_ends_up_without_trousers(tmp_db, tmp_path):
    """Staging: o LLM devolveu "bottom: none (one-piece)" para "black top" e a
    ficha saiu de short. Sem vestido no card, a perna ganha calça neutra; a
    trava v1 já gravada é refeita."""
    adv = make_adventure()
    card, _ = _character(adv["id"], tmp_path)
    card = _set_sheet(adv["id"], card["id"], clothing="black top with a round neckline",
                      outfit_lock={"top": "black top", "bottom": "none (one-piece)",
                                   "footwear": "black sandals", "accessories": "none",
                                   "basis_hash": "x"})
    assert outfit_lock.current(card) is None
    reply = json.dumps({**OUTFIT, "bottom": "none (one-piece)", "footwear": "none"})
    with db.session() as conn:
        lock = outfit_lock.ensure(conn, card, transport=_transport(reply))
    assert lock["bottom"] == "plain dark straight-leg trousers"
    assert lock["footwear"] == "plain dark closed shoes"

    dress, _ = _character(adv["id"], tmp_path, title="Dama")
    dress = _set_sheet(adv["id"], dress["id"], clothing="long red silk dress")
    reply = json.dumps({**OUTFIT, "top": "long red silk dress", "bottom": "none (one-piece)"})
    with db.session() as conn:
        lock = outfit_lock.ensure(conn, dress, transport=_transport(reply))
    assert lock["bottom"] == "none (one-piece)"
