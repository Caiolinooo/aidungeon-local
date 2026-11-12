"""A referência manda: look lido da foto (ou da âncora) e repetido em toda pintura."""

from __future__ import annotations

import io
import json
from pathlib import Path

import httpx
from PIL import Image

from app import db, image_client, jobs, likeness, look_lock, outfit_lock
from conftest import add_action, make_adventure
from test_likeness import _capture_generate
from test_scene import _face, pipeline_transport
from test_v2 import PNG_BYTES, override_transport
from test_v3 import use_tmp_storage

LOOK = {
    "hair_colour": "jet black", "hair_texture": "tight voluminous curls",
    "hair_length": "just past the shoulders", "hairstyle": "loose",
    "glasses": "large round thin gold-rimmed glasses", "facial_hair": "none",
    "skin": "medium tan", "eyes": "dark brown", "face": "no distinctive marks",
    "build": "slim", "top": "black graphic t-shirt", "bottom": "black skinny jeans",
    "footwear": "light grey canvas sneakers", "accessories": "thin silver necklace",
}


def _vision_transport(reply: str, calls: list) -> httpx.MockTransport:
    """O narrador com visão responde `reply`; o resto é o pipeline normal."""
    base = pipeline_transport()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/chat/completions"):
            payload = json.loads(request.content.decode("utf-8"))
            system = payload["messages"][0]["content"]
            if isinstance(system, str) and system.startswith("You read reference images"):
                calls.append(payload)
                return httpx.Response(200, json={"choices": [{"message": {"content": reply}}]})
        return base.handle_request(request)

    return httpx.MockTransport(handler)


def _photo(tmp_path: Path, name: str, color) -> str:
    path = tmp_path / name
    Image.new("RGB", (600, 600), color).save(path, format="JPEG")
    return str(path)


def _photo_card(adv_id: str, tmp_path: Path, monkeypatch, *colors) -> dict:
    refs = tmp_path / "refs"
    refs.mkdir(exist_ok=True)
    monkeypatch.setenv("AIDUNGEON_REFERENCES_DIR", str(refs))
    paths = [_photo(refs, f"p{i}.jpg", color) for i, color in enumerate(colors)]
    sheet = {"reference": {"image_path": paths[0], "images": [{"image_path": p} for p in paths],
                           "source": "upload"},
             "clothing": "a simple black top"}
    with db.session() as conn:
        return db.insert_card(conn, adv_id, type="character", title="Renata Lemos",
                              triggers=["Renata"], entry="Hair: long black hair reaching her back.",
                              sheet=json.dumps(sheet))


def test_look_is_read_once_from_every_photo_and_follows_the_photo(tmp_db, tmp_path, monkeypatch):
    adv = make_adventure()
    card = _photo_card(adv["id"], tmp_path, monkeypatch, (10, 10, 10), (200, 150, 120))
    calls: list = []
    transport = _vision_transport(json.dumps(LOOK), calls)
    with db.session() as conn:
        first = look_lock.ensure(conn, card, transport=transport, llm_cfg=None)
        stored = db.get_card(conn, adv["id"], card["id"])
        again = look_lock.ensure(conn, stored, transport=transport, llm_cfg=None)
    assert first["hair_length"] == "just past the shoulders"
    assert first["source"] == "photo" and first["images"] == 2
    assert again == first and len(calls) == 1                 # gravado; não relê
    request = calls[0]
    assert request["temperature"] == 0
    images = [part for part in request["messages"][1]["content"] if part["type"] == "image_url"]
    assert len(images) == 2                                    # todas as fotos, principal primeiro
    # foto nova = leitura nova
    sheet = json.loads(stored["sheet"])
    refs = Path(sheet["reference"]["image_path"]).parent
    sheet["reference"]["images"].append({"image_path": _photo(refs, "p9.jpg", (0, 90, 0))})
    with db.session() as conn:
        changed = db.update_card(conn, adv["id"], card["id"], {"sheet": json.dumps(sheet)})
        look_lock.ensure(conn, changed, transport=transport, llm_cfg=None)
    assert len(calls) == 2


def test_card_without_photo_reads_its_anchor_portrait(tmp_db, tmp_path):
    use_tmp_storage(tmp_path)
    adv = make_adventure()
    with db.session() as conn:
        card = db.insert_card(conn, adv["id"], type="character", title="Caio", entry="A pirate")
    _face(adv["id"], card["id"], PNG_BYTES)
    with db.session() as conn:
        card = db.get_card(conn, adv["id"], card["id"])
        source, images = look_lock.reference_images(conn, card)
    assert source == "anchor" and images == [PNG_BYTES]


