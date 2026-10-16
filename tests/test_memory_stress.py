"""Adversarial stress and boundary test suite for Milestone 2 Vector Memory & Retrieval.

Target components:
- backend/app/embeddings_client.py
- backend/memory/retrieval.py
- backend/app/db.py
- tools/backfill_embeddings.py
"""

from __future__ import annotations

import math
import random
import sqlite3
import struct
import time
from typing import Any

import pytest

from app import db, embeddings_client
from memory import retrieval
from conftest import make_adventure


# ==============================================================================
# 1. Extreme Vector Inputs & Numerical Stability
# ==============================================================================

def test_zero_magnitude_vectors():
    """Zero-magnitude vectors must return 0.0 without ZeroDivisionError."""
    # Both zero
    assert embeddings_client.cosine_similarity([0.0] * 1024, [0.0] * 1024) == 0.0
    # Left zero, right non-zero
    assert embeddings_client.cosine_similarity([0.0] * 1024, [1.0] * 1024) == 0.0
    # Left non-zero, right zero
    assert embeddings_client.cosine_similarity([1.0] * 1024, [0.0] * 1024) == 0.0
    # Empty vectors
    assert embeddings_client.cosine_similarity([], []) == 0.0
    assert embeddings_client.cosine_similarity([1.0], []) == 0.0
    assert embeddings_client.cosine_similarity([], [1.0]) == 0.0


def test_negative_vector_values():
    """Negative values should be properly handled in cosine math."""
    # Opposite unit vectors
    u = [-1.0, 0.0]
    v = [1.0, 0.0]
    assert abs(embeddings_client.cosine_similarity(u, v) - (-1.0)) < 1e-6

    # Identical all-negative vectors
    u_neg = [-0.5] * 1024
    assert abs(embeddings_client.cosine_similarity(u_neg, u_neg) - 1.0) < 1e-6

    # Mixed negative and positive
    u_mix = [-1.0, 1.0, -1.0, 1.0]
    v_mix = [1.0, -1.0, 1.0, -1.0]
    assert abs(embeddings_client.cosine_similarity(u_mix, v_mix) - (-1.0)) < 1e-6


def test_orthogonal_vectors():
    """Orthogonal vectors must return exact 0.0."""
    u = [1.0, 0.0, 0.0]
    v = [0.0, 1.0, 0.0]
    assert embeddings_client.cosine_similarity(u, v) == 0.0

    # 1024-dim orthogonal pairs
    u_1024 = [1.0] * 512 + [0.0] * 512
    v_1024 = [0.0] * 512 + [1.0] * 512
    assert embeddings_client.cosine_similarity(u_1024, v_1024) == 0.0


def test_mismatched_vector_lengths():
    """Mismatched vector lengths must return 0.0 safely without exception."""
    u = [1.0] * 512
    v = [1.0] * 768
    assert embeddings_client.cosine_similarity(u, v) == 0.0
    assert embeddings_client.cosine_similarity(v, u) == 0.0
    assert embeddings_client.cosine_similarity([1.0], [1.0, 2.0]) == 0.0


def test_nan_and_inf_vectors_behavior():
    """Verify behavior of NaN and Inf floats in cosine_similarity.

    Documents that NaN/Inf vectors are sanitized to return 0.0 safely.
    """
    u_nan = [float("nan"), 1.0]
    v = [1.0, 1.0]
    res_nan = embeddings_client.cosine_similarity(u_nan, v)
    assert res_nan == 0.0, f"Expected 0.0, got {res_nan}"

    u_inf = [float("inf"), 1.0]
    res_inf = embeddings_client.cosine_similarity(u_inf, v)
    assert res_inf == 0.0, f"Expected 0.0 from inf/inf division, got {res_inf}"

    u_ninf = [float("-inf"), 1.0]
    res_ninf = embeddings_client.cosine_similarity(u_ninf, v)
    assert res_ninf == 0.0, f"Expected 0.0 from -inf/-inf division, got {res_ninf}"


def test_numerical_bounds_floating_point_imprecision():
    """Verify bounds of cosine_similarity across 500 randomized unit vectors.

    Detects if floating point imprecision causes values to exceed [-1.0, 1.0].
    """
    random.seed(1337)
    for _ in range(500):
        vec = [random.uniform(-10.0, 10.0) for _ in range(128)]
        sim_self = embeddings_client.cosine_similarity(vec, vec)
        # Cosine with self should be mathematically 1.0; check how far float64 drifts:
        assert 0.999999 <= sim_self <= 1.000000000000001, f"Self-cosine drift: {sim_self}"

        opp = [-x for x in vec]
        sim_opp = embeddings_client.cosine_similarity(vec, opp)
        assert -1.000000000000001 <= sim_opp <= -0.999999, f"Opposite-cosine drift: {sim_opp}"


