"""Game engine — action/continue/retry/undo/edit (PLANO.md §4.2, openapi.yaml).

Semânticas do contrato:
- Lock por aventura: geração concorrente → 409 `generation_in_progress`.
  Só a narração (action/continue/retry) usa esse lock; choices, See e
  ui-profile rodam em paralelo como auxiliares (contrato C1).
- Toda ação nova entra por `appending` + `append_action`: max_seq, insert e
  commit sob um lock próprio, porque See e narrador gravam ao mesmo tempo.
- Ganchos C3: `meta_commands.match` antes do narrador (se casar, o narrador
  não roda) e `output_sanitizer.clean_narration` antes de gravar.
- `action` persiste a ação do jogador FORMATADA (do → "\\n> You {text}\\n";
  say → '\\n> You say "{text}"\\n'; story → prosa) — byte a byte como o backup.
- `retry` só sobre a última action `continue`: regenera com o mesmo contexto
  anterior a ela e substitui (mesmo `seq`, novo `id`). Se a última ação não
  for da IA → 409. Em erro de geração, a resposta antiga é preservada.
- `undo` remove o último turno (resposta da IA + ação do jogador que a
  originou) e NUNCA remove `start` (409 `nothing_to_undo`).
- `PATCH` marca `edited_at`.
- Pós-turno (G3), depois do `done` e com o lock já solto: choices pré-geradas
  (C7, slot 1) em paralelo com npc_chronicle e, depois dele,
  `memory.summarizer.maybe_update()` (slot 2), best-effort. Ação nova cancela
  choices e summary pendentes, nunca o chronicle.

Eventos emitidos pelo gerador (a rota serializa em SSE):
    {"event": "token", "data": {"token": "..."}}
    {"event": "done",  "data": {"player_action"?: Action, "action": Action}}
    {"event": "error", "data": {"message": "..."}}
Antes do `done`, `turn_done {action_id, seq}` sai em `events` (C2).
"""

from __future__ import annotations

import logging
import os
import sqlite3
import threading
from contextlib import contextmanager
from typing import Any, Iterator

from app import (
    choices_service,
    context_builder,
    db,
    events as events_bus,
    llm_client,
    meta_commands,
    npc_bonds,
    npc_chronicle,
    output_sanitizer,
    scripting,
    settings_store,
    web_search as web_search_mod,
)
from app.errors import GameError

log = logging.getLogger("aidungeon.game")

_locks_guard = threading.Lock()
_locks: dict[str, threading.Lock] = {}
# Trabalho pós-turno (summarizer + chronicle) não usa o lock de geração:
# esse já foi solto antes do thread. Um lock próprio por aventura impede
# dois passes de lerem os mesmos marcadores antes de qualquer um gravar.
_post_turn_guard = threading.Lock()
_post_turn_locks: dict[str, threading.Lock] = {}
_post_turn_cancels: dict[str, threading.Event] = {}
# Jogador rápido não pode deixar o resumo parado para sempre: depois de tantos
# passes adiados seguidos, a ação nova deixa o summary terminar.
_MAX_SUMMARY_DEFERRALS = 2
_summary_deferrals: dict[str, int] = {}
_append_guard = threading.Lock()
_append_locks: dict[str, threading.Lock] = {}


# --------------------------------------------------------------- locking
def _lock_for(adventure_id: str) -> threading.Lock:
    with _locks_guard:
        return _locks.setdefault(adventure_id, threading.Lock())


def _post_turn_lock_for(adventure_id: str) -> threading.Lock:
    with _post_turn_guard:
        return _post_turn_locks.setdefault(adventure_id, threading.Lock())


def _append_lock_for(adventure_id: str) -> threading.Lock:
    with _append_guard:
        return _append_locks.setdefault(adventure_id, threading.Lock())


@contextmanager
def appending(adventure_id: str) -> Iterator[sqlite3.Connection]:
    """Transação que grava no fim da aventura. O lock cobre até o commit:
    `actions` tem UNIQUE(adventure_id, seq) e See, jogador e narrador gravam
    em paralelo."""
    with _append_lock_for(adventure_id):
        with db.session() as conn:
            yield conn


def append_action(conn: sqlite3.Connection, adventure_id: str, type_: str,
                  text: str) -> dict:
    """Ação no próximo seq. Só dentro de `appending`."""
    last_seq = db.max_seq(conn, adventure_id)
    seq = 0 if last_seq is None else last_seq + 1
    action = db.insert_action(conn, adventure_id, seq, type_, text)
    db.touch_adventure(conn, adventure_id)
    return action


def acquire_generation_lock(adventure_id: str) -> bool:
    """Lock por aventura (não-bloqueante) da narração: action, continue e
    retry não rodam juntos na mesma aventura."""
    return _lock_for(adventure_id).acquire(blocking=False)


