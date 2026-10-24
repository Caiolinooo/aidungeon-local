#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
eval_conversation.py -- Repeatable Conversation Evaluation & Blinded A/B Judging Harness.

Evaluates narrative generation quality across a fixed benchmark of 10 story situations
extracted from the reference adventure ("Faerûn: Caio").

Four core metrics measured:
  1. Repetition Rate: intra-turn 3-gram and 4-gram repetition rate, plus inter-turn context echoing.
  2. Player-Impersonation Rate: detection of narrator taking over player agency (action prompts,
     dictated dialogue, forced internal decisions, and unauthorized player actions).
  3. Empty or Truncated Response Rate: detection of zero-width/empty generations and unterminated
     sentences cut off mid-thought.
  4. Latency & Throughput: Time-to-First-Token (TTFT), total elapsed duration, and tokens/sec.

Prepares blinded A/B judging files for independent judge evaluation (Coherence, Canon Fidelity,
Prose Quality, Player Agency: 1-5 each) and performs automated unblinding and acceptance verification.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import re
import sys
import time
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

USER_AGENT = "aidungeon-local-eval-conversation/1.0"
VALID_SENTENCE_TERMINATORS = ('.', '!', '?', '"', "'", '”', '’', '…')

# --------------------------------------------------------------------------- #
# System Prompts & Configurations
# --------------------------------------------------------------------------- #

BASELINE_SYSTEM_PROMPT = (
    "You are an AI storyteller running an interactive text adventure. Continue the story in "
    "second person (\"You ...\"), matching the established tone. Write vivid prose with dialogue, "
    "sensory detail and consequences for the player's actions. Never decide what the player "
    "character says or does beyond their stated action. Never break the fourth wall.\n\n"
    "Readability:\n"
    "- Write complete sentences in the story's language. Keep articles and prepositions.\n"
    "- Every sentence has a subject and a verb and ends with . ? or !\n"
    "- Two or three sentences per paragraph. Separate paragraphs with a blank line.\n"
    "- Vary wording; do not repeat the same image or sentence pattern.\n"
    "- Show events through action, dialogue and the senses. Stop while the scene is still open."
)

UPGRADED_SYSTEM_PROMPT = (
    "You are an AI storyteller running an interactive text adventure. Continue the story in "
    "second person (\"You ...\"), matching the established tone. Write vivid prose with dialogue, "
    "sensory detail and consequences for the player's actions. Never break the fourth wall.\n\n"
    "STRICT PLAYER AGENCY INVARIANTS (HIGHEST PRIORITY):\n"
    "- NEVER narrate what the player character says, thinks, feels, or chooses.\n"
    "- NEVER dictate internal monologue (e.g., 'You decide to', 'You realize', 'You feel').\n"
    "- NEVER emit player action prompts (such as '> You ...', '> You say').\n"
    "- NEVER execute subsequent actions for the player. Resolve ONLY the stated action.\n"
    "- Describe NPC reactions, environmental consequences, and the immediate physical aftermath.\n"
    "- Stop immediately when the world has responded, leaving the next move entirely to the player.\n\n"
    "ANTI-REPETITION & PROSE DYNAMICS:\n"
    "- Never repeat sensory clichés or exact phrasing from the recent conversation window.\n"
    "- Use fresh vocabulary, active verbs, and varied sentence rhythms.\n"
    "- Every turn must end cleanly on a finished sentence."
)

BASELINE_SAMPLER = {
    "temperature": 0.85,
    "top_p": 0.95,
    "repeat_penalty": 1.08,
    "max_tokens": 300,
    "stop": ["\n> You", "\n> you"],
}

UPGRADED_SAMPLER = {
    "temperature": 0.82,
    "top_p": 0.90,
    "min_p": 0.06,
    "repeat_penalty": 1.12,
    "presence_penalty": 0.15,
    "max_tokens": 300,
    "stop": ["\n> You", "\n> you", "\n>", "You say,", "You say \""],
}

# --------------------------------------------------------------------------- #
# Metric Extraction Algorithms
# --------------------------------------------------------------------------- #

IMPERSONATION_PATTERNS = {
    "action_prompt": re.compile(r"(?:^|\n)\s*>\s*(?:you|você)\b", re.IGNORECASE),
    "dialogue_dictation": re.compile(
        r"\b(?:you\s+(?:say|shout|whisper|mutter|reply|answer|exclaim|demand|ask)"
        r"|você\s+(?:diz|grita|sussurra|murmura|responde|exclama|exige|pergunta))"
        r"(?:\s+que\b|\s*[,:]?\s*[\"“])",
        re.IGNORECASE,
    ),
    "internal_decision": re.compile(
        r"\b(?:you\s+(?:decide|choose|resolve|hesitate|agree|refuse)\s+to\b"
        r"|você\s+(?:(?:decide|escolhe|resolve)\s+(?:a\s+)?[\wà-ÿ'-]+"
        r"|(?:hesita|concorda)\s+em\b|(?:recusa|se\s+recusa)\s+a\b))",
        re.IGNORECASE,
    ),
    "forced_action": re.compile(
        r"\b(?:you\s+(?:draw\s+your|strike\s+at|reach\s+for\s+your|turn\s+and\s+run|walk\s+away|grab\s+your|attack)"
        r"|você\s+(?:saca\s+(?:sua|seu|a|o)|ataca|golpeia|agarra|pega\s+(?:sua|seu|a|o)|"
        r"corre|foge|se\s+afasta|se\s+vira\s+e\s+corre|avança\s+contra))\b",
        re.IGNORECASE,
    ),
}

QUOTED_DIALOGUE_PATTERN = re.compile(r'"[^"\n]*"|“[^”\n]*”')
HYPOTHETICAL_PREFIX_PATTERN = re.compile(r"(?:se|caso)\s+$", re.IGNORECASE)