def test_photo_pieces_override_card_text_and_the_llm_pins_the_rest(tmp_db, tmp_path, monkeypatch):
    """A foto mostra camiseta e acessório; calça e sapato vêm do LLM, exatos.
    Texto do card ("simple black top") e o LLM não trocam o que a foto mostra."""
    adv = make_adventure()
    card = _photo_card(adv["id"], tmp_path, monkeypatch, (10, 10, 10))
    bust = {**LOOK, "bottom": "not visible", "footwear": "not visible"}
    sheet = json.loads(card["sheet"])
    sheet["look_lock"] = {**bust, "version": look_lock.LOOK_VERSION, "basis_hash": "abc"}
    with db.session() as conn:
        card = db.update_card(conn, adv["id"], card["id"], {"sheet": json.dumps(sheet)})
    asked: list = []
    base = pipeline_transport()

    def handler(request):
        if request.url.path.endswith("/chat/completions"):
            payload = json.loads(request.content.decode("utf-8"))
            if payload["messages"][0]["content"].startswith("You fix a fictional character"):
                asked.append(payload)
                return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps({
                    "top": "red hoodie", "bottom": "dark indigo wash straight-leg ankle-length jeans",
                    "footwear": "white low-top canvas sneakers with white soles",
                    "accessories": "none"})}}]})
        return base.handle_request(request)

    with db.session() as conn:
        lock = outfit_lock.ensure(conn, card, transport=httpx.MockTransport(handler))
    assert lock["top"] == "black graphic t-shirt"                       # da foto, literal
    assert lock["accessories"] == "thin silver necklace"
    assert lock["bottom"] == "dark indigo wash straight-leg ankle-length jeans"
    assert lock["source"] == "llm+reference"
    user = asked[0]["messages"][1]["content"]
    assert "Visible in the reference image (authoritative" in user
    assert "exact colour and shade or wash" in asked[0]["messages"][0]["content"]
    # a foto mostra tudo: nem pergunta ao LLM
    sheet = json.loads(card["sheet"])
    sheet["look_lock"] = {**LOOK, "version": look_lock.LOOK_VERSION, "basis_hash": "full"}
    sheet.pop("outfit_lock", None)
    with db.session() as conn:
        card = db.update_card(conn, adv["id"], card["id"], {"sheet": json.dumps(sheet)})
        lock = outfit_lock.ensure(conn, card, transport=httpx.MockTransport(handler))
    assert lock["bottom"] == "black skinny jeans" and lock["source"] == "reference"
    assert len(asked) == 1


def test_identity_text_keeps_glasses_and_hair():
    text = look_lock.identity_text(LOOK)
    assert "just past the shoulders, tight voluminous curls, jet black hair" in text
    assert "always wearing large round thin gold-rimmed glasses" in text
    assert "black graphic t-shirt" not in text                     # roupa não é identidade
    assert "always wearing glasses" in look_lock.identity_text({**LOOK, "glasses": "yes"})
    assert "no glasses" in look_lock.identity_text({**LOOK, "glasses": "none"})
    assert "thin metal frames, round shape glasses" in look_lock.identity_text(
        {**LOOK, "glasses": "thin metal frames, round shape"})


def test_see_of_three_people_carries_everyones_look(client, tmp_path, monkeypatch):
    """No See de três pessoas a Renata perdia os óculos: o prompt agora leva
    o look de cada um, também numa cena explícita (roupa muda, óculos não)."""
    adv = make_adventure()
    with db.session() as conn:
        db.set_setting(conn, "image.storage_dir", json.dumps(str(tmp_path / "imgs")))
    names = ["Renata Lemos", "Caio", "Ladrão"]
    cards = []
    for name in names:
        with db.session() as conn:
            card = db.insert_card(conn, adv["id"], type="character", title=name,
                                  triggers=[name], entry=f"{name} in the story.")
        _face(adv["id"], card["id"], PNG_BYTES)
        cards.append(card)
    sheet = {"look_lock": {**LOOK, "version": look_lock.LOOK_VERSION, "basis_hash": "x"}}
    with db.session() as conn:
        db.update_card(conn, adv["id"], cards[0]["id"], {"sheet": json.dumps(sheet)})
    add_action(adv["id"], 0, "do", "Renata Lemos, Caio and Ladrão talk on the beach at night.")
    seen = _capture_generate(monkeypatch, tmp_path)
    override_transport(client, pipeline_transport(
        "Renata Lemos, Caio and Ladrão stand on the beach at night."))
    try:
        resp = client.post(f"/api/adventures/{adv['id']}/see", json={"action_seq": 0})
    finally:
        client.app.dependency_overrides.clear()
    assert resp.status_code == 200, resp.text
    prompt = seen[-1]["prompt"]
    assert "Renata Lemos: just past the shoulders" in prompt
    assert "always wearing large round thin gold-rimmed glasses" in prompt
    with db.session() as conn:
        renata = db.get_card(conn, adv["id"], cards[0]["id"])
    explicit = likeness.opening_clause([renata], explicit=True)
    assert "always wearing large round thin gold-rimmed glasses" in explicit


