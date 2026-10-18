"""Vetor da consulta das memórias fora do TTFT (memory.retrieval).

O embedding da consulta roda antes do narrador. Com o serviço na CPU e o
narrador ocupado, os 7500 caracteres da varredura passavam de 30 s. O turno
usa o vetor da cena gravado no pós-turno; sem ele, embute só o fim do texto,
com prazo curto.
"""

from __future__ import annotations

import pytest

from app import db, embeddings_client, game_engine, settings_store
from memory import retrieval

from conftest import add_action, make_adventure

MODEL = "Qwen3-Embedding-0.6B"
EMB_CFG = {"enabled": True, "base_url": "http://emb.test/v1", "model": MODEL,
           "timeout_seconds": 30}
CLARA = "Clara guarda a chave da despensa."
BARCO = "O barco do pescador afundou."


@pytest.fixture(autouse=True)
def _clean_scene_vectors():
    retrieval.forget_scene_vectors()
    yield
    retrieval.forget_scene_vectors()


class FakeEmb:
    """Dublê do embeddings_client: grava as chamadas e devolve vetores fixos."""

    def __init__(self, vectors: dict | None = None, fail: bool = False):
        self.vectors = vectors or {}
        self.fail = fail
        self.calls: list[dict] = []

    def get_embedding(self, text, cfg=None, transport=None):
        self.calls.append({"text": text, "cfg": cfg})
        if self.fail:
            return None
        return self.vectors.get(text, [0.0, 1.0])


def _memories(adv_id: str, *, embedded: bool = True) -> None:
    """CLARA alinhada com [1, 0]; BARCO, mais recente, com [0, 1]."""
    with db.session() as conn:
        for text, seq, vec in ((CLARA, 2, [1.0, 0.0]), (BARCO, 4, [0.0, 1.0])):
            extra = {}
            if embedded:
                extra = {"embedding": embeddings_client.pack_vector(vec),
                         "embedding_model": MODEL, "embedding_dim": 2}
            db.insert_memory(conn, adv_id, text, seq_start=seq - 1, seq_end=seq, **extra)


def _select(adv_id: str, recent_text: str, emb: FakeEmb, **bank) -> list[str]:
    settings = {"memory_bank": {"top_k": 1, **bank}, "embeddings": EMB_CFG}
    with db.session() as conn:
        mems = retrieval.select_memories(conn, adv_id, recent_text, budget_tokens=600,
                                         settings=settings, embeddings_client_mod=emb)
    return [m["text"] for m in mems]


def test_turn_uses_the_scene_vector_primed_after_the_previous_turn(tmp_db):
    adv = make_adventure()
    _memories(adv["id"])
    add_action(adv["id"], 5, "continue", "Clara some na cozinha.")
    primer = FakeEmb(vectors={"Clara some na cozinha.": [1.0, 0.0]})
    assert retrieval.prime_scene_vector(adv["id"], "Clara some na cozinha.", 5, EMB_CFG,
                                        embeddings_client_mod=primer)
    add_action(adv["id"], 6, "do", "olho em volta")

    turn = FakeEmb(fail=True)
    assert _select(adv["id"], "Você olha em volta.", turn) == [CLARA]
    assert turn.calls == []


def test_scene_vector_from_before_an_undo_is_ignored(tmp_db):
    adv = make_adventure()
    _memories(adv["id"])
    for seq in range(4):
        add_action(adv["id"], seq, "continue", f"Trecho {seq}.")
    retrieval.remember_scene_vector(adv["id"], 6, MODEL, [1.0, 0.0])

    turn = FakeEmb()
    assert _select(adv["id"], "Você olha em volta.", turn) == [BARCO]
    assert len(turn.calls) == 1


def test_scene_vector_older_than_the_lag_is_ignored(tmp_db):
    adv = make_adventure()
    _memories(adv["id"])
    retrieval.remember_scene_vector(adv["id"], 0, MODEL, [1.0, 0.0])
    for seq in range(retrieval.SCENE_VECTOR_MAX_LAG + 2):
        add_action(adv["id"], seq, "continue", f"Trecho {seq}.")

    turn = FakeEmb()
    assert _select(adv["id"], "Você olha em volta.", turn) == [BARCO]
    assert len(turn.calls) == 1


