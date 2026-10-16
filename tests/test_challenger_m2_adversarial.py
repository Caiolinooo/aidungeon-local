"""Adversarial stress tests for Milestone 2: Memory Bank API, Summarizer, and Backfill.

Authored by challenger_m2_2.
Focus areas:
1. Memory Bank API edge cases, invalid payloads, boundary conditions, cross-adventure isolation, and serialization safety.
2. Summarizer & Embeddings fail-safe resilience (timeouts, 500, 404, connection refused, malformed JSON, math boundaries).
3. Backfill CLI resilience, mid-run crash recovery, idempotency, dry-run fidelity, and adventure scoping.
4. Concurrency & multi-threaded SQLite lock stress testing.
"""

from __future__ import annotations

import concurrent.futures
import json
import sqlite3
import struct
import subprocess
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import httpx
import pytest
from fastapi.testclient import TestClient

from app import db, embeddings_client, settings_store
from conftest import add_action, make_adventure
from memory import retrieval, summarizer
from tools.backfill_embeddings import backfill_memories as tool_backfill


# =====================================================================
# 1. Memory Bank API Stress Tests & Bug Demonstration
# =====================================================================

def test_api_whitespace_payload_triggers_typeerror_bug(client):
    """Remediated: POST and PATCH with whitespace text return HTTP 400 with code='invalid_payload'."""
    adv = make_adventure()
    aid = adv["id"]

    # 1. POST whitespace returns 400 Bad Request
    res_post = client.post(f"/api/adventures/{aid}/memories", json={"text": "   \n\t  "})
    assert res_post.status_code == 400
    assert res_post.json()["error"]["code"] == "invalid_payload"

    # Create valid memory
    res_valid = client.post(f"/api/adventures/{aid}/memories", json={"text": "Initial fact"})
    assert res_valid.status_code == 201
    mid = res_valid.json()["id"]

    # 2. PATCH whitespace returns 400 Bad Request
    res_patch = client.patch(f"/api/adventures/{aid}/memories/{mid}", json={"text": "   "})
    assert res_patch.status_code == 400
    assert res_patch.json()["error"]["code"] == "invalid_payload"


def test_api_whitespace_payload_returns_500_in_production(auth_token, tmp_db):
    """Remediated: in production (raise_server_exceptions=False), whitespace returns HTTP 400, not 500."""
    from app.main import app
    adv = make_adventure()
    aid = adv["id"]

    # With raise_server_exceptions=False (mimics real HTTP server)
    prod_client = TestClient(app, raise_server_exceptions=False)
    prod_client.headers["Authorization"] = f"Bearer {auth_token}"

    res_post = prod_client.post(f"/api/adventures/{aid}/memories", json={"text": "   "})
    assert res_post.status_code == 400, f"Expected HTTP 400, got {res_post.status_code}"
    assert res_post.json()["error"]["code"] == "invalid_payload"

    # Valid creation
    res_valid = prod_client.post(f"/api/adventures/{aid}/memories", json={"text": "Valid text"})
    assert res_valid.status_code == 201
    mid = res_valid.json()["id"]

    res_patch = prod_client.patch(f"/api/adventures/{aid}/memories/{mid}", json={"text": "   "})
    assert res_patch.status_code == 400, f"Expected HTTP 400, got {res_patch.status_code}"
    assert res_patch.json()["error"]["code"] == "invalid_payload"