def test_portrait_carries_the_reference_identity_in_any_story_state(tmp_db, tmp_path, monkeypatch):
    use_tmp_storage(tmp_path)
    adv = make_adventure()
    with db.session() as conn:
        card = db.insert_card(conn, adv["id"], type="character", title="Renata Lemos",
                              entry="A traveller")
    _face(adv["id"], card["id"], PNG_BYTES)
    sheet = {"look_lock": {**LOOK, "version": look_lock.LOOK_VERSION, "basis_hash": "x"},
             "current_outfit": "a soaked white dress"}
    with db.session() as conn:
        db.update_card(conn, adv["id"], card["id"], {"sheet": json.dumps(sheet),
                                                     "portrait_prompt": "a traveller, bust"})
        jobs.enqueue_portrait(conn, adv["id"], card["id"], replace=True)
    seen = _capture_generate(monkeypatch, tmp_path)
    jobs.process_pending(pipeline_transport())
    prompt = seen[-1]["prompt"]
    assert "Identity from the reference, keep exactly:" in prompt
    assert "always wearing large round thin gold-rimmed glasses" in prompt


def test_turnaround_uses_the_primary_and_the_clearest_face_photo(tmp_db, tmp_path, monkeypatch):
    adv = make_adventure()
    card = _photo_card(adv["id"], tmp_path, monkeypatch, (10, 10, 10), (200, 150, 120), (0, 0, 200))
    sheet = json.loads(card["sheet"])
    sheet["look_lock"] = {**LOOK, "clearest_face_image": 3, "source": "photo",
                          "version": look_lock.LOOK_VERSION, "basis_hash": "x"}
    with db.session() as conn:
        card = db.update_card(conn, adv["id"], card["id"], {"sheet": json.dumps(sheet)})
    photos = look_lock.reference_photos(card)
    colors = [Image.open(io.BytesIO(blob)).convert("RGB").getpixel((5, 5)) for blob in photos]
    assert len(photos) == 2
    assert colors[0][2] < 40 and colors[1][2] > 150      # principal, depois a do rosto
    sheet["look_lock"]["clearest_face_image"] = 1
    with db.session() as conn:
        card = db.update_card(conn, adv["id"], card["id"], {"sheet": json.dumps(sheet)})
    assert len(look_lock.reference_photos(card)) == 1


def test_a_new_reading_of_the_same_photo_rebuilds_the_outfit(tmp_db, tmp_path, monkeypatch):
    """Staging: o look v3 releu a mesma foto (outra estampa e outro acessório)
    e a trava de roupa, presa só ao hash das imagens, ficou com a leitura velha."""
    adv = make_adventure()
    card = _photo_card(adv["id"], tmp_path, monkeypatch, (10, 10, 10))
    sheet = json.loads(card["sheet"])
    sheet["look_lock"] = {**LOOK, "version": look_lock.LOOK_VERSION, "basis_hash": "same"}
    with db.session() as conn:
        card = db.update_card(conn, adv["id"], card["id"], {"sheet": json.dumps(sheet)})
        first = outfit_lock.ensure(conn, card)
        card = db.get_card(conn, adv["id"], card["id"])
    assert outfit_lock.current(card)["top"] == "black graphic t-shirt"
    sheet = json.loads(card["sheet"])
    sheet["look_lock"]["top"] = "black t-shirt with white text print"
    with db.session() as conn:
        card = db.update_card(conn, adv["id"], card["id"], {"sheet": json.dumps(sheet)})
        assert outfit_lock.current(card) is None
        second = outfit_lock.ensure(conn, card)
    assert first["top"] != second["top"] == "black t-shirt with white text print"


def test_see_reads_missing_looks_once_and_backs_off_after_a_failure(tmp_db, tmp_path, monkeypatch):
    use_tmp_storage(tmp_path)
    adv = make_adventure()
    with db.session() as conn:
        card = db.insert_card(conn, adv["id"], type="character", title="Caio", entry="A pirate")
    _face(adv["id"], card["id"], PNG_BYTES)
    look_lock._failures.clear()
    calls: list = []
    with db.session() as conn:
        cards = db.list_cards(conn, adv["id"])
    bad = _vision_transport("no idea", calls)
    look_lock.ensure_many(adv["id"], cards, transport=bad, llm_cfg=None)
    look_lock.ensure_many(adv["id"], cards, transport=bad, llm_cfg=None)
    assert len(calls) == 1                           # falhou: espera antes de reler
    look_lock._failures.clear()
    good = _vision_transport(json.dumps(LOOK), calls)
    updated = look_lock.ensure_many(adv["id"], cards, transport=good, llm_cfg=None)
    assert look_lock.current(updated[0])["source"] == "anchor"
    look_lock.ensure_many(adv["id"], updated, transport=good, llm_cfg=None)
    assert len(calls) == 2                           # já gravado: não relê
