"""Aventuras com o pós-turno em curso (crônica, consequências, mente, memória).

Depois de um turno a placa pintava retratos atualizados, clipes automáticos e
o fundo enquanto a crônica ainda gerava: com a placa ocupada o narrador cai de
60 para 8 tok/s e a crônica levou 275 s num turno de teste (prazo de 150 s e
nova tentativa). O worker de jobs segura o que não é urgente dessas aventuras
até o pós-turno acabar; fundo, See e retrato pedido seguem na hora.
"""

from __future__ import annotations

import threading
import time

# O registro fica na memória do processo: vale porque a API e o worker de jobs
# rodam no mesmo processo uvicorn (um worker). Com vários processos ele teria
# de ir para o banco.
# Teto: pós-turno travado não segura a fila para sempre.
HOLD_S = 240.0

_lock = threading.Lock()
_busy: dict[str, float] = {}


def begin(adventure_id: str) -> None:
    """O relógio conta do primeiro turno da sequência: quem joga um turno por
    minuto não renova a espera para sempre, e a cada `HOLD_S` a fila anda."""
    with _lock:
        _busy.setdefault(str(adventure_id), time.monotonic())


def end(adventure_id: str) -> None:
    with _lock:
        _busy.pop(str(adventure_id), None)


def busy() -> list[str]:
    now = time.monotonic()
    with _lock:
        for adventure_id in [key for key, started in _busy.items() if now - started > HOLD_S]:
            del _busy[adventure_id]
        return sorted(_busy)


def reset() -> None:
    with _lock:
        _busy.clear()
