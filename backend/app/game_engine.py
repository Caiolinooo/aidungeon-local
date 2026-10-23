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
    {"event": "action_check", "data": {"possible": bool, "blockers": [], "risks": [], "notes": "", "cost": "", "consequence": ""}}
    {"event": "token", "data": {"token": "..."}}
    {"event": "done",  "data": {"player_action"?: Action, "action": Action, "action_check"?: object}}
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
    action_check,
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
                  text: str, *, actor: str = "") -> dict:
    """Ação no próximo seq. Só dentro de `appending`."""
    last_seq = db.max_seq(conn, adventure_id)
    seq = 0 if last_seq is None else last_seq + 1
    action = db.insert_action(conn, adventure_id, seq, type_, text, actor=actor)
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


def _revert_applied_rules(adventure_id: str, action_id: int | None) -> None:
    """Narrador falhou depois do motor: desfaz PV/itens para a próxima tentativa."""
    if not action_id:
        return
    try:
        from app.sim import engine as sim_engine
        with appending(adventure_id) as conn:
            sim_engine.revert(conn, adventure_id, action_id)
    except Exception:
        log.warning("sim: revert falhou", exc_info=True)


def _publish_live(adventure_id: str, type_: str, data: dict) -> None:
    """Evento do bus para quem observa. Falha aqui não interrompe o stream."""
    try:
        events_bus.publish(adventure_id, type_, data)
    except Exception:
        log.warning("live event %s not published", type_, exc_info=True)


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
                from app import jobs, npc_life
                npc_life.run_mind(adventure_id, transport)
                life = settings_store.load_file_settings().get("life") or {}
                if life.get("auto_tick", True):
                    every = int(life.get("tick_every_turns", 5) or 5)
                    with db.session() as conn:
                        adventure = db.get_adventure(conn, adventure_id) or {}
                        actions = db.list_actions(conn, adventure_id)
                        seq = max((int(action["seq"]) for action in actions), default=0)
                        last = int(adventure.get("life_tick_seq") or 0)
                        life_clock = ((adventure.get("sim_state") or {}).get("life") or {})
                        if every > 0 and seq - last >= every and not life_clock.get("enabled"):
                            jobs.enqueue_day_tick(conn, adventure_id, reason="auto")
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


def _prepare_action_check(conn: sqlite3.Connection, adventure: dict, actions: list[dict],
                          player_action: dict | None, raw_text: str,
                          user: str) -> tuple[str, str, dict, dict | None]:
    """Estado da checagem, ou None quando este turno não passa por ela."""
    if player_action is None or player_action.get("type") not in ("do", "say"):
        return "", "", {}, None
    try:
        file_cfg = settings_store.load_file_settings().get("action_check") or {}
    except Exception:
        log.warning("action_check: configuração indisponível", exc_info=True)
        return "", "", {}, None
    if not isinstance(file_cfg, dict) or file_cfg.get("enabled") is not True:
        return "", "", {}, None
    text = (raw_text or "").strip()
    try:
        min_words = int(file_cfg.get("min_words", 4))
    except (TypeError, ValueError):
        min_words = 4
    if len(text.split()) < min_words:
        return "", "", {}, None
    try:
        cards = db.list_cards(conn, adventure["id"])
        state = action_check.collect_state(conn, adventure, actions, cards, user)
    except Exception:
        log.warning("action_check: estado indisponível", exc_info=True)
        return "", "", {}, None
    return text, player_action.get("type") or "", file_cfg, state


def _apply_action_check(built, text: str, type_: str, state: dict | None,
                        llm_cfg: dict, transport: Any, cfg: dict):
    """Injeta o bloco no contexto. None se não houve veredito; nunca propaga."""
    if state is None:
        return None
    try:
        verdict = action_check.check_action(
            text, type_, state, llm_cfg=llm_cfg, transport=transport, cfg=cfg,
        )
    except Exception:
        log.warning("action_check: checagem falhou", exc_info=True)
        return None
    if verdict is None or not built.messages:
        return verdict
    built.messages[-1] = {
        "role": built.messages[-1]["role"],
        "content": context_builder.with_reference(
            built.messages[-1]["content"], action_check.verdict_block(verdict)),
    }
    return verdict


