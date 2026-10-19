"""output_sanitizer (K1): o narrador não grava blocos de contexto, falas do
jogador nem frase cortada pelo teto de tokens."""

from __future__ import annotations

import pytest

from app import context_builder as cb
from app.output_sanitizer import (
    LORE_MARKER,
    clean_narration,
    cut_incomplete_sentence,
)

# Formato do seq 19 da Costazul: o jogador pediu "cria o card da clara" e o
# narrador devolveu um card no formato do contexto, sem nenhuma prosa.
SEQ_19 = (
    "[World info]\n"
    "Appearance: A woman in her late twenties with pale skin and long black hair. "
    "She wears a vibrant red dress.\n"
    "Personality: Calm and observant.\n"
    "Stats: str 8, dex 10, con 9, int 7, wis 6, cha 9\n"
    "\n"
    "[Continuity Anchor: Clara]\n"
    "Clara is a woman who was wandering the boardwalk. She waits in the suite."
)


def test_seq_19_card_dump_leaves_nothing_to_save():
    assert clean_narration(SEQ_19, False) == ""


def test_every_context_stop_is_also_cleaned():
    for marker in cb.CONTEXT_STOPS:
        assert LORE_MARKER.match(marker), marker


@pytest.mark.parametrize("leak", [
    "[World info]\nClara: devota.",
    "[world info]\nClara: devota.",
    "**[World Info]**\nClara: devota.",
    "[Memories]\nCaio salvou Clara.",
    "[Summary]\nCaio chegou a Costazul.",
    "[Player state]\nStats: HP 9",
    "[Plot]\nYou are Caio.",
    "[Author's note: Mundo vivo.]",
    "[Author’s Note: Mundo vivo.]",
    "[Active Lore: Clara — devota]",
    "[Continuity Anchor: Protagonist/Premise: Caio]",
    "[Style: complete sentences.]",
    "[Canon: a card overrides prose.]",
    "[Private: the <lore> notes are for you alone.]",
    "[Web Research & Canon Facts]\n- Costazul: cidade.",
    "<lore>\n[Summary]\nResumo.\n</lore>",
    "< LORE >\nResumo.",
])
def test_leaked_block_after_prose_is_cut(leak):
    prose = "Clara sorri e se aproxima da janela."
    assert clean_narration(f"{prose}\n\n{leak}\nMais texto que veio junto.", False) == prose


def test_leading_block_goes_and_the_prose_after_it_stays():
    text = "[World info]\nClara: devota.\nStats: str 8\n\nA porta range. Clara entra."
    assert clean_narration(text, False) == "A porta range. Clara entra."


def test_leading_lore_element_goes_through_its_closing_tag():
    text = "<lore>\n[Summary]\nResumo.\n</lore>\nA porta range."
    assert clean_narration(text, False) == "A porta range."


def test_marker_in_the_middle_cuts_the_half_sentence_before_it():
    text = "A porta range. Clara se vira e [Active Lore: Clara — devota] diz algo."
    assert clean_narration(text, False) == "A porta range."


def test_player_echo_at_the_start_goes_and_the_narration_stays():
    text = "> You look around.\nThe market is loud. A vendor waves."
    assert clean_narration(text, False) == "The market is loud. A vendor waves."
    assert clean_narration("> Você olha em volta.\n\nO mercado ferve.", False) == "O mercado ferve."


def test_narrator_writing_the_players_next_move_is_cut():
    text = 'The guard frowns. "Who goes there?"\n> You say "a friend"\nThe guard nods.'
    assert clean_narration(text, False) == 'The guard frowns. "Who goes there?"'


def test_blockquote_that_is_not_the_player_stays():
    text = "O bilhete diz:\n> Encontre-me à meia-noite.\nClara guarda o papel."
    assert clean_narration(text, False) == text


def test_brackets_that_are_not_context_stay():
    text = "Na placa se lê [Taverna do Corvo]. Clara entra primeiro."
    assert clean_narration(text, False) == text


def test_plain_prose_is_untouched_apart_from_trailing_space():
    text = "\n\nA chuva aperta lá fora.\n\n— Fique — pede Clara.  \n"
    assert clean_narration(text, False) == "\n\nA chuva aperta lá fora.\n\n— Fique — pede Clara."


def test_markdown_left_alone_after_a_cut_is_dropped():
    assert clean_narration("**[World info]**", False) == ""
    assert clean_narration("*\n[Summary]\nResumo.", False) == ""


# ------------------------------------------------------ frase incompleta
def test_truncated_reply_stops_at_the_last_full_sentence():
    assert clean_narration("She ran. And then", True) == "She ran."
    assert clean_narration("She ran. And then", False) == "She ran. And then"


@pytest.mark.parametrize("text, expected", [
    ('She says, "Come in." He waits and', 'She says, "Come in."'),
    ("— Entre — disse ela. Ele espera e", "— Entre — disse ela."),
    ("Ela hesita… e então", "Ela hesita…"),
    ("Quem é você?! Ela recua e", "Quem é você?!"),
    ("(Ela sorri.) Depois", "(Ela sorri.)"),
    ("Clara chama o Dr. Villar, que", "Clara chama o Dr. Villar, que"),
    ("A porta abre. O Dr. Villar entra e", "A porta abre."),
    ("Custa 3.5 moedas. Ela paga e", "Custa 3.5 moedas."),
    ("apesar", "apesar"),
    ("Done.", "Done."),
    ('"Fique."', '"Fique."'),
])
def test_cut_incomplete_sentence(text, expected):
    assert cut_incomplete_sentence(text) == expected
