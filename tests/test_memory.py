"""Testes do pacote backend/memory — triggers, retrieval e summarizer.

- triggers: pinned sempre, keyword case-insensitive, budgets, sem trigger não entra.
- retrieval: recency + keyword overlap, top_k e budget, stopwords ignoradas.
- summarizer: LLM mockado via dublê injetado (FakeLLM) e via httpx.MockTransport
  no llm_client real; falha do LLM é sempre silenciosa.
"""

from __future__ import annotations

import json

import httpx

from app import db, llm_client, settings_store
from memory import retrieval, summarizer, triggers

from conftest import add_action, make_adventure


# ------------------------------------------------------------------ helpers
def _cfg(conn, **over):
    """runtime_config real com overrides aninhados (ex.: memory_bank={...})."""
    cfg = settings_store.runtime_config(conn)
    for key, value in over.items():
        if isinstance(value, dict) and isinstance(cfg.get(key), dict):
            cfg[key].update(value)
        else:
            cfg[key] = value
    return cfg


def _actions(adv_id: str, n: int, chars: int = 200, start_seq: int = 0) -> None:
    for i in range(n):
        seq = start_seq + i
        add_action(adv_id, seq, "continue", f"Action {seq}. " + "x" * chars)


class FakeLLM:
    """Dublê do módulo llm_client: só chat_completion (sem stream)."""

    def __init__(self, reply: str = "Resumo fake.", fail: bool = False):
        self.reply = reply
        self.fail = fail
        self.calls: list[dict] = []

    def chat_completion(self, cfg, messages, *, temperature, max_tokens,
                        transport=None):
        self.calls.append({"cfg": cfg, "messages": messages,
                           "temperature": temperature, "max_tokens": max_tokens})
        if self.fail:
            raise RuntimeError("llm fora do ar")
        return self.reply


def _json_llm_transport(reply: str = "You did things.", capture: list | None = None):
    """MockTransport com resposta NÃO-stream de /chat/completions."""
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/models"):
            return httpx.Response(200, json={"data": [{"id": "mock-model"}]})
        if path.endswith("/chat/completions"):
            if capture is not None:
                capture.append(json.loads(request.content.decode("utf-8")))
            return httpx.Response(200, json={
                "choices": [{"message": {"content": reply}}],
            })
        return httpx.Response(404, json={"error": f"no mock route: {path}"})

    return httpx.MockTransport(handler)


# ================================================================== triggers
def test_pinned_card_always_included(tmp_db):
    adv = make_adventure()
    with db.session() as conn:
        db.insert_card(conn, adv["id"], title="Fixo", entry="Sempre presente.",
                       pinned=True)
        db.insert_card(conn, adv["id"], title="Solto", entry="Sem gatilho.",
                       triggers=["xyzzy"])
        cards = triggers.select_cards(conn, adv["id"], "texto sem nenhuma keyword",
                                      budget_tokens=1200)
    assert [c["title"] for c in cards] == ["Fixo"]


def test_keyword_trigger_case_insensitive(tmp_db):
    adv = make_adventure()
    with db.session() as conn:
        db.insert_card(conn, adv["id"], title="Cidade",
                       triggers=["baldur's gate"], entry="Porto movimentado.")
        cards = triggers.select_cards(conn, adv["id"],
                                      "You sail to BALDUR'S GATE at dawn",
                                      budget_tokens=1200)
    assert [c["title"] for c in cards] == ["Cidade"]


def test_no_trigger_no_card(tmp_db):
    adv = make_adventure()
    with db.session() as conn:
        db.insert_card(conn, adv["id"], title="Zhentarim",
                       triggers=["zhentarim"], entry="Rede sombria.")
        cards = triggers.select_cards(conn, adv["id"], "You walk alone.",
                                      budget_tokens=1200)
    assert cards == []


def test_scan_window_limits_to_last_chars(tmp_db):
    adv = make_adventure()
    with db.session() as conn:
        db.insert_card(conn, adv["id"], title="Antigo", triggers=["dragon"],
                       entry="...")
        recent = "dragon " + "x" * 2000  # trigger fora dos últimos 1000 chars
        cards = triggers.select_cards(conn, adv["id"], recent, budget_tokens=1200,
                                      settings={"scan_chars": 1000,
                                                "budget_por_card": 150})
    assert cards == []


