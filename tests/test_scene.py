"""Cena do chat: lugar, clima, elenco e fundo enfileirado."""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path

import httpx

from app import db, image_client, jobs, likeness, scene
from conftest import add_action, image_english_echo, make_adventure
from test_v2 import PNG_BYTES, override_transport
from test_v3 import use_tmp_storage


def _faces_dir() -> Path:
    root = Path(os.environ["AIDUNGEON_DB_PATH"]).parent / "faces"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _face(adventure_id: str, card_id: str, content: bytes) -> dict:
    """Grava um PNG e aponta âncora e retrato do card para ele."""
    path = _faces_dir() / f"{card_id}.png"
    path.write_bytes(content)
    with db.session() as conn:
        image = db.insert_image(
            conn, adventure_id=adventure_id, action_seq=-1,
            file_path=str(path), provider="test", width=8, height=8,
            kind="portrait", ref_id=card_id,
        )
        db.set_card_portrait(conn, card_id, image["id"], anchor_id=image["id"])
    return image


def pipeline_transport(chat_content: str = "silver-haired sky captain, bust portrait",
                       capture: dict | None = None) -> httpx.MockTransport:
    """LLM e ComfyUI. `capture['uploads']` guarda os PNGs na ordem do elenco."""

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/models"):
            return httpx.Response(200, json={
                "object": "list",
                "data": [{"id": "mock-model", "object": "model"}],
            })
        if path.endswith("/chat/completions"):
            payload = json.loads(request.content.decode("utf-8"))
            if not payload.get("stream"):
                echo = image_english_echo(payload)
                if echo is not None:
                    return httpx.Response(200, json={
                        "choices": [{"message": {"content": echo}}]})
            return httpx.Response(200, json={
                "choices": [{"message": {"content": chat_content}}],
            })
        if path.endswith("/upload/image"):
            if capture is not None:
                capture.setdefault("uploads", []).append(request.content)
            return httpx.Response(200, json={
                "name": "ref.png", "subfolder": "", "type": "input"})
        if path.endswith("/prompt"):
            body = json.loads(request.content.decode("utf-8"))
            if capture is not None:
                capture.setdefault("graphs", []).append(body["prompt"])
            return httpx.Response(200, json={
                "prompt_id": "pid-1", "number": 1, "node_errors": {}})
        if path.startswith("/history/"):
            pid = path.rstrip("/").rsplit("/", 1)[-1]
            return httpx.Response(200, json={pid: {
                "status": {"status_str": "success", "completed": True},
                "outputs": {"16": {"images": [{
                    "filename": "out.png", "subfolder": "", "type": "output",
                }]}},
            }})
        if path.endswith("/view"):
            return httpx.Response(200, content=PNG_BYTES)
        return httpx.Response(404, json={"error": f"no mock route: {path}"})

    return httpx.MockTransport(handler)


def _stub_painter(monkeypatch, tmp_path) -> None:
    """Prompt e âncora ficam no teste. O executor só grava o PNG."""

    def fake_generate(cfg, prompt, *, adventure_id, image_id, **kwargs):
        directory = tmp_path / "imgs" / adventure_id
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{image_id}.png"
        path.write_bytes(PNG_BYTES)
        return {
            "file_path": str(path),
            "width": int(kwargs.get("width") or 1024),
            "height": int(kwargs.get("height") or 1024),
            "provider": "comfyui",
        }

    monkeypatch.setattr(image_client, "generate_image", fake_generate)


def _use_comfy() -> None:
    with db.session() as conn:
        db.set_setting(conn, "image.provider", json.dumps("comfyui"))


def _seed(text="You enter the tavern at night. Ada smiles, happy to see you."):
    adv = make_adventure()
    with db.session() as conn:
        db.insert_card(conn, adv["id"], type="location", title="The Tavern",
                       triggers=["tavern"], entry="A smoky common room with a hearth.")
        db.insert_card(conn, adv["id"], type="character", title="Ada",
                       entry="The barkeep.")
    add_action(adv["id"], 0, "start", text)
    return adv


def test_scene_paints_backdrop_and_portrait(client, tmp_path, monkeypatch):
    use_tmp_storage(tmp_path)
    _stub_painter(monkeypatch, tmp_path)
    adv = _seed()
    resp = client.post(f"/api/adventures/{adv['id']}/scene", json={})
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["location"] == "The Tavern"
    assert data["atmosphere"] == "night"
    assert data["backdrop"]["status"] == "pending"
    assert data["cast"][0]["title"] == "Ada"
    assert data["cast"][0]["mood"] == "happy"
    assert data["cast"][0]["portrait"] is None
    with db.session() as conn:
        backdrop_job = next(item for item in db.list_jobs(conn, adv["id"])
                            if item["type"] == "scene_backdrop")
    assert "ada" not in backdrop_job["payload"]["prompt"].lower()
    assert "no people" in backdrop_job["payload"]["prompt"].lower()

    assert jobs.process_pending(pipeline_transport()) == 2

    # O retrato acabou de gravar o visual. A sala vazia não serve mais:
    # a próxima consulta pede a mesma Ada, com essa cara.
    resp = client.post(f"/api/adventures/{adv['id']}/scene", json={})
    data = resp.json()
    assert data["cast"][0]["portrait"]["id"]
    assert data["backdrop"]["status"] == "pending"
    with db.session() as conn:
        repaint = next(item for item in reversed(db.list_jobs(conn, adv["id"]))
                       if item["type"] == "scene_backdrop" and item["status"] == "pending")
    assert repaint["payload"]["include_people"] is True
    assert "silver-haired" in repaint["payload"]["prompt"].lower()

    assert jobs.process_pending(pipeline_transport()) == 1
    resp = client.post(f"/api/adventures/{adv['id']}/scene", json={})
    data = resp.json()
    assert data["backdrop"]["status"] == "ready"
    assert data["backdrop"]["image"]["url"].startswith("/api/images/")
    with db.session() as conn:
        image = db.get_image(conn, data["backdrop"]["image"]["id"])
        assert image["kind"] == "backdrop"
        assert image["width"] == 1344
        assert image["height"] == 768
        assert "wrong hair" in image["negative_prompt"]
        assert "silver-haired" in image["prompt"].lower()
        # mesma cara: não enfileira de novo
        assert len(db.list_jobs(conn, adv["id"])) == 3