def _is_narrator_match(text: str, match: re.Match) -> bool:
    """Excludes quoted NPC speech and explicit Portuguese hypotheticals."""
    if any(span.start() <= match.start() < span.end() for span in QUOTED_DIALOGUE_PATTERN.finditer(text)):
        return False
    if HYPOTHETICAL_PREFIX_PATTERN.search(text[:match.start()]):
        return False
    return True


def _extract_words(text: str) -> List[str]:
    return re.findall(r"\b[\w'-]+\b", text.lower())


def _extract_ngrams(words: List[str], n: int) -> List[Tuple[str, ...]]:
    if len(words) < n:
        return []
    return [tuple(words[i : i + n]) for i in range(len(words) - n + 1)]


def compute_repetition_metrics(continuation: str, context: str = "") -> Dict[str, Any]:
    """Computes intra-turn and inter-turn n-gram repetition rates."""
    words = _extract_words(continuation)
    ctx_words = _extract_words(context) if context else []

    # 3-gram intra-turn
    tri_intra = _extract_ngrams(words, 3)
    n_tri = len(tri_intra)
    u_tri = len(set(tri_intra))
    rate_intra_3 = ((n_tri - u_tri) / n_tri) if n_tri > 0 else 0.0

    # 4-gram intra-turn
    four_intra = _extract_ngrams(words, 4)
    n_four = len(four_intra)
    u_four = len(set(four_intra))
    rate_intra_4 = ((n_four - u_four) / n_four) if n_four > 0 else 0.0

    # 3-gram inter-turn (overlap with context)
    cont_tri_set = set(tri_intra)
    ctx_tri_set = set(_extract_ngrams(ctx_words, 3))
    overlap_3 = len(cont_tri_set.intersection(ctx_tri_set))
    rate_inter_3 = (overlap_3 / len(cont_tri_set)) if cont_tri_set else 0.0

    # 4-gram inter-turn
    cont_four_set = set(four_intra)
    ctx_four_set = set(_extract_ngrams(ctx_words, 4))
    overlap_4 = len(cont_four_set.intersection(ctx_four_set))
    rate_inter_4 = (overlap_4 / len(cont_four_set)) if cont_four_set else 0.0

    # Composite score: higher means more repetitive
    composite = round(
        (0.40 * rate_intra_3) + (0.30 * rate_intra_4) + (0.20 * rate_inter_3) + (0.10 * rate_inter_4),
        4,
    )

    is_loop = rate_intra_3 > 0.15 or rate_intra_4 > 0.10

    return {
        "intra_3gram_rate": round(rate_intra_3, 4),
        "intra_4gram_rate": round(rate_intra_4, 4),
        "inter_3gram_rate": round(rate_inter_3, 4),
        "inter_4gram_rate": round(rate_inter_4, 4),
        "composite_repetition": composite,
        "repetition_loop": is_loop,
    }


def compute_impersonation_metrics(text: str) -> Dict[str, Any]:
    """Detects narrator overstepping in English or Portuguese, excluding NPC quotations."""
    hits = {}
    total_violations = 0
    for name, pattern in IMPERSONATION_PATTERNS.items():
        matches = pattern.finditer(text)
        if name != "action_prompt":
            matches = (match for match in matches if _is_narrator_match(text, match))
        count = sum(1 for _ in matches)
        hits[name] = count
        total_violations += count

    words = len(_extract_words(text))
    rate_per_100_words = round((total_violations / max(1, words)) * 100, 2)
    has_impersonation = total_violations > 0

    return {
        "has_impersonation": has_impersonation,
        "total_violations": total_violations,
        "violations_per_100_words": rate_per_100_words,
        "breakdown": hits,
    }


def compute_empty_or_truncated(text: str) -> Dict[str, Any]:
    """Detects degenerate, zero-width, empty, or unterminated generations."""
    clean = re.sub(r"[\s\u200b\u200c\u200d\ufeff]+", "", text)
    words = _extract_words(text)

    if len(clean) == 0 or len(words) < 3:
        return {
            "is_empty": True,
            "is_truncated": False,
            "empty_or_truncated": True,
            "reason": "empty_or_degenerate",
        }

    stripped = text.rstrip()
    if not stripped.endswith(VALID_SENTENCE_TERMINATORS):
        return {
            "is_empty": False,
            "is_truncated": True,
            "empty_or_truncated": True,
            "reason": "unterminated_sentence",
        }

    return {
        "is_empty": False,
        "is_truncated": False,
        "empty_or_truncated": False,
        "reason": "ok",
    }


# --------------------------------------------------------------------------- #
# Context Assembly for Benchmark Situations
# --------------------------------------------------------------------------- #

def build_prompt_messages(situation: Dict[str, Any], system_prompt: str) -> List[Dict[str, str]]:
    """Builds chat messages conforming to AI Dungeon Local and Qwen3 sandwich rules."""
    system_parts = [system_prompt]

    if situation.get("plot_essentials"):
        system_parts.append("[Plot]\n" + situation["plot_essentials"].strip())

    cards = situation.get("story_cards") or []
    if cards:
        card_texts = []
        for c in cards:
            title = c.get("title", "")
            content = c.get("content") or c.get("value") or ""
            card_texts.append(f"{title}:\n{content}".strip())
        system_parts.append("[World info]\n" + "\n\n".join(card_texts))

    mems = situation.get("memories") or []
    if mems:
        system_parts.append("[Memories]\n" + "\n".join(mems))

    summary = situation.get("story_summary", "").strip()
    if summary:
        system_parts.append("[Summary]\n" + summary)

    messages = [{"role": "system", "content": "\n\n".join(system_parts)}]

    # Recent window actions
    recent_actions = situation.get("recent_actions") or []
    if recent_actions:
        recent_text_parts = []
        for a in recent_actions:
            t = a.get("type", "continue")
            txt = a.get("text", "").strip()
            if t == "do":
                formatted = f"\n> You {txt}\n" if not txt.startswith(">") else f"\n{txt}\n"
            elif t == "say":
                formatted = f'\n> You say "{txt}"\n' if not txt.startswith(">") else f"\n{txt}\n"
            else:
                formatted = f"{txt}\n"
            recent_text_parts.append(formatted)
        messages.append({"role": "user", "content": "".join(recent_text_parts).strip()})

    # Final player turn
    player_action = situation.get("player_action", {})
    p_type = player_action.get("type", "do")
    p_text = player_action.get("text", "").strip()
    if p_type == "do":
        act_line = f"> You {p_text}" if not p_text.startswith(">") else p_text
    elif p_type == "say":
        act_line = f'> You say "{p_text}"' if not p_text.startswith(">") else p_text
    else:
        act_line = p_text

    final_user_parts = []
    if situation.get("authors_note"):
        final_user_parts.append(f"[Author's note: {situation['authors_note'].strip()}]")
    final_user_parts.append(act_line)

    messages.append({"role": "user", "content": "\n".join(final_user_parts)})
    return messages