def test_total_budget_caps_block(tmp_db):
    adv = make_adventure()
    with db.session() as conn:
        for i in range(5):  # entry 400 chars → ~102 tk → truncado p/ 60 tk cada
            db.insert_card(conn, adv["id"], title=f"P{i:02d}",
                           entry="e" * 400, pinned=True)
        cards = triggers.select_cards(conn, adv["id"], "", budget_tokens=130,
                                      settings={"scan_chars": 1000,
                                                "budget_por_card": 60})
    assert len(cards) == 2  # 2×60 cabem em 130; o 3º estouraria
    assert [c["title"] for c in cards] == ["P00", "P01"]


def test_card_ordering_pinned_first_then_title(tmp_db):
    adv = make_adventure()
    with db.session() as conn:
        db.insert_card(conn, adv["id"], title="Zulu", triggers=["foo"], entry="z")
        db.insert_card(conn, adv["id"], title="Alpha", triggers=["foo"], entry="a")
        db.insert_card(conn, adv["id"], title="Pinned", pinned=True, entry="p")
        cards = triggers.select_cards(conn, adv["id"], "foo", budget_tokens=1200)
    assert [c["title"] for c in cards] == ["Pinned", "Alpha", "Zulu"]


def test_render_card_text_format_and_truncation():
    card = {"title": "T", "entry": "abcde"}
    assert triggers.render_card_text(card, 2) == "T: abcde"      # 8 chars = 2 tk
    assert triggers.render_card_text(card, 1) == "T: a"          # trunca a 4 chars
    assert triggers.render_card_text({"title": "", "entry": "e"}, 5) == "e"


def test_triggers_tolerates_raw_keys_string(tmp_db):
    adv = make_adventure()
    with db.session() as conn:
        # grava triggers como string solta (formato pré-import "a, b ,")
        conn.execute(
            "INSERT INTO story_cards(id, adventure_id, title, triggers, entry)"
            " VALUES('c1', ?, 'Raw', ' foo , bar ,', 'x')", (adv["id"],))
        cards = triggers.select_cards(conn, adv["id"], "say BAR loud",
                                      budget_tokens=1200)
    assert [c["title"] for c in cards] == ["Raw"]


# ================================================================= retrieval
def test_recency_order_and_top_k(tmp_db):
    adv = make_adventure()
    with db.session() as conn:
        for i in range(4):
            db.insert_memory(conn, adv["id"], f"mem {i} some words here",
                             seq_start=i, seq_end=i)
        mems = retrieval.select_memories(conn, adv["id"], "", budget_tokens=600,
                                         top_k=2)
    assert [m["seq_end"] for m in mems] == [2, 3]  # cronológico, os dois mais recentes


def test_keyword_overlap_can_beat_close_recency(tmp_db):
    adv = make_adventure()
    with db.session() as conn:
        db.insert_memory(conn, adv["id"],
                         "You promised Thuldan a sealed scroll delivery",
                         seq_start=8, seq_end=9)
        db.insert_memory(conn, adv["id"], "You had breakfast alone at the inn",
                         seq_start=10, seq_end=10)
        mems = retrieval.select_memories(
            conn, adv["id"],
            "Thuldan asks about the sealed scroll you promised",
            budget_tokens=600, top_k=1)
    # overlap (thuldan/sealed/scroll/promised) vence a recency de 1 seq
    assert mems[0]["seq_end"] == 9


def test_overlap_beats_distant_recency(tmp_db):
    adv = make_adventure()
    with db.session() as conn:
        db.insert_memory(conn, adv["id"], "the sealed scroll matter",
                         seq_start=0, seq_end=1)
        db.insert_memory(conn, adv["id"], "nothing relevant here",
                         seq_start=9, seq_end=10)
        only = retrieval.select_memories(conn, adv["id"], "sealed scroll",
                                         budget_tokens=600, top_k=1)
        both = retrieval.select_memories(conn, adv["id"], "sealed scroll",
                                         budget_tokens=600, top_k=2)
    assert only[0]["seq_end"] == 1  # o fato antigo vence a recência
    assert [m["seq_end"] for m in both] == [1, 10]  # o recente entra, em ordem


