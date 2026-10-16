#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Unit tests for conversation evaluation metrics and harness."""

import json
import sys
from pathlib import Path
import pytest

TOOLS_DIR = Path(__file__).resolve().parent.parent / "tools"
if str(TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(TOOLS_DIR))

from eval_conversation import (
    compute_repetition_metrics,
    compute_impersonation_metrics,
    compute_empty_or_truncated,
    BlindJudgePreparer,
    ConversationEvaluator,
    load_situations,
)


def test_repetition_clean():
    text = "The ancient archway stood firm against the howling desert wind. Sand drifted into deep ruts."
    res = compute_repetition_metrics(text)
    assert res["intra_3gram_rate"] == 0.0
    assert res["intra_4gram_rate"] == 0.0
    assert not res["repetition_loop"]


def test_repetition_loop():
    text = "He walked into the dark cavern. He walked into the dark cavern. He walked into the dark cavern."
    res = compute_repetition_metrics(text)
    assert res["intra_3gram_rate"] > 0.40
    assert res["repetition_loop"] is True


def test_repetition_inter():
    context = "Thuldan handed you a sealed parchment bearing the winged viper seal."
    continuation = "Taking the sealed parchment bearing the winged viper seal, you examine the wax."
    res = compute_repetition_metrics(continuation, context)
    assert res["inter_3gram_rate"] > 0.30


def test_impersonation_detection():
    # Prompt injection
    bad_prompt = "> You draw your blade and prepare for battle."
    assert compute_impersonation_metrics(bad_prompt)["has_impersonation"] is True
    assert compute_impersonation_metrics(bad_prompt)["breakdown"]["action_prompt"] >= 1

    # Dialogue dictation
    bad_dialogue = 'You say, "Never will I yield to the likes of you!" and step back.'
    assert compute_impersonation_metrics(bad_dialogue)["has_impersonation"] is True
    assert compute_impersonation_metrics(bad_dialogue)["breakdown"]["dialogue_dictation"] >= 1

    # Internal decision dictation
    bad_decision = "Seeing the odds against you, you decide to retreat into the shadows."
    assert compute_impersonation_metrics(bad_decision)["has_impersonation"] is True
    assert compute_impersonation_metrics(bad_decision)["breakdown"]["internal_decision"] >= 1

    # Clean text (NPC reaction only)
    clean_text = "The bandit sneers and raises his iron mace, stepping closer while his ally circles left."
    clean_res = compute_impersonation_metrics(clean_text)
    assert clean_res["has_impersonation"] is False
    assert clean_res["total_violations"] == 0


def test_truncation_empty_and_zero_width():
    assert compute_empty_or_truncated("")["empty_or_truncated"] is True
    assert compute_empty_or_truncated("   \n\t")["empty_or_truncated"] is True
    assert compute_empty_or_truncated("\u200b\u200b\n")["empty_or_truncated"] is True
    assert compute_empty_or_truncated("Yes.")["empty_or_truncated"] is True  # < 3 words


def test_truncation_unterminated_sentence():
    # Ends on comma
    assert compute_empty_or_truncated("The guards step back, while Thuldan,")["is_truncated"] is True
    # Ends mid-word
    assert compute_empty_or_truncated("The guards step back and rais")["is_truncated"] is True
    # Clean ending
    assert compute_empty_or_truncated("The guards step back and lower their halberds.")["empty_or_truncated"] is False


def test_benchmark_situations_integrity():
    situations_path = Path(__file__).resolve().parent.parent / "benchmark" / "conversation_situations.json"
    situations = load_situations(str(situations_path))
    assert len(situations) == 10

    categories = set()
    for s in situations:
        assert "id" in s
        assert "category" in s
        assert "plot_essentials" in s
        assert "player_action" in s
        assert "evaluation_focus" in s
        categories.add(s["category"])

    # Ensure 10 distinct categories
    assert len(categories) == 10


def test_evaluator_mock_run(tmp_path):
    situations_path = Path(__file__).resolve().parent.parent / "benchmark" / "conversation_situations.json"
    situations = load_situations(str(situations_path))
    evaluator = ConversationEvaluator(situations, mode="mock", seed=42)
    results = evaluator.run_benchmark()

    assert len(results["baseline"]) == 10
    assert len(results["new"]) == 10

    # Blind dataset preparation
    blind_key, blind_eval, blind_md = BlindJudgePreparer.prepare_blind_dataset(results, situations, seed=42)
    assert len(blind_key["situations"]) == 10
    assert len(blind_eval["situations"]) == 10
    assert "### Situation:" in blind_md