def test_scene_paints_the_card_face_not_the_prose(client, tmp_path, monkeypatch):
    use_tmp_storage(tmp_path)
    _stub_painter(monkeypatch, tmp_path)
    adv = make_adventure()
    with db.session() as conn:
        card = db.insert_card(
            conn, adv["id"], type="character", title="Princess Alusair",
            triggers=["Alusair"], entry="Steel Regent of Cormyr.",
            sheet=json.dumps({
                "appearance": "long red hair, green eyes",
                "clothing": "plate armor",
            }),
        )
    _face(adv["id"], card["id"], b"\x89PNG\r\nalusair-face")
    add_action(adv["id"], 0, "continue",
               "Princess Alusair reclines in a white gown, dark hair loose.")
    resp = client.post(f"/api/adventures/{adv['id']}/scene", json={})
    assert resp.status_code == 200, resp.text
    with db.session() as conn:
        job = next(item for item in db.list_jobs(conn, adv["id"])
                   if item["type"] == "scene_backdrop")
    prompt = job["payload"]["prompt"].lower()
    assert job["payload"]["include_people"] is True
    assert "long red hair" in prompt
    assert "plate armor" in prompt
    assert "white gown" not in prompt
    assert "dark hair" not in prompt
    assert "no people" not in prompt
    assert jobs.process_pending(pipeline_transport()) >= 1
    with db.session() as conn:
        image = db.latest_image(conn, adv["id"], "backdrop")
    assert "wrong hair" in image["negative_prompt"]
    assert "person, people" not in image["negative_prompt"]


def test_explicit_scene_does_not_restore_the_outfit(client, tmp_path, monkeypatch):
    use_tmp_storage(tmp_path)
    _stub_painter(monkeypatch, tmp_path)
    adv = make_adventure()
    with db.session() as conn:
        card = db.insert_card(
            conn, adv["id"], type="character", title="Princess Alusair",
            triggers=["Alusair"], entry="Steel Regent of Cormyr.",
            sheet=json.dumps({
                "appearance": "long red hair, green eyes",
                "clothing": "plate armor",
            }),
        )
    _face(adv["id"], card["id"], PNG_BYTES)
    add_action(adv["id"], 0, "continue", "Princess Alusair is naked on the bed.")
    resp = client.post(f"/api/adventures/{adv['id']}/scene", json={})
    assert resp.status_code == 200, resp.text
    with db.session() as conn:
        job = next(item for item in db.list_jobs(conn, adv["id"])
                   if item["type"] == "scene_backdrop")
    prompt = job["payload"]["prompt"].lower()
    assert job["payload"]["explicit"] is True
    assert "naked" in prompt
    assert "plate armor" not in prompt
    assert "long red hair" in prompt
    assert jobs.process_pending(pipeline_transport()) >= 1
    with db.session() as conn:
        image = db.latest_image(conn, adv["id"], "backdrop")
    assert "costume change" not in image["negative_prompt"].lower()
    assert "nude" not in image["negative_prompt"].lower()


def test_explicit_backdrop_locks_the_vampire_face(client, tmp_path, monkeypatch):
    use_tmp_storage(tmp_path)
    _stub_painter(monkeypatch, tmp_path)
    adv = make_adventure()
    with db.session() as conn:
        card = db.insert_card(
            conn, adv["id"], type="character", title="Selene",
            triggers=["Selene", "vampira"], entry="A vampira.",
            portrait_prompt=(
                "Selene is a pale gothic vampiress with long black hair and dark eyes, "
                "wearing a black velvet dress, captured in a bust portrait against a neutral background."
            ),
            sheet=json.dumps({
                "appearance": (
                    "Pele pálida como mármore, lábios vermelhos vibrantes, "
                    "cabelos pretos longos e lisos, olhos escuros profundos"
                ),
                "body": "Estrutura fina mas atlética, postura elegante e fluida",
                "clothing": "Vestido de veludo preto",
            }),
        )
    _face(adv["id"], card["id"], PNG_BYTES)
    add_action(adv["id"], 0, "continue", "Selene is naked on the sofa, rain on the window.")
    resp = client.post(f"/api/adventures/{adv['id']}/scene", json={})
    assert resp.status_code == 200, resp.text
    with db.session() as conn:
        job = next(item for item in db.list_jobs(conn, adv["id"])
                   if item["type"] == "scene_backdrop")
    prompt = job["payload"]["prompt"]
    head = prompt.lower()
    assert job["payload"]["explicit"] is True
    assert head.index("black hair") < head.index("naked")
    assert head.index("pale") < head.index("naked")
    assert "dark eyes" in head
    assert "velvet" not in head
    assert "bust portrait" not in head
    assert "pele pálida" not in head
    assert jobs.process_pending(pipeline_transport()) >= 1
    with db.session() as conn:
        image = db.latest_image(conn, adv["id"], "backdrop")
    negative = image["negative_prompt"].lower()
    assert "wrong skin tone" in negative
    assert "different person" in negative
    assert "nude" not in negative


def test_scene_new_atmosphere_paints_again(client, tmp_path):
    use_tmp_storage(tmp_path)
    adv = _seed()
    client.post(f"/api/adventures/{adv['id']}/scene", json={})
    jobs.process_pending(pipeline_transport())

    add_action(adv["id"], 1, "continue", "Fire engulfs the tavern. Ada is afraid.")
    resp = client.post(f"/api/adventures/{adv['id']}/scene", json={})
    assert resp.status_code == 200, resp.text
    assert "fire" in resp.json()["atmosphere"]
    assert resp.json()["backdrop"]["status"] == "pending"
    assert resp.json()["cast"][0]["mood"] == "fear"


def test_scene_error_does_not_requeue_until_retry(client, tmp_path):
    use_tmp_storage(tmp_path)
    adv = _seed("The tavern is quiet.")
    first = client.post(f"/api/adventures/{adv['id']}/scene", json={}).json()
    assert first["backdrop"]["status"] == "pending"
    with db.session() as conn:
        job = next(item for item in db.list_jobs(conn, adv["id"])
                   if item["type"] == "scene_backdrop")
        conn.execute("UPDATE jobs SET status='running' WHERE id=?", (job["id"],))
        assert db.finish_job(conn, job["id"], "error", error="boom")

    again = client.post(f"/api/adventures/{adv['id']}/scene", json={}).json()
    assert again["backdrop"]["status"] == "error"
    with db.session() as conn:
        assert len(db.list_jobs(conn, adv["id"])) == 1

    retried = client.post(f"/api/adventures/{adv['id']}/scene", json={"retry": True}).json()
    assert retried["backdrop"]["status"] == "pending"
    with db.session() as conn:
        assert sum(1 for j in db.list_jobs(conn, adv["id"]) if j["type"] == "scene_backdrop") == 2


