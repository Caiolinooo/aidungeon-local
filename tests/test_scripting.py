"""Testes para o motor de scripts em sandbox (backend/app/scripting.py / Milestone 4)."""

from __future__ import annotations

import time
import pytest

from app import db, scripting
from conftest import make_adventure


# =============================================================================
# 1. Modificadores funcionais normais (Input, Context, Output)
# =============================================================================

def test_input_modifier_dict_mutation():
    script = """
action["text"] = "Sneak quietly into the courtyard"
"""
    initial = {"type": "do", "text": "Walk in"}
    res = scripting.run_input_modifier(script, initial)
    assert res == {"type": "do", "text": "Sneak quietly into the courtyard"}


def test_input_modifier_function_definition():
    script = """
def modifier(act):
    act["text"] = act["text"].strip() + " with vigor!"
    act["type"] = "story"
    return act
"""
    initial = {"type": "do", "text": "Strike"}
    res = scripting.run_input_modifier(script, initial)
    assert res == {"type": "story", "text": "Strike with vigor!"}


def test_input_modifier_variable_reassignment():
    script = """
text = text.upper()
"""
    initial = {"type": "say", "text": "hello world"}
    res = scripting.run_input_modifier(script, initial)
    assert res["text"] == "HELLO WORLD"
    assert res["type"] == "say"


def test_context_modifier_list_mutation():
    script = """
context.append({"role": "system", "content": "Bonus instruction: speak in rhyme"})
"""
    initial = [{"role": "system", "content": "Original prompt"}]
    res = scripting.run_context_modifier(script, initial)
    assert len(res) == 2
    assert res[1]["content"] == "Bonus instruction: speak in rhyme"


def test_context_modifier_function():
    script = """
def modifier(ctx):
    # Filter out empty messages
    return [m for m in ctx if m.get("content")]
"""
    initial = [
        {"role": "system", "content": "System prompt"},
        {"role": "user", "content": ""},
        {"role": "assistant", "content": "Hello!"},
    ]
    res = scripting.run_context_modifier(script, initial)
    assert len(res) == 2
    assert [m["role"] for m in res] == ["system", "assistant"]


def test_output_modifier_string_replacement():
    script = """
text = text.replace("orc", "goblin")
"""
    initial = "An angry orc appears from the shadows."
    res = scripting.run_output_modifier(script, initial)
    assert res == "An angry goblin appears from the shadows."


def test_output_modifier_function():
    script = """
def modifier(t):
    return t.title()
"""
    initial = "the dark woods"
    res = scripting.run_output_modifier(script, initial)
    assert res == "The Dark Woods"


# =============================================================================
# 2. Rejeição estática AST (Imports, Dunder, Builtins Perigosos)
# =============================================================================

@pytest.mark.parametrize("malicious_code", [
    "import os\nos.system('echo hacked')",
    "import sys\nsys.exit(1)",
    "from pathlib import Path\nPath('/etc/passwd').read_text()",
    "import subprocess",
    "from os import environ",
])
def test_malicious_imports_rejected(malicious_code):
    initial = {"type": "do", "text": "original input"}
    res = scripting.run_input_modifier(malicious_code, initial)
    assert res == initial  # fallback seguro: valor não modificado


@pytest.mark.parametrize("sandbox_escape_code", [
    "().__class__.__bases__[0].__subclasses__()",
    "x = ().__class__",
    "action['__class__'] = 1",
    "code = action.__code__",
    "b = action.__builtins__",
    "eval('1 + 1')",
    "exec('x = 1')",
    "open('/tmp/test', 'w')",
    "getattr(action, 'keys')",
    "setattr(action, 'foo', 'bar')",
    "globals()",
    "locals()",
    "__import__('os')",
    "breakpoint()",
])
def test_sandbox_escapes_rejected(sandbox_escape_code):
    initial = {"type": "do", "text": "safe original"}
    res = scripting.run_input_modifier(sandbox_escape_code, initial)
    assert res == initial


# =============================================================================
# 3. Timeout e loops infinitos
# =============================================================================