def test_budget_skips_oversized_and_keeps_smaller(tmp_db):
    adv = make_adventure()
    with db.session() as conn:
        db.insert_memory(conn, adv["id"], "b" * 4000, seq_start=0, seq_end=5)
        db.insert_memory(conn, adv["id"], "tiny note", seq_start=0, seq_end=4)
        mems = retrieval.select_memories(conn, adv["id"], "", budget_tokens=100,
                                         top_k=5)
    assert [m["text"] for m in mems] == ["tiny note"]


def test_top_k_and_settings_injection(tmp_db):
    adv = make_adventure()
    with db.session() as conn:
        for i in range(3):
            db.insert_memory(conn, adv["id"], f"m{i} words", seq_start=0,
                             seq_end=i)
        mems = retrieval.select_memories(conn, adv["id"], "", budget_tokens=600,
                                         settings={"memory_bank": {"top_k": 2}})
    assert len(mems) == 2  # top_k default veio de settings.memory_bank.top_k


def test_stopwords_and_short_words_do_not_count():
    assert retrieval.significant_words("the and of to in a an it is") == set()
    assert retrieval.significant_words("Thuldan and the scroll") == \
        {"thuldan", "scroll"}
    assert retrieval.significant_words("que não com para uma princesa") == {"princesa"}


def test_memory_without_seq_falls_back_to_id(tmp_db):
    adv = make_adventure()
    with db.session() as conn:
        db.insert_memory(conn, adv["id"], "older imported")  # seq NULL
        db.insert_memory(conn, adv["id"], "newer imported")
        mems = retrieval.select_memories(conn, adv["id"], "", budget_tokens=600,
                                         top_k=1)
    assert mems[0]["text"] == "newer imported"  # id maior = mais recente


# =============================================================== summarizer
def test_memory_bank_creates_memory_over_trigger(tmp_db):
    adv = make_adventure()
    _actions(adv["id"], 4, chars=200)  # ~53 tk cada; trigger 100 → chunk = 2
    fake = FakeLLM(reply="You did things.")
    with db.session() as conn:
        cfg = _cfg(conn, memory_bank={"trigger_tokens": 100}, context_size=7000)
        assert summarizer.maybe_update(adv["id"], conn=conn, llm_client=fake,
                                       settings=cfg) is None
        mems = db.list_memories(conn, adv["id"])
    assert len(fake.calls) == 2  # arquivo do que saiu da janela + story summary
    assert len(mems) == 1
    assert mems[0]["text"] == "You did things."
    assert mems[0]["seq_start"] == 0
    assert mems[0]["seq_end"] == 1  # um trecho por chamada (2 ações ≥ 100 tk)
    prompt = fake.calls[0]["messages"][0]["content"]
    assert "at most 120 words" in prompt
    assert "Action 0." in prompt and "Action 1." in prompt
    assert "Action 2." not in prompt  # restante fica p/ a próxima geração
    assert fake.calls[0]["temperature"] == 0.3
    assert fake.calls[0]["max_tokens"] == 200


def test_memory_bank_below_trigger_no_llm_call(tmp_db):
    adv = make_adventure()
    add_action(adv["id"], 0, "start", "Short intro.")
    fake = FakeLLM()
    with db.session() as conn:
        summarizer.maybe_update(adv["id"], conn=conn, llm_client=fake,
                                settings=_cfg(conn))
        mems = db.list_memories(conn, adv["id"])
    assert fake.calls == []
    assert mems == []


def test_memory_bank_resumes_from_last_memory(tmp_db):
    adv = make_adventure()
    _actions(adv["id"], 6, chars=200)
    with db.session() as conn:
        db.insert_memory(conn, adv["id"], "coberto até 3", seq_start=0, seq_end=3)
        fake = FakeLLM(reply="Later events.")
        cfg = _cfg(conn, memory_bank={"trigger_tokens": 100}, context_size=7000)
        summarizer.maybe_update(adv["id"], conn=conn, llm_client=fake,
                                settings=cfg)
        mems = db.list_memories(conn, adv["id"])
    # só ações com seq > 3 entram no trecho
    assert mems[-1]["seq_start"] == 4
    assert "Action 4." in fake.calls[0]["messages"][0]["content"]
    assert "Action 3." not in fake.calls[0]["messages"][0]["content"]


