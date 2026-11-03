"""English rewriting of the final positive image-model prompt."""

from __future__ import annotations

import pytest

from app import image_client, image_english, llm_client, settings_store


@pytest.fixture(autouse=True)
def clear_rewriter_cache():
    with image_english._CACHE_LOCK:
        image_english._CACHE.clear()


def install_stub(monkeypatch, replies):
    calls = []

    def chat(cfg, messages, **kwargs):
        calls.append({"cfg": cfg, "messages": messages, **kwargs})
        reply = replies.pop(0) if isinstance(replies, list) else replies
        if isinstance(reply, Exception):
            raise reply
        return reply

    monkeypatch.setattr(llm_client, "chat_completion", chat)
    return calls


def test_whitespace_is_unchanged_without_settings_or_llm(monkeypatch):
    calls = install_stub(monkeypatch, "unused")
    monkeypatch.setattr(
        settings_store, "effective_llm_settings",
        lambda conn: pytest.fail("whitespace must not load settings"),
    )

    assert image_english.render_english("   ", llm_cfg={}) == "   "
    assert image_english.render_english(None, llm_cfg={}) == ""
    assert calls == []


def test_english_prompt_round_trips(monkeypatch):
    prompt = "wide cinematic shot, ink sketch"
    calls = install_stub(monkeypatch, prompt)

    assert image_english.render_english(prompt, llm_cfg={}) == prompt
    assert len(calls) == 1
    assert calls[0]["role"] == "image_prompt"
    assert calls[0]["temperature"] == 0
    assert calls[0]["max_tokens"] == 800


def test_portuguese_clause_is_translated_and_break_is_preserved(monkeypatch):
    source = "wide cinematic shot, o vampiro morde o pescoço BREAK ink sketch"
    translated = "wide cinematic shot, the vampire bites the neck BREAK ink sketch"
    calls = install_stub(monkeypatch, translated)

    assert image_english.render_english(source, llm_cfg={}) == translated
    user = calls[0]["messages"][1]["content"]
    system = calls[0]["messages"][0]["content"]
    assert "morde" in user
    assert "BREAK" in user
    assert "morde" not in system
    assert "vampiro" not in system


def test_names_are_masked_and_restored(monkeypatch):
    calls = install_stub(monkeypatch, "[[0]] bites the neck")

    result = image_english.render_english(
        "A Donzela da Noite morde o pescoço", llm_cfg={})

    assert result == "A Donzela da Noite bites the neck"
    user = calls[0]["messages"][1]["content"]
    assert "[[0]]" in user
    assert "morde" in user
    assert "Donzela" not in user


def test_two_rejected_replies_raise_image_error(monkeypatch):
    calls = install_stub(monkeypatch, ["nope", "nope"])

    with pytest.raises(image_client.ImageError) as caught:
        image_english.render_english("knight BREAK ink sketch", llm_cfg={})

    assert str(caught.value) == (
        "generation_failed: image prompt could not be translated to English")
    assert len(calls) == 2
    assert calls[1]["messages"][0]["content"].endswith(
        "The previous answer was rejected. Return only the English prompt.")


def test_only_accepted_results_are_memoized(monkeypatch):
    calls = install_stub(monkeypatch, "[[0]] bites the neck")
    source = "A Donzela da Noite morde o pescoço"

    first = image_english.render_english(source, llm_cfg={})
    second = image_english.render_english(source, llm_cfg={})

    assert first == second == "A Donzela da Noite bites the neck"
    assert len(calls) == 1


def test_unaccented_portuguese_clause_can_be_translated(monkeypatch):
    source = (
        "criando uma cortina entre o mundo exterior e sua intimidade "
        "BREAK ink sketch"
    )
    translated = (
        "creating a curtain between the outside world and their intimacy "
        "BREAK ink sketch"
    )
    calls = install_stub(monkeypatch, translated)

    assert image_english.render_english(source, llm_cfg={}) == translated
    assert len(calls) == 1
