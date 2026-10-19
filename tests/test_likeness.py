"""Guardrail de pessoa real (C8) e a referência da Wikipedia."""

from __future__ import annotations

import base64
import json
import time
from datetime import date
from pathlib import Path

import httpx

from app import db, events, jobs, likeness, npc_assets
from app.routes.images import _plan_see
from app.schemas import SeeRequest
from conftest import add_action, make_adventure
from test_v2 import PNG_BYTES, make_v2_transport, override_transport
from test_v3 import use_tmp_storage

_SUMMARY = {
    "type": "standard",
    "title": "Zendaya",
    "description": "American actress and singer",
    "extract": "Zendaya is an American actress and singer. She was born in 1996.",
    "originalimage": {"source": "https://upload.wikimedia.org/wikipedia/commons/a/a0/Zendaya_face.jpg"},
    "content_urls": {"desktop": {"page": "https://en.wikipedia.org/wiki/Zendaya"}},
}


def _wiki_transport(summary: dict | None = None, *, missing: set[str] | None = None,
                    search: dict[str, str] | None = None) -> httpx.MockTransport:
    page = summary or _SUMMARY
    missing = missing or set()
    search = search or {}

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/sdapi/v1/txt2img") or path.endswith("/sdapi/v1/img2img"):
            return httpx.Response(200, json={"images": [
                "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8"
                "z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
            ]})
        if "page/summary/" in path:
            title = urllib_unquote(path.rsplit("/", 1)[-1]).replace("_", " ")
            if title in missing:
                return httpx.Response(404, json={"type": "not_found"})
            if title in search:
                return httpx.Response(404, json={"type": "not_found"})
            body = dict(page)
            body["title"] = title if title != "Zendaya" else page["title"]
            if title == "Neymar":
                body = {
                    "type": "standard",
                    "title": "Neymar",
                    "description": "Brazilian footballer",
                    "extract": "Neymar da Silva Santos Júnior is a Brazilian footballer born in 1992.",
                    "originalimage": page["originalimage"],
                    "content_urls": page["content_urls"],
                }
            return httpx.Response(200, json=body)
        if request.url.params.get("action") == "opensearch":
            query = request.url.params.get("search") or ""
            found = search.get(query)
            return httpx.Response(200, json=[query, [found] if found else [], [], []])
        if "upload.wikimedia.org" in request.url.host:
            return httpx.Response(200, content=PNG_BYTES, headers={"content-type": "image/png"})
        if "commons.wikimedia.org" in request.url.host:
            return httpx.Response(200, json={
                "query": {"pages": {"1": {"imageinfo": [{
                    "extmetadata": {"LicenseShortName": {"value": "CC BY-SA 4.0"}},
                }]}}},
            })
        return httpx.Response(404, json={"error": path})

    return httpx.MockTransport(handler)


def urllib_unquote(text: str) -> str:
    from urllib.parse import unquote
    return unquote(text)


def _character(adventure_id: str, title: str, **sheet: object) -> dict:
    with db.session() as conn:
        return db.insert_card(
            conn, adventure_id, type="character", title=title,
            triggers=[title], entry="A person in the story.",
            sheet=json.dumps(sheet, ensure_ascii=False) if sheet else "",
        )


def test_real_person_in_a_safe_scene_is_not_explicit():
    zendaya = {"type": "character", "title": "Zendaya", "sheet": json.dumps({
        "real_person": True, "real_name": "Zendaya",
    })}
    decision = likeness.decide_frame([zendaya], explicit=False)
    assert decision.refusal is None
    assert decision.allow_explicit is False
    assert decision.cast == [zendaya]


def test_explicit_scene_with_only_a_real_person_is_refused():
    zendaya = {"type": "character", "title": "Zendaya", "sheet": json.dumps({
        "real_person": True,
    })}
    decision = likeness.decide_frame([zendaya], explicit=True)
    assert decision.refusal == "real_person_explicit"
    assert decision.allow_explicit is False
    assert decision.cast == []