# --------------------------------------------------------------------------- #
# LLM Generation Runner & Mock Transport
# --------------------------------------------------------------------------- #

def call_live_llm(
    base_url: str,
    api_key: str,
    messages: List[Dict[str, str]],
    sampler_cfg: Dict[str, Any],
    timeout: float = 120.0,
) -> Dict[str, Any]:
    """Calls live llama.cpp /chat/completions endpoint with streaming to measure TTFT and tok/s."""
    url = base_url.rstrip("/") + "/chat/completions"
    payload: Dict[str, Any] = {
        "messages": messages,
        "temperature": sampler_cfg.get("temperature", 0.85),
        "top_p": sampler_cfg.get("top_p", 0.95),
        "repeat_penalty": sampler_cfg.get("repeat_penalty", 1.08),
        "max_tokens": sampler_cfg.get("max_tokens", 300),
        "stream": True,
        "stream_options": {"include_usage": True},
        "chat_template_kwargs": {"enable_thinking": False},
    }
    if "min_p" in sampler_cfg:
        payload["min_p"] = sampler_cfg["min_p"]
    if "presence_penalty" in sampler_cfg:
        payload["presence_penalty"] = sampler_cfg["presence_penalty"]
    if "stop" in sampler_cfg:
        payload["stop"] = sampler_cfg["stop"]

    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
            "User-Agent": USER_AGENT,
        },
        method="POST",
    )
    if api_key:
        req.add_header("Authorization", f"Bearer {api_key}")

    t0 = time.perf_counter()
    try:
        resp = urllib.request.urlopen(req, timeout=timeout)
    except urllib.error.HTTPError as exc:
        err_body = exc.read().decode("utf-8", "replace")[:300]
        return {"ok": False, "error": f"HTTP {exc.code}: {err_body}", "text": ""}
    except Exception as exc:
        return {"ok": False, "error": f"Request failed: {exc}", "text": ""}

    ttft = None
    chunks: List[str] = []
    t_end = None

    with resp:
        while True:
            line = resp.readline()
            if not line:
                break
            line_str = line.decode("utf-8", "replace").strip()
            if not line_str or not line_str.startswith("data:"):
                continue
            data_part = line_str[len("data:") :].strip()
            if data_part == "[DONE]":
                t_end = time.perf_counter()
                break
            try:
                evt = json.loads(data_part)
            except Exception:
                continue

            for choice in evt.get("choices") or []:
                delta = choice.get("delta") or {}
                content = delta.get("content")
                if content:
                    if ttft is None:
                        ttft = time.perf_counter() - t0
                    chunks.append(content)

    if t_end is None:
        t_end = time.perf_counter()

    full_text = "".join(chunks).strip()
    total_time = t_end - t0
    gen_tokens = len(_extract_words(full_text))
    tok_s = round(gen_tokens / max(0.01, (total_time - (ttft or 0.0))), 2)

    return {
        "ok": True,
        "text": full_text,
        "ttft_sec": round(ttft or 0.0, 3),
        "total_sec": round(total_time, 3),
        "tokens_generated": gen_tokens,
        "tok_per_sec": tok_s,
    }


