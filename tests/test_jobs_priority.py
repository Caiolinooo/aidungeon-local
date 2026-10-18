"""G5: fila com prioridade e substituição (C5), See assíncrono (C6), aventura
apagada no meio do render e jobs interrompidos por um reinício."""

from __future__ import annotations

import json
import sqlite3
import threading
import time

import httpx
import pytest

from app import auth as auth_core
from app import db, deps, events, jobs
from conftest import TEST_AUTH_SECRET, add_action, make_adventure, sse_body
from test_v3 import PNG_B64, make_npc, use_tmp_storage


class _Stack:
    """llama-server e sdwebui fakes. `llm_gate` segura o prompt do See e
    `sd_gate` segura o render."""

    def __init__(self) -> None:
        self.llm_gate = threading.Event()
        self.llm_gate.set()
        self.sd_gate = threading.Event()
        self.sd_gate.set()
        self.sd_entered = threading.Event()
        self.llm_fail = False
        self.sd_fail = False
        self.renders = 0
        self.transport = httpx.MockTransport(self._handle)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/props"):
            return httpx.Response(200, json={"total_slots": 5})
        if path.endswith("/models"):
            return httpx.Response(200, json={"data": [{"id": "mock-model"}]})
        if path.endswith("/chat/completions"):
            payload = json.loads(request.content.decode("utf-8"))
            if payload.get("stream"):
                return httpx.Response(200, headers={"content-type": "text/event-stream"},
                                      text=sse_body(("The bar", " goes quiet.")))
            self.llm_gate.wait(5)
            if self.llm_fail:
                raise httpx.ConnectError("mock: connection refused")
            return httpx.Response(200, json={
                "choices": [{"message": {"content": "A lantern swings over the bar."}}]})
        if path.endswith("/sdapi/v1/txt2img"):
            self.renders += 1
            self.sd_entered.set()
            self.sd_gate.wait(5)
            if self.sd_fail:
                return httpx.Response(500, text="CUDA out of memory")
            return httpx.Response(200, json={"images": [PNG_B64], "parameters": {}})
        return httpx.Response(404, json={"error": f"no mock route: {path}"})


@pytest.fixture()
def stack(client, tmp_path):
    use_tmp_storage(tmp_path)
    fake = _Stack()
    client.app.dependency_overrides[deps.get_http_transport] = lambda: fake.transport
    yield fake
    fake.llm_gate.set()
    fake.sd_gate.set()
    _join_see_threads()


def _join_see_threads() -> None:
    for thread in threading.enumerate():
        if thread.name == "see-prompt":
            thread.join(5)


