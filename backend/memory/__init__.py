"""Pacote de memória — PLANO.md §4.3 (agente engenheiro-memoria).

- triggers.py:   gatilhos de story cards (keyword scan + pinned + budgets).
- retrieval.py:  recuperação de memórias do memory bank (recency + keyword; v1
                 sem embeddings — fase 4 do plano).
- summarizer.py: memory bank (resumos a cada ~1500 tokens) e story summary
                 rolante, via hook best-effort do game_engine.

Interface injetável consumida por app.context_builder / app.game_engine via
import tardio (ver context_builder._memory_module).
"""