def test_turn_without_scene_vector_embeds_only_the_tail_with_a_short_timeout(tmp_db):
    adv = make_adventure()
    _memories(adv["id"])
    emb = FakeEmb()
    _select(adv["id"], "palavra " * 2000 + "Clara aparece na porta.", emb)

    [call] = emb.calls
    assert len(call["text"]) <= retrieval.DEFAULT_QUERY_CHARS
    assert call["text"].startswith("palavra ")
    assert call["text"].endswith("Clara aparece na porta.")
    assert call["cfg"]["timeout_seconds"] == retrieval.DEFAULT_QUERY_TIMEOUT_S
    assert EMB_CFG["timeout_seconds"] == 30


def test_query_size_and_deadline_come_from_memory_bank_settings(tmp_db):
    adv = make_adventure()
    _memories(adv["id"])
    emb = FakeEmb()
    _select(adv["id"], "x" * 900, emb, query_chars=200, query_timeout_s=0.5)

    [call] = emb.calls
    assert call["text"] == "x" * 200
    assert call["cfg"]["timeout_seconds"] == 0.5


def test_zero_deadline_skips_the_turn_embedding(tmp_db):
    adv = make_adventure()
    _memories(adv["id"])
    emb = FakeEmb()
    assert _select(adv["id"], "Você olha em volta.", emb, query_timeout_s=0) == [BARCO]
    assert emb.calls == []


def test_failed_turn_embedding_falls_back_to_keywords(tmp_db):
    adv = make_adventure()
    _memories(adv["id"])
    emb = FakeEmb(fail=True)
    assert _select(adv["id"], "A chave da despensa sumiu.", emb) == [CLARA]
    assert len(emb.calls) == 1


def test_turn_embedding_is_reused_while_the_story_stands_still(tmp_db):
    adv = make_adventure()
    _memories(adv["id"])
    add_action(adv["id"], 0, "continue", "Trecho.")
    emb = FakeEmb(vectors={"Clara?": [1.0, 0.0]})
    assert _select(adv["id"], "Clara?", emb) == [CLARA]
    assert _select(adv["id"], "Clara?", emb) == [CLARA]
    assert len(emb.calls) == 1


def test_memories_without_vectors_skip_the_embedding(tmp_db):
    adv = make_adventure()
    _memories(adv["id"], embedded=False)
    emb = FakeEmb()
    assert _select(adv["id"], "Você olha em volta.", emb) == [BARCO]
    assert emb.calls == []


def test_query_tail_starts_on_a_whole_word():
    assert retrieval.query_tail("  curto  ") == "curto"
    assert retrieval.query_tail("abcdef ghij", 6) == "ghij"
    assert retrieval.query_tail("x" * 50, 10) == "x" * 10


def test_post_turn_primes_the_scene_vector(tmp_db, monkeypatch):
    adv = make_adventure()
    add_action(adv["id"], 0, "start", "Costazul acorda com fome. " * 100)
    add_action(adv["id"], 1, "see", "portrait of Clara, red dress")
    add_action(adv["id"], 2, "do", "procuro a Clara")
    seen: list[str] = []

    def fake_get_embedding(text, cfg=None, transport=None):
        seen.append(text)
        return [1.0, 0.0]

    monkeypatch.setattr(embeddings_client, "get_embedding", fake_get_embedding)
    monkeypatch.setattr(settings_store, "effective_embeddings_settings",
                        lambda conn=None: dict(EMB_CFG))
    game_engine._prime_memory_query(adv["id"])

    [text] = seen
    assert text.endswith("> You procuro a Clara")
    assert "portrait" not in text
    assert len(text) <= retrieval.DEFAULT_QUERY_CHARS
    assert retrieval.scene_vector(adv["id"], MODEL, 3) == [1.0, 0.0]
