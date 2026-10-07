"""Game engine — action/continue/retry/undo/edit (PLANO.md §4.2, openapi.yaml).

Semânticas do contrato:
- Lock por aventura: geração concorrente → 409 `generation_in_progress`.
- `action` persiste a ação do jogador FORMATADA (do → "\\n> You {text}\\n";
  say → '\\n> You say "{text}"\\n'; story → prosa) — byte a byte como o backup.
- `retry` só sobre a última action `continue`: regenera com o mesmo contexto
  anterior a ela e substitui (mesmo `seq`, novo `id`). Se a última ação não
  for da IA → 409. Em erro de geração, a resposta antiga é preservada.
- `undo` remove o último turno (resposta da IA + ação do jogador que a
  originou) e NUNCA remove `start` (409 `nothing_to_undo`).
- `PATCH` marca `edited_at`.
- Após gerar, chama `memory.summarizer.maybe_update()` best-effort (import
  tardio — o módulo pode ainda não existir).

Eventos emitidos pelo gerador (a rota serializa em SSE):
    {"event": "token", "data": {"token": "..."}}
    {"event": "done",  "data": {"player_action"?: Action, "action": Action}}
    {"event": "error", "data": {"message": "..."}}
"""

from __future__ import annotations

import threading
from typing import Any, Iterator

from app import context_builder, db, llm_client, settings_store
from app.errors import GameError

_locks_guard = threading.Lock()
_locks: dict[str, threading.Lock] = {}


# --------------------------------------------------------------- locking
def _lock_for(adventure_id: str) -> threading.Lock:
    with _locks_guard:
        return _locks.setdefault(adventure_id, threading.Lock())


def _acquire(adventure_id: str) -> bool:
    return _lock_for(adventure_id).acquire(blocking=False)


def _release(adventure_id: str) -> None:
    _lock_for(adventure_id).release()


# --------------------------------------------------------------- helpers
def _adventure_or_404(adventure_id: str) -> dict:
    with db.session() as conn:
        adv = db.get_adventure(conn, adventure_id)
    if not adv:
        raise GameError.not_found(f"adventure {adventure_id} not found")
    return adv


def _format_player_text(type_: str, text: str) -> str:
    """Texto persistido da ação do jogador — mesmo formato do backup original."""
    inner = text.strip()
    if type_ == "do":
        return f"\n> You {inner}\n"
    if type_ == "say":
        return f'\n> You say "{inner}"\n'
    return inner  # story: prosa pura


def _maybe_update_memory(adventure_id: str) -> None:
    """Hook do memory bank/summarizer — best-effort, nunca quebra a geração."""
    mod = context_builder._memory_module("summarizer")
    if mod is None or not hasattr(mod, "maybe_update"):
        return
    try:
        mod.maybe_update(adventure_id)
    except Exception:
        pass


# -------------------------------------------------------------- geração
def _stream_generation(adventure_id: str, *, player_action: dict | None,
                       retry_of: dict | None,
                       transport: Any) -> Iterator[dict]:
    """Gerador de eventos. Contexto = ações atuais (retry: sem a antiga)."""

    def events() -> Iterator[dict]:
        full: list[str] = []
        try:
            with db.session() as conn:
                adventure = db.get_adventure(conn, adventure_id)
                actions = db.list_actions(conn, adventure_id)
                if retry_of is not None:
                    actions = [a for a in actions if a["id"] != retry_of["id"]]
                cfg = settings_store.runtime_config(conn)
                built = context_builder.build_context(adventure, actions, cfg, conn)
                llm_cfg = cfg["llm"]
                sampler = cfg["sampler"]

            for token in llm_client.stream_chat_completion(
                    llm_cfg, built.messages, sampler=sampler,
                    stop=built.stop_sequences, transport=transport):
                full.append(token)
                yield {"event": "token", "data": {"token": token}}

            text = "".join(full).rstrip()
            with db.session() as conn:
                if retry_of is not None:
                    # substitui: mesmo seq, novo id (apaga a antiga só no sucesso)
                    db.delete_action(conn, retry_of["id"])
                    action = db.insert_action(conn, adventure_id, retry_of["seq"],
                                              "continue", text)
                else:
                    last_seq = db.max_seq(conn, adventure_id)
                    seq = 0 if last_seq is None else last_seq + 1
                    action = db.insert_action(conn, adventure_id, seq, "continue", text)
                db.touch_adventure(conn, adventure_id)

            data: dict[str, Any] = {"action": action}
            if player_action is not None:
                data["player_action"] = player_action
            yield {"event": "done", "data": data}
            _maybe_update_memory(adventure_id)
        except llm_client.LlmError as exc:
            yield {"event": "error", "data": {"message": exc.message}}
        except Exception as exc:  # defesa — o stream sempre termina com erro legível
            yield {"event": "error", "data": {"message": f"{type(exc).__name__}: {exc}"}}
        finally:
            _release(adventure_id)

    return events()