def release_generation_lock(adventure_id: str) -> None:
    lock = _lock_for(adventure_id)
    if lock.locked():
        try:
            lock.release()
        except RuntimeError:
            pass


def is_any_generation_active() -> bool:
    """True quando qualquer aventura está narrando no momento."""
    with _locks_guard:
        return any(lock.locked() for lock in _locks.values())


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


def _maybe_update_memory(adventure_id: str,
                         cancel: threading.Event | None = None) -> None:
    """Hook do memory bank/summarizer — best-effort, nunca quebra a geração."""
    mod = context_builder._memory_module("summarizer")
    if mod is None or not hasattr(mod, "maybe_update"):
        return
    try:
        if cancel is None:
            mod.maybe_update(adventure_id)
        else:
            mod.maybe_update(adventure_id, cancel=cancel)
    except Exception:
        pass


def _prime_memory_query(adventure_id: str) -> None:
    """Vetor da cena para as memórias do próximo turno, fora do TTFT."""
    mod = context_builder._memory_module("retrieval")
    if mod is None or not hasattr(mod, "prime_scene_vector"):
        return
    try:
        with db.session() as conn:
            actions = db.list_actions(conn, adventure_id)
            mem_cfg = settings_store.runtime_config(conn).get("memory_bank", {})
            emb_cfg = settings_store.effective_embeddings_settings(conn)
        mod.prime_scene_vector(
            adventure_id, context_builder.story_text(actions),
            actions[-1]["seq"] if actions else None, emb_cfg,
            query_chars=int(mem_cfg.get("query_chars", mod.DEFAULT_QUERY_CHARS)))
    except Exception:
        pass


def _spawn_post_turn(adventure_id: str, transport: Any = None) -> None:
    """Fora dos testes, roda em background, depois do done e com o lock da
    aventura já solto: choices pré-geradas, o vetor da cena das memórias e,
    em série, npc_chronicle + summarizer."""
    if os.environ.get("AIDUNGEON_CHRONICLE", "1") == "0":
        # Modo de teste determinístico: summarizer roda síncrono e chronicle fica desligado
        _maybe_update_memory(adventure_id)
        return
    choices_service.start(adventure_id, transport=transport)
    cancel = threading.Event()
    with _post_turn_guard:
        _post_turn_cancels[adventure_id] = cancel

    def _bg() -> None:
        with _post_turn_lock_for(adventure_id):
            try:
                npc_chronicle.update_from_turn(adventure_id, transport)
            except Exception:
                pass
            if not cancel.is_set():
                _maybe_update_memory(adventure_id, cancel)
        _finish_post_turn(adventure_id, cancel)

    threading.Thread(target=_prime_memory_query, args=(adventure_id,),
                     name="memory-query", daemon=True).start()
    threading.Thread(target=_bg, name="post-turn-worker", daemon=True).start()


def _finish_post_turn(adventure_id: str, cancel: threading.Event) -> None:
    with _post_turn_guard:
        if _post_turn_cancels.get(adventure_id) is cancel:
            del _post_turn_cancels[adventure_id]
        if cancel.is_set():
            _summary_deferrals[adventure_id] = _summary_deferrals.get(adventure_id, 0) + 1
        else:
            _summary_deferrals.pop(adventure_id, None)


def cancel_post_turn(adventure_id: str) -> None:
    """Ação nova do jogador: choices e summary pendentes do turno anterior
    perdem a vez (o summary roda no pós-turno seguinte); o chronicle segue."""
    with _post_turn_guard:
        cancel = _post_turn_cancels.get(adventure_id)
        if cancel is not None \
                and _summary_deferrals.get(adventure_id, 0) < _MAX_SUMMARY_DEFERRALS:
            cancel.set()
    choices_service.cancel(adventure_id)


def forget_adventure(adventure_id: str) -> None:
    """Aventura apagada: pós-turno cancelado, choices e eventos descartados."""
    with _post_turn_guard:
        cancel = _post_turn_cancels.pop(adventure_id, None)
    if cancel is not None:
        cancel.set()
    choices_service.forget(adventure_id)
    events_bus.forget(adventure_id)


_retry_counts: dict[tuple[str, int], int] = {}


def reset_retry_counts() -> None:
    _retry_counts.clear()


def get_retry_count(adventure_id: str, seq: int) -> int:
    return _retry_counts.get((adventure_id, seq), 0)


# -------------------------------------------------------------- geração
def _advance_committed_turn(conn: sqlite3.Connection, adventure_id: str,
                            player_action: dict | None, user: str) -> str | None:
    """do/say/story do dono da vez passam a vez. continue/retry/see não entram."""
    if not user or not player_action or player_action.get("type") not in db.PLAYER_TYPES:
        return None
    adv = db.get_adventure(conn, adventure_id)
    if not adv or not adv.get("turn_mode") or (adv.get("turn_user") or "") != user:
        return None
    return db.advance_turn(conn, adv)