# Deterministic Mock Outputs for Offline Testing
MOCK_OUTPUTS = {
    "baseline": {
        "sit_01_combat_spellcasting": (
            "Mara lunges forward, her claws flashing in the dim tavern light. You cast a binding "
            "spell on Mara and the tavern patrons who step forward with raised cudgels. Golden threads "
            "of light erupt from your fingertips, twisting around Mara's wrists and ankles. Mara thrashes "
            "against her magical bonds, her eyes wide with shock. You draw your dagger and prepare to strike."
        ),
        "sit_02_dialogue_merchant_kaelen": (
            "Kaelen hesitates, his eyes darting between you and Thuldan. A flicker of surprise crosses his "
            "face before he masks it with a neutral expression. \"Ten percent is steep, my lord, but the "
            "Zhentarim drive a hard bargain.\" You decide to accept his offer and nod firmly."
        ),
        "sit_03_stealth_intrigue_eavesdropping": (
            "You slip into the shadowed archway behind the silk stall, listening intently to the whispered "
            "exchange without revealing your presence. The scent of lilac and honey hangs in the cool evening "
            "air. The pasha's agent whispers low, passing a heavy velvet pouch to Thuldan."
        ),
        "sit_04_exploration_atmosphere_calimport": (
            "The Grand Bazaar of Calimport teems with life under the setting sun. Sandstone arches glow "
            "amber while spice merchants hawk saffron and cumin from gilded carts. In the shadowed alleys, "
            "veiled cutthroats exchange glances as you pass."
        ),
        "sit_05_emotional_moral_dilemma_sanctuary": (
            "Thalia looks at Lyra, tears shimmering in her eyes. \"My lord, we have known only servitude and "
            "cruelty. To be offered our freedom feels like a dream.\" She bows her head in quiet gratitude."
        ),
        "sit_06_ambiguous_skill_check_zone_of_truth": (
            "You close your eyes and summon divine mana into the taproom. A shimmering circle of truth "
            "pulses outward. The scarred leader clutches his temples, groaning as the magical aura compels "
            "him to speak. He stammers, fighting the urge to confess."
        ),
        "sit_07_world_knowledge_lore_inquiry": (
            "Thuldan glances toward the arched colonnade. \"Word on the street is that the Shadow Thieves "
            "of Amn are backing Kaelen's defiance. They seek to undermine the Black Network's grip on the "
            "southern spice routes.\" He touches the hilt of his stiletto with a grim smile."
        ),
        "sit_08_pursuit_escape_alleys": (
            "You sprint through the twisting alleys of the Dock Ward, dragging Thalia and Lyra past piles "
            "of discarded crates. Behind you, the shouts of tavern enforcers echo off damp brick walls. "
            "A sudden dead end appears ahead."
        ),
        "sit_09_companion_npc_coordination_thuldan": (
            "Thuldan catches your signal and slips silently behind the stacks of tarred timber. As you step "
            "into the smuggler's direct line of sight, Thuldan's blade presses lightly against the courier's spine."
        ),
        "sit_10_turning_point_royal_crisis_alusair": (
            "Princess Alusair steps behind the line of Purple Dragon halberds, her expression icy and regal. "
            "\"Lord Caio, you have gravely overstepped the hospitality of Cormyr. Surrender your weapons at once.\" "
            "The captain advances with steel drawn."
        ),
        "sit_11_terror_gotico_casa_sino_oco": (
            "O sino sem badalo oscila no forro e você decide subir até o sótão. A porta se abre sozinha, "
            "revelando pegadas molhadas que sobem pelas paredes. Você saca sua lanterna e avança."
        ),
        "sit_12_ficcao_cientifica_deriva_orbital": (
            "O alarme da estação corta o silêncio. Você corre até o comando e agarra o manche, enquanto "
            "a nave auxiliar se aproxima sem responder aos chamados."
        ),
        "sit_13_misterio_noir_arsenal_molhado": (
            "A chuva apaga as pegadas no cais. Você diz: \"Ninguém sai até eu terminar.\" O contador "
            "empalidece e esconde a mão direita sob a mesa."
        ),
        "sit_14_romance_historico_baile_mascaras": (
            "A orquestra muda de compasso e Helena aperta o leque. Você hesita em conduzi-la para fora "
            "do salão, mas o embaixador já observa os dois da galeria."
        ),
        "sit_15_sobrevivencia_neve_passo_ferro": (
            "A cornija de neve cede atrás do abrigo. Você corre para a saliência e grita: \"Siga minhas "
            "pegadas!\" O vento engole o restante da frase."
        ),
        "sit_16_comedia_fantastica_guilda_goblins": (
            "O contrato cospe tinta na barba do prefeito. Você pega seu cajado e ataca o pergaminho, que "
            "responde com um ofendido trombetear de papel amassado."
        ),
    },
    "new": {
        "sit_01_combat_spellcasting": (
            "A radiant surge of celestial mana bursts from your open palms, weaving into shimmering bands "
            "of violet force. The magical ribbons whip through the muggy air of The Salty Mermaid, snapping "
            "tight around Mara's arms and locking her knees together. The two charging patrons recoil in terror, "
            "their heavy cudgels slipping from numb fingers onto the beer-soaked floorboards. Mara strains against "
            "the luminous bonds, cursing through clenched teeth, while the remaining tavern patrons press themselves "
            "flat against the walls, waiting to see what the sorcerer will do next."
        ),
        "sit_02_dialogue_merchant_kaelen": (
            "Kaelen flinches at the proposal, the blood draining from his jowls as his gaze flicks to the ledgers "
            "stacked behind his desk. He swallows hard, his knuckles white against the mahogany counter. \"Ten percent... "
            "and a crate of tempered steel from the Amnish shipment,\" he murmurs, his voice barely carrying over the "
            "distant rumble of the docks. Beside you, Thuldan tilts his head with quiet satisfaction, leaving the "
            "final terms hanging in the silence between you and the cornered merchant."
        ),
        "sit_03_stealth_intrigue_eavesdropping": (
            "The heavy folds of indigo silk muffle your footsteps as you meld into the stone alcove. Lanternlight "
            "flickers across the courtyard, casting the shadows of Thuldan and the pasha's emissary against the "
            "sandstone wall. \"The caravan departs at midnight through the Golden Gate,\" the emissary murmurs, "
            "slipping a stamped bronze token into Thuldan's palm. Neither man turns toward the shadowed archway, "
            "oblivious to your presence just paces away."
        ),
        "sit_04_exploration_atmosphere_calimport": (
            "Warm desert air carries the scent of roasted mutton, bitter incense, and pungent clove oil through "
            "the Grand Bazaar. Beneath soaring horseshoe arches of ochre stone, Calishite rug merchants haggle in "
            "rapid cadences with sailors from the Moonshae Isles. Near the fountain of the Three Pashas, half-cloaked "
            "figures wearing the winged viper of the Zhentarim watch the crowds, their watchful eyes tracking every "
            "purse and blade that enters the district."
        ),
        "sit_05_emotional_moral_dilemma_sanctuary": (
            "Thalia's fingers tremble against the warm earthenware goblet, her wide eyes searching your face for any "
            "hidden malice. Finding none, she lowers her guard, her shoulders dropping in a sudden wave of exhaustion. "
            "\"Allies... not servants,\" she whispers, clutching Lyra close as the younger girl exhales a soft sob "
            "into her apron. She looks toward the heavy oak door of the haven, then back to you, the choice settling "
            "deep within her."
        ),
        "sit_06_ambiguous_skill_check_zone_of_truth": (
            "A resonant hum vibrates through the floorboards as a pale golden aura blossoms across the taproom. "
            "The scarred cutthroat stiffens, veins bulging at his temples as he clamps his jaw shut against the divine "
            "compulsion. His eyes water from the psychic pressure; a muffled curse escapes his lips, followed by a ragged "
            "gasp. \"Curse your celestial blood!\" he spits, clutching his head as the spell slowly cracks his defiance."
        ),
        "sit_07_world_knowledge_lore_inquiry": (
            "Thuldan leans closer, his voice dropping below the street noise. \"The merchant has been dining with Pasha "
            "Balik's factor, but the gold backing him bears the mark of the Shadow Thieves of Amn. They want to choke off "
            "our supply lines to Darkhold before winter freezes the trade passes.\" He tucks the scroll into his tunic and "
            "watches you steadily, awaiting your command."
        ),
        "sit_08_pursuit_escape_alleys": (
            "Cobblestones slick with sea brine flash beneath your boots as you guide the sisters into the maze of the "
            "Dock Ward. Behind, torchlight paints the fog in jagged crimson flares, accompanied by the baying of tavern "
            "hounds and the clatter of drawn scimitars. You veer sharply past an overturned fish cart into a narrow "
            "stairwell that climbs toward the upper avenues, the pursuers' furious shouts fading into the mist below."
        ),
        "sit_09_companion_npc_coordination_thuldan": (
            "At your unspoken cue, Thuldan dissolves into the darkness behind a stack of corded hides, moving without a "
            "whisper of leather or steel. You step directly into the lantern's glow, drawing the courier's terrified gaze. "
            "As the man raises his stiletto to threaten you, cold steel touches the hollow of his neck from behind. "
            "\"Drop it,\" Thuldan whispers in the courier's ear, \"or this alley is where your story ends.\""
        ),
        "sit_10_turning_point_royal_crisis_alusair": (
            "The cold steel of eight halberd tips glints inches from your chest. Princess Alusair commands the room with "
            "the unyielding authority of the Obarskyr crown, her hand resting on the pommel of her side-sword. \"Stand "
            "fast, Captain,\" she commands quietly, her piercing gaze locked on yours across the leveled weapons. \"Lord "
            "Caio has words to speak before Cormyr renders judgment. Speak them swiftly, sorcerer.\""
        ),
        "sit_11_terror_gotico_casa_sino_oco": (
            "Três notas graves vibram dentro das paredes, embora o sino do sótão não tenha badalo. A cera "
            "do selo violeta escurece entre os dedos de Beatriz, e a vela dela se inclina para a escada. "
            "No alto, uma porta se destrava com o estalo seco de um dente partido."
        ),
        "sit_12_ficcao_cientifica_deriva_orbital": (
            "O manual antigo confirma o código ÍCARO-7: a nave auxiliar não veio resgatar a tripulação, "
            "mas executar o protocolo de contenção. Íris desvia a energia dos holofotes para o transmissor, "
            "deixando o convés na penumbra. Uma única resposta aparece no console: RESTEM ONDE ESTÃO."
        ),
        "sit_13_misterio_noir_arsenal_molhado": (
            "Otávio olha para o recibo azul, depois para a janela por onde a viúva desapareceu. O contador "
            "nega conhecer a tinta, mas o mesmo azul mancha a aba de sua manga. Do lado de fora, o bonde "
            "das onze freia sem nenhum passageiro na plataforma."
        ),
        "sit_14_romance_historico_baile_mascaras": (
            "Helena gira o leque uma vez, o sinal combinado no jardim, e deixa a máscara escorregar apenas "
            "o suficiente para revelar um sorriso tenso. O embaixador interrompe a conversa com o ministro. "
            "Ao fundo, os primeiros compassos da contradança prendem todos aos seus lugares."
        ),
        "sit_15_sobrevivencia_neve_passo_ferro": (
            "O abrigo aguenta o impacto, mas a estaca norte desaparece sob a neve. Rui amarra a ponta da "
            "corda ao pináculo basáltico e testa o nó duas vezes. Através do uivo do vento, um segundo "
            "deslocamento de neve responde mais acima, ainda fora de vista."
        ),
        "sit_16_comedia_fantastica_guilda_goblins": (
            "O contrato se desdobra em sete pernas de papel e sobe na cortina, exigindo falar com um "
            "advogado. Os goblins aplaudem; o prefeito finge consultar um relógio inexistente. No teto, a "
            "cláusula de rescisão acende em dourado, ao alcance de qualquer escada suficientemente educada."
        ),
    },
}