def test_scene_disabled_does_not_enqueue(client):
    adv = _seed()
    with db.session() as conn:
        db.set_setting(conn, "image.enabled", json.dumps(False))
    resp = client.post(f"/api/adventures/{adv['id']}/scene", json={})
    assert resp.status_code == 200, resp.text
    assert resp.json()["backdrop"]["status"] == "disabled"
    with db.session() as conn:
        assert db.list_jobs(conn, adv["id"]) == []


def test_scene_unknown_adventure_404(client):
    resp = client.post("/api/adventures/does-not-exist/scene", json={})
    assert resp.status_code == 404


# ------------------------------------------------------- See e fundo: a mesma cena
_FAIRY_PROMPT = (
    "Elara is portrayed as a slender, agile fairy queen with a graceful posture, "
    "wearing translucent silk and crystal garments that shimmer against a neutral "
    "background. Her hair floats around her head like stars, and her amber eyes "
    "gaze forward with an intense, captivating expression in a bust portrait."
)


def _fairy(adventure_id: str) -> None:
    with db.session() as conn:
        card = db.insert_card(
            conn, adventure_id, type="character", title="Elara",
            triggers=["Elara", "rainha"], entry="Rainha de cristal.",
            portrait_prompt=_FAIRY_PROMPT,
        )
    _face(adventure_id, card["id"], PNG_BYTES)


def _fairy_protest(adventure_id: str) -> None:
    """A fada vestida e o jogador protestando contra uma rainha nua."""
    _fairy(adventure_id)
    add_action(adventure_id, 0, "start", "Elara espera na floresta de cristal.")
    add_action(adventure_id, 1, "say",
               "Elara, tenha decência! Crie uma roupa de cristal, não posso me "
               "vingar lutando com uma rainha nua!")
    add_action(adventure_id, 2, "continue",
               "Elara recua um passo, ainda vestida de seda e cristal, e ergue as mãos.")


def _backdrop_job(adventure_id: str) -> dict:
    with db.session() as conn:
        return next(item for item in db.list_jobs(conn, adventure_id)
                    if item["type"] == "scene_backdrop")


def _see(client, adventure_id: str, action_seq: int, monkeypatch, tmp_path) -> str:
    _stub_painter(monkeypatch, tmp_path)
    override_transport(client, pipeline_transport(
        "Elara stands in the crystal forest, wearing translucent silk "
        "and crystal garments, hair floating like stars."))
    try:
        resp = client.post(f"/api/adventures/{adventure_id}/see",
                           json={"action_seq": action_seq})
    finally:
        client.app.dependency_overrides.clear()
    assert resp.status_code == 200, resp.text
    return resp.json()["prompt"]


def test_the_players_protest_does_not_paint_the_fairy_nude(client, tmp_path, monkeypatch):
    use_tmp_storage(tmp_path)
    _stub_painter(monkeypatch, tmp_path)
    adv = make_adventure()
    _fairy_protest(adv["id"])
    resp = client.post(f"/api/adventures/{adv['id']}/scene", json={})
    assert resp.status_code == 200, resp.text
    job = _backdrop_job(adv["id"])
    prompt = job["payload"]["prompt"].lower()
    assert job["payload"]["explicit"] is False
    assert "wearing translucent silk and crystal garments" in prompt
    assert "hair floats around her head like stars" in prompt
    assert "nude" not in prompt
    assert "naked" not in prompt
    assert "clothes off" not in prompt
    assert jobs.process_pending(pipeline_transport()) >= 1
    with db.session() as conn:
        image = db.latest_image(conn, adv["id"], "backdrop")
    assert "nude" in image["negative_prompt"].lower()


def test_a_hypothetical_nudity_does_not_paint_the_fairy_nude(client, tmp_path):
    """A fada fala em condicional ("se estiver nua") e continua vestida."""
    use_tmp_storage(tmp_path)
    adv = make_adventure()
    _fairy(adv["id"])
    add_action(adv["id"], 0, "continue",
               "O vestido surge das profundezas da floresta: camadas fluidas que "
               "brilham sob o frio cortante, bordados em fios finos feitos com "
               "cristais. A decência é apenas uma ilusão — sussurra Elara enquanto "
               "ajusta a nova camada de seda translúcida, os ombros agora "
               "protegidos por um colar. E não posso me vingar com elegância se "
               "estiver nua!")
    resp = client.post(f"/api/adventures/{adv['id']}/scene", json={})
    assert resp.status_code == 200, resp.text
    job = _backdrop_job(adv["id"])
    prompt = job["payload"]["prompt"].lower()
    assert job["payload"]["explicit"] is False
    assert "wearing translucent silk and crystal garments" in prompt
    assert "nude" not in prompt
    assert "clothes off" not in prompt


def test_the_see_and_the_backdrop_paint_the_same_scene(client, tmp_path, monkeypatch):
    use_tmp_storage(tmp_path)
    adv = make_adventure()
    _fairy_protest(adv["id"])
    client.post(f"/api/adventures/{adv['id']}/scene", json={})
    backdrop = _backdrop_job(adv["id"])["payload"]["prompt"]
    seen = _see(client, adv["id"], 2, monkeypatch, tmp_path)

    for prompt in (backdrop, seen):
        folded = prompt.lower()
        assert "wearing translucent silk and crystal garments" in folded
        assert "hair floats around her head like stars" in folded
        assert "nude" not in folded
        assert "naked" not in folded
        assert "clothes off" not in folded


def test_stateful_action_injects_each_state_right_after_the_name():
    from app.routes.images import _stateful_action

    sentence = "Caio stands in the apartment, while A Donzela da Noite gently touches his chest."
    states = [
        ("A Donzela da Noite",
         "completely naked, bare breasts fully exposed with visible nipples, "
         "natural female anatomy"),
        ("Caio", "fully clothed in a sharply tailored black suit"),
    ]
    action = _stateful_action(sentence, states)
    assert (
        "Caio, fully clothed in a sharply tailored black suit, stands in the apartment"
        in action
    )
    assert (
        "A Donzela da Noite, completely naked, bare breasts fully exposed with "
        "visible nipples, natural female anatomy, gently touches" in action
    )
    assert ", while" not in action
    assert "  " not in action


