"""Meta-comandos do jogador (contrato C3, docs/agents/CONTRATOS.md).

`game_engine.handle_action` chama `match(text, type_)` antes do narrador. Se
casar, roda `execute(adventure_id, cmd, transport)` no lugar da narração:
nada é gravado como prosa e os eventos vão direto para o SSE da ação. Os
comandos (`/card`, `/foto`, `/cena` e as frases equivalentes em pt/en) são da
tarefa K2; por enquanto nada casa.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterator


@dataclass(frozen=True)
class MetaCommand:
    kind: str           # "card" | "portrait" | "scene"
    name: str | None    # alvo ("Clara"); None em /cena
    raw: str            # texto original


def match(text: str, type_: str) -> MetaCommand | None:
    return None


def execute(adventure_id: str, cmd: MetaCommand,
            transport: Any = None) -> Iterator[dict]:
    yield {"event": "error",
           "data": {"message": f"meta command '{cmd.kind}' is not available"}}
