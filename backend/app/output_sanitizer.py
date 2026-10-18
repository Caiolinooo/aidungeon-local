"""Limpeza da narração antes de gravar (contrato C3, docs/agents/CONTRATOS.md).

`game_engine` chama `clean_narration(text, truncated)` depois do stream e antes
de persistir; o `done` leva o texto já limpo. Hoje só corta a frase que o teto
de tokens deixou pela metade. A remoção de blocos de contexto vazados
(`[World info]`, `[Continuity Anchor` etc.) é da tarefa K1.
"""

from __future__ import annotations


def cut_incomplete_sentence(text: str) -> str:
    """O teto de tokens cortou no meio. A prosa gravada para no último . ! ?"""
    stripped = text.rstrip()
    if not stripped or stripped[-1] in ".!?":
        return stripped
    end = max(stripped.rfind("."), stripped.rfind("!"), stripped.rfind("?"))
    if end < 0:
        return stripped
    return stripped[: end + 1].rstrip()


def clean_narration(text: str, truncated: bool) -> str:
    text = text.rstrip()
    if truncated:
        text = cut_incomplete_sentence(text)
    return text