def test_short_outfit_keeps_color_and_garment_only():
    from app.routes.images import _clothed_predicate, _short_outfit

    assert _short_outfit(
        "a sharply tailored black or dark grey suit") == "black suit"
    assert _short_outfit(
        "white ribbed off-the-shoulder top with a keyhole cutout") == "white top"
    card = {
        "title": "Caio",
        "sheet": json.dumps({
            "appearance": "Homem de aparência imortal, pele pálida.",
            "clothing": "a sharply tailored black or dark grey suit",
        }),
    }
    assert _clothed_predicate(card, "Caio") == (
        "fully clothed in his black suit")
    woman = {
        "title": "A Donzela",
        "sheet": json.dumps({
            "appearance": "Uma mulher de pele clara.",
            "clothing": "a black lace dress",
        }),
    }
    assert _clothed_predicate(woman, "A Donzela") == (
        "fully clothed in her black dress")


def test_stateful_action_tolerates_the_article_and_appends_missing_people():
    from app.routes.images import _stateful_action

    states = [
        ("A Donzela da Noite", "completely naked"),
        ("Caio", "fully clothed in his black suit"),
    ]
    sentence = "The Donzela da Noite dances by the window."
    action = _stateful_action(sentence, states)
    assert (
        "The Donzela da Noite, completely naked, dances by the window"
        in action
    )
    # Quem não aparece na frase não vira sujeito: entra no fim, sem inverter
    # a oração principal.
    assert action.endswith("Caio is fully clothed in his black suit")
    assert action.startswith("The Donzela da Noite")

    only_sentence = "She dances by the window."
    only = _stateful_action(only_sentence, states)
    assert only.startswith("She dances by the window")
    assert "A Donzela da Noite is completely naked" in only
    assert only.index("She dances") < only.index("A Donzela da Noite")


def test_staged_explicit_action_gives_the_nude_woman_a_full_body_stance():
    from app.routes.images import _staged_explicit_action, scrub_clothing_states

    nude = "A Donzela da Noite"
    clothed = "Caio"
    predicates = {
        nude: (
            "completely naked, bare breasts fully exposed with visible nipples, "
            "natural female anatomy, her whole body bare, natural skin"
        ),
        clothed: "fully clothed in his black suit",
    }
    pronouns = {nude: "her", clothed: "his"}
    sentence = (
        "A Donzela da Noite, completely naked, bare breasts fully exposed with "
        "visible nipples, natural female anatomy, her whole body bare, natural "
        "skin, traces Caio's clavicle with her black nails while he, having "
        "shed his suit, stands beside her in the neon-lit room."
    )
    cleaned = scrub_clothing_states(sentence)
    assert "shed" not in cleaned
    action = _staged_explicit_action(
        cleaned, [nude], [clothed], predicates, pronouns)
    assert action == (
        "A Donzela da Noite, completely naked, bare breasts fully exposed with "
        "visible nipples, natural female anatomy, her whole body bare, natural "
        "skin, stands in the center of the room, her arms raised as she traces "
        "Caio's clavicle with her black nails, while Caio, fully clothed in his "
        "black suit, standing before her."
    )


def test_action_core_drops_the_nude_predicate_and_keeps_only_the_verb():
    from app.routes.images import _action_core

    assert _action_core(
        "The Donzela da Noite, breasts fully exposed with visible nipples, "
        "natural female anatomy, natural skin, traces Caio's clavicle with "
        "her black nails",
        "A Donzela da Noite",
    ) == "traces Caio's clavicle with her black nails"
    # O scrub de "completely naked" deixa o advérbio órfão; ele não é ação.
    assert _action_core(
        "A Donzela da Noite, completely, traces Caio's jaw",
        "A Donzela da Noite",
    ) == "traces Caio's jaw"


def test_staged_explicit_action_keeps_lying_and_sitting_postures():
    from app.routes.images import _staged_explicit_action, scrub_clothing_states

    nude = "A Donzela da Noite"
    clothed = "Caio"
    predicates = {
        nude: "completely naked, natural skin",
        clothed: "fully clothed in his black suit",
    }
    pronouns = {nude: "her", clothed: "his"}
    for sentence in (
        "A Donzela da Noite lies on the bed while Caio watches.",
        "A Donzela da Noite is sitting on the sofa while Caio watches.",
        "A Donzela da Noite kneels on the floor while Caio watches.",
    ):
        cleaned = scrub_clothing_states(sentence)
        assert _staged_explicit_action(
            cleaned, [nude], [clothed], predicates, pronouns) == ""


def test_a_nude_narration_paints_the_see_and_the_backdrop_nude(client, tmp_path, monkeypatch):
    use_tmp_storage(tmp_path)
    adv = make_adventure()
    _fairy(adv["id"])
    add_action(adv["id"], 0, "continue",
               "Elara desliza o vestido de seda e fica nua, a pele na luz da lua.")
    client.post(f"/api/adventures/{adv['id']}/scene", json={})
    job = _backdrop_job(adv["id"])
    backdrop = job["payload"]["prompt"]
    seen = _see(client, adv["id"], 0, monkeypatch, tmp_path)

    assert job["payload"]["explicit"] is True
    for prompt in (backdrop, seen):
        folded = prompt.lower()
        assert "completely naked" in folded
        # Elara é "adult woman" pelo retrato ("her"): o predicado dela é o
        # feminino ("whole body bare"), que não usa a palavra nude.
        assert "nude" in folded or "whole body bare" in folded
        assert "translucent silk and crystal garments" not in folded


def test_a_salamander_is_not_painted_as_a_naked_woman(client, tmp_path, monkeypatch):
    """O nome curto do plot não vira uma segunda pessoa humana."""
    use_tmp_storage(tmp_path)
    adv = make_adventure(
        plot_essentials="[Name: Sandra; Role: Salamandra; Appearance: escamas âmbar]",
        image_style="dark fantasy, amphibian characters",
    )
    with db.session() as conn:
        card = db.insert_card(
            conn, adv["id"], type="character",
            title="Sandra, A Salamandra",
            entry="Realmente uma salamandra que se chama sandra.",
            use_for_character_creation=True,
            portrait_prompt="Sandra the Salamander, an ancient amphibian, amber scales, long tail",
            sheet=json.dumps({"class": "salamandra", "clothing": ""}),
        )
    _face(adv["id"], card["id"], b"\x89PNG\r\nsalamander-face")
    _stub_painter(monkeypatch, tmp_path)
    add_action(adv["id"], 0, "start", "Sandra está no escritório pantanoso.")
    add_action(adv["id"], 1, "do", "> You pular até o teto com os cristais")
    add_action(adv["id"], 2, "continue",
               "Sandra salta e a cauda bate na mesa. Ela segura os cristais.")
    override_transport(client, pipeline_transport(
        "Sandra leaps to the ceiling clutching the crystals."))
    try:
        resp = client.post(f"/api/adventures/{adv['id']}/see", json={"action_seq": 2})
    finally:
        client.app.dependency_overrides.clear()
    assert resp.status_code == 200, resp.text
    prompt = resp.json()["prompt"].lower()
    assert "adult woman" not in prompt
    assert "salamander" in prompt
    assert "not a human" in prompt
    assert "natural hide" in prompt
    assert "nude" not in prompt
    assert "naked" not in prompt
    assert "clothes off" not in prompt
    assert "natural skin texture" not in prompt
    with db.session() as conn:
        negative = db.get_image(conn, resp.json()["id"])["negative_prompt"].lower()
    assert "nude" in negative
    assert "naked" in negative
    assert "nipples" in negative