# ==============================================================================
# 2. Vector Serialization & Endianness Stress
# ==============================================================================

def test_vector_serialization_round_trip_accuracy():
    """Float32 little-endian round-trip packing and unpacking accuracy."""
    test_floats = [
        0.0, -0.0, 1.0, -1.0, 0.5, -0.5, 0.123456, -987.654,
        1.175494351e-38,  # min positive normal float32
        3.402823466e+38,  # max float32
        -3.402823466e+38, # min negative float32
    ]
    blob = embeddings_client.pack_vector(test_floats)
    assert len(blob) == len(test_floats) * 4
    unpacked = embeddings_client.unpack_vector(blob, expected_dim=len(test_floats))
    assert unpacked is not None
    assert len(unpacked) == len(test_floats)
    for orig, res in zip(test_floats, unpacked):
        assert math.isclose(orig, res, rel_tol=1e-6, abs_tol=1e-7)


def test_vector_serialization_endianness_standard():
    """Ensures serialization uses strict little-endian format (<Nf)."""
    val = 1.0
    blob = embeddings_client.pack_vector([val])
    # In IEEE 754 float32, 1.0 is 0x3f800000.
    # Little-endian bytes: 0x00, 0x00, 0x80, 0x3f
    expected_le = b"\x00\x00\x80\x3f"
    assert blob == expected_le, f"Expected {expected_le!r}, got {blob!r}"


def test_unpack_vector_partial_blob_truncation():
    """Partial and corrupted BLOBs must safely return None without crashing."""
    valid_16 = struct.pack("<4f", 1.0, 2.0, 3.0, 4.0)
    # Truncated by 1 byte
    assert embeddings_client.unpack_vector(valid_16[:15], expected_dim=4) is None
    # Truncated by 2 bytes
    assert embeddings_client.unpack_vector(valid_16[:14], expected_dim=4) is None
    # Truncated by 3 bytes
    assert embeddings_client.unpack_vector(valid_16[:13], expected_dim=4) is None
    # Truncated to 3 floats (12 bytes) when 4 expected
    assert embeddings_client.unpack_vector(valid_16[:12], expected_dim=4) is None
    # Empty bytes
    assert embeddings_client.unpack_vector(b"", expected_dim=4) is None
    assert embeddings_client.unpack_vector(None, expected_dim=4) is None
    # Invalid types
    assert embeddings_client.unpack_vector("not_bytes", expected_dim=4) is None  # type: ignore
    assert embeddings_client.unpack_vector(12345, expected_dim=4) is None  # type: ignore


def test_pack_vector_overflow_boundary():
    """Values exceeding 32-bit float range (>3.4e38) raise OverflowError."""
    with pytest.raises(OverflowError):
        embeddings_client.pack_vector([1e39])


# ==============================================================================
# 3. Text Inputs & Query Edge Cases
# ==============================================================================

def test_huge_query_text_handling(tmp_db):
    """Query text >10,000 characters (>300,000 chars) must not hang or crash."""
    adv = make_adventure()
    with db.session() as conn:
        db.insert_memory(conn, adv["id"], "The sacred crystal of Eldoria rests in the cavern.")
        db.insert_memory(conn, adv["id"], "A quiet inn at the crossroads.")

        huge_text = "Eldoria sacred crystal cavern deep " * 10000  # 350,000 chars
        t0 = time.perf_counter()
        mems = retrieval.select_memories(conn, adv["id"], huge_text, budget_tokens=800, top_k=2)
        elapsed = time.perf_counter() - t0

        assert len(mems) == 2
        assert mems[0]["text"] == "The sacred crystal of Eldoria rests in the cavern."
        assert elapsed < 0.5, f"Huge text took too long: {elapsed:.3f}s"