def _stream_generation(adventure_id: str, *, player_action: dict | None,
                       retry_of: dict | None,
                       retry_count: int = 0,
                       web_search: bool = False,
                       raw_query_text: str = "",
                       transport: Any,
                       user: str = "") -> Iterator[dict]:
    """Gerador de eventos. Contexto = ações atuais (retry: sem a antiga)."""

    def events() -> Iterator[dict]:
        full: list[str] = []
        lock_released = False
        web_sources: list[dict] = []
        new_turn: str | None = None
        try:
            with db.session() as conn:
                adventure = db.get_adventure(conn, adventure_id)
                actions = db.list_actions(conn, adventure_id)
                if retry_of is not None:
                    actions = [a for a in actions if a["id"] != retry_of["id"]]
                cfg = settings_store.runtime_config(conn)
                built = context_builder.build_context(adventure, actions, cfg, conn)
                llm_cfg = cfg["llm"]
                sampler = dict(cfg["sampler"])
                if retry_of is not None and retry_count > 0:
                    base_t = float(sampler.get("temperature", 0.85))
                    step = float(sampler.get("retry_temperature_step", 0.05))
                    max_t = float(sampler.get("max_retry_temperature", 1.15))
                    sampler["temperature"] = round(min(base_t + step * retry_count, max_t), 4)

            if web_search and raw_query_text.strip():
                research = web_search_mod.research_context(
                    raw_query_text,
                    llm_cfg=llm_cfg,
                    language="pt",
                    mode="always",
                    max_results=3,
                    transport=transport,
                )
                web_sources = research.get("sources") or []
                ctx_block = research.get("context_block") or ""
                if ctx_block and built.messages:
                    built.messages[-1] = {
                        "role": built.messages[-1]["role"],
                        "content": context_builder.with_reference(
                            built.messages[-1]["content"], ctx_block),
                    }

            # Scripting hook: context modifier (before LLM dispatch)
            ctx_script = scripting.get_adventure_script(adventure_id, "context_modifier")
            if ctx_script:
                built.messages = scripting.run_context_modifier(ctx_script, built.messages)

            stream_state = llm_client.StreamState()
            for token in llm_client.stream_chat_completion(
                    llm_cfg, built.messages, sampler=sampler,
                    stop=built.stop_sequences, transport=transport,
                    state=stream_state):
                full.append(token)
                yield {"event": "token", "data": {"token": token}}

            text = output_sanitizer.clean_narration("".join(full), stream_state.truncated)
            text = npc_bonds.strip_leaked(text)
            # Scripting hook: output modifier (after generation completion)
            out_script = scripting.get_adventure_script(adventure_id, "output_modifier")
            if out_script:
                text = scripting.run_output_modifier(out_script, text)
            if not text.strip():
                # Só notas de contexto (cortadas pela stop ou pelo sanitizer) ou
                # resposta vazia: um continue vazio quebraria choices e chronicle.
                yield {"event": "error", "data": {
                    "message": "the narrator returned no story text; nothing was saved, try again"}}
                return

            with appending(adventure_id) as conn:
                if retry_of is not None:
                    # substitui: mesmo seq, novo id (apaga a antiga só no sucesso)
                    db.delete_action(conn, retry_of["id"])
                    action = db.insert_action(conn, adventure_id, retry_of["seq"],
                                              "continue", text)
                    db.touch_adventure(conn, adventure_id)
                else:
                    action = append_action(conn, adventure_id, "continue", text)
                # Pesos no mesmo commit da prosa: o turno seguinte já lê
                # os números novos. Falha aqui não apaga a narração.
                try:
                    player_text = (player_action or {}).get("text") or ""
                    npc_bonds.apply_turn(conn, adventure_id, player_text, text)
                except Exception:
                    log.warning("npc_bonds: pesos não atualizados", exc_info=True)
                new_turn = _advance_committed_turn(conn, adventure_id, player_action, user)

            data: dict[str, Any] = {"action": action}
            if player_action is not None:
                data["player_action"] = player_action
            if web_sources:
                data["web_sources"] = web_sources

            # Libera o lock antes de emitir 'done' para que chamadas imediatas do
            # cliente (ex.: /choices no onDone) nunca tomem 409 por corrida de thread.
            release_generation_lock(adventure_id)
            lock_released = True
            if new_turn is not None:
                events_bus.publish(adventure_id, "turn_changed", {"turn_user": new_turn})
            events_bus.publish(adventure_id, "turn_done",
                               {"action_id": action["id"], "seq": action["seq"]})
            _spawn_post_turn(adventure_id, transport)
            yield {"event": "done", "data": data}
        except llm_client.LlmError as exc:
            yield {"event": "error", "data": {"message": exc.message}}
        except Exception as exc:  # defesa — o stream sempre termina com erro legível
            yield {"event": "error", "data": {"message": f"{type(exc).__name__}: {exc}"}}
        finally:
            if not lock_released:
                release_generation_lock(adventure_id)

    return events()