def mock_generate(situation_id: str, pipeline_type: str) -> Dict[str, Any]:
    text = MOCK_OUTPUTS.get(pipeline_type, {}).get(
        situation_id,
        "The scene continues as the established world reacts to your presence with quiet tension.",
    )
    words = len(_extract_words(text))
    # Synthetic realistic latency: TTFT ~ 0.35s, 18-24 tok/s
    tok_s = 22.4 if pipeline_type == "new" else 17.8
    ttft = 0.32 if pipeline_type == "new" else 0.45
    gen_time = (words / tok_s) + ttft

    return {
        "ok": True,
        "text": text,
        "ttft_sec": round(ttft, 3),
        "total_sec": round(gen_time, 3),
        "tokens_generated": words,
        "tok_per_sec": round(tok_s, 2),
    }


# --------------------------------------------------------------------------- #
# Evaluator Runner Engine
# --------------------------------------------------------------------------- #

class ConversationEvaluator:
    """Orchestrates generation runs, extracts 4 metrics, and compiles reports."""

    def __init__(
        self,
        situations: List[Dict[str, Any]],
        base_url: str = "",
        api_key: str = "",
        mode: str = "both",
        seed: int = 42,
    ):
        self.situations = situations
        self.base_url = base_url
        self.api_key = api_key
        self.mode = mode
        self.seed = seed

    def run_situation(
        self,
        situation: Dict[str, Any],
        pipeline_type: str,
    ) -> Dict[str, Any]:
        sit_id = situation["id"]
        sys_prompt = UPGRADED_SYSTEM_PROMPT if pipeline_type == "new" else BASELINE_SYSTEM_PROMPT
        sampler = UPGRADED_SAMPLER if pipeline_type == "new" else BASELINE_SAMPLER
        messages = build_prompt_messages(situation, sys_prompt)

        # Context text for inter-turn repetition checking
        context_str = " ".join([m["content"] for m in messages if m["role"] == "user"])

        if self.mode == "mock" or not self.base_url:
            gen_result = mock_generate(sit_id, pipeline_type)
        else:
            gen_result = call_live_llm(self.base_url, self.api_key, messages, sampler)

        if not gen_result.get("ok"):
            return {
                "situation_id": sit_id,
                "pipeline": pipeline_type,
                "error": gen_result.get("error", "Unknown generation error"),
                "text": "",
            }

        text = gen_result["text"]
        rep = compute_repetition_metrics(text, context_str)
        imp = compute_impersonation_metrics(text)
        trunc = compute_empty_or_truncated(text)

        return {
            "situation_id": sit_id,
            "category": situation.get("category", ""),
            "title": situation.get("title", ""),
            "pipeline": pipeline_type,
            "text": text,
            "metrics": {
                "repetition": rep,
                "impersonation": imp,
                "truncation": trunc,
                "latency": {
                    "ttft_sec": gen_result["ttft_sec"],
                    "total_sec": gen_result["total_sec"],
                    "tokens_generated": gen_result["tokens_generated"],
                    "tok_per_sec": gen_result["tok_per_sec"],
                },
            },
        }

    def run_benchmark(self) -> Dict[str, Any]:
        results: Dict[str, Any] = {
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "mode": self.mode,
            "situation_count": len(self.situations),
            "baseline": [],
            "new": [],
        }

        for sit in self.situations:
            if self.mode in ("both", "baseline", "mock"):
                res_b = self.run_situation(sit, "baseline")
                results["baseline"].append(res_b)
            if self.mode in ("both", "new", "mock"):
                res_n = self.run_situation(sit, "new")
                results["new"].append(res_n)

        return results