def _reference_image_nodes(graph: dict) -> list:
    qwen = next((node["inputs"] for node in graph.values()
                 if node["class_type"] == "TextEncodeQwenImage21"), None)
    if qwen is not None:
        return [qwen["images"][key] for key in sorted(qwen["images"])]
    positive = next(node["inputs"]["positive"] for node in graph.values()
                    if node["class_type"] == "CFGGuider")
    references = []
    while graph[str(positive[0])]["class_type"] == "ReferenceLatent":
        reference = graph[str(positive[0])]["inputs"]
        encode = graph[str(reference["latent"][0])]["inputs"]
        scale = graph[str(encode["pixels"][0])]["inputs"]
        load_id = str(scale["image"][0])
        assert graph[load_id]["class_type"] == "LoadImage"
        references.append([load_id, 0])
        positive = reference["conditioning"]
    return list(reversed(references))


def test_missing_portrait_does_not_paint_a_partial_scene(client, tmp_path):
    use_tmp_storage(tmp_path)
    _use_comfy()
    adv = make_adventure()
    with db.session() as conn:
        nia = db.insert_card(
            conn, adv["id"], type="character", title="Nia", triggers=["Nia"],
            entry="Ranger.", portrait_prompt="short black hair",
        )
        bob = db.insert_card(
            conn, adv["id"], type="character", title="Bob", triggers=["Bob"],
            entry="Smith.", portrait_prompt="grey beard",
        )
    _face(adv["id"], nia["id"], b"\x89PNG\r\nnia-only")
    add_action(adv["id"], 0, "continue", "Nia and Bob wait by the door.")
    capture: dict = {}
    override_transport(client, pipeline_transport("Nia and Bob wait.", capture))
    try:
        seen = client.post(f"/api/adventures/{adv['id']}/see", json={"action_seq": 0})
    finally:
        client.app.dependency_overrides.clear()
    assert seen.status_code == 409, seen.text
    assert seen.json()["error"]["code"] == "portrait_required"
    assert capture.get("uploads", []) == []
    with db.session() as conn:
        portraits = [job for job in db.list_jobs(conn, adv["id"])
                     if job["type"] == "character_portrait"]
        assert [job["ref_id"] for job in portraits] == [bob["id"]]
        assert not any(job["type"] == "scene_backdrop" for job in db.list_jobs(conn, adv["id"]))
    scene = client.post(f"/api/adventures/{adv['id']}/scene", json={})
    assert scene.status_code == 200, scene.text
    assert scene.json()["backdrop"]["error"]
    with db.session() as conn:
        assert not any(
            job["type"] == "scene_backdrop" and job["status"] == "pending"
            for job in db.list_jobs(conn, adv["id"])
        )


def test_async_see_fails_portrait_required_after_queueing(client, monkeypatch):
    monkeypatch.setattr(jobs, "SEE_PORTRAIT_WAIT_S", 0.0)
    adv = make_adventure()
    with db.session() as conn:
        card = db.insert_card(
            conn, adv["id"], type="character", title="Nia", triggers=["Nia"],
            entry="Ranger.", portrait_prompt="short black hair",
        )
    add_action(adv["id"], 0, "continue", "Nia waits.")
    resp = client.post(
        f"/api/adventures/{adv['id']}/see",
        json={"action_seq": 0},
        headers={"Prefer": "respond-async"},
    )
    assert resp.status_code == 202, resp.text
    for thread in threading.enumerate():
        if thread.name == "see-prompt":
            thread.join(5)
    with db.session() as conn:
        see_job = db.get_job(conn, resp.json()["job_id"])
        portraits = [job for job in db.list_jobs(conn, adv["id"])
                     if job["type"] == "character_portrait"]
    assert see_job["status"] == "error"
    assert see_job["result"]["code"] == "portrait_required"
    assert [job["ref_id"] for job in portraits] == [card["id"]]


def test_async_see_waits_for_portrait_job_then_runs(client, monkeypatch):
    monkeypatch.setattr(jobs, "SEE_PORTRAIT_WAIT_S", 5.0)
    monkeypatch.setattr(jobs, "SEE_PORTRAIT_POLL_S", 0.05)
    adv = make_adventure()
    with db.session() as conn:
        portrait = db.insert_job(
            conn, adventure_id=adv["id"], type="character_portrait",
            status="pending", payload={},
        )

    calls = {"n": 0}

    def plan() -> dict:
        calls["n"] += 1
        if calls["n"] == 1:
            raise likeness.portrait_required()
        return {
            "prompt": "Nia waits.", "negative_extra": "", "negative_prompt": "",
            "caption": "Nia waits.", "cast_card_ids": [], "reference_image_ids": [],
            "reference_hashes": [], "expected_people": 0, "explicit": False,
        }

    def finish_portrait() -> None:
        time.sleep(0.2)
        with db.session() as conn:
            db.finish_job(conn, portrait["id"], "done")

    threading.Thread(target=finish_portrait, daemon=True).start()
    job = jobs.start_see(
        adv["id"], {"action_seq": 0, "extra_prompt": "", "variation": 0}, plan)
    for thread in threading.enumerate():
        if thread.name == "see-prompt":
            thread.join(5)
    with db.session() as conn:
        ready = db.get_job(conn, job["id"])
    assert ready["status"] == "pending"
    assert ready["payload"]["prompt"] == "Nia waits."
    assert calls["n"] == 2