def test_story_summary_updated_when_window_slides(tmp_db):
    adv = make_adventure()
    _actions(adv["id"], 3, chars=580)  # ~148 tk cada; janela 100 → nada cabe
    fake = FakeLLM(reply="You reached Calimport and met Thuldan.")
    with db.session() as conn:
        cfg = _cfg(conn, context_size=3800 + 512 + 100)  # janela = 100 tk
        summarizer.maybe_update(adv["id"], conn=conn, llm_client=fake,
                                settings=cfg)
        adv2 = db.get_adventure(conn, adv["id"])
        covered = db.get_setting(conn, f"memory.summary_covered_seq.{adv['id']}")
    # o que saiu da janela vira memória e o summary reescreve o "agora"
    assert len(fake.calls) == 2
    assert adv2["story_summary"] == "You reached Calimport and met Thuldan."
    assert covered == "1"  # última ação (seq 2) nunca é "antiga"
    prompt = next(call["messages"][0]["content"] for call in fake.calls
                  if "running summary" in call["messages"][0]["content"])
    assert "running summary" in prompt
    assert "New events:" in prompt
    assert "Action 0." in prompt and "Action 1." in prompt


def test_story_summary_not_rewritten_without_new_old_actions(tmp_db):
    adv = make_adventure()
    _actions(adv["id"], 3, chars=580)
    fake = FakeLLM(reply="Summary v1.")
    with db.session() as conn:
        cfg = _cfg(conn, context_size=3800 + 512 + 100)
        summarizer.maybe_update(adv["id"], conn=conn, llm_client=fake,
                                settings=cfg)
    with db.session() as conn:  # 2ª passada sem ações novas → sem nova chamada
        summarizer.maybe_update(adv["id"], conn=conn, llm_client=fake,
                                settings=_cfg(conn, context_size=3800 + 512 + 100))
        adv2 = db.get_adventure(conn, adv["id"])
    assert len(fake.calls) == 2  # 1ª passada: arquivo + summary; 2ª não chama
    assert adv2["story_summary"] == "Summary v1."


def test_imported_summary_marks_position_without_llm(tmp_db):
    adv = make_adventure(story_summary="Resumo que veio do backup.")
    _actions(adv["id"], 3, chars=580)
    fake = FakeLLM()
    with db.session() as conn:
        cfg = _cfg(conn, context_size=3800 + 512 + 100)
        summarizer.maybe_update(adv["id"], conn=conn, llm_client=fake,
                                settings=cfg)
        covered = db.get_setting(conn, f"memory.summary_covered_seq.{adv['id']}")
        adv2 = db.get_adventure(conn, adv["id"])
    assert covered == "1"
    assert adv2["story_summary"] == "Resumo que veio do backup."
    assert all("running summary" not in call["messages"][0]["content"]
               for call in fake.calls)


def test_summary_respects_budget_hard_cap(tmp_db):
    adv = make_adventure()
    _actions(adv["id"], 3, chars=580)
    fake = FakeLLM(reply="y" * 4000)  # 1000 tk >> budget 800
    with db.session() as conn:
        cfg = _cfg(conn, context_size=3800 + 512 + 100)
        summarizer.maybe_update(adv["id"], conn=conn, llm_client=fake,
                                settings=cfg)
        adv2 = db.get_adventure(conn, adv["id"])
    assert len(adv2["story_summary"]) <= 800 * 4


def test_llm_failure_is_silent(tmp_db):
    adv = make_adventure()
    _actions(adv["id"], 4, chars=580)  # dispara memory bank E story summary
    fake = FakeLLM(fail=True)
    with db.session() as conn:
        cfg = _cfg(conn, memory_bank={"trigger_tokens": 100},
                   context_size=3800 + 512 + 100)
        result = summarizer.maybe_update(adv["id"], conn=conn, llm_client=fake,
                                         settings=cfg)
        mems = db.list_memories(conn, adv["id"])
        covered = db.get_setting(conn, f"memory.summary_covered_seq.{adv['id']}")
        adv2 = db.get_adventure(conn, adv["id"])
    assert result is None            # nunca levanta exceção
    assert len(mems) == 1            # o arquivo cru fica, mesmo com o LLM fora
    assert "Action 0." in mems[0]["text"]
    assert covered is None           # summary não avança (tenta de novo depois)
    assert adv2["story_summary"] == ""