def handle_action(adventure_id: str, type_: str, text: str,
                  transport: Any = None, *,
                  web_search: bool = False,
                  user: str = "") -> Iterator[dict]:
    """POST /action: persiste a ação do jogador e streama a resposta da IA."""
    _adventure_or_404(adventure_id)
    if type_ not in db.PLAYER_TYPES:
        raise GameError.conflict("invalid_action_type", f"type must be one of {db.PLAYER_TYPES}")
    cmd = meta_commands.match(text, type_)
    if cmd is not None:
        return _meta_command_events(adventure_id, cmd, transport)
    if not acquire_generation_lock(adventure_id):
        raise GameError.conflict("generation_in_progress",
                                 "a generation is already running for this adventure")
    try:
        cancel_post_turn(adventure_id)
        # Scripting hook: input modifier (before input processing)
        in_script = scripting.get_adventure_script(adventure_id, "input_modifier")
        if in_script:
            mod_action = scripting.run_input_modifier(in_script, {"type": type_, "text": text})
            if isinstance(mod_action, dict):
                type_ = mod_action.get("type", type_)
                text = mod_action.get("text", text)

        with appending(adventure_id) as conn:
            player_action = append_action(conn, adventure_id, type_,
                                          _format_player_text(type_, text))
            _retry_counts.pop((adventure_id, player_action["seq"]), None)
    except Exception:
        release_generation_lock(adventure_id)
        raise
    return _stream_generation(adventure_id, player_action=player_action,
                              retry_of=None, retry_count=0,
                              web_search=web_search, raw_query_text=text,
                              transport=transport, user=user)


def _meta_command_events(adventure_id: str, cmd: meta_commands.MetaCommand,
                         transport: Any) -> Iterator[dict]:
    """Meta-comando no lugar do narrador: sem lock de geração, nada vira prosa."""
    try:
        yield from meta_commands.execute(adventure_id, cmd, transport)
    except llm_client.LlmError as exc:
        yield {"event": "error", "data": {"message": exc.message}}
    except Exception as exc:  # defesa — o stream sempre termina com erro legível
        yield {"event": "error", "data": {"message": f"{type(exc).__name__}: {exc}"}}


def handle_continue(adventure_id: str, transport: Any = None) -> Iterator[dict]:
    """POST /continue: a IA segue sozinha (sem nova entrada do jogador)."""
    _adventure_or_404(adventure_id)
    if not acquire_generation_lock(adventure_id):
        raise GameError.conflict("generation_in_progress",
                                 "a generation is already running for this adventure")
    cancel_post_turn(adventure_id)
    return _stream_generation(adventure_id, player_action=None,
                              retry_of=None, retry_count=0, transport=transport)


def handle_retry(adventure_id: str, transport: Any = None, *,
                 retry_count: int | None = None) -> Iterator[dict]:
    """POST /retry: regenera a última resposta da IA com stepping de temperatura."""
    _adventure_or_404(adventure_id)
    with db.session() as conn:
        actions = db.list_actions(conn, adventure_id)
    if not actions or actions[-1]["type"] != "continue":
        raise GameError.conflict("not_regenerable",
                                 "last action is not an AI response (continue)")
    old = actions[-1]
    if not acquire_generation_lock(adventure_id):
        raise GameError.conflict("generation_in_progress",
                                 "a generation is already running for this adventure")
    cancel_post_turn(adventure_id)

    key = (adventure_id, int(old["seq"]))
    if retry_count is None:
        _retry_counts[key] = _retry_counts.get(key, 0) + 1
        effective_count = _retry_counts[key]
    else:
        effective_count = retry_count

    return _stream_generation(adventure_id, player_action=None,
                              retry_of=old, retry_count=effective_count, transport=transport)


# ------------------------------------------------------------------ undo
def handle_undo(adventure_id: str) -> dict:
    """POST /undo: remove o último turno; nunca remove a action `start`."""
    with appending(adventure_id) as conn:
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
            # Um See pedido no meio do turno grava entre a ação e a resposta;
            # ele não é parte do turno e fica.
            prev = next((a for a in reversed(actions[:-1]) if a["type"] != "see"), None)
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
        choices_service.cancel(adventure_id)
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
        edited = db.get_action(conn, action_id)
    # O cache das choices é preso a (id, seq): texto editado não muda a chave.
    choices_service.forget(action["adventure_id"])
    return edited
