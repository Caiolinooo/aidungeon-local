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


def test_scene_context_is_private_and_order_stays_the_user_message(monkeypatch):
    calls = install_stub(monkeypatch, "she takes off the dress")

    result = image_english.render_english(
        "ela tira o vestido",
        llm_cfg={},
        context="A Donzela da Noite stands beside Caio in the refuge.",
    )

    assert result == "she takes off the dress"
    messages = calls[0]["messages"]
    assert messages[0]["role"] == "system"
    assert "A Donzela da Noite stands beside Caio" in messages[0]["content"]
    assert messages[1] == {"role": "user", "content": "ela tira o vestido"}


def test_break_invented_for_a_source_without_break_is_dropped(monkeypatch):
    source = "appearance: dark hair\nbody: broad shoulders\nclothing: navy shirt"
    reply = "appearance: dark hair BREAK body: broad shoulders BREAK clothing: navy shirt"
    calls = install_stub(monkeypatch, reply)

    result = image_english.render_english(source, llm_cfg={})

    assert result == "appearance: dark hair body: broad shoulders clothing: navy shirt"
    assert len(calls) == 1
    system = calls[0]["messages"][0]["content"]
    assert "Never add the token BREAK" in system
    assert "same count" not in system


def test_break_rule_is_sent_only_when_the_source_has_break(monkeypatch):
    calls = install_stub(monkeypatch, "knight BREAK ink sketch")

    image_english.render_english("knight BREAK ink sketch", llm_cfg={})

    system = calls[0]["messages"][0]["content"]
    assert "Keep the token BREAK exactly where it is" in system
    assert "Never add" not in system
    assert "{break_rule}" not in system


def test_extra_break_is_still_rejected_when_the_source_has_break(monkeypatch):
    install_stub(monkeypatch, ["knight BREAK ink BREAK sketch"] * 2)

    with pytest.raises(image_client.ImageError):
        image_english.render_english(
            "o cavaleiro BREAK ink sketch", llm_cfg={})


def test_english_do_and_no_do_not_block_the_passthrough(monkeypatch):
    source = "Mandatory appearance, do not contradict: no jewelry is visible"
    install_stub(monkeypatch, [llm_client.LlmError("down")] * 2)

    assert image_english.render_english(source, llm_cfg={}) == source
    assert not image_english._is_probable_english("o cavaleiro com uma espada")


def test_line_breaks_reach_the_model_as_single_spaces(monkeypatch):
    calls = install_stub(monkeypatch, "portrait of a red-haired woman in a green dress")

    result = image_english.render_english(
        "retrato de uma mulher ruiva\ncom vestido verde", llm_cfg={})

    assert result == "portrait of a red-haired woman in a green dress"
    assert calls[0]["messages"][1]["content"] == (
        "retrato de uma mulher ruiva com vestido verde")


def test_portuguese_echo_on_first_attempt_retries_to_english(monkeypatch):
    source = "wide shot, A Donzela observa cruzando os braços"
    translated = "wide shot, A Donzela watches with arms crossed"
    calls = install_stub(monkeypatch, [
        "wide shot, [[0]] observa cruzando os braços",
        "wide shot, [[0]] watches with arms crossed",
    ])

    assert image_english.render_english(source, llm_cfg={}) == translated
    assert len(calls) == 2


def test_name_masking_does_not_cross_sentences_or_mask_portuguese_nouns():
    masked, names = image_english._mask_names(
        "estilo National Geographic. Sem 3D. clothing: Vestuário moderno."
    )
    assert names == ["National Geographic"]
    assert "[[0]]. Sem 3D." in masked
    assert "Vestuário" in masked


def test_partial_translation_feeds_retry_and_recovers_on_second_attempt_error(monkeypatch):
    source = "Fotografia natural com iluminação quente. appearance: Cabelos pretos e olhos castanhos."
    partial = "Natural photography with warm lighting. appearance: Cabelos pretos e olhos castanhos."
    calls = install_stub(monkeypatch, [
        partial,
        llm_client.LlmError("HTTP 429 em /chat/completions: Too Many Requests"),
    ])

    result = image_english.render_english(source, llm_cfg={})
    assert result == partial
    assert len(calls) == 2
    assert calls[1]["messages"][-1]["content"] == partial


def test_llm_client_retries_on_http_429_with_retry_after():
    import httpx

    attempts = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": [{"id": "solar-mini4:free"}]})
        attempts["count"] += 1
        if attempts["count"] < 3:
            return httpx.Response(
                429,
                headers={"Retry-After": "0"},
                text="Too Many Requests",
            )
        return httpx.Response(200, json={
            "choices": [{"message": {"content": "Natural photography portrait"}}],
        })

    cfg = {
        "base_url": "https://inference-api.nousresearch.com/v1",
        "api_key": "test-key",
        "model_alias": "solar-mini4:free",
        "timeout_seconds": 15,
    }
    reply = llm_client.chat_completion(
        cfg,
        [{"role": "user", "content": "translate"}],
        role="image_prompt",
        transport=httpx.MockTransport(handler),
    )
    assert reply == "Natural photography portrait"
    assert attempts["count"] == 3