def test_maybe_update_opens_own_session(tmp_db):
    """Assinatura do hook do game_engine: maybe_update(adventure_id) sozinho."""
    adv = make_adventure()
    add_action(adv["id"], 0, "start", "Intro curta.")  # abaixo dos triggers
    assert summarizer.maybe_update(adv["id"]) is None  # sem exceção, sem LLM


# ------------------------------------------- llm_client real (transport fake)
def test_chat_completion_non_streaming_payload():
    captured: list[dict] = []
    transport = _json_llm_transport(reply="Resumo.", capture=captured)
    cfg = {"base_url": "http://fake/v1", "model_alias": "auto",
           "disable_thinking": True, "timeout_seconds": 30}
    text = llm_client.chat_completion(cfg, [{"role": "user", "content": "hi"}],
                                      temperature=0.3, max_tokens=50,
                                      transport=transport)
    assert text == "Resumo."
    payload = captured[0]
    assert payload["stream"] is False
    assert payload["model"] == "mock-model"
    assert payload["temperature"] == 0.3
    assert payload["max_tokens"] == 50
    assert payload["chat_template_kwargs"] == {"enable_thinking": False}


def test_maybe_update_with_real_llm_client_and_transport(tmp_db):
    adv = make_adventure()
    _actions(adv["id"], 4, chars=200)
    transport = _json_llm_transport(reply="You did things.")
    with db.session() as conn:
        cfg = _cfg(conn, memory_bank={"trigger_tokens": 100}, context_size=7000)
        summarizer.maybe_update(adv["id"], conn=conn, settings=cfg,
                                transport=transport)  # llm_client real
        mems = db.list_memories(conn, adv["id"])
    assert len(mems) == 1
    assert mems[0]["text"] == "You did things."


def test_short_evicted_passage_is_still_archived(tmp_db):
    """Sair da janela basta. Não espera mais 1500 tokens novos."""
    adv = make_adventure()
    _actions(adv["id"], 3, chars=40)
    fake = FakeLLM(reply="You left the hall and kept the scroll.")
    with db.session() as conn:
        cfg = _cfg(conn, context_size=7000, memory_bank={"trigger_tokens": 800})
        summarizer.maybe_update(adv["id"], conn=conn, llm_client=fake, settings=cfg)
        mems = db.list_memories(conn, adv["id"])
    assert len(mems) == 1
    assert mems[0]["seq_start"] == 0
    assert mems[0]["seq_end"] == 1
    assert "Action 0." in fake.calls[0]["messages"][0]["content"]


def test_summarizer_does_not_archive_see_prompts(tmp_db):
    adv = make_adventure()
    add_action(adv["id"], 0, "continue", "Action 0. " + "x" * 580)
    add_action(adv["id"], 1, "see",
               "exact likeness, Bust portrait of Alex BREAK people present")
    add_action(adv["id"], 2, "continue", "Action 2. " + "x" * 580)
    add_action(adv["id"], 3, "continue", "Action 3. " + "x" * 580)
    fake = FakeLLM(reply="Kept the facts.")
    with db.session() as conn:
        cfg = _cfg(conn, context_size=3800 + 512 + 100)
        summarizer.maybe_update(adv["id"], conn=conn, llm_client=fake, settings=cfg)
    blob = " ".join(call["messages"][0]["content"] for call in fake.calls)
    assert "Bust portrait" not in blob
    assert "people present" not in blob
    assert "Action 0." in blob


def test_memory_endpoint_lists_the_bank(client):
    adv = make_adventure(plot_essentials="plot alpha")
    with db.session() as conn:
        db.insert_memory(conn, adv["id"], "You met Alusair.", seq_start=0, seq_end=2)
    resp = client.get(f"/api/adventures/{adv['id']}/memory")
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["plot_essentials"] == "plot alpha"
    assert data["memories"][0]["text"] == "You met Alusair."
    assert data["memories"][0]["seq_start"] == 0
    assert data["memories"][0]["seq_end"] == 2