def test_cast_anchors_keep_order_for_see_and_backdrop(client, tmp_path):
    use_tmp_storage(tmp_path)
    adv = make_adventure(plot_essentials="[Name: Caio]")
    blobs = [
        b"\x89PNG\r\nplayer-anchor",
        b"\x89PNG\r\nnia-anchor",
        b"\x89PNG\r\nbob-anchor",
    ]
    with db.session() as conn:
        player = db.insert_card(
            conn, adv["id"], type="character", title="Caio", triggers=["Caio"],
            entry="The player.", use_for_character_creation=True,
            portrait_prompt="short brown hair",
        )
        nia = db.insert_card(
            conn, adv["id"], type="character", title="Nia", triggers=["Nia"],
            entry="Ranger.", portrait_prompt="black hair",
        )
        bob = db.insert_card(
            conn, adv["id"], type="character", title="Bob", triggers=["Bob"],
            entry="Smith.", portrait_prompt="grey beard",
        )
    ids = []
    for card, blob in ((player, blobs[0]), (nia, blobs[1]), (bob, blobs[2])):
        ids.append(_face(adv["id"], card["id"], blob)["id"])
    with db.session() as conn:
        conn.execute(
            "UPDATE story_cards SET portrait_anchor_id='' WHERE id=?",
            (player["id"],),
        )
    add_action(adv["id"], 0, "continue", "Caio meets Nia and then Bob.")
    _use_comfy()
    capture: dict = {}
    override_transport(client, pipeline_transport("Caio meets Nia and Bob.", capture))
    try:
        seen = client.post(f"/api/adventures/{adv['id']}/see", json={"action_seq": 0})
    finally:
        client.app.dependency_overrides.clear()
    assert seen.status_code == 200, seen.text
    assert [blob in capture["uploads"][index] for index, blob in enumerate(blobs)] == [True, True, True]
    references = _reference_image_nodes(capture["graphs"][0])
    assert len(references) == 3
    assert len({reference[0] for reference in references}) == 3
    scene_resp = client.post(f"/api/adventures/{adv['id']}/scene", json={})
    assert scene_resp.status_code == 200, scene_resp.text
    with db.session() as conn:
        backdrop = next(job for job in db.list_jobs(conn, adv["id"])
                        if job["type"] == "scene_backdrop" and job["status"] == "pending")
    assert backdrop["payload"]["cast_card_ids"] == [player["id"], nia["id"], bob["id"]]
    assert backdrop["payload"]["reference_image_ids"] == ids
    assert jobs.process_pending(pipeline_transport(capture=capture)) == 1
    assert [blob in capture["uploads"][3 + index] for index, blob in enumerate(blobs)] == [True, True, True]
    with db.session() as conn:
        image = db.latest_image(conn, adv["id"], "backdrop")
    assert image["cache_key"] == backdrop["payload"]["cache_key"]


def test_see_without_prompt_anchors_player_and_named_npc(client, tmp_path):
    use_tmp_storage(tmp_path)
    adv = make_adventure(plot_essentials="[Name: Caio]")
    player_blob = b"\x89PNG\r\nplayer-anchor"
    nia_blob = b"\x89PNG\r\nnia-anchor"
    with db.session() as conn:
        player = db.insert_card(
            conn, adv["id"], type="character", title="Caio", triggers=["Caio"],
            entry="The player.", use_for_character_creation=True,
            portrait_prompt="short brown hair",
        )
        nia = db.insert_card(
            conn, adv["id"], type="character", title="Nia", triggers=["Nia"],
            entry="Ranger.", portrait_prompt="black hair",
        )
    _face(adv["id"], player["id"], player_blob)
    _face(adv["id"], nia["id"], nia_blob)
    # A narração em terceira pessoa não repete o nome do jogador.
    add_action(adv["id"], 0, "continue", "Nia waits for her lover in the tavern.")
    _use_comfy()
    capture: dict = {}
    override_transport(client, pipeline_transport(capture=capture))
    try:
        seen = client.post(f"/api/adventures/{adv['id']}/see", json={"action_seq": 0})
    finally:
        client.app.dependency_overrides.clear()

    assert seen.status_code == 200, seen.text
    assert any(player_blob in upload for upload in capture["uploads"])
    assert any(nia_blob in upload for upload in capture["uploads"])
    prompt = seen.json()["prompt"]
    assert "Exactly 2 people" in prompt
    assert "Caio" in prompt and "Nia" in prompt
    references = _reference_image_nodes(capture["graphs"][0])
    assert len(references) == 2


def test_second_anchor_invalidates_only_that_scene(client, tmp_path):
    use_tmp_storage(tmp_path)
    _use_comfy()
    adv = make_adventure()
    ada_bytes = b"\x89PNG\r\nada-anchor"
    bob_bytes = b"\x89PNG\r\nbob-anchor"
    with db.session() as conn:
        ada = db.insert_card(
            conn, adv["id"], type="character", title="Ada", triggers=["Ada"],
            entry="Barkeep.", portrait_prompt="silver hair",
        )
        bob = db.insert_card(
            conn, adv["id"], type="character", title="Bob", triggers=["Bob"],
            entry="Smith.", portrait_prompt="grey beard",
        )
    ada_image = _face(adv["id"], ada["id"], ada_bytes)
    bob_image = _face(adv["id"], bob["id"], bob_bytes)
    add_action(adv["id"], 0, "continue", "Ada talks to Bob.")
    first = client.post(f"/api/adventures/{adv['id']}/scene", json={}).json()
    assert first["backdrop"]["status"] == "pending"
    with db.session() as conn:
        job = next(item for item in db.list_jobs(conn, adv["id"])
                   if item["type"] == "scene_backdrop")
        cfg = image_client.effective_settings(conn)
    old_key = job["payload"]["cache_key"]
    old_hashes = list(job["payload"]["reference_hashes"])
    solo = jobs.backdrop_cache_key(
        cfg, "only ada", include_people=True, explicit=False,
        reference_images=[ada_bytes])
    bob_path = _faces_dir() / f"{bob['id']}.png"
    bob_path.write_bytes(b"\x89PNG\r\nbob-replaced")
    second = client.post(f"/api/adventures/{adv['id']}/scene", json={}).json()
    assert second["backdrop"]["status"] == "pending"
    with db.session() as conn:
        fresh = next(item for item in db.list_jobs(conn, adv["id"])
                     if item["type"] == "scene_backdrop" and item["status"] == "pending")
        assert job["id"] != fresh["id"]
        stored = db.get_job(conn, job["id"])
    assert stored["status"] == "canceled"
    assert fresh["payload"]["cache_key"] != old_key
    assert fresh["payload"]["reference_hashes"][0] == old_hashes[0]
    assert fresh["payload"]["reference_hashes"][1] != old_hashes[1]
    assert fresh["payload"]["reference_image_ids"] == [ada_image["id"], bob_image["id"]]
    assert jobs.backdrop_cache_key(
        cfg, "only ada", include_people=True, explicit=False,
        reference_images=[ada_bytes]) == solo