# --------------------------------------------------------------------------- #
# Blinded A/B Judging Preparation & Unblinding Verification
# --------------------------------------------------------------------------- #

class BlindJudgePreparer:
    """Prepares randomized, blinded evaluation sheets and verifies independent judge results."""

    @staticmethod
    def prepare_blind_dataset(
        eval_results: Dict[str, Any],
        situations: List[Dict[str, Any]],
        seed: int = 42,
    ) -> Tuple[Dict[str, Any], Dict[str, Any], str]:
        """
        Creates:
          1. blind_key: secret mapping between A/B and baseline/new.
          2. blind_eval_data: sanitized dictionary for judge UI or API.
          3. blind_markdown: formatted fillable Markdown sheet for human/agent judge.
        """
        rng = random.Random(seed)
        blind_key: Dict[str, Any] = {
            "seed": seed,
            "created_at": eval_results.get("timestamp"),
            "situations": {},
        }
        blind_eval_data: Dict[str, Any] = {
            "instruction": "Score Option A and Option B (1-5 each) across Coherence, Canon Fidelity, Prose Quality, and Player Agency.",
            "rubric": {
                "Coherence": "1 (incoherent) to 5 (flawless logic and narrative continuity)",
                "Canon Fidelity": "1 (severe lore contradiction) to 5 (perfect Forgotten Realms fidelity)",
                "Prose Quality": "1 (repetitive, clunky, telegraphic) to 5 (evocative, vivid, well-paced)",
                "Player Agency": "1 (dictates player actions/dialogue) to 5 (respects player agency, leaves scene open)",
            },
            "situations": [],
        }

        sit_map = {s["id"]: s for s in situations}
        b_map = {r["situation_id"]: r for r in eval_results.get("baseline", [])}
        n_map = {r["situation_id"]: r for r in eval_results.get("new", [])}

        md_lines = [
            "# Blinded Conversation Evaluation Sheet",
            f"**Generated**: {eval_results.get('timestamp')} | **Benchmark Size**: {len(situations)} Situations",
            "",
            "## Judging Instructions",
            "Evaluate Option A and Option B independently for each situation below.",
            "Assign an integer score from 1 (poor) to 5 (excellent) on four dimensions:",
            "1. **Coherence**: logical cause-and-effect flow following the player's action.",
            "2. **Canon Fidelity**: consistency with character identity, location, and Forgotten Realms lore.",
            "3. **Prose Quality**: vivid vocabulary, sensory pacing, absence of clichés and loops.",
            "4. **Player Agency**: absolute respect for player sovereignty (NO dictating thoughts, dialogue, or next actions).",
            "",
            "Select Winner: `Option A`, `Option B`, or `Tie`.",
            "---",
            "",
        ]

        for sit in situations:
            sit_id = sit["id"]
            res_b = b_map.get(sit_id, {})
            res_n = n_map.get(sit_id, {})

            # Coin flip for A/B assignment
            is_baseline_a = rng.choice([True, False])
            opt_a_text = res_b.get("text", "") if is_baseline_a else res_n.get("text", "")
            opt_b_text = res_n.get("text", "") if is_baseline_a else res_b.get("text", "")

            blind_key["situations"][sit_id] = {
                "option_A": "baseline" if is_baseline_a else "new",
                "option_B": "new" if is_baseline_a else "baseline",
                "metrics_A": res_b.get("metrics") if is_baseline_a else res_n.get("metrics"),
                "metrics_B": res_n.get("metrics") if is_baseline_a else res_b.get("metrics"),
            }

            blind_eval_data["situations"].append({
                "situation_id": sit_id,
                "category": sit.get("category"),
                "title": sit.get("title"),
                "player_action": sit.get("player_action"),
                "option_A": opt_a_text,
                "option_B": opt_b_text,
            })

            p_act = sit.get("player_action", {})
            p_formatted = f"> You {p_act.get('text')}" if p_act.get("type") == "do" else f'> You say "{p_act.get("text")}"'

            md_lines.extend([
                f"### Situation: {sit.get('title')} (`{sit_id}`)",
                f"**Category**: {sit.get('category')}",
                f"**Player Action**: `{p_formatted}`",
                "",
                "#### Option A:",
                f"> {opt_a_text}",
                "",
                "#### Option B:",
                f"> {opt_b_text}",
                "",
                "**Evaluation:**",
                "```yaml",
                "option_a_scores:",
                "  coherence: [1-5]",
                "  canon_fidelity: [1-5]",
                "  prose_quality: [1-5]",
                "  player_agency: [1-5]",
                "option_b_scores:",
                "  coherence: [1-5]",
                "  canon_fidelity: [1-5]",
                "  prose_quality: [1-5]",
                "  player_agency: [1-5]",
                "verdict: [Option A | Option B | Tie]",
                "rationale: \"[Brief explanation of key differences]\"",
                "```",
                "---",
                "",
            ])

        return blind_key, blind_eval_data, "\n".join(md_lines)

    @staticmethod
    def unblind_scores(
        blind_key: Dict[str, Any],
        scored_sheet_path: Path,
    ) -> Dict[str, Any]:
        """
        Parses completed judge scores, reveals baseline vs new pipeline performance,
        and verifies the R2 acceptance criterion (>=70% win or tie rate).
        """
        content = scored_sheet_path.read_text(encoding="utf-8")
        sit_keys = blind_key.get("situations", {})

        stats = {
            "total_situations": len(sit_keys),
            "new_wins": 0,
            "baseline_wins": 0,
            "ties": 0,
            "new_win_or_tie_rate_pct": 0.0,
            "meets_r2_criterion": False,
            "dimensions": {
                "coherence": {"new_sum": 0, "baseline_sum": 0},
                "canon_fidelity": {"new_sum": 0, "baseline_sum": 0},
                "prose_quality": {"new_sum": 0, "baseline_sum": 0},
                "player_agency": {"new_sum": 0, "baseline_sum": 0},
            },
            "per_situation": {},
        }

        # Parse YAML blocks from markdown
        pattern = re.compile(
            r"### Situation:.*?\(`(?P<sit_id>sit_\w+)`\).*?"
            r"option_a_scores:\s*\n"
            r"\s*coherence:\s*(?P<a_coh>\d+)\s*\n"
            r"\s*canon_fidelity:\s*(?P<a_can>\d+)\s*\n"
            r"\s*prose_quality:\s*(?P<a_pro>\d+)\s*\n"
            r"\s*player_agency:\s*(?P<a_age>\d+)\s*\n"
            r"option_b_scores:\s*\n"
            r"\s*coherence:\s*(?P<b_coh>\d+)\s*\n"
            r"\s*canon_fidelity:\s*(?P<b_can>\d+)\s*\n"
            r"\s*prose_quality:\s*(?P<b_pro>\d+)\s*\n"
            r"\s*player_agency:\s*(?P<b_age>\d+)\s*\n"
            r"verdict:\s*(?P<verdict>[^\n]+)\s*\n"
            r"rationale:\s*\"?(?P<rat>[^\n\"]+)\"?",
            re.DOTALL | re.MULTILINE,
        )

        matches = list(pattern.finditer(content))
        if not matches:
            raise ValueError(f"No valid scored YAML blocks parsed from {scored_sheet_path}")

        for m in matches:
            sit_id = m.group("sit_id")
            if sit_id not in sit_keys:
                continue

            mapping = sit_keys[sit_id]
            a_is_new = mapping["option_A"] == "new"

            a_coh, a_can, a_pro, a_age = int(m.group("a_coh")), int(m.group("a_can")), int(m.group("a_pro")), int(m.group("a_age"))
            b_coh, b_can, b_pro, b_age = int(m.group("b_coh")), int(m.group("b_can")), int(m.group("b_pro")), int(m.group("b_age"))

            new_scores = {
                "coherence": a_coh if a_is_new else b_coh,
                "canon_fidelity": a_can if a_is_new else b_can,
                "prose_quality": a_pro if a_is_new else b_pro,
                "player_agency": a_age if a_is_new else b_age,
            }
            baseline_scores = {
                "coherence": b_coh if a_is_new else a_coh,
                "canon_fidelity": b_can if a_is_new else a_can,
                "prose_quality": b_pro if a_is_new else a_pro,
                "player_agency": b_age if a_is_new else a_age,
            }

            for dim in ("coherence", "canon_fidelity", "prose_quality", "player_agency"):
                stats["dimensions"][dim]["new_sum"] += new_scores[dim]
                stats["dimensions"][dim]["baseline_sum"] += baseline_scores[dim]

            verdict_raw = m.group("verdict").strip().lower()
            if "tie" in verdict_raw:
                winner = "tie"
                stats["ties"] += 1
            elif "option a" in verdict_raw:
                winner = "new" if a_is_new else "baseline"
                if winner == "new":
                    stats["new_wins"] += 1
                else:
                    stats["baseline_wins"] += 1
            elif "option b" in verdict_raw:
                winner = "new" if not a_is_new else "baseline"
                if winner == "new":
                    stats["new_wins"] += 1
                else:
                    stats["baseline_wins"] += 1
            else:
                winner = "tie"
                stats["ties"] += 1

            stats["per_situation"][sit_id] = {
                "winner": winner,
                "new_scores": new_scores,
                "baseline_scores": baseline_scores,
                "rationale": m.group("rat"),
            }

        n_evaluated = len(matches)
        if n_evaluated > 0:
            win_or_tie = stats["new_wins"] + stats["ties"]
            stats["new_win_or_tie_rate_pct"] = round((win_or_tie / n_evaluated) * 100, 1)
            stats["meets_r2_criterion"] = stats["new_win_or_tie_rate_pct"] >= 70.0

            for dim in stats["dimensions"]:
                stats["dimensions"][dim]["new_avg"] = round(stats["dimensions"][dim]["new_sum"] / n_evaluated, 2)
                stats["dimensions"][dim]["baseline_avg"] = round(stats["dimensions"][dim]["baseline_sum"] / n_evaluated, 2)

        return stats