def test_api_post_invalid_payloads(client):
    """Stress tests invalid and boundary inputs on POST /api/adventures/{id}/memories."""
    adv = make_adventure()
    aid = adv["id"]

    # 1. Empty string rejected by Pydantic min_length=1
    res = client.post(f"/api/adventures/{aid}/memories", json={"text": ""})
    assert res.status_code == 422, f"Expected 422 for empty string, got {res.status_code}"

    # 2. Missing text field
    res = client.post(f"/api/adventures/{aid}/memories", json={})
    assert res.status_code == 422, f"Expected 422 for missing text field, got {res.status_code}"

    # 3. Nonexistent adventure ID
    res = client.post("/api/adventures/nonexistent_adv_id_12345/memories", json={"text": "Valid text"})
    assert res.status_code == 404, f"Expected 404 for nonexistent adventure, got {res.status_code}"

    # 4. Invalid data types
    res = client.post(f"/api/adventures/{aid}/memories", json={"text": 12345})
    assert res.status_code == 422, f"Expected 422 for integer text, got {res.status_code}"
    res = client.post(f"/api/adventures/{aid}/memories", json={"text": None})
    assert res.status_code == 422, f"Expected 422 for null text, got {res.status_code}"
    res = client.post(f"/api/adventures/{aid}/memories", json={"text": "Valid", "seq_start": "not_an_int"})
    assert res.status_code == 422, f"Expected 422 for invalid seq_start, got {res.status_code}"

    # 5. Overlong text (>4000 characters)
    overlong = "A" * 4001
    res = client.post(f"/api/adventures/{aid}/memories", json={"text": overlong})
    assert res.status_code == 422, f"Expected 422 for >4000 chars, got {res.status_code}"

    # 6. Boundary text (exactly 4000 characters)
    max_text = "A" * 4000
    res = client.post(f"/api/adventures/{aid}/memories", json={"text": max_text})
    assert res.status_code == 201, f"Expected 201 for 4000 chars, got {res.status_code}"


def test_api_serialization_safety_and_no_bytes_leaks(client):
    """Ensures responses never contain raw bytes and are 100% JSON serializable."""
    adv = make_adventure()
    aid = adv["id"]

    # Insert a memory directly into DB with raw BLOB embedding
    raw_vector = [0.123] * 1024
    packed = struct.pack(f"<{len(raw_vector)}f", *raw_vector)
    with db.session() as conn:
        db.insert_memory(
            conn, aid, "Memory with raw embedding BLOB in DB",
            embedding=packed, embedding_model="Qwen3-Embedding-0.6B", embedding_dim=1024
        )

    # 1. GET /api/adventures/{id}/memories
    res = client.get(f"/api/adventures/{aid}/memories")
    assert res.status_code == 200
    data = res.json()
    assert len(data) == 1
    # Verify standard json.dumps succeeds without TypeError
    dumped = json.dumps(data)
    assert "Memory with raw embedding" in dumped
    # Raw embedding BLOB must NOT be in the API payload
    assert "embedding" not in data[0] or data[0]["embedding"] is None

    # 2. GET /api/adventures/{id}/memory (state endpoint)
    res_state = client.get(f"/api/adventures/{aid}/memory")
    assert res_state.status_code == 200
    state_data = res_state.json()
    dumped_state = json.dumps(state_data)
    assert "memories" in state_data
    assert len(state_data["memories"]) == 1
    assert "embedding" not in state_data["memories"][0] or state_data["memories"][0]["embedding"] is None


def test_api_cross_adventure_isolation(client):
    """Verifies that memories cannot be modified or deleted across different adventures."""
    adv1 = make_adventure(title="Adv 1")
    adv2 = make_adventure(title="Adv 2")

    res1 = client.post(f"/api/adventures/{adv1['id']}/memories", json={"text": "Adv 1 secret"})
    assert res1.status_code == 201
    mem1_id = res1.json()["id"]

    # Attempt to PATCH mem1 using adv2 route
    res_tamper = client.patch(
        f"/api/adventures/{adv2['id']}/memories/{mem1_id}",
        json={"text": "Hacked from Adv 2"}
    )
    assert res_tamper.status_code == 404, f"Expected 404 for cross-adventure PATCH, got {res_tamper.status_code}"

    # Verify mem1 is unchanged
    res_check = client.get(f"/api/adventures/{adv1['id']}/memories")
    assert res_check.json()[0]["text"] == "Adv 1 secret"

    # Attempt to DELETE mem1 using adv2 route
    res_del_tamper = client.delete(f"/api/adventures/{adv2['id']}/memories/{mem1_id}")
    assert res_del_tamper.status_code == 404, f"Expected 404 for cross-adventure DELETE, got {res_del_tamper.status_code}"

    # Verify mem1 still exists
    res_check = client.get(f"/api/adventures/{adv1['id']}/memories")
    assert len(res_check.json()) == 1