def test_execution_recomputes_cache_after_config_change(client, tmp_path):
    use_tmp_storage(tmp_path)
    _use_comfy()
    adv = make_adventure()
    with db.session() as conn:
        ada = db.insert_card(
            conn, adv["id"], type="character", title="Ada", triggers=["Ada"],
            entry="Barkeep.", portrait_prompt="silver hair",
        )
    _face(adv["id"], ada["id"], PNG_BYTES)
    add_action(adv["id"], 0, "continue", "Ada waits outside.")
    response = client.post(f"/api/adventures/{adv['id']}/scene", json={})
    assert response.status_code == 200
    with db.session() as conn:
        job = next(item for item in db.list_jobs(conn, adv["id"])
                   if item["type"] == "scene_backdrop")
        old_key = job["payload"]["cache_key"]
        db.set_setting(conn, "image.seed", "42")
    assert jobs.process_pending(pipeline_transport()) == 1
    with db.session() as conn:
        image = db.latest_image(conn, adv["id"], "backdrop")
    assert image["cache_key"] != old_key


def test_see_execution_recomputes_cache_and_negative_after_config_change(tmp_db, tmp_path):
    use_tmp_storage(tmp_path)
    _use_comfy()
    adv = make_adventure()
    with db.session() as conn:
        job = db.insert_job(
            conn, adventure_id=adv["id"], type="see_image", ref_id="", status="pending",
            payload={
                "prompt": "Ada is naked outside", "caption": "Ada outside",
                "negative_extra": "nude", "negative_prompt": "nude, text",
                "cache_key": "stale", "explicit": True,
                "reference_image_ids": [], "reference_hashes": [],
            }, priority=jobs.PRIORITY_SEE,
        )
        db.set_setting(conn, "image.seed", "42")
    assert jobs.process_pending(pipeline_transport()) == 1
    with db.session() as conn:
        image = db.latest_image(conn, adv["id"], "scene")
    assert image["cache_key"] != "stale"
    assert "nude" not in image["negative_prompt"].lower()
    assert "text" in image["negative_prompt"].lower()


def test_missing_reference_file_fails_the_backdrop_job(client, tmp_path):
    use_tmp_storage(tmp_path)
    _use_comfy()
    adv = make_adventure()
    with db.session() as conn:
        ada = db.insert_card(
            conn, adv["id"], type="character", title="Ada", triggers=["Ada"],
            entry="Barkeep.", portrait_prompt="silver hair",
        )
    image = _face(adv["id"], ada["id"], b"\x89PNG\r\nada-file")
    add_action(adv["id"], 0, "continue", "Ada waits.")
    assert client.post(f"/api/adventures/{adv['id']}/scene", json={}).status_code == 200
    (_faces_dir() / f"{ada['id']}.png").unlink()
    capture: dict = {}
    assert jobs.process_pending(pipeline_transport(capture=capture)) == 1
    assert capture.get("graphs", []) == []
    with db.session() as conn:
        job = next(item for item in db.list_jobs(conn, adv["id"])
                   if item["type"] == "scene_backdrop")
        assert job["status"] == "error"
        assert job["result"]["code"] == "portrait_required"
        assert image["id"] in job["payload"]["reference_image_ids"]


def test_prompt_mood_follow_visual_state_not_bond_numbers():
    card = {
        "title": "Ada",
        "type": "character",
        "portrait_prompt": "silver hair, bust portrait",
        "sheet": json.dumps({
            "visual_state": {
                "action": "raises a lantern",
                "emotion": "wary",
                "pose": "half turned",
                "seq": 4,
            },
            "alignment": "desconfiado",
            "bonds": {"trust": 42, "relationship": 40},
        }),
    }
    prompt, people = scene._prompt(
        "The Tavern", "A smoky room.", "", "night", [card], "Ada waits.")
    folded = prompt.lower()
    assert people is True
    assert "raises a lantern" in folded
    assert "wary" in folded
    assert "half turned" in folded
    assert "current reaction" in folded
    assert "42" not in prompt
    assert "trust" not in folded
    assert scene._cast_moods([card], "Ada smiles, happy to see you.")["Ada"] == "wary"
    assert scene._cast_moods(
        [{"title": "Ada", "sheet": "{}"}], "Ada smiles, happy to see you.",
    )["Ada"] == "happy"
    moods = scene._cast_moods(
        [{"title": "Ada", "sheet": "{}"}, {"title": "Lexi", "sheet": "{}"}],
        "Ada is happy and Lexi is afraid.",
    )
    assert moods == {"Ada": "happy", "Lexi": "fear"}

    bare = {
        "title": "Ada",
        "type": "character",
        "entry": "Barkeep.",
        "sheet": json.dumps({
            "visual_state": {"action": "waves", "emotion": "wary", "seq": 1},
        }),
    }
    empty, include = scene._prompt("The Tavern", "", "", "night", [bare], "Ada waits at night.")
    assert include is False
    assert "waves" not in empty.lower()
    assert "no people" in empty.lower()


