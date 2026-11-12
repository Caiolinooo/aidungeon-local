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
    "hair_colour": "jet black", "hair_dye": "none", "hair_sheen": "none",
    "hair_texture": "tight voluminous curls",
    "hair_length": "just past the shoulders", "hairstyle": "loose",
    "glasses": "large round thin gold-rimmed glasses", "facial_hair": "none",
    "skin": "medium tan", "eyes": "dark brown", "face_shape": "round face with full cheeks",
    "eyebrows": "thick dark arched eyebrows", "lips": "full lips", "face": "no distinctive marks",
    "build": "slim", "top": "black graphic t-shirt", "bottom": "black skinny jeans",
    "footwear": "light grey canvas sneakers", "accessories": "thin silver necklace",
    "face_images": [1],
}


def _vision_transport(reply: str, calls: list) -> httpx.MockTransport:
    """O narrador com visão responde `reply`; o resto é o pipeline normal."""
    base = pipeline_transport()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/chat/completions"):
            payload = json.loads(request.content.decode("utf-8"))
            system = payload["messages"][0]["content"]
            if isinstance(system, str) and system.startswith(("You read reference images",
                                                              "You read the clothing")):
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
    assert again == first and len(calls) == 2                 # traços + roupa; gravado, não relê
    request = calls[0]
    assert request["temperature"] == 0
    images = [part for part in request["messages"][1]["content"] if part["type"] == "image_url"]
    assert len(images) == 2                                    # todas as fotos, principal primeiro
    clothing = calls[1]["messages"][1]["content"]
    assert len([part for part in clothing if part["type"] == "image_url"]) == 1   # só a principal
    # foto nova = leitura nova
    sheet = json.loads(stored["sheet"])
    refs = Path(sheet["reference"]["image_path"]).parent
    sheet["reference"]["images"].append({"image_path": _photo(refs, "p9.jpg", (0, 90, 0))})
    with db.session() as conn:
        changed = db.update_card(conn, adv["id"], card["id"], {"sheet": json.dumps(sheet)})
        look_lock.ensure(conn, changed, transport=transport, llm_cfg=None)
    assert len(calls) == 4


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
    # da foto, com a estampa normalizada (sem letras, sempre igual)
    assert lock["top"] == "black graphic t-shirt"      # estampa de desenho fica como lida
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
    assert "Renata Lemos: glasses: large round thin gold-rimmed glasses" in prompt
    assert "just past the shoulders, tight voluminous curls, jet black hair" in prompt
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


def test_face_picks_are_checked_and_thumbnails_upgraded(tmp_db, tmp_path, monkeypatch):
    """A leitura escolheu uma selfie com o celular na frente do rosto e uma
    miniatura de 150 px. O detector descarta o rosto virado e a miniatura vira
    a mesma foto em resolução maior."""
    adv = make_adventure()
    card = _photo_card(adv["id"], tmp_path, monkeypatch, (10, 10, 10), (200, 150, 120), (0, 0, 200))
    sheet = json.loads(card["sheet"])
    refs = Path(sheet["reference"]["image_path"]).parent
    big = refs / "big.jpg"
    Image.new("RGB", (1000, 1000), (0, 0, 200)).save(big, format="JPEG")
    sheet["reference"]["images"].append({"image_path": str(big)})
    sheet["look_lock"] = {**LOOK, "face_images": [2, 3], "source": "photo",
                          "version": look_lock.LOOK_VERSION, "basis_hash": "x"}
    with db.session() as conn:
        card = db.update_card(conn, adv["id"], card["id"], {"sheet": json.dumps(sheet)})
    frontal = {(200, 150, 120): False}

    def fake_frontal(photo):
        color = Image.open(io.BytesIO(photo)).convert("RGB").getpixel((5, 5))
        return frontal.get(tuple(min(255, round(c / 10) * 10) for c in color), True)

    monkeypatch.setattr(look_lock, "_frontal_face", fake_frontal)
    picks = look_lock.face_photos(card)
    assert len(picks) == 1
    with Image.open(io.BytesIO(picks[0])) as chosen:
        assert chosen.size == (1000, 1000)                  # a cópia grande da azul
    assert look_lock.primary_photo(card) == likeness.visual_reference_images(card)[0]