def test_explicit_scene_drops_the_real_person_and_keeps_the_fictional_one():
    zendaya = {"type": "character", "title": "Zendaya", "triggers": ["Zendaya"],
               "sheet": json.dumps({"real_person": True, "real_name": "Zendaya"})}
    clara = {"type": "character", "title": "Clara", "triggers": ["Clara"],
             "sheet": json.dumps({"appearance": "dark eyes"})}
    decision = likeness.decide_frame([zendaya, clara], explicit=True)
    assert decision.refusal is None
    assert decision.allow_explicit is True
    assert decision.cast == [clara]
    assert "Zendaya" not in likeness.omit_names("Zendaya and Clara are naked", decision.dropped)


def test_explicit_scene_with_a_minor_is_refused():
    clara = {"type": "character", "title": "Clara", "sheet": json.dumps({"minor": True, "age": 16})}
    decision = likeness.decide_frame([clara], explicit=True)
    assert decision.refusal == "minor"
    adult = {"type": "character", "title": "Mara", "sheet": "{}"}
    mixed = likeness.decide_frame([clara, adult], explicit=True)
    assert mixed.refusal == "minor"
    assert mixed.cast == []


def test_opening_clause_puts_the_real_descriptor_before_the_fictional_face():
    zendaya = {"type": "character", "title": "Zendaya", "sheet": json.dumps({
        "real_person": True,
        "real_name": "Zendaya",
        "likeness": {"descriptor": "dark curly hair, olive skin, brown eyes"},
    })}
    clara = {"type": "character", "title": "Clara",
             "portrait_prompt": "long red hair, green eyes"}
    clause = likeness.opening_clause([zendaya, clara], explicit=False)
    assert clause.startswith("exact likeness of Zendaya: dark curly hair")
    assert clause.index("Zendaya") < clause.index("long red hair")
    assert likeness.opening_clause([clara]) == npc_assets.likeness_clause([clara])


def test_english_bits_keeps_one_accented_name():
    text = "Neymar da Silva Santos Júnior is a Brazilian footballer."
    assert likeness.english_bits(text) == "Neymar da Silva Santos is a Brazilian footballer."
    assert likeness.english_bits("Ela é uma cantora, também é atriz e é famosa.") == ""


def test_download_falls_back_to_the_thumbnail_after_429():
    def handler(request: httpx.Request) -> httpx.Response:
        if "upload.wikimedia.org" in request.url.host:
            return httpx.Response(429, text="slow down")
        if "thumb.wikimedia.org" in request.url.host:
            return httpx.Response(200, content=PNG_BYTES, headers={"content-type": "image/png"})
        return httpx.Response(404)

    with likeness._client(httpx.MockTransport(handler)) as client:
        content, ext = likeness._download_with_fallback(
            client,
            "https://upload.wikimedia.org/wikipedia/commons/a/a0/Face.jpg",
            "https://thumb.wikimedia.org/wikipedia/commons/thumb/a/a0/Face.jpg/330px-Face.jpg",
        )
    assert content == PNG_BYTES
    assert ext == ".png"


def test_commons_filename_ignores_the_thumbnail_size():
    url = (
        "https://thumb.wikimedia.org/wikipedia/commons/thumb/5/5a/"
        "Zendaya-byPhilipRomano.jpg/3840px-Zendaya-byPhilipRomano.jpg?utm_source=en"
    )
    assert likeness._commons_filename(url) == "Zendaya-byPhilipRomano.jpg"


def test_age_class_flags_a_child_and_not_a_historical_adult():
    assert likeness.age_class("She was born in 2015.", today=date(2026, 10, 18)) == "minor"
    assert likeness.age_class("Born 1840, died 1910.", today=date(2026, 10, 18)) == "adult"
    assert likeness.age_class("born 1600 and died 1610.") == "minor"
    assert likeness.age_class("Zendaya is an American actress.") == "unknown"


