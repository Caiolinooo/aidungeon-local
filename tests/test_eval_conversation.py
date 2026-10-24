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
    build_prompt_messages,
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



@pytest.mark.parametrize(
    ("text", "expected_breakdown"),
    [
        ("> Você observa a saída antes de agir.", "action_prompt"),
        ('Você responde: "Não entregarei o mapa."', "dialogue_dictation"),
        ("Você diz que aceita a proposta do embaixador.", "dialogue_dictation"),
        ("Você decide atravessar a ponte antes da noite.", "internal_decision"),
        ("Você se recusa a assinar o documento.", "internal_decision"),
        ("Diante do guarda, você saca sua espada.", "forced_action"),
    ],
)
def test_impersonation_detection_portuguese(text, expected_breakdown):
    result = compute_impersonation_metrics(text)

    assert result["has_impersonation"] is True
    assert result["breakdown"][expected_breakdown] == 1


@pytest.mark.parametrize(
    "text",
    [
        "O sino vibra quando você aproxima a mão da corda.",
        "Você pode decidir depois; Beatriz permanece junto à porta.",
        "Se você atacar, o guarda estará pronto para reagir.",
        'O guarda avisa: "Você decide agora: rendição ou combate."',
        'Íris recorda: "Você disse que manteria a antena desligada."',
        'The captain says, "You decide whether we leave or stay."',
    ],
)
def test_impersonation_portuguese_obvious_false_positives(text):
    result = compute_impersonation_metrics(text)

    assert result["has_impersonation"] is False
    assert result["total_violations"] == 0

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
    assert len(situations) == 16

    original_ids = [
        "sit_01_combat_spellcasting",
        "sit_02_dialogue_merchant_kaelen",
        "sit_03_stealth_intrigue_eavesdropping",
        "sit_04_exploration_atmosphere_calimport",
        "sit_05_emotional_moral_dilemma_sanctuary",
        "sit_06_ambiguous_skill_check_zone_of_truth",
        "sit_07_world_knowledge_lore_inquiry",
        "sit_08_pursuit_escape_alleys",
        "sit_09_companion_npc_coordination_thuldan",
        "sit_10_turning_point_royal_crisis_alusair",
    ]
    assert [s["id"] for s in situations[:10]] == original_ids
    assert len({s["id"] for s in situations}) == 16

    categories = set()
    for s in situations:
        assert "id" in s
        assert "category" in s
        assert "plot_essentials" in s
        assert "player_action" in s
        assert "evaluation_focus" in s
        categories.add(s["category"])

    assert len(categories) == 16

    portuguese_situations = situations[10:]
    assert all("Você é" in s["plot_essentials"] for s in portuguese_situations)

    long_continuity = next(s for s in situations if s["id"] == "sit_12_ficcao_cientifica_deriva_orbital")
    assert len(long_continuity["recent_actions"]) == 12
    messages = build_prompt_messages(long_continuity, "sistema")
    assert messages[1]["content"].startswith("O primeiro contato")
    assert messages[1]["content"].endswith("chamado de resgate.\"")
    assert "ÍCARO-7" in messages[1]["content"]

def test_evaluator_mock_run(tmp_path):
    situations_path = Path(__file__).resolve().parent.parent / "benchmark" / "conversation_situations.json"
    situations = load_situations(str(situations_path))
    evaluator = ConversationEvaluator(situations, mode="mock", seed=42)
    results = evaluator.run_benchmark()

    assert len(results["baseline"]) == 16
    assert len(results["new"]) == 16
    assert results["situation_count"] == 16

    # Blind dataset preparation
    blind_key, blind_eval, blind_md = BlindJudgePreparer.prepare_blind_dataset(results, situations, seed=42)
    assert len(blind_key["situations"]) == 16
    assert len(blind_eval["situations"]) == 16
    assert "### Situation:" in blind_md