def test_face_crop_is_large_and_centred_on_the_face(monkeypatch):
    from app import likeness as likeness_module

    image = Image.new("RGB", (400, 600), (90, 90, 90))
    image.paste(Image.new("RGB", (100, 120), (230, 180, 150)), (150, 100))
    raw = io.BytesIO(); image.save(raw, format="PNG")
    monkeypatch.setattr(likeness_module, "_largest_face_box", lambda img: (150, 100, 100, 120))
    crop = Image.open(io.BytesIO(look_lock.face_crop(raw.getvalue())))
    assert max(crop.size) == look_lock.FACE_CROP_SIDE
    centre = crop.getpixel((crop.width // 2, crop.height // 2))
    assert centre == (230, 180, 150)
    monkeypatch.setattr(likeness_module, "_largest_face_box", lambda img: None)
    assert look_lock.face_crop(raw.getvalue()) is None


def test_turnaround_references_drop_the_anchor_when_there_is_a_photo():
    refs = jobs._turnaround_references
    faces, primary, master, anchor = [b"f1", b"f2"], [b"p"], b"m", b"a"
    assert refs("master", faces=faces, primary=primary, photos=[], anchor=anchor, master=b"") \
        == [b"f1", b"f2"]
    assert refs("master", faces=[], primary=primary, photos=[], anchor=anchor, master=b"") == [b"p"]
    assert refs("face", faces=faces, primary=primary, photos=[], anchor=anchor, master=master) \
        == [b"f1", b"f2", b"m"]
    assert refs("back", faces=faces, primary=primary, photos=[], anchor=anchor, master=master) \
        == [b"f1", b"m"]
    # sem foto: a âncora continua
    assert refs("master", faces=[], primary=[], photos=[], anchor=anchor, master=b"") == [b"a"]
    assert refs("face", faces=[], primary=[], photos=[], anchor=anchor, master=master) == [b"m", b"a"]


def test_prints_lose_their_letters_and_hair_tint_stays_faint():
    # letras que a foto não deixa ler: a peça fica lisa, sem mancha no lugar
    assert look_lock.no_text("black t-shirt with white text print") == "black t-shirt"
    assert look_lock.no_text("black t-shirt with small white lettering on the chest") == "black t-shirt"
    # palavras transcritas e estampa de desenho ficam como foram lidas
    quoted = 'white t-shirt with large black printed words "OCEAN CLUB" across the chest'
    assert look_lock.no_text(quoted) == quoted
    floral = "black t-shirt with small white line-art flower graphic on the chest"
    assert look_lock.no_text(floral) == floral
    assert look_lock.no_text("plain black crew-neck t-shirt") == "plain black crew-neck t-shirt"
    assert look_lock.has_lettering(quoted) and not look_lock.has_lettering(floral)
    assert look_lock.has_print(floral) and not look_lock.has_print("black t-shirt")
    lock = {"top": quoted, "bottom": "olive cargo trousers", "footwear": "tan boots",
            "accessories": "none"}
    assert "The print is only on the chest; the back of the top is plain." in \
        outfit_lock.full_text(lock)
    assert "back of the top" not in outfit_lock.full_text({**lock, "top": "black t-shirt"})
    card = {"id": "c", "title": "Marina", "type": "character", "sheet": "{}"}
    prompts = jobs._turnaround_prompts(card, lock, identity="exact likeness of Marina")
    for kind in ("master", "front", "face"):
        assert "no lettering other than the print on the top" in prompts[kind]
        assert "No text, no labels" not in prompts[kind]
    # de costas a estampa não aparece, e os óculos não entram nos acessórios
    assert '"OCEAN CLUB"' in prompts["front"] and '"OCEAN CLUB"' in prompts["profile"]
    assert '"OCEAN CLUB"' not in prompts["back"]
    assert "top: white t-shirt, plain back with no print" in prompts["back"]
    assert "No text, no labels, no logos" in prompts["back"]
    assert look_lock.no_eyewear("round tortoiseshell eyeglasses") == "none"
    assert look_lock.no_eyewear("silver watch, black-framed glasses and a leather belt") == \
        "silver watch, a leather belt"
    assert look_lock.no_eyewear("black wristband") == "black wristband"
    assert look_lock.visible_clothing({"top": "white t-shirt", "accessories":
                                       "round tortoiseshell eyeglasses"})["accessories"] == "none"
    plain = jobs._turnaround_prompts(card, {**lock, "top": "black t-shirt"},
                                     identity="exact likeness of Marina")
    assert "No text, no labels, no logos" in plain["master"]
    sheen = look_lock.identity_text({**LOOK, "hair_colour": "near-black",
                                     "hair_sheen": "faint warm reddish tint in bright light"})
    assert "near-black hair, uniformly dark from roots to ends, only faint warm reddish tint" in sheen
    assert "highlights" not in sheen
    dyed = look_lock.identity_text({**LOOK, "hair_dye": "bright copper dyed ends"})
    assert "jet black hair with bright copper dyed ends" in dyed
    full = look_lock.identity_text(LOOK)
    assert "round face with full cheeks" in full and "thick dark arched eyebrows" in full
    assert "full lips" in full and "eyes: dark brown" in full
    from app import image_client
    assert "text, letters, words, writing" in image_client._STANDARD_NEGATIVE


def test_reader_labels_every_image_and_keeps_three_face_picks(tmp_db):
    calls: list = []
    reply = json.dumps({**LOOK, "top": "black tee with white text print", "face_images": [4, "x", 2, 4, 9]})
    from app import settings_store
    with db.session() as conn:
        llm_cfg = settings_store.effective_llm_settings(conn)
    look = look_lock._read([PNG_BYTES, PNG_BYTES], transport=_vision_transport(reply, calls),
                           llm_cfg=llm_cfg)
    assert look["face_images"] == [4, 2, 9]
    assert look["top"] == "black tee"                  # letras ilegíveis: peça lisa
    labels = [part["text"] for part in calls[0]["messages"][1]["content"] if part["type"] == "text"]
    assert labels[1:] == ["Image 1:", "Image 2:"]

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
    assert len(calls) == 3                           # falha + traços + roupa; gravado: não relê


def test_multi_person_clause_keeps_everyones_glasses_within_the_see_budget(tmp_db):
    """Três pessoas com look: a descrição livre sai, os traços curtos (óculos
    primeiro) entram e cabem nos 700 caracteres do See."""
    from app.routes.images import _CLAUSE_CHARS, clip_words

    adv = make_adventure()
    cards = []
    for name, extra in (("Renata Lemos", {}),
                        ("Ladrão de Jibóia", {"glasses": "none", "facial_hair": "goatee"}),
                        ("Caio Calça-Quadrada", {"glasses": "none", "face": "skull mask"})):
        sheet = {"look_lock": {**LOOK, **extra, "version": look_lock.LOOK_VERSION, "basis_hash": "x"}}
        with db.session() as conn:
            cards.append(db.insert_card(
                conn, adv["id"], type="character", title=name,
                entry="A stout pirate with a large belly, messy gray hair and a dirty torn shirt.",
                sheet=json.dumps(sheet)))
    clause = clip_words(likeness.opening_clause(cards), _CLAUSE_CHARS)
    assert "Renata Lemos: glasses: large round thin gold-rimmed glasses" in clause
    assert "face: skull mask" in clause
    assert "Caio Calça-Quadrada: no glasses; face: skull mask" in clause   # óculos não vazam
    assert "stout pirate" not in clause                    # o look manda sobre o texto
    assert "skin:" not in clause                            # versão curta


def test_short_look_puts_the_beard_before_the_face_text():
    caio = {**LOOK, "glasses": "none", "facial_hair": "full dark beard and mustache",
            "face": "clean-shaven except for beard, smiling"}
    text = look_lock.identity_text(caio, compact=True)
    assert text.startswith("no glasses; full dark beard and mustache; face:")


def test_photo_wins_on_body_and_the_description_fills_the_rest():
    photo = {**LOOK, "build": "petite and slim", "height": "not visible", "waist": "very narrow",
             "hips": "slightly wider than the waist", "shoulders": "not visible", "limbs": "slender"}
    stated = {"build": "slim", "height": "about 1.55 m", "waist": "narrow",
              "hips": "pronounced hips", "shoulders": "narrow shoulders", "limbs": "not stated"}
    merged = look_lock.merge_body(photo, stated)
    assert merged["hips"] == "slightly wider than the waist"          # a foto vence
    assert merged["height"] == "about 1.55 m"                          # altura: o texto
    assert merged["shoulders"] == "narrow shoulders"                   # a foto não mostra
    assert merged["limbs"] == "slender"
    assert merged["body_sources"]["shoulders"] == "description"
    # sem foto de corpo (só retrato de busto) o corpo vem todo da descrição
    bust = {**LOOK, **{key: "not visible" for key in look_lock.BODY_KEYS}}
    filled = look_lock.merge_body(bust, stated)
    assert filled["build"] == "slim" and filled["hips"] == "pronounced hips"


def test_levels_budget_the_detail_for_each_render():
    look = {**LOOK, "hair_curl": "tight springy ringlets, small curls", "hair_volume": "very voluminous",
            "skin_tone": "medium tan with warm undertone", "skin_marks": "small mole above the left lip",
            "nose": "rounded tip", "smile": "cheeks lift into round apples",
            "build": "petite and slim", "waist": "very narrow", "hips": "slightly wider than the waist",
            "height": "about 1.55 m", "shoulders": "narrow", "limbs": "slender arms and legs"}
    full = look_lock.identity_text(look, level="full")
    face = look_lock.identity_text(look, level="face")
    views = look_lock.identity_text(look, level="views")
    scene = look_lock.identity_text(look, level="scene")
    compact = look_lock.identity_text(look, level="compact")
    assert "marks: small mole above the left lip" in full and "nose: rounded tip" in full
    assert "body: petite and slim build" in full and "body:" not in face
    assert "very narrow waist" in views and "mole" not in views
    assert "when smiling: cheeks lift into round apples" in scene
    assert compact.startswith("glasses: large round thin gold-rimmed glasses") and "very narrow" in compact
    assert len(full) > len(face) and len(full) > len(views) > len(compact)
    # sem look nada entra (card sem foto e sem âncora lida)
    assert look_lock.identity_text(None, level="scene") == ""


def test_turnaround_prompts_carry_full_detail_on_master_and_body_on_views(tmp_db):
    adv = make_adventure()
    look = {**LOOK, "nose": "rounded tip", "skin_marks": "small mole on the chin",
            "build": "petite and slim", "waist": "very narrow",
            "version": look_lock.LOOK_VERSION, "basis_hash": "x", "source": "photo"}
    with db.session() as conn:
        card = db.insert_card(conn, adv["id"], type="character", title="Renata Lemos",
                              entry="Long hair reaching her back.",
                              sheet=json.dumps({"look_lock": look}))
    prompts = jobs._turnaround_prompts(card, {"top": "black tee", "bottom": "black jeans",
                                              "footwear": "grey sneakers", "accessories": "none"},
                                       identity="exact likeness of Renata Lemos")
    assert "small mole on the chin" in prompts["master"] and "nose: rounded tip" in prompts["master"]
    assert "small mole on the chin" in prompts["face"] and "body:" not in prompts["face"]
    for kind in ("front", "profile", "back", "clip"):
        assert "very narrow waist" in prompts[kind] and "mole" not in prompts[kind]
    assert "reaching her back" not in prompts["master"]               # o texto do card não manda


def test_glasses_wearers_get_the_best_face_and_the_glasses_photo(tmp_db, tmp_path, monkeypatch):
    adv = make_adventure()
    card = _photo_card(adv["id"], tmp_path, monkeypatch, (10, 10, 10), (200, 150, 120), (0, 0, 200))
    sheet = json.loads(card["sheet"])
    sheet["look_lock"] = {**LOOK, "face_images": [1, 2], "glasses_image": 3, "source": "photo",
                          "version": look_lock.LOOK_VERSION, "basis_hash": "x"}
    with db.session() as conn:
        card = db.update_card(conn, adv["id"], card["id"], {"sheet": json.dumps(sheet)})
    monkeypatch.setattr(look_lock, "_frontal_face", lambda photo: True)
    colors = [Image.open(io.BytesIO(p)).convert("RGB").getpixel((5, 5)) for p in look_lock.face_photos(card)]
    assert colors[0][0] < 40 and colors[1][2] > 150                # rosto 1, depois a de óculos
    sheet["look_lock"]["glasses"] = "none"
    with db.session() as conn:
        card = db.update_card(conn, adv["id"], card["id"], {"sheet": json.dumps(sheet)})
    colors = [Image.open(io.BytesIO(p)).convert("RGB").getpixel((5, 5)) for p in look_lock.face_photos(card)]
    assert colors[1][0] > 150                                       # sem óculos: as duas melhores


def test_masked_faces_keep_the_mask_and_drop_invented_features():
    caio = {**LOOK, "glasses": "none", "face": "bone-white skull mask covering the whole face",
            "lips": "full lips", "face_shape": "square jaw", "eyebrows": "thick dark brows",
            "smile": "wide grin", "build": "athletic", "waist": "narrow"}
    for level in ("compact", "scene", "views", "face", "full"):
        text = look_lock.identity_text(caio, level=level)
        assert "bone-white skull mask" in text
        assert "full lips" not in text and "square jaw" not in text and "grin" not in text
    assert look_lock.identity_text(caio, level="compact").startswith(
        "no glasses; face: bone-white skull mask")


def test_backdrop_worker_reads_the_looks_its_cast_is_missing(tmp_db, monkeypatch):
    """O prompt do fundo nasce sem LLM; quem pinta lê o look que falta para o
    próximo fundo (produção, depois da v8: Caio sem linha e sem barba)."""
    adv = make_adventure()
    with db.session() as conn:
        card = db.insert_card(conn, adv["id"], title="Bruno", type="character", entry="")
    seen: list[list[str]] = []
    monkeypatch.setattr(look_lock, "ensure_many",
                        lambda adventure_id, cards, **kwargs: seen.append(
                            [item["id"] for item in cards]) or cards)
    job = {"id": "j1", "adventure_id": adv["id"],
           "payload": {"cast_card_ids": [card["id"], "gone"]}}
    jobs._read_missing_looks(job, None, {})
    assert seen == [[card["id"]]]
    jobs._read_missing_looks({"id": "j2", "adventure_id": adv["id"], "payload": {}}, None, {})
    assert len(seen) == 1                       # fundo sem elenco não lê nada

    def broken(*args, **kwargs):
        raise RuntimeError("vision offline")
    monkeypatch.setattr(look_lock, "ensure_many", broken)
    jobs._read_missing_looks(job, None, {})     # leitura quebrada não derruba o fundo