def _wait(predicate, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError("condição não chegou a tempo")
        time.sleep(0.01)


def _job(job_id: str) -> dict | None:
    with db.session() as conn:
        return db.get_job(conn, job_id)


def _next_of(sub: events.Subscription, type_: str) -> events.Event:
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        event = sub.get(0.2)
        if event is not None and event.type == type_:
            return event
    raise AssertionError(f"evento {type_} não chegou")


def _drain(sub: events.Subscription) -> list[events.Event]:
    out = []
    while (event := sub.get(0.05)) is not None:
        out.append(event)
    return out


def _tavern() -> dict:
    adv = make_adventure()
    with db.session() as conn:
        db.insert_card(conn, adv["id"], type="location", title="The Tavern",
                       triggers=["tavern"], entry="A smoky common room with a hearth.")
        db.insert_card(conn, adv["id"], type="character", title="Ada",
                       entry="The barkeep.", portrait_prompt="silver-haired barkeep")
    add_action(adv["id"], 0, "start", "You enter the tavern at night. Ada smiles.")
    return adv


def _see_async(client, adventure_id: str, **body):
    return client.post(f"/api/adventures/{adventure_id}/see",
                       json={"action_seq": 0, **body},
                       headers={"Prefer": "respond-async"})


def _pending_see(adventure_id: str) -> dict:
    with db.session() as conn:
        return db.insert_job(conn, adventure_id=adventure_id, type="see_image",
                             priority=jobs.PRIORITY_SEE,
                             payload={"prompt": "a lantern swings over the bar",
                                      "caption": "A lantern swings over the bar."})


# ------------------------------------------------------------------ fila C5
def test_claim_takes_lowest_priority_then_oldest(tmp_db):
    adv = make_adventure()
    with db.session() as conn:
        bulk = jobs.enqueue(conn, "character_portrait", adv["id"], "c1",
                            priority=jobs.PRIORITY_BULK_PORTRAIT)
        backdrop = jobs.enqueue(conn, "scene_backdrop", adv["id"], "r1", {"prompt": "x"})
        present = jobs.enqueue(conn, "character_portrait", adv["id"], "c2",
                               priority=jobs.PRIORITY_PRESENT_PORTRAIT)
        present_later = jobs.enqueue(conn, "character_portrait", adv["id"], "c3",
                                     priority=jobs.PRIORITY_PRESENT_PORTRAIT)
        see = db.insert_job(conn, adventure_id=adv["id"], type="see_image",
                            priority=jobs.PRIORITY_SEE)
    order = []
    with db.session() as conn:
        while (job := db.claim_next_job(conn)) is not None:
            order.append(job["id"])
    assert order == [see["id"], present["id"], present_later["id"],
                     backdrop["id"], bulk["id"]]


def test_narrative_lets_only_the_see_through(client, stack):
    adv = _tavern()
    with db.session() as conn:
        backdrop = jobs.enqueue(conn, "scene_backdrop", adv["id"], "r1",
                                {"prompt": "an empty tavern"})
    see = _pending_see(adv["id"])

    assert jobs.process_pending(stack.transport, should_pause=lambda: True) == 1
    assert _job(see["id"])["status"] == "done"
    assert _job(backdrop["id"])["status"] == "pending"

    assert jobs.process_pending(stack.transport) == 1
    assert _job(backdrop["id"])["status"] == "done"


def test_present_portrait_jumps_the_bulk_queue(client):
    adv = make_adventure()
    make_npc(adv["id"], title="Ada", entry="Captain.")
    rex = make_npc(adv["id"], title="Rex", entry="Engineer.")
    bulk = client.post(f"/api/adventures/{adv['id']}/portraits").json()["jobs"]
    assert {job["priority"] for job in bulk} == {jobs.PRIORITY_BULK_PORTRAIT}

    with db.session() as conn:
        again = jobs.enqueue_portrait(conn, adv["id"], rex["id"])
        lower = jobs.enqueue_portrait(conn, adv["id"], rex["id"],
                                      priority=jobs.PRIORITY_BULK_PORTRAIT)
    assert again["id"] == next(job["id"] for job in bulk if job["ref_id"] == rex["id"])
    assert again["priority"] == jobs.PRIORITY_PRESENT_PORTRAIT
    assert lower["priority"] == jobs.PRIORITY_PRESENT_PORTRAIT
    with db.session() as conn:
        assert db.claim_next_job(conn)["ref_id"] == rex["id"]


def test_new_backdrop_cancels_the_stale_pending_one(client, stack):
    adv = _tavern()
    with events.subscribe(adv["id"]) as sub:
        first = client.post(f"/api/adventures/{adv['id']}/scene", json={}).json()
        add_action(adv["id"], 1, "continue", "Fire engulfs the tavern.")
        second = client.post(f"/api/adventures/{adv['id']}/scene", json={}).json()
        assert first["signature"] != second["signature"]
        with db.session() as conn:
            old, new = [job for job in db.list_jobs(conn, adv["id"])
                        if job["type"] == "scene_backdrop"]
        assert old["status"] == "canceled" and old["error"] == "superseded"
        assert new["status"] == "pending"
        assert new["supersede_key"] == f"backdrop:{adv['id']}"
        assert new["priority"] == jobs.PRIORITY_BACKDROP

        assert jobs.process_pending(stack.transport) == 2
        ready = _next_of(sub, "backdrop_ready")
    assert ready.data["job_id"] == new["id"]
    assert ready.data["ref_id"] == new["ref_id"]
    assert stack.renders == 2


def test_backdrop_events_follow_the_job(client, stack):
    adv = make_adventure()
    key = f"backdrop:{adv['id']}"
    with events.subscribe(adv["id"]) as sub:
        with db.session() as conn:
            old = jobs.enqueue(conn, "scene_backdrop", adv["id"], "r1", {"prompt": "a"},
                               supersede_key=key)
            new = jobs.enqueue(conn, "scene_backdrop", adv["id"], "r2", {"prompt": "b"},
                               supersede_key=key)
        assert jobs.process_pending(stack.transport) == 1
        seen = [(event.type, event.data.get("job_id"), event.data.get("status"))
                for event in _drain(sub)]
    assert seen == [
        ("job_queued", old["id"], None),
        ("job_queued", new["id"], None),
        ("job_progress", old["id"], "canceled"),
        ("job_progress", new["id"], "running"),
        ("job_progress", new["id"], "done"),
        ("backdrop_ready", new["id"], None),
    ]


def test_running_backdrop_is_not_superseded(tmp_db):
    adv = make_adventure()
    key = f"backdrop:{adv['id']}"
    with db.session() as conn:
        old = jobs.enqueue(conn, "scene_backdrop", adv["id"], "r1", {"prompt": "a"},
                           supersede_key=key)
        assert db.claim_next_job(conn)["id"] == old["id"]
        new = jobs.enqueue(conn, "scene_backdrop", adv["id"], "r2", {"prompt": "b"},
                           supersede_key=key)
    assert _job(old["id"])["status"] == "running"
    assert _job(new["id"])["status"] == "pending"


# -------------------------------------------------------------- See C6
@pytest.mark.parametrize("style", ["prefer", "query"])
def test_async_see_answers_202_fast_and_paints_in_the_job(client, stack, style):
    adv = _tavern()
    stack.llm_gate.clear()
    with events.subscribe(adv["id"]) as sub:
        started = time.monotonic()
        if style == "prefer":
            resp = _see_async(client, adv["id"], extra_prompt="from the door")
        else:
            resp = client.post(f"/api/adventures/{adv['id']}/see?async=1",
                               json={"action_seq": 0, "extra_prompt": "from the door"})
        elapsed = time.monotonic() - started
        assert resp.status_code == 202, resp.text
        assert elapsed < 0.3
        job_id = resp.json()["job_id"]

        polled = client.get(f"/api/jobs/{job_id}").json()
        assert polled["status"] == "preparing"
        assert polled["type"] == "see_image"
        assert polled["priority"] == jobs.PRIORITY_SEE
        assert jobs.process_pending(stack.transport) == 0

        stack.llm_gate.set()
        _wait(lambda: _job(job_id)["status"] == "pending")
        assert "from the door" in _job(job_id)["payload"]["prompt"]
        assert jobs.process_pending(stack.transport) == 1

        done = client.get(f"/api/jobs/{job_id}").json()
        assert done["status"] == "done"
        image = done["result"]["image"]
        assert image["url"] == f"/api/images/{image['id']}"
        assert image["action"]["type"] == "see"
        assert image["action"]["image"]["id"] == image["id"]
        seen = [(event.type, event.data.get("status")) for event in _drain(sub)
                if event.data.get("job_id") == job_id]
    assert seen == [
        ("job_queued", None),
        ("job_progress", "pending"),
        ("job_progress", "running"),
        ("job_progress", "done"),
        ("see_ready", None),
    ]
    assert client.get(image["url"]).status_code == 200


def test_see_ready_carries_the_image_result(client, stack):
    adv = _tavern()
    with events.subscribe(adv["id"]) as sub:
        job_id = _see_async(client, adv["id"]).json()["job_id"]
        _wait(lambda: _job(job_id)["status"] == "pending")
        assert jobs.process_pending(stack.transport) == 1
        ready = _next_of(sub, "see_ready")
    assert ready.data == {"job_id": job_id, "image": _job(job_id)["result"]["image"]}
    with db.session() as conn:
        image = db.get_image(conn, ready.data["image"]["id"])
    assert image["action_seq"] == ready.data["image"]["action"]["seq"]


def test_player_acts_while_the_see_is_painted(client, stack):
    adv = _tavern()
    job_id = _see_async(client, adv["id"]).json()["job_id"]
    _wait(lambda: _job(job_id)["status"] == "pending")
    stack.sd_gate.clear()
    worker = threading.Thread(target=jobs.process_pending, args=(stack.transport,))
    worker.start()
    try:
        assert stack.sd_entered.wait(5)
        resp = client.post(f"/api/adventures/{adv['id']}/action",
                           json={"type": "do", "text": "order a drink"})
        assert resp.status_code == 200, resp.text
        assert "event: done" in resp.text
        assert _job(job_id)["status"] == "running"
    finally:
        stack.sd_gate.set()
        worker.join(5)
    assert _job(job_id)["status"] == "done"
    with db.session() as conn:
        types = [action["type"] for action in db.list_actions(conn, adv["id"])]
    assert types[-1] == "see"
    assert "do" in types[:-1]


def test_async_see_prompt_failure_ends_in_see_failed(client, stack):
    adv = _tavern()
    stack.llm_fail = True
    with events.subscribe(adv["id"]) as sub:
        job_id = _see_async(client, adv["id"]).json()["job_id"]
        failed = _next_of(sub, "see_failed")
    assert failed.data == {"job_id": job_id, "code": "unreachable",
                           "message": failed.data["message"]}
    job = client.get(f"/api/jobs/{job_id}").json()
    assert job["status"] == "error"
    assert job["result"] == {"code": "unreachable"}
    assert jobs.process_pending(stack.transport) == 0
    assert stack.renders == 0


def test_async_see_render_failure_ends_in_see_failed(client, stack):
    adv = _tavern()
    stack.sd_fail = True
    with events.subscribe(adv["id"]) as sub:
        job_id = _see_async(client, adv["id"]).json()["job_id"]
        _wait(lambda: _job(job_id)["status"] == "pending")
        assert jobs.process_pending(stack.transport) == 1
        failed = _next_of(sub, "see_failed")
    job = _job(job_id)
    assert job["status"] == "error"
    assert failed.data["code"] == job["result"]["code"]
    with db.session() as conn:
        assert [a["type"] for a in db.list_actions(conn, adv["id"])] == ["start"]


def test_cancel_while_preparing_never_paints(client, stack):
    adv = _tavern()
    stack.llm_gate.clear()
    job_id = _see_async(client, adv["id"]).json()["job_id"]
    resp = client.delete(f"/api/adventures/{adv['id']}/jobs/{job_id}")
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "canceled"
    stack.llm_gate.set()
    _join_see_threads()
    assert _job(job_id)["status"] == "canceled"
    assert jobs.process_pending(stack.transport) == 0
    assert stack.renders == 0


def test_async_see_checks_before_answering(client, stack):
    assert _see_async(client, "nope").status_code == 404
    adv = _tavern()
    with db.session() as conn:
        db.set_setting(conn, "image.enabled", json.dumps(False))
    resp = _see_async(client, adv["id"])
    assert resp.status_code == 503
    assert resp.json()["error"]["code"] == "image_disabled"
    with db.session() as conn:
        assert db.list_jobs(conn, adv["id"]) == []


def test_job_of_someone_elses_story_is_404(client):
    adv = make_adventure(owner="alice")
    with db.session() as conn:
        job = jobs.enqueue(conn, "scene_backdrop", adv["id"], "r1", {"prompt": "x"})
    bob = auth_core.make_token("bob", TEST_AUTH_SECRET, ttl_h=1)
    alice = auth_core.make_token("alice", TEST_AUTH_SECRET, ttl_h=1)
    url = f"/api/jobs/{job['id']}"
    assert client.get(url, headers={"Authorization": f"Bearer {bob}"}).status_code == 404
    assert client.get(url, headers={"Authorization": f"Bearer {alice}"}).status_code == 200
    assert client.get(url, headers={"Authorization": ""}).status_code == 401
    assert client.get("/api/jobs/nope").status_code == 404


# ------------------------------------------------- aventura apagada / reinício
@pytest.mark.parametrize("kind", ["scene_backdrop", "character_portrait", "see_image"])
def test_adventure_deleted_mid_render_drops_the_png(client, stack, tmp_path, kind):
    adv = _tavern()
    with db.session() as conn:
        if kind == "scene_backdrop":
            job = jobs.enqueue(conn, kind, adv["id"], "r1", {"prompt": "an empty tavern"})
        elif kind == "character_portrait":
            ada = next(card for card in db.list_cards(conn, adv["id"])
                       if card["type"] == "character")
            job = jobs.enqueue_portrait(conn, adv["id"], ada["id"])
    if kind == "see_image":
        job = _pending_see(adv["id"])
    stack.sd_gate.clear()
    processed: list[int] = []
    worker = threading.Thread(
        target=lambda: processed.append(jobs.process_pending(stack.transport)))
    worker.start()
    try:
        assert stack.sd_entered.wait(5)
        assert client.delete(f"/api/adventures/{adv['id']}").status_code == 200
    finally:
        stack.sd_gate.set()
        worker.join(5)
    assert processed == [1]
    assert _job(job["id"]) is None
    assert list((tmp_path / "imgs" / adv["id"]).glob("*.png")) == []
    with db.session() as conn:
        assert conn.execute("SELECT COUNT(*) FROM images").fetchone()[0] == 0
    assert adv["id"] not in events._channels


def test_restart_requeues_assets_and_fails_interrupted_see(tmp_db):
    adv = make_adventure()
    with db.session() as conn:
        portrait = jobs.enqueue(conn, "character_portrait", adv["id"], "c1")
        assert db.claim_next_job(conn)["id"] == portrait["id"]
        see = db.insert_job(conn, adventure_id=adv["id"], type="see_image",
                            priority=jobs.PRIORITY_SEE, status="preparing")
        waiting = jobs.enqueue(conn, "scene_backdrop", adv["id"], "r1", {"prompt": "x"})

    assert jobs.recover_interrupted() == (1, 1)
    requeued = _job(portrait["id"])
    assert requeued["status"] == "pending" and requeued["started_at"] is None
    failed = _job(see["id"])
    assert failed["status"] == "error"
    assert failed["result"] == {"code": "interrupted"}
    assert _job(waiting["id"])["status"] == "pending"


def test_migration_021_upgrades_a_v20_database(tmp_path, monkeypatch):
    path = tmp_path / "old.db"
    before = db.SCHEMA_PATH.read_text(encoding="utf-8").split("-- --- Grok (G5)")[0]
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript(before)
    conn.execute("PRAGMA user_version = 20")
    adv = db.insert_adventure(conn, title="Old")
    conn.execute(
        "INSERT INTO jobs(id, adventure_id, type, status, ref_id, payload, created_at)"
        " VALUES('j1', ?, 'character_portrait', 'pending', 'c1', '{}', ?)",
        (adv["id"], db.utcnow()),
    )
    conn.commit()
    conn.close()

    monkeypatch.setenv("AIDUNGEON_DB_PATH", str(path))
    db.init_db()
    with db.session() as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] >= 21
        job = db.get_job(conn, "j1")
        indexes = {row["name"] for row in conn.execute("PRAGMA index_list(jobs)")}
    assert job["priority"] == jobs.PRIORITY_BACKDROP
    assert job["supersede_key"] == ""
    assert "jobs_queue" in indexes