def test_resolve_saves_the_wikipedia_photo_and_keeps_a_user_upload(tmp_db, tmp_path, monkeypatch):
    monkeypatch.setenv("AIDUNGEON_REFERENCES_DIR", str(tmp_path / "refs"))
    adv = make_adventure()
    card = _character(adv["id"], "Zendaya")
    transport = _wiki_transport()
    with db.session() as conn:
        updated = likeness.resolve_card(
            conn, card, transport=transport,
            describe=lambda _content: "dark curly hair, olive skin, brown eyes",
        )
    sheet = json.loads(updated["sheet"])
    assert sheet["real_person"] is True
    assert sheet["real_name"] == "Zendaya"
    assert sheet["likeness"]["license"] == "CC BY-SA 4.0"
    assert sheet["likeness"]["descriptor"].startswith("dark curly hair")
    photo = Path(sheet["likeness"]["reference_image_path"])
    assert photo.is_file()
    assert likeness.face_refs_for([updated])[0].image_path == str(photo.resolve())
    with db.session() as conn:
        uploaded = likeness.save_upload(conn, updated, PNG_BYTES, filename="face.png")
        kept = likeness.resolve_card(conn, uploaded, transport=transport, describe=lambda _c: "nope")
    kept_sheet = json.loads(kept["sheet"])
    assert kept_sheet["likeness"]["verified_by_user"] is True
    assert kept_sheet["likeness"]["license"] == "user upload"
    assert kept_sheet["likeness"]["descriptor"].startswith("dark curly hair")


def test_resolve_refuses_a_minor_and_writes_no_file(tmp_db, tmp_path, monkeypatch):
    monkeypatch.setenv("AIDUNGEON_REFERENCES_DIR", str(tmp_path / "refs"))
    adv = make_adventure()
    card = _character(adv["id"], "Ada")
    summary = dict(_SUMMARY)
    summary["title"] = "Ada"
    summary["extract"] = "Ada is a singer. She was born in 2015."
    summary["description"] = "singer"
    with db.session() as conn:
        try:
            likeness.resolve_card(conn, card, query="Ada", transport=_wiki_transport(summary))
        except Exception as exc:
            assert getattr(exc, "code", "") == "minor"
        else:
            raise AssertionError("menor deveria recusar")
        stored = db.get_card(conn, adv["id"], card["id"])
    sheet = json.loads(stored["sheet"])
    assert sheet["minor"] is True
    assert sheet.get("real_person") is not True
    assert list((tmp_path / "refs").glob("*")) == []


def test_resolve_searches_when_the_exact_title_is_missing(tmp_db, tmp_path, monkeypatch):
    monkeypatch.setenv("AIDUNGEON_REFERENCES_DIR", str(tmp_path / "refs"))
    adv = make_adventure()
    card = _character(adv["id"], "Neymar Jr.")
    transport = _wiki_transport(missing={"Neymar Jr."}, search={"Neymar Jr.": "Neymar"})
    with db.session() as conn:
        updated = likeness.resolve_card(
            conn, card, query="Neymar Jr.", transport=transport,
            describe=lambda _content: "short dark hair, light beard",
        )
    assert json.loads(updated["sheet"])["wiki_title"] == "Neymar"


def _see_failed(adventure_id: str, text: str) -> dict:
    add_action(adventure_id, 0, "do", text)
    with events.subscribe(adventure_id) as sub:
        job = jobs.start_see(
            adventure_id, {"action_seq": 0},
            lambda: _plan_see(adventure_id, SeeRequest(action_seq=0), None),
        )
        failed = None
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and failed is None:
            event = sub.get(0.2)
            if event is not None and event.type == "see_failed":
                failed = event
    assert failed is not None
    assert failed.data["job_id"] == job["id"]
    with db.session() as conn:
        stored = db.get_job(conn, job["id"])
    assert stored["status"] == "error"
    assert stored["result"] == {"code": failed.data["code"]}
    return failed.data