def _compliance_flag(verdict, text: str) -> list[str]:
    """O narrador escreveu obediência numa ação que o mundo já recusou."""
    if verdict is None or getattr(verdict, "possible", True):
        return []
    blockers = getattr(verdict, "blockers", None) or []
    if not any("recusa" in (item or "").casefold() for item in blockers):
        return []
    from app import world_facts
    if world_facts.narration_lacks_refusal(text):
        return ["compliance"]
    return []


def _refusal_names(verdict) -> list[str]:
    names: list[str] = []
    for item in getattr(verdict, "blockers", None) or []:
        folded = (item or "").casefold()
        if "recusa" not in folded:
            continue
        head = (item or "").split(" recusa")[0].split(" Recusa")[0].strip(" .")
        if head and head not in names:
            names.append(head)
    return names


def _correct_narration(text: str, *, player_text: str, retry_of, built,
                       llm_cfg: dict, sampler: dict, transport,
                       out_script: str | None,
                       extra_flags: list[str] | None = None) -> tuple[str, list[str]]:
    """Um retry silencioso quando a prosa viola as regras. Empate fica com a segunda."""
    expected = "pt" if output_sanitizer.looks_portuguese(player_text) else ""
    flags = output_sanitizer.quality_flags(text, expected_language=expected)
    for flag in extra_flags or []:
        if flag not in flags:
            flags.append(flag)
    if not flags or retry_of is not None:
        return text, flags
    log.info("narrator quality flags %s — one corrective retry", flags)
    retry_messages = [dict(message) for message in built.messages]
    note = (
        "Your previous attempt broke these rules: "
        + ", ".join(flags)
        + ". Rewrite the same beat, obeying them."
    )
    if "compliance" in flags:
        note += (
            " compliance means the NPC already refused. "
            "Rewrite the beat as that refusal: they take one visible action "
            "and the attempt does not succeed. Do not write them obeying, "
            "undressing, kissing back, or leaving with the player."
        )
    if retry_messages:
        last = retry_messages[-1]
        retry_messages[-1] = {
            "role": last["role"],
            "content": (last.get("content") or "") + "\n" + note,
        }
    retry_sampler = dict(sampler)
    retry_sampler["temperature"] = round(
        min(1.0, float(sampler.get("temperature", 0.85)) + 0.1), 4)
    parts: list[str] = []
    try:
        for token in llm_client.stream_chat_completion(
                llm_cfg, retry_messages, sampler=retry_sampler,
                stop=built.stop_sequences, transport=transport):
            parts.append(token)
    except llm_client.LlmError:
        log.warning("corrective retry failed; keeping the first narration")
        return text, flags
    alt = output_sanitizer.clean_narration("".join(parts), False)
    alt = npc_bonds.strip_leaked(alt)
    if out_script:
        alt = scripting.run_output_modifier(out_script, alt)
    if not alt.strip():
        return text, flags
    alt_flags = output_sanitizer.quality_flags(alt, expected_language=expected)
    if len(alt_flags) <= len(flags):
        return alt, alt_flags
    return text, flags


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
            verdict = None
            check_state = None
            check_cfg: dict = {}
            check_text = ""
            check_type = ""
            resolution = None
            applied_action_id = None
            rules_block = ""
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
                check_text, check_type, check_cfg, check_state = _prepare_action_check(
                    conn, adventure, actions, player_action, raw_query_text, user)

            from app.sim import engine as sim_engine
            if player_action is not None and retry_of is None:
                outcome = sim_engine.run_turn(
                    adventure_id, adventure, player_action, actions, user,
                    llm_cfg, cfg, transport,
                )
                if outcome and outcome.get("refusal"):
                    refusal = outcome["refusal"]
                    with appending(adventure_id) as conn:
                        db.delete_action(conn, player_action["id"])
                    _publish_live(adventure_id, "undo", {})
                    yield {"event": "refused", "data": {
                        "action_id": player_action["id"], "reason": refusal,
                    }}
                    yield {"event": "error", "data": {
                        "message": refusal, "code": "rules_refused",
                    }}
                    return
                if outcome and outcome.get("resolution"):
                    resolution = outcome["resolution"]
                    applied_action_id = player_action["id"]
                    rules_block = resolution.get("block") or ""
            elif retry_of is not None:
                rules_block = sim_engine.reuse_block(actions, retry_of) or ""

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

            if rules_block and built.messages:
                built.messages[-1] = {
                    "role": built.messages[-1]["role"],
                    "content": context_builder.with_reference(
                        built.messages[-1]["content"], rules_block),
                }
            if resolution is not None and player_action is not None:
                rolled = {**resolution, "action_id": player_action["id"]}
                yield {"event": "rules_roll", "data": rolled}
                _publish_live(adventure_id, "rules_roll", rolled)

            verdict = _apply_action_check(
                built, check_text, check_type, check_state, llm_cfg, transport, check_cfg)
            if verdict is not None:
                payload = verdict.to_dict()
                yield {"event": "action_check", "data": payload}
                # No barramento também: os outros jogadores veem a checagem
                # da ação (com o id da ação para amarrar no balão).
                live = dict(payload)
                if player_action:
                    live["action_id"] = player_action.get("id")
                    live["seq"] = player_action.get("seq")
                _publish_live(adventure_id, "action_check", live)

            stream_state = llm_client.StreamState()
            for token in llm_client.stream_chat_completion(
                    llm_cfg, built.messages, sampler=sampler,
                    stop=built.stop_sequences, transport=transport,
                    state=stream_state):
                full.append(token)
                _publish_live(adventure_id, "token", {"token": token})
                yield {"event": "token", "data": {"token": token}}

            text = output_sanitizer.clean_narration("".join(full), stream_state.truncated)
            text = npc_bonds.strip_leaked(text)
            # Scripting hook: output modifier (after generation completion)
            out_script = scripting.get_adventure_script(adventure_id, "output_modifier")
            if out_script:
                text = scripting.run_output_modifier(out_script, text)
            player_text = (player_action or {}).get("text") or raw_query_text or ""
            compliance = _compliance_flag(verdict, text)
            text, quality = _correct_narration(
                text, player_text=player_text, retry_of=retry_of, built=built,
                llm_cfg=llm_cfg, sampler=sampler, transport=transport,
                out_script=out_script,
                extra_flags=compliance,
            )
            if "compliance" in quality:
                from app import world_facts
                text = world_facts.refusal_prose(_refusal_names(verdict))
                quality = [flag for flag in quality if flag != "compliance"]
            if not text.strip():
                # Só notas de contexto (cortadas pela stop ou pelo sanitizer) ou
                # resposta vazia: um continue vazio quebraria choices e chronicle.
                _revert_applied_rules(adventure_id, applied_action_id)
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
                if quality:
                    db.merge_action_rules(conn, action["id"], {"quality_flags": quality})
                    action["quality_flags"] = quality
                    if player_action is not None:
                        db.merge_action_rules(
                            conn, player_action["id"], {"quality_flags": quality})
                        player_action["quality_flags"] = quality
                _publish_live(adventure_id, "action", action)
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
            if verdict is not None:
                data["action_check"] = verdict.to_dict()
            if resolution is not None:
                data["rules"] = resolution

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
            _revert_applied_rules(adventure_id, applied_action_id)
            yield {"event": "error", "data": {"message": exc.message}}
        except Exception as exc:  # defesa — o stream sempre termina com erro legível
            _revert_applied_rules(adventure_id, applied_action_id)
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
                                          _format_player_text(type_, text),
                                          actor=user)
            _retry_counts.pop((adventure_id, player_action["seq"]), None)
        _publish_live(adventure_id, "player_action", player_action)
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
                from app.sim import engine as sim_engine
                sim_engine.revert(conn, adventure_id, prev["id"])
                db.delete_action(conn, prev["id"])
                removed.append(prev)
        else:
            # ação do jogador órfã (geração falhou) ou 'see': remove só ela
            if last["type"] in db.PLAYER_TYPES:
                from app.sim import engine as sim_engine
                sim_engine.revert(conn, adventure_id, last["id"])
            db.delete_action(conn, last["id"])
            removed.append(last)

        removed.sort(key=lambda a: a["seq"])
        db.touch_adventure(conn, adventure_id)
        remaining = db.list_actions(conn, adventure_id)
        choices_service.cancel(adventure_id)
        result = {
            "removed": removed,
            "last_action": remaining[-1] if remaining else None,
            "action_count": len(remaining),
        }
    _publish_live(adventure_id, "undo", {})
    return result


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