def test_infinite_loop_timeout_protection():
    infinite_loop = """
while True:
    pass
"""
    initial = {"type": "do", "text": "safe"}
    t0 = time.time()
    # Usa timeout de 0.2s para teste ágil
    res = scripting.run_input_modifier(infinite_loop, initial, timeout=0.2)
    elapsed = time.time() - t0

    assert res == initial
    assert elapsed < 1.0, f"Execution took too long: {elapsed:.2f}s"


def test_nested_loop_timeout_protection():
    nested_loop = """
for i in range(10000000):
    for j in range(10000000):
        x = i * j
"""
    initial = "original output"
    t0 = time.time()
    res = scripting.run_output_modifier(nested_loop, initial, timeout=0.2)
    elapsed = time.time() - t0

    assert res == initial
    assert elapsed < 1.0


# =============================================================================
# 4. Falhas de sintaxe e exceções em runtime
# =============================================================================

def test_syntax_error_fallback():
    broken_syntax = "def invalid_syntax(:"
    initial = {"type": "do", "text": "intact"}
    res = scripting.run_input_modifier(broken_syntax, initial)
    assert res == initial


def test_runtime_exception_fallback():
    runtime_error = "x = 1 / 0"
    initial = "intact output"
    res = scripting.run_output_modifier(runtime_error, initial)
    assert res == initial


def test_empty_or_whitespace_script_noop():
    initial = {"type": "say", "text": "Hello"}
    assert scripting.run_input_modifier("", initial) == initial
    assert scripting.run_input_modifier("   \n  ", initial) == initial
    assert scripting.run_context_modifier(None, [{"role": "user", "content": "hi"}]) == [{"role": "user", "content": "hi"}]
    assert scripting.run_output_modifier("", "hello") == "hello"


# =============================================================================
# 5. Integração com game_engine
# =============================================================================

def test_game_engine_scripting_hooks_integration(client):
    """Testa se os hooks de scripting configurados na aventura executam no fluxo do game_engine."""
    from app import game_engine
    from conftest import make_llm_transport

    adv = make_adventure(title="Scripting Adventure")

    # Configura script input_modifier
    scripting.set_adventure_script(
        adv["id"],
        "input_modifier",
        "action['text'] = action['text'].replace('sword', 'enchanted blade')",
    )

    # Configura script output_modifier
    scripting.set_adventure_script(
        adv["id"],
        "output_modifier",
        "text = text + ' [The End of Turn]'",
    )

    transport = make_llm_transport(tokens=("The", " goblin", " flees."))

    # Executa handle_action
    stream = game_engine.handle_action(adv["id"], "do", "Draw your sword", transport=transport)
    events = list(stream)

    # Verifica se a ação do jogador persistida foi modificada pelo input_modifier
    with db.session() as conn:
        actions = db.list_actions(conn, adv["id"])
        player_act = [a for a in actions if a["type"] == "do"][0]
        assert "enchanted blade" in player_act["text"]
        assert "sword" not in player_act["text"]

        # Verifica se o texto da IA recebeu o output_modifier
        ai_act = [a for a in actions if a["type"] == "continue"][0]
        assert "[The End of Turn]" in ai_act["text"]


def test_game_engine_failing_script_does_not_crash_turn(client):
    """Um script com erro de execução ou loop infinito não derruba o turno do jogo."""
    from app import game_engine
    from conftest import make_llm_transport

    adv = make_adventure(title="Crashproof Adventure")

    # Configura script com divisão por zero no input_modifier
    scripting.set_adventure_script(adv["id"], "input_modifier", "1 / 0")

    transport = make_llm_transport(tokens=("You", " survive."))
    stream = game_engine.handle_action(adv["id"], "do", "Attack safely", transport=transport)
    events = list(stream)

    assert any(e.get("event") == "done" for e in events)
    with db.session() as conn:
        actions = db.list_actions(conn, adv["id"])
        player_act = [a for a in actions if a["type"] == "do"][0]
        assert "Attack safely" in player_act["text"]