def test_see_refuses_an_explicit_real_person(client):
    adv = make_adventure()
    _character(adv["id"], "Zendaya", real_person=True, real_name="Zendaya")
    add_action(adv["id"], 0, "do", "Zendaya is naked on the bed.")
    resp = client.post(f"/api/adventures/{adv['id']}/see", json={"action_seq": 0})
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "real_person_explicit"


def test_async_see_refuses_an_explicit_real_person_before_enqueue(client):
    adv = make_adventure()
    _character(adv["id"], "Zendaya", real_person=True, real_name="Zendaya")
    add_action(adv["id"], 0, "do", "Zendaya is naked on the bed.")
    started = time.monotonic()
    resp = client.post(
        f"/api/adventures/{adv['id']}/see",
        json={"action_seq": 0},
        headers={"Prefer": "respond-async"},
    )
    assert time.monotonic() - started < 0.3
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "real_person_explicit"
    with db.session() as conn:
        assert db.list_jobs(conn, adv["id"]) == []


def test_worker_see_failed_carries_real_person_and_minor(client):
    real = make_adventure()
    _character(real["id"], "Zendaya", real_person=True, real_name="Zendaya")
    assert _see_failed(real["id"], "Zendaya is naked on the bed.")["code"] == "real_person_explicit"
    minor = make_adventure()
    _character(minor["id"], "Clara", minor=True, age=16)
    assert _see_failed(minor["id"], "Clara is naked on the bed.")["code"] == "minor"


def test_see_drops_the_real_person_from_an_explicit_scene(client, tmp_path):
    adv = make_adventure()
    with db.session() as conn:
        db.set_setting(conn, "image.storage_dir", json.dumps(str(tmp_path / "imgs")))
    _character(adv["id"], "Zendaya", real_person=True, real_name="Zendaya",
               appearance="dark curly hair")
    _character(adv["id"], "Clara", appearance="long red hair, green eyes")
    add_action(adv["id"], 0, "do", "Zendaya and Clara are naked together.")
    override_transport(client, make_v2_transport(
        chat_content="Zendaya and Clara stand nude in the room."))
    try:
        resp = client.post(f"/api/adventures/{adv['id']}/see", json={"action_seq": 0})
    finally:
        client.app.dependency_overrides.clear()
    assert resp.status_code == 200, resp.text
    prompt = resp.json()["prompt"]
    folded = prompt.lower()
    assert "clara" in folded
    assert "zendaya" not in folded
    assert "nude" in folded
    assert "clothes off" in folded


def test_see_of_a_real_person_opens_with_the_face_and_stays_clothed(client, tmp_path):
    adv = make_adventure()
    with db.session() as conn:
        db.set_setting(conn, "image.storage_dir", json.dumps(str(tmp_path / "imgs")))
    _character(
        adv["id"], "Zendaya", real_person=True, real_name="Zendaya",
        likeness={"descriptor": "dark curly hair, olive skin, brown eyes"},
    )
    add_action(adv["id"], 0, "continue", "Zendaya waves from the stage.")
    override_transport(client, make_v2_transport(chat_content="Zendaya waves from the stage."))
    try:
        resp = client.post(f"/api/adventures/{adv['id']}/see", json={"action_seq": 0})
    finally:
        client.app.dependency_overrides.clear()
    assert resp.status_code == 200, resp.text
    head = resp.json()["prompt"].split("BREAK", 1)[0]
    assert head.lower().startswith("exact likeness of zendaya")
    assert "dark curly hair" in head.lower()
    assert "nude" not in head.lower()
    assert "clothes off" not in head.lower()