def test_api_patch_and_delete_edge_cases(client):
    """Tests non-existent IDs, partial patches, and double deletes."""
    adv = make_adventure()
    aid = adv["id"]

    # 1. PATCH nonexistent memory
    res = client.patch(f"/api/adventures/{aid}/memories/999999", json={"text": "New text"})
    assert res.status_code == 404

    # 2. DELETE nonexistent memory
    res = client.delete(f"/api/adventures/{aid}/memories/999999")
    assert res.status_code == 404

    # Create valid memory
    res = client.post(f"/api/adventures/{aid}/memories", json={"text": "Original text"})
    mid = res.json()["id"]

    # 3. PATCH with empty string -> 422 rejected by Pydantic min_length=1
    res_empty = client.patch(f"/api/adventures/{aid}/memories/{mid}", json={"text": ""})
    assert res_empty.status_code == 422

    # 4. PATCH without text (only seq fields)
    res_seq = client.patch(f"/api/adventures/{aid}/memories/{mid}", json={"seq_start": 10, "seq_end": 20})
    assert res_seq.status_code == 200
    assert res_seq.json()["seq_start"] == 10
    assert res_seq.json()["seq_end"] == 20
    assert res_seq.json()["text"] == "Original text"

    # 5. DELETE valid memory -> 204
    res_del = client.delete(f"/api/adventures/{aid}/memories/{mid}")
    assert res_del.status_code == 204

    # 6. DELETE again (idempotent 404)
    res_del_again = client.delete(f"/api/adventures/{aid}/memories/{mid}")
    assert res_del_again.status_code == 404


def test_api_special_characters_and_sql_injection(client):
    """Tests resilience against SQL injection attempts, unicode, emojis, and newlines."""
    adv = make_adventure()
    aid = adv["id"]

    attack_payloads = [
        "'; DROP TABLE memories; --",
        "' OR '1'='1",
        "🐉🐲🔥 Deep in the mountain caves of ᚠᛇᚻ ᛒᛦᚦ...",
        "Line 1\nLine 2\r\nLine 3\tTabbed",
        '{"json": "inside string", "quote": "\""}',
    ]

    for payload in attack_payloads:
        res = client.post(f"/api/adventures/{aid}/memories", json={"text": payload})
        assert res.status_code == 201, f"Failed on payload: {payload}"
        data = res.json()
        assert data["text"] == payload

    # Verify table memories still intact and has all inserted records
    res_list = client.get(f"/api/adventures/{aid}/memories")
    assert res_list.status_code == 200
    assert len(res_list.json()) == len(attack_payloads)


# =====================================================================
# 2. Summarizer & Embeddings Fail-Safe Tests
# =====================================================================

