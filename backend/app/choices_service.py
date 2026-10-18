"""Choices pré-geradas (contrato C7).

- `start(adventure_id)` logo depois do `done`: gera em background (role
  `choices`, slot 1) as opções da última ação narrativa e publica
  `choices_ready {for_seq, choices}` ou `choices_failed {for_seq, code,
  message}`.
- `get(adventure_id)` (POST /choices) devolve o cache da última ação, ou
  espera no máximo `wait_s` pela geração em curso (inicia uma se não houver);
  nunca 409.
- `cancel(adventure_id)`: ação nova do jogador cancela a geração pendente.

O cache é um por aventura, preso à ação narrativa (id e seq): `retry` troca o
id e `undo` troca a última ação, então nenhum dos dois reaproveita opções
velhas. `see` não conta como ação do turno.
"""

from __future__ import annotations

import json
import logging
import re
import threading
from dataclasses import dataclass, field
from typing import Any

from app import db, events, llm_client, settings_store

log = logging.getLogger("aidungeon.choices")

CHOICE_TYPES = ("do", "say", "story")
MAX_TEXT = 120

SYSTEM = """You suggest the next actions a player could take in an interactive fiction game.
Given the recent scene, output STRICT JSON: an array of exactly {count} options, each {{"type": "do"|"say"|"story", "text": "..."}}.
- "do": a physical action ("draw your sword")
- "say": something to speak ("ask the innkeeper about the missing caravan")
- "story": a narrative beat the player injects ("a storm rolls in")
Texts in English, concise (under 120 chars), varied. Output ONLY the JSON array."""


class ChoicesPending(Exception):
    """A geração não terminou dentro do `wait_s`."""


class ChoicesFailed(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def choices_cfg() -> dict:
    cfg = settings_store.load_file_settings().get("choices", {})
    return {
        "enabled": bool(cfg.get("enabled", True)),
        "count": int(cfg.get("count", 3)),
        "max_tokens": int(cfg.get("max_tokens", 220)),
        "temperature": float(cfg.get("temperature", 0.8)),
        "wait_s": float(cfg.get("wait_s", 8.0)),
    }


def parse_choices(content: str, count: int) -> list[dict]:
    """Extração robusta do array JSON + normalização (type válido, ≤120 chars)."""
    cleaned = re.sub(r"```(?:json)?", "", content).strip()
    start, end = cleaned.find("["), cleaned.rfind("]")
    if start < 0 or end <= start:
        raise ValueError("resposta sem array JSON")
    raw = json.loads(cleaned[start:end + 1])
    if not isinstance(raw, list):
        raise ValueError("resposta não é uma lista")
    choices: list[dict] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        text = str(item.get("text") or "").strip()[:MAX_TEXT]
        if not text:
            continue
        type_ = str(item.get("type") or "").strip().lower()
        if type_ not in CHOICE_TYPES:
            type_ = "do"
        choices.append({"type": type_, "text": text})
        if len(choices) >= count:
            break
    return choices


def messages_for(actions: list[dict], count: int) -> list[dict]:
    scene = "".join(a["text"] for a in actions if a["type"] != "see")[-2000:]
    return [
        {"role": "system", "content": SYSTEM.replace("{count}", str(count))},
        {"role": "user",
         "content": f"Recent scene:\n{scene.strip() or '(beginning of the adventure)'}"},
    ]


def _turn_key(actions: list[dict]) -> tuple[int, int]:
    """(id, seq) da última ação que não é `see`; (0, -1) sem ações."""
    for action in reversed(actions):
        if action["type"] != "see":
            return int(action["id"]), int(action["seq"])
    return 0, -1


@dataclass
class _Entry:
    key: tuple[int, int]
    cancel: threading.Event = field(default_factory=threading.Event)
    done: threading.Event = field(default_factory=threading.Event)
    choices: list[dict] | None = None
    error: tuple[str, str] | None = None

    @property
    def for_seq(self) -> int:
        return self.key[1]

    def reusable(self) -> bool:
        if self.cancel.is_set():
            return False
        return not self.done.is_set() or self.error is None


_lock = threading.Lock()
_entries: dict[str, _Entry] = {}


def start(adventure_id: str, *, transport: Any = None) -> _Entry | None:
    """Geração das choices da última ação (reaproveita a em curso ou pronta).
    None quando choices está desligado ou a aventura não existe."""
    cfg = choices_cfg()
    if not cfg["enabled"]:
        return None
    with db.session() as conn:
        if db.get_adventure(conn, adventure_id) is None:
            return None
        actions = db.list_actions(conn, adventure_id)
        llm_cfg = settings_store.effective_llm_settings(conn)
    key = _turn_key(actions)
    with _lock:
        entry = _entries.get(adventure_id)
        if entry is not None and entry.key == key and entry.reusable():
            return entry
        if entry is not None:
            entry.cancel.set()
        entry = _entries[adventure_id] = _Entry(key)
    threading.Thread(target=_run, args=(adventure_id, entry, actions, llm_cfg, cfg, transport),
                     name="choices-pregen", daemon=True).start()
    return entry


def _run(adventure_id: str, entry: _Entry, actions: list[dict], llm_cfg: dict,
         cfg: dict, transport: Any) -> None:
    try:
        content = llm_client.chat_completion(
            llm_cfg, messages_for(actions, cfg["count"]), role="choices",
            temperature=cfg["temperature"], max_tokens=cfg["max_tokens"],
            transport=transport, cancel=entry.cancel)
        entry.choices = parse_choices(content, cfg["count"])
    except llm_client.LlmError as exc:
        entry.error = (exc.code, exc.message)
    except (ValueError, TypeError) as exc:
        log.warning("choices: resposta do LLM não parseável: %s", exc)
        entry.error = ("generation_failed", "LLM returned an unparseable choices response")
    except Exception:
        log.exception("choices: falha inesperada (adventure=%s)", adventure_id)
        entry.error = ("generation_failed", "choices generation failed")
    finally:
        entry.done.set()
    if entry.cancel.is_set():
        return
    if entry.error is None:
        events.publish(adventure_id, "choices_ready",
                       {"for_seq": entry.for_seq, "choices": entry.choices})
    else:
        code, message = entry.error
        events.publish(adventure_id, "choices_failed",
                       {"for_seq": entry.for_seq, "code": code, "message": message})


def get(adventure_id: str, *, transport: Any = None, wait_s: float | None = None) -> list[dict]:
    """Choices da última ação; [] com choices desligado. Levanta
    `ChoicesPending` depois de `wait_s` e `ChoicesFailed` com o erro da geração."""
    entry = start(adventure_id, transport=transport)
    if entry is None:
        return []
    wait = choices_cfg()["wait_s"] if wait_s is None else wait_s
    if not entry.done.wait(wait):
        raise ChoicesPending("choices are still being generated")
    if entry.error is not None:
        raise ChoicesFailed(*entry.error)
    return list(entry.choices or [])


def cancel(adventure_id: str) -> None:
    with _lock:
        entry = _entries.get(adventure_id)
    if entry is not None and not entry.done.is_set():
        entry.cancel.set()


def forget(adventure_id: str) -> None:
    with _lock:
        entry = _entries.pop(adventure_id, None)
    if entry is not None:
        entry.cancel.set()


def reset() -> None:
    """Testes: cancela e descarta o cache."""
    with _lock:
        entries = list(_entries.values())
        _entries.clear()
    for entry in entries:
        entry.cancel.set()