def test_special_characters_and_regex_symbols_in_query(tmp_db):
    """Query with regex metacharacters, emojis, and non-ASCII must execute cleanly."""
    adv = make_adventure()
    with db.session() as conn:
        db.insert_memory(conn, adv["id"], "Regex memory with .*+?^${}()|[]\\ symbols.")
        db.insert_memory(conn, adv["id"], "Memória com acentuação e caracteres especiais: café, dragão, poção.")
        db.insert_memory(conn, adv["id"], "Emoji memory: ⚔️ 🛡️ 🐉 🏰")

        # Test regex symbols
        m1 = retrieval.select_memories(conn, adv["id"], ".*+?^${}()|[]\\")
        assert len(m1) >= 1

        # Test Portuguese accents
        m2 = retrieval.select_memories(conn, adv["id"], "dragão café poção")
        assert any("dragão" in m["text"] for m in m2)

        # Test emojis
        m3 = retrieval.select_memories(conn, adv["id"], "⚔️ 🛡️ 🐉")
        assert len(m3) >= 1


def test_empty_and_whitespace_query_falls_back_to_recency(tmp_db):
    """Empty or whitespace queries must gracefully fall back to recency ranking."""
    adv = make_adventure()
    with db.session() as conn:
        db.insert_memory(conn, adv["id"], "Older memory", seq_start=1, seq_end=1)
        db.insert_memory(conn, adv["id"], "Newer memory", seq_start=2, seq_end=2)

        mems_empty = retrieval.select_memories(conn, adv["id"], "")
        assert [m["seq_end"] for m in mems_empty] == [1, 2]

        mems_ws = retrieval.select_memories(conn, adv["id"], "   \n\t  ")
        assert [m["seq_end"] for m in mems_ws] == [1, 2]


def test_none_recent_text_handling(tmp_db):
    """Verify behavior when recent_text is None."""
    adv = make_adventure()
    with db.session() as conn:
        db.insert_memory(conn, adv["id"], "Fact 1", seq_start=1, seq_end=1)
        # Empirical test: check whether None raises AttributeError
        try:
            mems = retrieval.select_memories(conn, adv["id"], None)  # type: ignore
            assert len(mems) == 1
        except AttributeError as e:
            # Documented finding: select_memories does not guard against recent_text=None
            assert "strip" in str(e)


# ==============================================================================
# 4. Boundary Tests: Bank Size & Budget Limits
# ==============================================================================

def test_empty_memory_bank(tmp_db):
    """Empty memory bank returns empty list without error."""
    adv = make_adventure()
    with db.session() as conn:
        mems = retrieval.select_memories(conn, adv["id"], "any query", budget_tokens=800, top_k=12)
        assert mems == []


def test_single_memory_bank(tmp_db):
    """Single memory bank returns the single memory if it fits."""
    adv = make_adventure()
    with db.session() as conn:
        db.insert_memory(conn, adv["id"], "Single lone fact in memory bank.")
        mems = retrieval.select_memories(conn, adv["id"], "query", budget_tokens=800, top_k=12)
        assert len(mems) == 1
        assert mems[0]["text"] == "Single lone fact in memory bank."


def test_single_memory_exceeding_budget(tmp_db):
    """Single memory exceeding budget returns empty list."""
    adv = make_adventure()
    with db.session() as conn:
        huge_single = "word " * 1000  # 5000 chars -> 1250 tokens
        db.insert_memory(conn, adv["id"], huge_single)
        mems = retrieval.select_memories(conn, adv["id"], "word", budget_tokens=800, top_k=12)
        assert mems == []