def handle_action(adventure_id: str, type_: str, text: str,
                  transport: Any = None) -> Iterator[dict]:
    """POST /action: persiste a ação do jogador e streama a resposta da IA."""
    _adventure_or_404(adventure_id)
    if type_ not in db.PLAYER_TYPES:
        raise GameError.conflict("invalid_action_type", f"type must be one of {db.PLAYER_TYPES}")
    if not _acquire(adventure_id):
        raise GameError.conflict("generation_in_progress",
                                 "a generation is already running for this adventure")
    try:
        with db.session() as conn:
            last_seq = db.max_seq(conn, adventure_id)
            seq = 0 if last_seq is None else last_seq + 1
            player_action = db.insert_action(conn, adventure_id, seq,
                                             type_, _format_player_text(type_, text))
            db.touch_adventure(conn, adventure_id)
    except Exception:
        _release(adventure_id)
        raise
    return _stream_generation(adventure_id, player_action=player_action,
                              retry_of=None, transport=transport)


def handle_continue(adventure_id: str, transport: Any = None) -> Iterator[dict]:
    """POST /continue: a IA segue sozinha (sem nova entrada do jogador)."""
    _adventure_or_404(adventure_id)
    if not _acquire(adventure_id):
        raise GameError.conflict("generation_in_progress",
                                 "a generation is already running for this adventure")
    return _stream_generation(adventure_id, player_action=None,
                              retry_of=None, transport=transport)


def handle_retry(adventure_id: str, transport: Any = None) -> Iterator[dict]:
    """POST /retry: regenera a última resposta da IA (mesmo seq, novo id)."""
    _adventure_or_404(adventure_id)
    with db.session() as conn:
        actions = db.list_actions(conn, adventure_id)
    if not actions or actions[-1]["type"] != "continue":
        raise GameError.conflict("not_regenerable",
                                 "last action is not an AI response (continue)")
    old = actions[-1]
    if not _acquire(adventure_id):
        raise GameError.conflict("generation_in_progress",
                                 "a generation is already running for this adventure")
    return _stream_generation(adventure_id, player_action=None,
                              retry_of=old, transport=transport)


# ------------------------------------------------------------------ undo
def handle_undo(adventure_id: str) -> dict:
    """POST /undo: remove o último turno; nunca remove a action `start`."""
    with db.session() as conn:
        if not db.get_adventure(conn, adventure_id):
            raise GameError.not_found(f"adventure {adventure_id} not found")
        actions = db.list_actions(conn, adventure_id)
        if not actions:
            raise GameError.conflict("nothing_to_undo", "adventure has no actions")
        last = actions[-1]
        if last["type"] == "start":
            raise GameError.conflict("nothing_to_undo", "cannot undo the opening action")

        removed: list[dict] = []
        if last["type"] == "continue":
            db.delete_action(conn, last["id"])
            removed.append(last)
            prev = actions[-2] if len(actions) >= 2 else None
            if prev and prev["type"] in db.PLAYER_TYPES:
                db.delete_action(conn, prev["id"])
                removed.append(prev)
        else:
            # ação do jogador órfã (geração falhou) ou 'see': remove só ela
            db.delete_action(conn, last["id"])
            removed.append(last)

        removed.sort(key=lambda a: a["seq"])
        db.touch_adventure(conn, adventure_id)
        remaining = db.list_actions(conn, adventure_id)
        return {
            "removed": removed,
            "last_action": remaining[-1] if remaining else None,
            "action_count": len(remaining),
        }


# ------------------------------------------------------------------ edit
def handle_edit(action_id: int, text: str) -> dict:
    """PATCH /api/actions/{id}: atualiza texto e marca edited_at."""
    with db.session() as conn:
        action = db.get_action(conn, action_id)
        if not action:
            raise GameError.not_found(f"action {action_id} not found")
        db.update_action_text(conn, action_id, text)
        db.touch_adventure(conn, action["adventure_id"])
        return db.get_action(conn, action_id)