def test_explicit_backdrop_refuses_a_real_person_and_paints_the_room(client, tmp_path):
    from test_scene import use_tmp_storage
    use_tmp_storage(tmp_path)
    adv = make_adventure()
    _character(adv["id"], "Zendaya", real_person=True, real_name="Zendaya",
               appearance="dark curly hair")
    add_action(adv["id"], 0, "continue", "Zendaya is naked on the sofa.")
    resp = client.post(f"/api/adventures/{adv['id']}/scene", json={})
    assert resp.status_code == 200, resp.text
    with db.session() as conn:
        job = next(item for item in db.list_jobs(conn, adv["id"]) if item["type"] == "scene_backdrop")
    prompt = job["payload"]["prompt"].lower()
    assert job["payload"]["explicit"] is False
    assert "zendaya" not in prompt
    assert "nude" not in prompt
    assert "no people" in prompt


def test_real_person_portrait_payload_includes_the_reference(client, tmp_path, monkeypatch):
    """Sem âncora, o img2img do retrato leva a foto da pessoa real e um denoise baixo."""
    monkeypatch.setenv("AIDUNGEON_REFERENCES_DIR", str(tmp_path / "refs"))
    refs = tmp_path / "refs"
    refs.mkdir()
    photo = refs / "zendaya.png"
    photo.write_bytes(PNG_BYTES)
    use_tmp_storage(tmp_path)
    adv = make_adventure()
    card = _character(
        adv["id"], "Zendaya", real_person=True, real_name="Zendaya",
        likeness={"reference_image_path": str(photo)},
    )
    with db.session() as conn:
        db.update_card(conn, adv["id"], card["id"], {
            "portrait_prompt": "dark curly hair, olive skin, brown eyes, bust portrait",
        })
        queued = jobs.enqueue_portrait(conn, adv["id"], card["id"])
    assert queued is not None
    seen: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/sdapi/v1/img2img"):
            seen.append(json.loads(request.content.decode("utf-8")))
            return httpx.Response(200, json={
                "images": [base64.b64encode(PNG_BYTES).decode("ascii")],
                "parameters": {},
            })
        return httpx.Response(404, json={"error": f"no mock route: {request.url.path}"})

    assert jobs.process_pending(httpx.MockTransport(handler)) == 1
    assert len(seen) == 1
    payload = seen[0]
    assert base64.b64decode(payload["init_images"][0]) == PNG_BYTES
    assert payload["denoising_strength"] == likeness.REFERENCE_DENOISE
    assert likeness.REFERENCE_DENOISE <= 0.30


def test_portrait_init_uses_the_reference_until_there_is_an_anchor(tmp_path, monkeypatch):
    monkeypatch.setenv("AIDUNGEON_REFERENCES_DIR", str(tmp_path))
    photo = tmp_path / "face.png"
    photo.write_bytes(PNG_BYTES)
    card = {"type": "character", "title": "Zendaya", "sheet": json.dumps({
        "real_person": True,
        "likeness": {"reference_image_path": str(photo)},
    })}
    data, denoise = likeness.portrait_init(card, None, anchor_denoise=0.35)
    assert data == PNG_BYTES
    assert denoise == likeness.REFERENCE_DENOISE
    # Pessoa real com foto não herda a âncora gerada, mesmo se ela existir.
    kept, denoise = likeness.portrait_init(card, b"anchor", anchor_denoise=0.35)
    assert kept == PNG_BYTES
    assert denoise == likeness.REFERENCE_DENOISE
    fictional = {"type": "character", "title": "Clara", "sheet": "{}"}
    fiction_bytes, fiction_denoise = likeness.portrait_init(
        fictional, b"anchor", anchor_denoise=0.35,
    )
    assert fiction_bytes == b"anchor"
    assert fiction_denoise == 0.35
    no_photo = {"type": "character", "title": "Alok", "sheet": json.dumps({
        "real_person": True,
    })}
    fallback, fallback_denoise = likeness.portrait_init(
        no_photo, b"anchor", anchor_denoise=0.35,
    )
    assert fallback == b"anchor"
    assert fallback_denoise == 0.35
    minor = {"type": "character", "title": "Ada", "sheet": json.dumps({
        "real_person": True, "minor": True,
    })}
    try:
        likeness.portrait_init(minor, None, anchor_denoise=0.35)
    except Exception as exc:
        assert getattr(exc, "code", "") == "minor"
    else:
        raise AssertionError("retrato de menor deveria recusar")