@pytest.mark.parametrize("error_scenario", [
    "timeout",
    "http500",
    "http404",
    "connect_error",
    "bad_json",
    "empty_data",
    "null_embedding",
    "string_embedding",
])
def test_embeddings_client_fail_safes(error_scenario):
    """Simulates various transport and payload failures in embeddings_client.get_embedding."""
    def mock_handler(request: httpx.Request) -> httpx.Response:
        if error_scenario == "timeout":
            raise httpx.ReadTimeout("Simulated timeout", request=request)
        elif error_scenario == "connect_error":
            raise httpx.ConnectError("Connection refused", request=request)
        elif error_scenario == "http500":
            return httpx.Response(500, json={"error": "Internal GPU / llama-server crash"})
        elif error_scenario == "http404":
            return httpx.Response(404, json={"error": "Not Found"})
        elif error_scenario == "bad_json":
            return httpx.Response(200, text="Not a valid JSON response {{{")
        elif error_scenario == "empty_data":
            return httpx.Response(200, json={"data": []})
        elif error_scenario == "null_embedding":
            return httpx.Response(200, json={"data": [{"embedding": None}]})
        elif error_scenario == "string_embedding":
            return httpx.Response(200, json={"data": [{"embedding": "not-a-list"}]})
        return httpx.Response(200, json={"data": [{"embedding": [0.1] * 1024}]})

    transport = httpx.MockTransport(mock_handler)
    cfg = {"base_url": "http://127.0.0.1:8081/v1", "model": "Qwen3-Embedding-0.6B", "enabled": True}

    # Must return None and NEVER raise an exception
    result = embeddings_client.get_embedding("Test text", cfg=cfg, transport=transport)
    assert result is None, f"Expected None on {error_scenario}, got {result}"


def test_summarizer_maybe_update_never_crashes_under_failures(tmp_db):
    """Verifies that maybe_update records memory verbatim and stores NULL vector under failure."""
    adv = make_adventure()
    for i in range(5):
        add_action(adv["id"], i, "continue", f"Action {i} detailed description of event.")

    class FailingLLM:
        def chat_completion(self, *args, **kwargs):
            raise RuntimeError("LLM network failure")

    class FailingEmb:
        def get_embedding(self, *args, **kwargs):
            raise httpx.ConnectError("Embedding llama-server offline")

    with db.session() as conn:
        cfg = settings_store.runtime_config(conn)
        cfg["memory_bank"] = {"trigger_tokens": 50}
        cfg["context_size"] = 7000
        cfg["embeddings"] = {"enabled": True, "model": "Qwen3-Embedding-0.6B"}

        # Call maybe_update with both LLM and Embedder failing completely
        summarizer.maybe_update(
            adv["id"],
            conn=conn,
            llm_client=FailingLLM(),
            embeddings_client=FailingEmb(),
            settings=cfg,
        )

        # Must have recorded verbatim excerpt, and embedding must be NULL
        mems = db.list_memories(conn, adv["id"], include_embedding=True)
        assert len(mems) >= 1
        assert mems[0]["embedding"] is None
        assert mems[0]["embedding_model"] is None
        assert mems[0]["embedding_dim"] is None
        assert "Action 0" in mems[0]["text"]


def test_vector_math_and_cosine_edge_cases():
    """Stress tests boundary conditions in vector packing, unpacking, and similarity."""
    # Empty and invalid pack
    assert embeddings_client.pack_vector([]) == b""

    # Unpack non-multiple of 4
    assert embeddings_client.unpack_vector(b"\x00\x01\x02") is None
    assert embeddings_client.unpack_vector(b"") is None
    assert embeddings_client.unpack_vector(None) is None

    # Mismatched lengths
    assert embeddings_client.cosine_similarity([1.0, 2.0], [1.0, 2.0, 3.0]) == 0.0

    # All zeros
    assert embeddings_client.cosine_similarity([0.0, 0.0], [0.0, 0.0]) == 0.0
    assert embeddings_client.cosine_similarity([0.0, 0.0], [1.0, 1.0]) == 0.0

    # Orthogonal and opposite
    assert embeddings_client.cosine_similarity([1.0, 0.0], [0.0, 1.0]) == 0.0
    assert abs(embeddings_client.cosine_similarity([1.0, 0.0], [-1.0, 0.0]) - (-1.0)) < 1e-6


# =====================================================================
# 3. Backfill Script Resilience Tests
# =====================================================================