def test_visual_state_paints_the_backdrop_once_until_it_changes(client, tmp_path):
    use_tmp_storage(tmp_path)
    adv = make_adventure()
    sheet = {
        "appearance": "silver hair",
        "visual_state": {
            "action": "raises a lantern",
            "emotion": "wary",
            "pose": "half turned",
            "seq": 2,
        },
        "alignment": "desconfiado",
        "bonds": {
            "relationship": 40, "trust": 40, "distrust": 30,
            "fear": 15, "desire": 15, "boldness": 40,
        },
    }
    with db.session() as conn:
        ada = db.insert_card(
            conn, adv["id"], type="character", title="Ada", triggers=["Ada"],
            entry="Barkeep.", portrait_prompt="silver hair, bust portrait",
            sheet=json.dumps(sheet),
        )
    image = _face(adv["id"], ada["id"], b"\x89PNG\r\nada-live")
    add_action(
        adv["id"], 0, "continue",
        "Ada smiles, happy to see you. The tavern is quiet at night.",
    )
    resp = client.post(f"/api/adventures/{adv['id']}/scene", json={})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["cast"][0]["mood"] == "wary"
    with db.session() as conn:
        job = next(item for item in db.list_jobs(conn, adv["id"])
                   if item["type"] == "scene_backdrop")
        stored = db.get_card(conn, adv["id"], ada["id"])
    prompt = job["payload"]["prompt"].lower()
    assert "raises a lantern" in prompt
    assert "wary" in prompt
    assert "half turned" in prompt
    assert "trust" not in prompt
    assert job["priority"] == jobs.PRIORITY_BACKDROP
    assert stored["portrait_anchor_id"] == image["id"]
    signature = body["signature"]

    sheet["bonds"]["trust"] = 44
    sheet["visual_state"]["seq"] = 9
    with db.session() as conn:
        db.update_card(conn, adv["id"], ada["id"], {"sheet": json.dumps(sheet)})
    again = client.post(f"/api/adventures/{adv['id']}/scene", json={})
    assert again.status_code == 200, again.text
    assert again.json()["signature"] == signature
    assert again.json()["cast"][0]["mood"] == "wary"
    with db.session() as conn:
        backs = [item for item in db.list_jobs(conn, adv["id"])
                 if item["type"] == "scene_backdrop"]
    assert len(backs) == 1
    assert backs[0]["id"] == job["id"]
    assert backs[0]["status"] == "pending"
    assert backs[0]["priority"] == jobs.PRIORITY_BACKDROP

    sheet["visual_state"]["action"] = "draws a sword"
    sheet["visual_state"]["emotion"] = "angry"
    with db.session() as conn:
        db.update_card(conn, adv["id"], ada["id"], {"sheet": json.dumps(sheet)})
    changed = client.post(f"/api/adventures/{adv['id']}/scene", json={})
    assert changed.status_code == 200, changed.text
    assert changed.json()["signature"] != signature
    assert changed.json()["cast"][0]["mood"] == "angry"
    with db.session() as conn:
        backs = [item for item in db.list_jobs(conn, adv["id"])
                 if item["type"] == "scene_backdrop"]
        stored = db.get_card(conn, adv["id"], ada["id"])
    live = [item for item in backs if item["status"] != "canceled"]
    assert len(live) == 1
    assert len(backs) == 2
    assert live[0]["priority"] == jobs.PRIORITY_BACKDROP
    assert "draws a sword" in live[0]["payload"]["prompt"].lower()
    assert "angry" in live[0]["payload"]["prompt"].lower()
    assert stored["portrait_anchor_id"] == image["id"]


def test_long_identity_prompt_preserves_action_location_and_per_person_clothing():
    cards = []
    for name in ("Ada", "Elara", "Lexi", "Caio"):
        cards.append({
            "title": name,
            "type": "character",
            "portrait_prompt": "silver hair, sharp eyes, " * 30,
            "portrait_image_id": f"{name}-img",
            "sheet": json.dumps({
                "reference": {"turnaround": {}},
                "visual_state": {
                    "action": "raises a detailed lantern and turns toward the crowd",
                    "emotion": "anger", "pose": "half turned", "seq": 1,
                },
            }),
        })
    prompt, people = scene._prompt(
        "The Ancient Tavern", "A crowded room.", "cinematic", "night", cards,
        "Ada is naked and dances on the central tavern table.",
    )
    assert people is True
    assert len(prompt) <= 1400
    assert "dances on the central tavern table" in prompt
    assert "The Ancient Tavern" in prompt
    assert "use the reference images only for the named characters" in prompt.lower()
    assert "ada" in prompt.lower() and "is completely naked" in prompt.lower()
    for name in ("Elara", "Lexi", "Caio"):
        assert f"{name} is fully clothed" in prompt



def test_explicit_word_with_everyone_clothed_keeps_identity_and_full_references(
        client, tmp_path, monkeypatch):
    """Uma palavra explícita na narração ("bunda") marca a cena, mas ninguém
    fica nu. O See tem de descrever o rosto e o cabelo de quem aparece e usar
    as referências inteiras, não só recortes do rosto: a personagem saía sem
    óculos e com outro cabelo."""
    use_tmp_storage(tmp_path)
    adv = make_adventure()
    _fairy(adv["id"])
    add_action(adv["id"], 0, "continue",
               "Elara, vestida de seda e cristal, tem a bunda avantajada e sorri na clareira.")
    cropped: list[int] = []
    real_crop = likeness.identity_face_pngs
    monkeypatch.setattr(likeness, "identity_face_pngs",
                        lambda blobs: cropped.append(len(blobs)) or real_crop(blobs))
    client.post(f"/api/adventures/{adv['id']}/scene", json={})
    backdrop = _backdrop_job(adv["id"])["payload"]
    seen = _see(client, adv["id"], 0, monkeypatch, tmp_path).lower()
    assert backdrop["explicit"] is True
    assert backdrop["reference_faces"] is False
    assert cropped == []
    assert "amber eyes" in seen
    assert "hair floats around her head like stars" in seen
    # todos vestidos: o estado de roupa aparece uma vez, não três, e não
    # entra no meio da frase de ação
    assert seen.count("elara is fully clothed") == 1
    assert "elara, fully clothed" not in seen


def test_look_block_shrinks_by_whole_traits_and_keeps_every_person():
    """O fundo de produção saiu com "Renata Lemos: glasses: …; just past." e
    sem a linha do Caio: o corte era por caracteres. Agora cai traço inteiro,
    do menos importante para o mais, e a frase só de nomes fica como está."""
    look = (
        "exact likeness of Caio, Renata. Ladrão: A stout pirate with a large belly, messy gray hair. "
        "Caio: no glasses; full dark beard and mustache; just past the ears, slightly wavy, "
        "near-black hair; body: slim build, narrow waist; very thick dark brows. "
        "Renata: glasses: thin round silver wire frames; just past the shoulders, tight "
        "ringlets, near-black hair; body: slim, petite, small narrow frame; very thick dark brows."
    )
    short = scene._shrink_look(look, 330)
    assert len(short) <= 330
    assert short.startswith("exact likeness of Caio, Renata. ")
    assert "Ladrão: A stout pirate with a large belly, messy gray hair." in short
    assert "Caio: no glasses; full dark beard and mustache" in short
    assert "Renata: glasses: thin round silver wire frames" in short
    for person in short.split(". ")[1:]:
        for trait in person.rstrip(".").split("; "):
            assert trait in look                      # nenhum traço pela metade
    assert scene._shrink_look(look, 2000) == look


def test_old_jobs_without_the_flag_keep_face_only_references():
    assert jobs._reference_faces({"explicit": True}) is True
    assert jobs._reference_faces({"explicit": True, "reference_faces": False}) is False
    assert jobs._reference_faces({}) is False