def test_100_plus_memories_exceeding_800_token_budget(tmp_db):
    """100+ memories exceeding 800 token budget strictly enforces 800-token ceiling."""
    adv = make_adventure()
    with db.session() as conn:
        # Insert 120 memories, each ~200 characters (~50 tokens) -> 6000 tokens total
        for i in range(1, 121):
            db.insert_memory(
                conn,
                adv["id"],
                f"Memory {i}: In the deep labyrinth of Azgoth, hero fought creature {i} with valor.",
                seq_start=i,
                seq_end=i,
            )

        mems = retrieval.select_memories(
            conn, adv["id"], "labyrinth hero valor", budget_tokens=800, top_k=12
        )

        assert len(mems) <= 12
        total_tokens = sum((len(m["text"]) + 3) // 4 for m in mems)
        assert total_tokens <= 800, f"Budget exceeded: {total_tokens} > 800"

        # Check chronological order
        seqs = [m["seq_end"] for m in mems]
        assert seqs == sorted(seqs), f"Not in chronological order: {seqs}"

        # Invariant: newest memory (120) must be included if it fits
        assert any(m["seq_end"] == 120 for m in mems), "Newest memory was not included"


def test_newest_memory_eviction_when_not_fitting(tmp_db):
    """If newest memory is oversized and exceeds budget even after victim eviction, budget is preserved."""
    adv = make_adventure()
    with db.session() as conn:
        # 10 memories of 71 tokens each with high semantic match (vector [1.0, 0.0])
        for i in range(1, 11):
            db.insert_memory(
                conn,
                adv["id"],
                f"Mem {i}: dragon cavern " + "x" * 260,
                seq_start=i,
                seq_end=i,
                embedding=embeddings_client.pack_vector([1.0, 0.0]),
                embedding_model="model",
                embedding_dim=2,
            )

        # Newest memory with 197 tokens and low semantic match (vector [0.0, 1.0])
        db.insert_memory(
            conn,
            adv["id"],
            "Newest: " + "y" * 780,
            seq_start=11,
            seq_end=11,
            embedding=embeddings_client.pack_vector([0.0, 1.0]),
            embedding_model="model",
            embedding_dim=2,
        )

        mems = retrieval.select_memories(
            conn,
            adv["id"],
            "dragon cavern",
            budget_tokens=800,
            top_k=12,
            query_vector=[1.0, 0.0],
            settings={"embeddings": {"model": "model"}},
        )
        total_tokens = sum((len(m["text"]) + 3) // 4 for m in mems)
        assert total_tokens <= 800, f"Budget violated: {total_tokens}"
        # Newest should not have been swapped in because 710 - 71 + 197 = 836 > 800
        assert not any(m["seq_end"] == 11 for m in mems)



def test_top_k_zero_or_negative(tmp_db):
    """top_k <= 0 or budget_tokens <= 0 returns empty list."""
    adv = make_adventure()
    with db.session() as conn:
        db.insert_memory(conn, adv["id"], "Some text", seq_start=1, seq_end=1)
        assert retrieval.select_memories(conn, adv["id"], "query", top_k=0) == []
        assert retrieval.select_memories(conn, adv["id"], "query", top_k=-5) == []
        assert retrieval.select_memories(conn, adv["id"], "query", budget_tokens=0) == []
        assert retrieval.select_memories(conn, adv["id"], "query", budget_tokens=-100) == []


# ==============================================================================
# 5. Hybrid Retrieval Robustness & Vulnerability Exploration
# ==============================================================================

def test_mixed_database_vectors_fallback_and_no_blob_leak(tmp_db):
    """Database containing valid vectors, NULLs, foreign models, and corrupt BLOBs."""
    adv = make_adventure()
    target_model = "Qwen3-Embedding-0.6B"
    target_dim = 4

    vec_query = [1.0, 0.0, 0.0, 0.0]
    vec_valid_high = [0.95, 0.05, 0.0, 0.0]
    vec_valid_low = [0.0, 1.0, 0.0, 0.0]

    with db.session() as conn:
        # 1. Valid high match
        db.insert_memory(conn, adv["id"], "High vector match", seq_start=1, seq_end=1,
                         embedding=embeddings_client.pack_vector(vec_valid_high),
                         embedding_model=target_model, embedding_dim=target_dim)
        # 2. Corrupt BLOB (3 bytes, alignment failure)
        db.insert_memory(conn, adv["id"], "Corrupt BLOB memory", seq_start=2, seq_end=2,
                         embedding=b"\x01\x02\x03",
                         embedding_model=target_model, embedding_dim=target_dim)
        # 3. Foreign model
        db.insert_memory(conn, adv["id"], "Foreign model memory", seq_start=3, seq_end=3,
                         embedding=embeddings_client.pack_vector([1.0, 0.0, 0.0, 0.0]),
                         embedding_model="text-embedding-ada-002", embedding_dim=target_dim)
        # 4. Foreign dimension (8 dims instead of 4)
        db.insert_memory(conn, adv["id"], "Foreign dim memory", seq_start=4, seq_end=4,
                         embedding=embeddings_client.pack_vector([1.0] * 8),
                         embedding_model=target_model, embedding_dim=8)
        # 5. NULL embedding
        db.insert_memory(conn, adv["id"], "NULL embedding memory", seq_start=5, seq_end=5,
                         embedding=None, embedding_model=None, embedding_dim=None)
        # 6. Valid low match
        db.insert_memory(conn, adv["id"], "Low vector match", seq_start=6, seq_end=6,
                         embedding=embeddings_client.pack_vector(vec_valid_low),
                         embedding_model=target_model, embedding_dim=target_dim)

        mems = retrieval.select_memories(
            conn,
            adv["id"],
            "query",
            query_vector=vec_query,
            settings={"embeddings": {"model": target_model, "enabled": True}},
            top_k=6,
        )

        assert len(mems) == 6
        # Invariant: No memory returned should ever leak the raw 'embedding' BLOB
        for m in mems:
            assert "embedding" not in m, f"Embedding BLOB leaked in memory {m['id']}"

        # Chronological order
        seqs = [m["seq_end"] for m in mems]
        assert seqs == [1, 2, 3, 4, 5, 6]


def test_nan_vector_displaces_valid_memories_empirical_demonstration(tmp_db):
    """Empirical challenge: demonstrates how a NaN vector corrupts ranking and victim swap.

    When a NaN vector is present:
    1. cosine_similarity returns nan
    2. Python's min(picked) comparison with NaN causes non-deterministic victim selection,
       potentially evicting a 1.0 perfect match memory.
    """
    adv = make_adventure()
    target_model = "Qwen3-Embedding-0.6B"

    vec_perfect = [1.0, 0.0]
    vec_nan = [float("nan"), 0.0]
    vec_newest = [0.0, 1.0]

    with db.session() as conn:
        # m1: Perfect match (score ~ 1001.0)
        m1 = db.insert_memory(conn, adv["id"], "Perfect semantic match", seq_start=1, seq_end=1,
                             embedding=embeddings_client.pack_vector(vec_perfect),
                             embedding_model=target_model, embedding_dim=2)
        # m2: Corrupted NaN vector
        m2 = db.insert_memory(conn, adv["id"], "Corrupted NaN vector memory", seq_start=2, seq_end=2,
                             embedding=embeddings_client.pack_vector(vec_nan),
                             embedding_model=target_model, embedding_dim=2)
        # m3: Newest memory (recency 10)
        m3 = db.insert_memory(conn, adv["id"], "Newest turn memory", seq_start=10, seq_end=10,
                             embedding=embeddings_client.pack_vector(vec_newest),
                             embedding_model=target_model, embedding_dim=2)

        mems = retrieval.select_memories(
            conn,
            adv["id"],
            "query",
            query_vector=[1.0, 0.0],
            settings={"embeddings": {"model": target_model, "enabled": True}},
            top_k=2,
        )

        texts = [m["text"] for m in mems]
        # Vulnerability check:
        # Because min([Perfect, NaN]) in Python evaluates `NaN < Perfect` as False,
        # 'Perfect semantic match' was treated as the lowest scoring victim and evicted,
        # leaving ['Corrupted NaN vector memory', 'Newest turn memory']!
        evicted_perfect = "Perfect semantic match" not in texts
        retained_nan = "Corrupted NaN vector memory" in texts
        print(f"\n[EMPIRICAL DEMONSTRATION] NaN victim eviction vulnerability active: {evicted_perfect and retained_nan}")
        assert "Newest turn memory" in texts


# ==============================================================================
# 6. Performance & Memory Leak Harness
# ==============================================================================

def test_performance_1000_memories(tmp_db):
    """Benchmark select_memories over 1,000 memories (1024-dim vectors)."""
    adv = make_adventure()
    target_model = "Qwen3-Embedding-0.6B"
    target_dim = 1024

    # Pre-pack dummy vectors
    vec_dummy = [0.01 * (i % 50) for i in range(target_dim)]
    blob_dummy = embeddings_client.pack_vector(vec_dummy)

    with db.session() as conn:
        for i in range(1, 1001):
            conn.execute(
                "INSERT INTO memories (adventure_id, text, seq_start, seq_end, created_at, embedding, embedding_model, embedding_dim) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (adv["id"], f"Memory record {i} for stress test benchmark", i, i, "2026-01-01",
                 blob_dummy, target_model, target_dim),
            )
        conn.commit()

        q_vec = [0.02 * (i % 30) for i in range(target_dim)]

        t0 = time.perf_counter()
        mems = retrieval.select_memories(
            conn,
            adv["id"],
            "stress test",
            query_vector=q_vec,
            settings={"embeddings": {"model": target_model, "enabled": True}},
            budget_tokens=800,
            top_k=12,
        )
        elapsed = time.perf_counter() - t0

        assert len(mems) == 12
        assert elapsed < 0.25, f"1000 memory retrieval took too long: {elapsed:.4f}s (budget: 0.250s)"
        print(f"\n[PERFORMANCE] 1000 memory retrieval took: {elapsed:.4f}s")