def test_backfill_interrupted_run_and_resume(tmp_db):
    """Simulates an interruption (crash/kill) mid-backfill and verifies restartability."""
    adv = make_adventure()
    with db.session() as conn:
        for i in range(12):
            db.insert_memory(conn, adv["id"], f"Memory {i} for backfill test")

    call_count = 0

    class CrashMidwayEmb:
        def get_embedding(self, text, cfg=None, transport=None):
            nonlocal call_count
            call_count += 1
            if call_count > 5:
                raise KeyboardInterrupt("Simulated SIGINT / crash after 5 records")
            return [0.05] * 1024

        def pack_vector(self, vec):
            return embeddings_client.pack_vector(vec)

    # Run pass 1: crashes after 5
    with db.session() as conn:
        with pytest.raises(KeyboardInterrupt):
            tool_backfill(conn, client=CrashMidwayEmb(), batch_size=1)

    # Verify that exactly 5 records were committed to SQLite
    with db.session() as conn:
        embedded_rows = conn.execute(
            "SELECT COUNT(*) FROM memories WHERE embedding IS NOT NULL AND adventure_id = ?",
            (adv["id"],)
        ).fetchone()[0]
        unembedded_rows = conn.execute(
            "SELECT COUNT(*) FROM memories WHERE embedding IS NULL AND adventure_id = ?",
            (adv["id"],)
        ).fetchone()[0]
        assert embedded_rows == 5, f"Expected 5 committed records, got {embedded_rows}"
        assert unembedded_rows == 7, f"Expected 7 unembedded records, got {unembedded_rows}"

    # Run pass 2: healthy client resumes
    class HealthyEmb:
        def get_embedding(self, text, cfg=None, transport=None):
            return [0.05] * 1024

        def pack_vector(self, vec):
            return embeddings_client.pack_vector(vec)

    with db.session() as conn:
        s2 = tool_backfill(conn, client=HealthyEmb(), batch_size=2)
        assert s2["total"] == 7
        assert s2["updated"] == 7
        assert s2["failed"] == 0

    # Verify all 12 are now embedded
    with db.session() as conn:
        total_embedded = conn.execute(
            "SELECT COUNT(*) FROM memories WHERE embedding IS NOT NULL AND adventure_id = ?",
            (adv["id"],)
        ).fetchone()[0]
        assert total_embedded == 12

    # Run pass 3: idempotent re-run
    with db.session() as conn:
        s3 = tool_backfill(conn, client=HealthyEmb())
        assert s3["total"] == 0
        assert s3["updated"] == 0


def test_backfill_dry_run_accuracy(tmp_db):
    """Verifies that dry-run accurately reports count without touching database."""
    adv = make_adventure()
    with db.session() as conn:
        for i in range(8):
            db.insert_memory(conn, adv["id"], f"Dry run memory {i}")

    with db.session() as conn:
        stats = tool_backfill(conn, dry_run=True)
        assert stats["total"] == 8
        assert stats["updated"] == 0
        assert stats["skipped"] == 8

        # Database rows must still have embedding IS NULL
        unembedded = conn.execute("SELECT COUNT(*) FROM memories WHERE embedding IS NULL").fetchone()[0]
        assert unembedded == 8


def test_backfill_adventure_scoping(tmp_db):
    """Verifies that backfill with adventure_id only affects specified adventure."""
    adv1 = make_adventure(title="Adv 1")
    adv2 = make_adventure(title="Adv 2")

    with db.session() as conn:
        for i in range(4):
            db.insert_memory(conn, adv1["id"], f"Adv1 mem {i}")
            db.insert_memory(conn, adv2["id"], f"Adv2 mem {i}")

    class MockEmb:
        def get_embedding(self, text, cfg=None, transport=None):
            return [0.01] * 1024

        def pack_vector(self, vec):
            return embeddings_client.pack_vector(vec)

    with db.session() as conn:
        stats = tool_backfill(conn, adventure_id=adv1["id"], client=MockEmb())
        assert stats["total"] == 4
        assert stats["updated"] == 4

        # Adv 1 has 4 embedded, Adv 2 has 0 embedded
        c1 = conn.execute("SELECT COUNT(*) FROM memories WHERE embedding IS NOT NULL AND adventure_id = ?", (adv1["id"],)).fetchone()[0]
        c2 = conn.execute("SELECT COUNT(*) FROM memories WHERE embedding IS NOT NULL AND adventure_id = ?", (adv2["id"],)).fetchone()[0]
        assert c1 == 4
        assert c2 == 0