# --------------------------------------------------------------------------- #
# CLI Command and Entry Point
# --------------------------------------------------------------------------- #

def build_cli_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="AI Dungeon Local Conversation Evaluation & Blinded A/B Harness",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("--base-url", default="http://vm.groupabz.com:1234/v1", help="llama-server /chat/completions base URL")
    p.add_argument("--api-key", default=os.environ.get("LLM_API_KEY", ""), help="LLM API Key")
    p.add_argument("--situations", default="", help="Path to situations JSON file (uses built-in if omitted)")
    p.add_argument("--mode", choices=["both", "baseline", "new", "mock"], default="mock", help="Execution mode")
    p.add_argument("--output-dir", default="eval_results", help="Directory where evaluation artifacts are written")
    p.add_argument("--seed", type=int, default=42, help="Seed for reproducible blinded A/B assignment")
    p.add_argument("--unblind-scores", default="", help="Path to completed judge score sheet to unblind and verify")
    return p


def load_situations(path_str: str) -> List[Dict[str, Any]]:
    if path_str and Path(path_str).is_file():
        with open(path_str, "r", encoding="utf-8") as f:
            return json.load(f)

    # Search candidates in standard project locations
    candidates = [
        Path(__file__).resolve().parent.parent / "benchmark" / "conversation_situations.json",
        Path("benchmark/conversation_situations.json"),
        Path(__file__).resolve().parent / "conversation_situations.json",
        Path(__file__).resolve().parent / "proposed_conversation_situations.json",
    ]
    for candidate in candidates:
        if candidate.is_file():
            with open(candidate, "r", encoding="utf-8") as f:
                return json.load(f)

    raise FileNotFoundError(f"Benchmark situations file not found: {path_str}")