def test_real_person_with_anchor_still_sends_the_reference(client, tmp_path, monkeypatch):
    """Regenerar pessoa real manda a foto, não o PNG da âncora, no denoise da referência."""
    monkeypatch.setenv("AIDUNGEON_REFERENCES_DIR", str(tmp_path / "refs"))
    refs = tmp_path / "refs"
    refs.mkdir()
    photo = refs / "zendaya.png"
    photo.write_bytes(PNG_BYTES)
    anchor_bytes = b"generated-anchor-not-the-photo"
    use_tmp_storage(tmp_path)
    adv = make_adventure()
    card = _character(
        adv["id"], "Zendaya", real_person=True, real_name="Zendaya",
        likeness={"reference_image_path": str(photo)},
    )
    anchor_path = tmp_path / "bad-anchor.png"
    anchor_path.write_bytes(anchor_bytes)
    with db.session() as conn:
        db.update_card(conn, adv["id"], card["id"], {
            "portrait_prompt": "dark curly hair, olive skin, brown eyes, bust portrait",
        })
        image = db.insert_image(
            conn, adventure_id=adv["id"], action_seq=-1,
            file_path=str(anchor_path), prompt="txt2img anchor",
            kind="portrait", ref_id=card["id"], width=512, height=768,
        )
        db.set_card_portrait(conn, card["id"], image["id"], anchor_id=image["id"])
        queued = jobs.enqueue_portrait(conn, adv["id"], card["id"], replace=True)
    assert queued is not None
    assert queued["payload"]["init_image_id"] == image["id"]
    seen: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/sdapi/v1/img2img"):
            seen.append(json.loads(request.content.decode("utf-8")))
            return httpx.Response(200, json={
                "images": [base64.b64encode(PNG_BYTES).decode("ascii")],
                "parameters": {},
            })
        return httpx.Response(404, json={"error": f"no mock route: {request.url.path}"})

    assert jobs.process_pending(httpx.MockTransport(handler)) == 1
    assert len(seen) == 1
    payload = seen[0]
    sent = base64.b64decode(payload["init_images"][0])
    assert sent == PNG_BYTES
    assert sent != anchor_bytes
    assert payload["denoising_strength"] == likeness.REFERENCE_DENOISE
    assert payload["denoising_strength"] != jobs.PORTRAIT_DENOISE


def test_enrich_keeps_the_real_person_flag():
    sheet = npc_assets._normalize_sheet(
        {"appearance": "long red hair"},
        {"appearance": "old face", "real_person": True, "real_name": "Zendaya",
         "likeness": {"descriptor": "dark curly hair"}},
    )
    assert sheet["real_person"] is True
    assert sheet["real_name"] == "Zendaya"
    assert sheet["likeness"]["descriptor"] == "dark curly hair"
    assert sheet["appearance"] == "old face"


def test_reference_upload_and_delete_roundtrip(client, tmp_path, monkeypatch):
    monkeypatch.setenv("AIDUNGEON_REFERENCES_DIR", str(tmp_path / "refs"))
    adv = make_adventure()
    card = _character(adv["id"], "Zendaya", real_person=True, real_name="Zendaya")
    resp = client.put(
        f"/api/adventures/{adv['id']}/cards/{card['id']}/reference",
        files={"file": ("face.png", PNG_BYTES, "image/png")},
    )
    assert resp.status_code == 200, resp.text
    sheet = json.loads(resp.json()["sheet"])
    assert sheet["likeness"]["verified_by_user"] is True
    assert Path(sheet["likeness"]["reference_image_path"]).is_file()
    gone = client.delete(f"/api/adventures/{adv['id']}/cards/{card['id']}/reference")
    assert gone.status_code == 200, gone.text
    cleared = json.loads(gone.json()["sheet"])
    assert cleared["likeness"]["reference_image_path"] == ""
    assert cleared["real_person"] is True