def test_backfill_cli_execution(tmp_path):
    """Runs the CLI backfill tool via subprocess to test exit codes and argument parsing."""
    db_file = tmp_path / "cli_test.db"
    python_bin = sys.executable

    # Initialize DB
    init_cmd = [
        python_bin, "-c",
        f"import sys; sys.path.insert(0, 'backend'); import app.db as db, os; os.environ['AIDUNGEON_DB_PATH'] = '{db_file}'; db.init_db()"
    ]
    sub_init = subprocess.run(init_cmd, cwd=str(REPO_ROOT), capture_output=True, text=True)
    assert sub_init.returncode == 0, f"Init DB failed: {sub_init.stderr}"

    # Test --help
    help_cmd = [python_bin, "tools/backfill_embeddings.py", "--help"]
    sub_help = subprocess.run(help_cmd, cwd=str(REPO_ROOT), capture_output=True, text=True)
    assert sub_help.returncode == 0
    assert "Backfill memory embeddings" in sub_help.stdout

    # Test --dry-run on initialized empty DB
    run_cmd = [
        python_bin, "tools/backfill_embeddings.py",
        "--db-path", str(db_file),
        "--dry-run"
    ]
    sub_run = subprocess.run(run_cmd, cwd=str(REPO_ROOT), capture_output=True, text=True)
    assert sub_run.returncode == 0
    assert "Summary:" in sub_run.stdout


# =====================================================================
# 4. Concurrency & Race Condition Tests
# =====================================================================

def test_concurrent_memory_bank_api_operations(client):
    """Fires concurrent threads performing insert, update, and read operations on SQLite."""
    adv = make_adventure()
    aid = adv["id"]

    num_threads = 8
    num_ops_per_thread = 5

    def worker(worker_id: int):
        created_ids = []
        errors = []
        for i in range(num_ops_per_thread):
            try:
                # 1. Create
                res = client.post(
                    f"/api/adventures/{aid}/memories",
                    json={"text": f"Worker {worker_id} op {i} memory"}
                )
                if res.status_code != 201:
                    errors.append(f"Create failed: {res.status_code} {res.text}")
                    continue
                mid = res.json()["id"]
                created_ids.append(mid)

                # 2. Patch
                res_patch = client.patch(
                    f"/api/adventures/{aid}/memories/{mid}",
                    json={"text": f"Worker {worker_id} op {i} updated"}
                )
                if res_patch.status_code != 200:
                    errors.append(f"Patch failed: {res_patch.status_code} {res_patch.text}")

                # 3. List
                res_list = client.get(f"/api/adventures/{aid}/memories")
                if res_list.status_code != 200:
                    errors.append(f"List failed: {res_list.status_code} {res_list.text}")
            except Exception as e:
                errors.append(f"Exception in worker {worker_id}: {e}")

        return created_ids, errors

    with concurrent.futures.ThreadPoolExecutor(max_workers=num_threads) as executor:
        futures = [executor.submit(worker, w) for w in range(num_threads)]
        all_errors = []
        all_created = []
        for f in concurrent.futures.as_completed(futures):
            created, errs = f.result()
            all_created.extend(created)
            all_errors.extend(errs)

    assert not all_errors, f"Encountered concurrency errors: {all_errors}"
    assert len(all_created) == num_threads * num_ops_per_thread

    # Final read
    final_res = client.get(f"/api/adventures/{aid}/memories")
    assert final_res.status_code == 200
    assert len(final_res.json()) == len(all_created)