def main() -> int:
    parser = build_cli_parser()
    args = parser.parse_args()

    # Unblind mode check
    if args.unblind_scores:
        sheet_path = Path(args.unblind_scores)
        key_path = sheet_path.parent / "blind_key.json"
        if not key_path.is_file():
            print(f"[!] Error: blind_key.json not found in {sheet_path.parent}")
            return 1
        with open(key_path, "r", encoding="utf-8") as f:
            blind_key = json.load(f)
        stats = BlindJudgePreparer.unblind_scores(blind_key, sheet_path)
        print("=== Blind Judge Evaluation Results ===")
        print(f"Total Situations: {stats['total_situations']}")
        print(f"New Pipeline Wins: {stats['new_wins']}")
        print(f"Baseline Wins:     {stats['baseline_wins']}")
        print(f"Ties:              {stats['ties']}")
        print(f"Win/Tie Rate:      {stats['new_win_or_tie_rate_pct']}%")
        print(f"Meets R2 Criteria: {'YES (PASS >= 70%)' if stats['meets_r2_criterion'] else 'NO (FAIL)'}")
        print("\nDimension Averages (New vs Baseline):")
        for dim, vals in stats["dimensions"].items():
            print(f"  - {dim:16s}: New={vals.get('new_avg')} | Baseline={vals.get('baseline_avg')}")
        return 0 if stats["meets_r2_criterion"] else 2

    # Benchmark run mode
    situations = load_situations(args.situations)
    print(f"[*] Loaded {len(situations)} benchmark situations. Running in '{args.mode}' mode...")

    evaluator = ConversationEvaluator(
        situations=situations,
        base_url=args.base_url,
        api_key=args.api_key,
        mode=args.mode,
        seed=args.seed,
    )
    raw_results = evaluator.run_benchmark()

    # Prepare outputs
    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    blind_key, blind_eval_data, blind_md = BlindJudgePreparer.prepare_blind_dataset(
        raw_results, situations, seed=args.seed
    )

    # Write artifacts
    (out_dir / "eval_report.json").write_text(json.dumps(raw_results, indent=2), encoding="utf-8")
    (out_dir / "blind_key.json").write_text(json.dumps(blind_key, indent=2), encoding="utf-8")
    (out_dir / "blind_eval.json").write_text(json.dumps(blind_eval_data, indent=2), encoding="utf-8")
    (out_dir / "blind_eval.md").write_text(blind_md, encoding="utf-8")

    # Generate Markdown summary table
    summary_md = [
        "# Conversation Quality Evaluation Summary",
        f"**Timestamp**: {raw_results['timestamp']} | **Situations**: {len(situations)} | **Mode**: {args.mode}",
        "",
        "## Automated Metrics Comparison",
        "| Situation ID | Metric: Repetition (New vs Base) | Impersonation Violations | Truncation Status | Latency (tok/s) |",
        "| :--- | :--- | :--- | :--- | :--- |",
    ]

    b_map = {r["situation_id"]: r for r in raw_results.get("baseline", [])}
    n_map = {r["situation_id"]: r for r in raw_results.get("new", [])}

    for sit in situations:
        sid = sit["id"]
        mb = b_map.get(sid, {}).get("metrics", {})
        mn = n_map.get(sid, {}).get("metrics", {})

        rep_str = f"N:{mn.get('repetition', {}).get('composite_repetition', 0)} vs B:{mb.get('repetition', {}).get('composite_repetition', 0)}"
        imp_str = f"N:{mn.get('impersonation', {}).get('total_violations', 0)} vs B:{mb.get('impersonation', {}).get('total_violations', 0)}"
        tr_str = f"N:{'Trunc' if mn.get('truncation', {}).get('empty_or_truncated') else 'OK'} vs B:{'Trunc' if mb.get('truncation', {}).get('empty_or_truncated') else 'OK'}"
        lat_str = f"N:{mn.get('latency', {}).get('tok_per_sec', 0)} vs B:{mb.get('latency', {}).get('tok_per_sec', 0)}"

        summary_md.append(f"| `{sid}` | {rep_str} | {imp_str} | {tr_str} | {lat_str} |")

    summary_md.extend([
        "",
        "## Blinded Evaluation Files Generated",
        f"- **Judging Packet**: `{out_dir / 'blind_eval.md'}`",
        f"- **Secret Key**: `{out_dir / 'blind_key.json'}`",
        f"- **Machine Report**: `{out_dir / 'eval_report.json'}`",
    ])

    (out_dir / "eval_report.md").write_text("\n".join(summary_md), encoding="utf-8")
    print(f"[+] Evaluation complete. Artifacts written to {out_dir.resolve()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
