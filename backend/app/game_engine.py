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
- Pós-turno (G3), depois do `done` e com o lock já solto, só em turno novo
  (retry não dispara): choices pré-geradas (C7, slot 1) em paralelo com a
  série chronicle → consequências → XP → mente → tick e, depois,
  `memory.summarizer.maybe_update()` (slot 2), best-effort. Falha do chronicle
  não cancela o resto. A mente não regrava os vínculos. Ação nova cancela
  choices e summary pendentes, nunca o chronicle.

Eventos emitidos pelo gerador (a rota serializa em SSE):
    {"event": "rules_roll", "data": {resolution}}
    {"event": "action_check", "data": {"possible": bool, "blockers": [], "risks": [], "notes": "", "cost": "", "consequence": ""}}
    {"event": "adjudication", "data": {"decision", "outcome", "reactions", "initiative"}}
    {"event": "token", "data": {"token": "..."}}
    {"event": "done",  "data": {"player_action"?: Action, "action": Action, "action_check"?: object, "adjudication"?: object}}
    {"event": "error", "data": {"message": "..."}}
`adjudication` sai antes dos tokens e não traz `relation_changes`. Esses só
entram no `done`, já filtrados para fatos observáveis. Retry e story não
emitem nem reaplicam. Antes do `done`, `turn_done {action_id, seq}` sai em
`events` (C2).
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
from contextlib import contextmanager
from typing import Any, Iterator

from app import (
    action_check,
    canon,
    choices_service,
    context_builder,
    db,
    events as events_bus,
    llm_client,
    meta_commands,
    npc_bonds,
    npc_chronicle,
    npc_life,
    output_sanitizer,
    scripting,
    settings_store,
    web_search as web_search_mod,
    world_facts,
)
from app.errors import GameError

log = logging.getLogger("aidungeon.game")


def _preempt_automatic_clips(adventure_id: str, transport: Any) -> None:
    """Mídia de fundo nunca pode bloquear a próxima ação do jogador."""
    try:
        from app import jobs
        jobs.preempt_automatic_clips(adventure_id, transport)
    except Exception:
        log.exception("não foi possível preemptar clipes automáticos de %s", adventure_id)

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


def is_generation_active(adventure_id: str) -> bool:
    return _lock_for(adventure_id).locked()


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
def _evaluate_consequences(adventure_id: str, transport: Any = None) -> None:
    """Registra magia, percepção e fatos sociais do último trecho canônico."""
    try:
        from app import consequences
        with db.session() as conn:
            adventure = db.get_adventure(conn, adventure_id)
            if not adventure:
                return
            actions = db.list_actions(conn, adventure_id)
            cards = db.list_cards(conn, adventure_id)
            llm_cfg = settings_store.effective_llm_settings(conn)
        passage = "\n".join(
            (action.get("text") or "") for action in actions[-3:]
            if action.get("type") in world_facts.CANON_TYPES
        )
        if not passage.strip():
            return
        evaluation = consequences.evaluate_passage(
            llm_cfg, passage, cards, adventure, transport=transport,
        )
        with db.session() as conn:
            consequences.apply_evaluation(conn, adventure_id, evaluation)
    except Exception:
        log.warning("consequences: avaliação falhou", exc_info=True)

def _apply_progression(adventure_id: str, user: str) -> None:
    """XP por turno canônico do jogador."""
    try:
        from app import progression
        with db.session() as conn:
            player = db.player_character(conn, adventure_id, user)
            if player:
                progression.apply_turn_progression(conn, adventure_id, player["id"], 5)
    except Exception:
        log.warning("progression: falha ao aplicar XP", exc_info=True)


def _guard_post(label: str, fn) -> None:
    """Um passo do pós-turno. Falha fica no log e não corta o seguinte."""
    try:
        fn()
    except Exception:
        log.warning("post-turn %s falhou", label, exc_info=True)



def _maybe_auto_tick(adventure_id: str) -> None:
    from app import jobs
    life = settings_store.load_file_settings().get("life") or {}
    if not life.get("auto_tick", True):
        return
    every = int(life.get("tick_every_turns", 5) or 5)
    with db.session() as conn:
        adventure = db.get_adventure(conn, adventure_id) or {}
        actions = db.list_actions(conn, adventure_id)
        seq = max((int(action["seq"]) for action in actions), default=0)
        last = int(adventure.get("life_tick_seq") or 0)
        life_clock = ((adventure.get("sim_state") or {}).get("life") or {})
        if every > 0 and seq - last >= every and not life_clock.get("enabled"):
            jobs.enqueue_day_tick(conn, adventure_id, reason="auto")


def _spawn_post_turn(adventure_id: str, transport: Any = None, user: str = "") -> None:
    """Fora dos testes, roda em background, depois do done e com o lock da
    aventura já solto: choices pré-geradas, o vetor da cena das memórias e,
    em série, chronicle, consequências, XP, mente e tick, depois o summarizer.
    Retry não chama esta função."""
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
            _guard_post("chronicle", lambda: npc_chronicle.update_from_turn(adventure_id, transport))
            _guard_post("consequences", lambda: _evaluate_consequences(adventure_id, transport))
            _guard_post("progression", lambda: _apply_progression(adventure_id, user))
            _guard_post("mind", lambda: npc_life.run_mind(adventure_id, transport))
            _guard_post("tick", lambda: _maybe_auto_tick(adventure_id))
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


def _prepare_action_check(player_action: dict | None, raw_text: str,
                          ) -> tuple[str, str, dict]:
    """Texto, tipo e config quando este turno passa pela checagem.

    O retrato do mundo é lido depois, com o sim já resolvido.
    """
    if player_action is None or player_action.get("type") not in ("do", "say"):
        return "", "", {}
    try:
        file_cfg = settings_store.load_file_settings().get("action_check") or {}
    except Exception:
        log.warning("action_check: configuração indisponível", exc_info=True)
        return "", "", {}
    if not isinstance(file_cfg, dict) or file_cfg.get("enabled") is not True:
        return "", "", {}
    text = (raw_text or "").strip()
    try:
        min_words = int(file_cfg.get("min_words", 4))
    except (TypeError, ValueError):
        min_words = 4
    if len(text.split()) < min_words:
        return "", "", {}
    return text, player_action.get("type") or "", file_cfg


def _public_sim(adventure: dict, resolution: dict | None) -> dict | None:
    """Recorte do sim já gravado. Sem RNG e sem bloco interno do narrador."""
    from app.sim import state as sim_state
    loaded = sim_state.load(adventure if isinstance(adventure, dict) else {})
    actors_raw = loaded.get("actors") if isinstance(loaded.get("actors"), dict) else {}
    foes_raw = loaded.get("foes") if isinstance(loaded.get("foes"), dict) else {}
    if resolution is None and not loaded.get("pack") and not actors_raw and not foes_raw:
        return None

    def row(item: dict) -> dict:
        return {
            "name": item.get("name") or "",
            "hp": item.get("hp"),
            "hp_max": item.get("hp_max"),
            "dead": bool(item.get("dead")),
        }

    view: dict[str, Any] = {
        "pack": loaded.get("pack") or "",
        "actors": [row(item) for item in actors_raw.values() if isinstance(item, dict)],
        "foes": [row(item) for item in foes_raw.values() if isinstance(item, dict)],
    }
    if isinstance(resolution, dict):
        changes = []
        for change in resolution.get("changes") or []:
            if not isinstance(change, dict):
                continue
            changes.append({
                "who": change.get("who") or "",
                "what": change.get("what") or "",
                "from": change.get("from"),
                "to": change.get("to"),
            })
        view["changes"] = changes
    return view


def _action_check_state(adventure_id: str, user: str,
                        resolution: dict | None) -> dict | None:
    """Mundo depois que o sim commitou status, PV e fatos do turno."""
    try:
        with db.session() as conn:
            adventure = db.get_adventure(conn, adventure_id)
            if not adventure:
                return None
            actions = db.list_actions(conn, adventure_id)
            cards = db.list_cards(conn, adventure_id)
            state = action_check.collect_state(conn, adventure, actions, cards, user)
        sim_view = _public_sim(adventure, resolution)
        if sim_view is not None:
            state["sim"] = sim_view
            # O sim é autoridade para PV/morte; a ficha ainda pode dizer "alive".
            foes = sim_view.get("foes") or []
            for row in (sim_view.get("actors") or []) + foes:
                name = str(row.get("name") or "").strip().casefold()
                if not name:
                    continue
                status = "dead" if row.get("dead") else "alive"
                hp = row.get("hp")
                targets = list(state.get("present_npcs") or [])
                player = state.get("player")
                if isinstance(player, dict):
                    targets.append(player)
                matched = False
                for target in targets:
                    if str(target.get("name") or "").strip().casefold() != name:
                        continue
                    matched = True
                    target["status"] = status
                    if hp is not None:
                        target["hp"] = hp
                        stats = target.get("stats")
                        if isinstance(stats, dict):
                            stats["hp"] = hp
                if not matched and row in foes:
                    state.setdefault("present_npcs", []).append({
                        "name": row.get("name") or "", "status": status, "hp": hp,
                    })
                state["absent"] = [
                    item for item in state.get("absent") or []
                    if str(item).strip().casefold() != name
                ]
        return state
    except Exception:
        log.warning("action_check: estado indisponível", exc_info=True)
        return None


def _apply_action_check(built, text: str, type_: str, state: dict | None,
                        llm_cfg: dict, transport: Any, cfg: dict, cancel=None):
    """Injeta o bloco no contexto. None se não houve veredito; nunca propaga."""
    if state is None:
        return None
    try:
        verdict = action_check.check_action(
            text, type_, state, llm_cfg=llm_cfg, transport=transport, cfg=cfg,
            cancel=cancel,
        )
    except llm_client.LlmCancelled:
        raise
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


def _compliance_flag(text: str, adjudication: dict | None) -> list[str]:
    """O narrador obedeceu a uma decisão refuse/partial."""
    decision = str((adjudication or {}).get("decision") or "").strip().lower()
    if decision not in ("refuse", "partial"):
        return []
    from app import world_facts
    if world_facts.narration_lacks_refusal(text):
        return ["compliance"]
    return []


def _living_roster(adventure_id: str) -> list[dict]:
    """NPCs vivos para o cue. Morto, missing e o jogador ficam de fora."""
    from app import npc_assets
    roster = []
    with db.session() as conn:
        for card in db.list_cards(conn, adventure_id):
            if (card.get("type") or "") != "character" or card.get("player_username"):
                continue
            sheet = npc_assets.parse_sheet(card.get("sheet") or "")
            if not npc_bonds._living(sheet):
                continue
            name = (card.get("title") or "").strip()
            if not name:
                continue
            roster.append({"name": name, "card_id": card.get("id")})
    return roster


def _visual_map(conn: sqlite3.Connection, adventure_id: str) -> dict[str, str]:
    from app import npc_assets
    snap: dict[str, str] = {}
    for card in db.list_cards(conn, adventure_id):
        sheet = npc_assets.parse_sheet(card.get("sheet") or "")
        visual = sheet.get("visual_state")
        snap[str(card["id"])] = (
            json.dumps(visual, sort_keys=True, ensure_ascii=False)
            if isinstance(visual, dict) else ""
        )
    return snap


def _sync_visual_changes(conn: sqlite3.Connection, adventure_id: str,
                         before: dict[str, str]) -> None:
    """Enfileira retrato só de quem mudou `visual_state`. O job segue assíncrono."""
    from app import portrait_sync
    after = _visual_map(conn, adventure_id)
    for card_id, visual in after.items():
        if before.get(card_id) == visual:
            continue
        try:
            portrait_sync.sync_card(conn, adventure_id, card_id)
        except Exception:
            log.warning("portrait_sync: card %s não sincronizado", card_id, exc_info=True)


def _public_reaction(raw: dict) -> dict:
    item = {
        "npc": raw.get("npc") or "",
        "card_id": raw.get("card_id") or "",
        "reaction": raw.get("reaction") or "",
        "line": raw.get("line") or "",
    }
    for key in ("emotion", "pose"):
        value = raw.get(key)
        if isinstance(value, str) and value.strip():
            item[key] = value.strip()
    return item


def _public_relation_changes(changes) -> list[dict]:
    """Só o fato observável. Motivação oculta não atravessa reason/evidence."""
    public = []
    for raw in changes or []:
        if not isinstance(raw, dict):
            continue
        delta_raw = raw.get("delta") if isinstance(raw.get("delta"), dict) else {}
        delta = {}
        for key, value in delta_raw.items():
            if isinstance(value, bool) or not isinstance(value, int):
                continue
            delta[str(key)] = value
        public.append({
            "card_id": str(raw.get("card_id") or ""),
            "name": str(raw.get("name") or ""),
            "target": str(raw.get("target") or ""),
            "delta": delta,
            "reason": str(raw.get("reason") or ""),
            "evidence": str(raw.get("evidence") or ""),
        })
    return public


def _public_adjudication(adjudication: dict | None,
                         relation_changes=None, *,
                         include_changes: bool = False) -> dict | None:
    """SSE: decision/outcome/reactions/initiative. `done` soma relation_changes."""
    if not isinstance(adjudication, dict):
        return None
    reactions = []
    for raw in adjudication.get("reactions") or []:
        if isinstance(raw, dict):
            reactions.append(_public_reaction(raw))
    initiative = None
    raw_beat = adjudication.get("initiative")
    if isinstance(raw_beat, dict):
        initiative = {
            "npc": raw_beat.get("npc") or "",
            "card_id": raw_beat.get("card_id") or "",
            "line": raw_beat.get("line") or "",
        }
        if not initiative["npc"] and not initiative["line"]:
            initiative = None
    public = {
        "decision": str(adjudication.get("decision") or "allow"),
        "outcome": str(adjudication.get("outcome") or ""),
        "reactions": reactions,
        "initiative": initiative,
    }
    if include_changes:
        public["relation_changes"] = _public_relation_changes(relation_changes)
    return public


def _actor_name(conn: sqlite3.Connection, adventure_id: str, user: str) -> str:
    if not user:
        return ""
    card = db.player_character(conn, adventure_id, user)
    title = ((card or {}).get("title") or "").strip()
    return title or user


def _correct_narration(text: str, *, player_text: str, retry_of, built,
                       llm_cfg: dict, sampler: dict, transport,
                       out_script: str | None,
                       extra_flags: list[str] | None = None,
                       adjudication: dict | None = None,
                       adventure_id: str = "",
                       cancel=None) -> tuple[str, list[str]]:
    """Um retry silencioso quando a prosa viola as regras. Empate fica com a segunda."""
    expected = "pt" if output_sanitizer.looks_portuguese(player_text) else ""
    flags = output_sanitizer.quality_flags(text, expected_language=expected)
    for flag in extra_flags or []:
        if flag not in flags:
            flags.append(flag)
    if adventure_id:
        from app import death_rules
        with db.session() as conn:
            cards = db.list_cards(conn, adventure_id)
        if death_rules.dead_acting_alive(cards, text) and "dead_acting_alive" not in flags:
            flags.append("dead_acting_alive")
    if not flags or retry_of is not None:
        return text, flags
    log.info("narrator quality flags %s — one corrective retry", flags)
    retry_messages = [dict(message) for message in built.messages]
    note = (
        "Your previous attempt broke these rules: "
        + ", ".join(flags)
        + ". Rewrite the same beat, obeying them."
    )
    if adjudication:
        decided = (adjudication.get("outcome") or adjudication.get("cue") or "").strip()
        if decided:
            note += " O GM já decidiu o fato: " + decided[:500]
    if "compliance" in flags:
        note += (
            " The action was refused: do not write the NPC obeying, "
            "undressing, kissing back, or leaving with the player. "
            "Show their own refusal/reaction instead."
        )
    if "dead_acting_alive" in flags:
        note += (
            " A dead character was shown alive. Dead people cannot speak, act, "
            "move, smile, adjust objects or keep working. Describe only the corpse "
            "or the living people's reaction. Do not invent resurrection."
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
                stop=built.stop_sequences, transport=transport, cancel=cancel):
            parts.append(token)
    except llm_client.LlmCancelled:
        raise
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
                       user: str = "",
                       cancel: threading.Event | None = None,
                       generation_id: str = "") -> Iterator[dict]:
    """Gerador de eventos. Contexto = ações atuais (retry: sem a antiga)."""

    def events() -> Iterator[dict]:
        full: list[str] = []
        lock_released = False
        web_sources: list[dict] = []
        new_turn: str | None = None
        try:
            verdict = None
            adjudication = None
            relation_changes: list[dict] = []
            check_state = None
            check_cfg: dict = {}
            check_text = ""
            check_type = ""
            resolution = None
            applied_action_id = None
            rules_block = ""
            if cancel is not None and cancel.is_set():
                raise llm_client.LlmCancelled("llm call cancelled")
            with db.session() as conn:
                adventure = db.get_adventure(conn, adventure_id)
                actions = db.list_actions(conn, adventure_id)
                if retry_of is not None:
                    actions = [a for a in actions if a["id"] != retry_of["id"]]
                cfg = settings_store.runtime_config(conn)
                built = context_builder.build_context(adventure, actions, cfg, conn)
                llm_cfg = cfg["llm"]
                sampler = dict(cfg["sampler"])
                preferences = (adventure.get("ui_state") or {}).get("_generation") or {}
                if isinstance(preferences, dict):
                    try:
                        if "max_tokens" in preferences:
                            sampler["max_tokens"] = max(100, min(2048, int(preferences["max_tokens"])))
                        if "temperature" in preferences:
                            sampler["temperature"] = max(0.0, min(2.0, float(preferences["temperature"])))
                    except (TypeError, ValueError):
                        log.warning("preferências de geração inválidas; usando defaults")
                if retry_of is not None and retry_count > 0:
                    base_t = float(sampler.get("temperature", 0.85))
                    step = float(sampler.get("retry_temperature_step", 0.05))
                    max_t = float(sampler.get("max_retry_temperature", 1.15))
                    sampler["temperature"] = round(min(base_t + step * retry_count, max_t), 4)
                check_text, check_type, check_cfg = _prepare_action_check(
                    player_action, raw_query_text)

            from app.sim import engine as sim_engine
            if player_action is not None and retry_of is None:
                outcome = sim_engine.run_turn(
                    adventure_id, adventure, player_action, actions, user,
                    llm_cfg, cfg, transport, cancel=cancel,
                )
                if outcome and outcome.get("refusal"):
                    refusal = outcome["refusal"]
                    with appending(adventure_id) as conn:
                        canon.restore(conn, adventure_id, [player_action["id"]])
                        db.delete_action(conn, player_action["id"])
                        metadata = {
                            "actor": user, "entity_id": str(player_action["id"]),
                            "turn_seq": player_action["seq"],
                            "generation_id": generation_id,
                        }
                        events_bus.record(conn, adventure_id, "undo", {}, **metadata)
                        events_bus.record(
                            conn, adventure_id, "generation_failed",
                            {"message": refusal, "code": "rules_refused"}, **metadata,
                        )
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

            if cancel is not None and cancel.is_set():
                raise llm_client.LlmCancelled("llm call cancelled")
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

            if check_text:
                check_state = _action_check_state(adventure_id, user, resolution)
            verdict = _apply_action_check(
                built, check_text, check_type, check_state, llm_cfg, transport, check_cfg,
                cancel=cancel)
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

            social_turn = retry_of is None and (
                player_action is None or player_action.get("type") in ("do", "say")
            )
            if social_turn:
                from app import npc_gate
                roster = _living_roster(adventure_id)
                try:
                    if player_action is None:
                        adjudication = npc_gate.own_beat(
                            adventure_id, llm_cfg, transport, cancel=cancel)
                    else:
                        adjudication = npc_gate.decide(
                            adventure_id,
                            (player_action.get("text") or raw_query_text or ""),
                            llm_cfg,
                            transport,
                            user,
                            world_check=verdict.to_dict() if verdict else None,
                            cancel=cancel,
                        )
                except llm_client.LlmCancelled:
                    raise
                except Exception:
                    log.warning("npc_gate: adjudicador indisponível", exc_info=True)
                    adjudication = None
                cue = npc_gate.cue_block(adjudication, roster)
                if cue and built.messages:
                    last = built.messages[-1]
                    built.messages[-1] = {
                        "role": last["role"],
                        "content": (last.get("content") or "") + "\n" + cue,
                    }
                visible = _public_adjudication(adjudication)
                if visible is not None:
                    yield {"event": "adjudication", "data": visible}
                    _publish_live(adventure_id, "adjudication", visible)

            stream_state = llm_client.StreamState()
            for token in llm_client.stream_chat_completion(
                    llm_cfg, built.messages, sampler=sampler,
                    stop=built.stop_sequences, transport=transport,
                    state=stream_state, cancel=cancel):
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
            compliance = _compliance_flag(text, adjudication)
            text, quality = _correct_narration(
                text, player_text=player_text, retry_of=retry_of, built=built,
                llm_cfg=llm_cfg, sampler=sampler, transport=transport,
                out_script=out_script,
                extra_flags=compliance,
                adjudication=adjudication,
                adventure_id=adventure_id,
                cancel=cancel,
            )
            beat = (adjudication or {}).get("initiative") or {}
            if beat.get("line") and beat.get("npc") and beat["npc"].casefold() not in text.casefold():
                text = (text.rstrip() + "\n\n" + beat["line"]).strip()
            if not text.strip():
                # Só notas de contexto (cortadas pela stop ou pelo sanitizer) ou
                # resposta vazia: um continue vazio quebraria choices e chronicle.
                _revert_applied_rules(adventure_id, applied_action_id)
                if player_action is None:
                    _discard_pending_canon(adventure_id, generation_id)
                source = player_action or retry_of or {}
                payload = {
                    "message": "the narrator returned no story text; nothing was saved, try again",
                }
                events_bus.publish(
                    adventure_id, "generation_failed", payload,
                    actor=user, entity_id=str(source.get("id") or ""),
                    turn_seq=source.get("seq"), generation_id=generation_id,
                )
                yield {"event": "error", "data": payload}
                return

            if cancel is not None and cancel.is_set():
                raise llm_client.LlmCancelled("llm call cancelled")
            with appending(adventure_id) as conn:
                if retry_of is not None:
                    # substitui: mesmo seq, novo id (apaga a antiga só no sucesso)
                    db.delete_action(conn, retry_of["id"])
                    action = db.insert_action(conn, adventure_id, retry_of["seq"],
                                              "continue", text)
                    canon.reanchor(conn, adventure_id, retry_of["id"], action["id"])
                    db.touch_adventure(conn, adventure_id)
                else:
                    action = append_action(conn, adventure_id, "continue", text)
                    canon.attach(conn, adventure_id, generation_id, action["id"])
                if quality:
                    db.merge_action_rules(conn, action["id"], {"quality_flags": quality})
                    action["quality_flags"] = quality
                    if player_action is not None:
                        db.merge_action_rules(
                            conn, player_action["id"], {"quality_flags": quality})
                        player_action["quality_flags"] = quality
                # O evento público sai depois que a action já contém a reação.
                # Relação e visual no mesmo commit da prosa. Falha não apaga
                # a narração. Story e retry não entram: um turno aplica uma vez.
                if social_turn:
                    try:
                        before_visual = _visual_map(conn, adventure_id)
                        social_text = raw_query_text if player_action is not None else ""
                        raw_changes = npc_bonds.apply_adjudication(
                            conn,
                            adventure_id,
                            adjudication or {},
                            int(action["seq"]),
                            player_text=social_text,
                            actor_name=_actor_name(conn, adventure_id, user),
                        )
                        relation_changes = raw_changes if isinstance(raw_changes, list) else []
                        _sync_visual_changes(conn, adventure_id, before_visual)
                    except Exception:
                        log.warning("npc_bonds: adjudicação não aplicada", exc_info=True)
                        relation_changes = []
                    try:
                        from app import npc_gate
                        npc_gate.remember(conn, adventure_id, adjudication)
                    except Exception:
                        log.warning("npc_gate: histórico não gravado", exc_info=True)
                persisted = None
                if retry_of is not None:
                    persisted = retry_of.get("adjudication")
                elif isinstance(adjudication, dict) or relation_changes:
                    persisted = _public_adjudication(
                        adjudication if isinstance(adjudication, dict) else {
                            "decision": "allow", "outcome": "", "reactions": [],
                            "initiative": None,
                        }, relation_changes, include_changes=True)
                if persisted:
                    db.merge_action_rules(conn, action["id"], {"adjudication": persisted})
                    action["adjudication"] = persisted
                events_bus.record(
                    conn, adventure_id, "action", action,
                    actor="assistant", entity_id=str(action["id"]),
                    turn_seq=action["seq"], generation_id=generation_id,
                )
                new_turn = _advance_committed_turn(conn, adventure_id, player_action, user)
                if new_turn is not None:
                    events_bus.record(
                        conn, adventure_id, "turn_changed", {"turn_user": new_turn},
                        entity_id=str(action["id"]), turn_seq=action["seq"],
                        generation_id=generation_id,
                    )
                events_bus.record(
                    conn, adventure_id, "turn_done",
                    {"action_id": action["id"], "seq": action["seq"]},
                    actor="assistant", entity_id=str(action["id"]),
                    turn_seq=action["seq"], generation_id=generation_id,
                )

            data: dict[str, Any] = {"action": action}
            if player_action is not None:
                data["player_action"] = player_action
            if web_sources:
                data["web_sources"] = web_sources
            if verdict is not None:
                data["action_check"] = verdict.to_dict()
            if resolution is not None:
                data["rules"] = resolution
            if action.get("adjudication"):
                data["adjudication"] = action["adjudication"]

            # Libera o lock antes de emitir 'done' para que chamadas imediatas do
            # cliente (ex.: /choices no onDone) nunca tomem 409 por corrida de thread.
            release_generation_lock(adventure_id)
            lock_released = True
            if retry_of is None:
                _spawn_post_turn(adventure_id, transport, user)
            yield {"event": "done", "data": data}
        except llm_client.LlmCancelled as exc:
            _revert_applied_rules(adventure_id, applied_action_id)
            source = player_action or retry_of or {}
            metadata = {
                "actor": user, "entity_id": str(source.get("id") or ""),
                "turn_seq": source.get("seq"), "generation_id": generation_id,
            }
            payload = {"message": exc.message, "code": "llm_cancelled"}
            if player_action is not None and retry_of is None:
                with appending(adventure_id) as conn:
                    canon.restore(conn, adventure_id, [player_action["id"]])
                    db.delete_action(conn, player_action["id"])
                    events_bus.record(conn, adventure_id, "undo", {}, **metadata)
                    events_bus.record(
                        conn, adventure_id, "generation_cancelled", payload, **metadata)
            else:
                _discard_pending_canon(adventure_id, generation_id)
                events_bus.publish(adventure_id, "generation_cancelled", payload, **metadata)
            yield {"event": "error", "data": payload}
        except llm_client.LlmError as exc:
            _revert_applied_rules(adventure_id, applied_action_id)
            _discard_pending_canon(adventure_id, generation_id)
            source = player_action or retry_of or {}
            payload = {"message": exc.message, "code": exc.code}
            events_bus.publish(
                adventure_id, "generation_failed", payload,
                actor=user, entity_id=str(source.get("id") or ""),
                turn_seq=source.get("seq"), generation_id=generation_id,
            )
            yield {"event": "error", "data": payload}
        except Exception as exc:  # defesa — o stream sempre termina com erro legível
            _revert_applied_rules(adventure_id, applied_action_id)
            _discard_pending_canon(adventure_id, generation_id)
            source = player_action or retry_of or {}
            payload = {"message": f"{type(exc).__name__}: {exc}",
                       "code": "generation_failed"}
            events_bus.publish(
                adventure_id, "generation_failed", payload,
                actor=user, entity_id=str(source.get("id") or ""),
                turn_seq=source.get("seq"), generation_id=generation_id,
            )
            yield {"event": "error", "data": {"message": payload["message"]}}
        finally:
            if not lock_released:
                release_generation_lock(adventure_id)

    return events()


def handle_action(adventure_id: str, type_: str, text: str,
                  transport: Any = None, *,
                  web_search: bool = False,
                  user: str = "",
                  cancel: threading.Event | None = None) -> Iterator[dict]:
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
    generation_id = db.new_id()
    try:
        _preempt_automatic_clips(adventure_id, transport)
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
            events_bus.record(
                conn, adventure_id, "player_action", player_action,
                actor=user, entity_id=str(player_action["id"]),
                turn_seq=player_action["seq"], generation_id=generation_id,
            )
            canon.capture(conn, adventure_id, generation_id, player_action["id"])
    except Exception:
        release_generation_lock(adventure_id)
        raise
    return _stream_generation(adventure_id, player_action=player_action,
                              retry_of=None, retry_count=0,
                              web_search=web_search, raw_query_text=text,
                              transport=transport, user=user, cancel=cancel,
                              generation_id=generation_id)


def _meta_command_events(adventure_id: str, cmd: meta_commands.MetaCommand,
                         transport: Any) -> Iterator[dict]:
    """Meta-comando no lugar do narrador: sem lock de geração, nada vira prosa."""
    try:
        yield from meta_commands.execute(adventure_id, cmd, transport)
    except llm_client.LlmError as exc:
        yield {"event": "error", "data": {"message": exc.message}}
    except Exception as exc:  # defesa — o stream sempre termina com erro legível
        yield {"event": "error", "data": {"message": f"{type(exc).__name__}: {exc}"}}


def handle_continue(adventure_id: str, transport: Any = None, *,
                    cancel: threading.Event | None = None) -> Iterator[dict]:
    """POST /continue: a IA segue sozinha (sem nova entrada do jogador)."""
    _adventure_or_404(adventure_id)
    if not acquire_generation_lock(adventure_id):
        raise GameError.conflict("generation_in_progress",
                                 "a generation is already running for this adventure")
    _preempt_automatic_clips(adventure_id, transport)
    cancel_post_turn(adventure_id)
    generation_id = db.new_id()
    with appending(adventure_id) as conn:
        canon.capture(conn, adventure_id, generation_id)
    return _stream_generation(adventure_id, player_action=None,
                              retry_of=None, retry_count=0, transport=transport,
                              cancel=cancel, generation_id=generation_id)


def handle_retry(adventure_id: str, transport: Any = None, *,
                 retry_count: int | None = None,
                 cancel: threading.Event | None = None) -> Iterator[dict]:
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
    _preempt_automatic_clips(adventure_id, transport)
    cancel_post_turn(adventure_id)

    with appending(adventure_id) as conn:
        _restore_body_from_action(conn, adventure_id, old)
    key = (adventure_id, int(old["seq"]))
    if retry_count is None:
        _retry_counts[key] = _retry_counts.get(key, 0) + 1
        effective_count = _retry_counts[key]
    else:
        effective_count = retry_count

    return _stream_generation(adventure_id, player_action=None,
                              retry_of=old, retry_count=effective_count,
                              transport=transport, cancel=cancel,
                              generation_id=db.new_id())


def _discard_pending_canon(adventure_id: str, generation_id: str) -> None:
    """Continue que não chegou a gravar não deixa foto órfã."""
    if not generation_id:
        return
    try:
        with db.session() as conn:
            canon.discard_unanchored(conn, adventure_id, generation_id)
    except Exception:
        log.warning("canon: foto sem action não descartada", exc_info=True)


def _restore_body_from_action(conn, adventure_id: str, action: dict | None) -> int:
    """Restaura corpo/expressão/retrato gravados antes do turno removido."""
    if not action:
        return 0
    rules = db.get_action_rules(conn, action.get("id")) or {}
    from app import body_state, npc_assets, portrait_sync
    restored = 0
    snapshots = rules.get("body_before")
    if isinstance(snapshots, dict):
        for card_id, snap in snapshots.items():
            card = db.get_card(conn, adventure_id, card_id)
            if not card or not isinstance(snap, dict):
                continue
            sheet = npc_assets.parse_sheet(card.get("sheet") or "")
            updated = body_state.restore_sheet(sheet, snap)
            db.update_card(conn, adventure_id, card_id, {
                "sheet": json.dumps(updated, ensure_ascii=False),
            })
            try:
                portrait_sync.sync_card(conn, adventure_id, card_id)
            except Exception:
                log.warning("undo: retrato não ressincronizado", exc_info=True)
            restored += 1
    ui_before = rules.get("ui_before")
    if isinstance(ui_before, dict):
        db.update_adventure_ui_state(conn, adventure_id, ui_before)
        restored += 1
    return restored


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
            removed.append(last)
            # Um See pedido no meio do turno grava entre a ação e a resposta;
            # ele não é parte do turno e fica.
            prev = next((a for a in reversed(actions[:-1]) if a["type"] != "see"), None)
            if prev and prev["type"] in db.PLAYER_TYPES:
                removed.append(prev)
        else:
            removed.append(last)
        restored = canon.restore(conn, adventure_id, [action["id"] for action in removed])
        if not restored:
            if last["type"] == "continue":
                _restore_body_from_action(conn, adventure_id, last)
                prev = next((action for action in removed if action["id"] != last["id"]), None)
                if prev is not None:
                    from app.sim import engine as sim_engine
                    sim_engine.revert(conn, adventure_id, prev["id"])
            elif last["type"] in db.PLAYER_TYPES:
                from app.sim import engine as sim_engine
                sim_engine.revert(conn, adventure_id, last["id"])
                _restore_body_from_action(conn, adventure_id, last)
        for action in removed:
            db.delete_action(conn, action["id"])

        removed.sort(key=lambda a: a["seq"])
        db.touch_adventure(conn, adventure_id)
        remaining = db.list_actions(conn, adventure_id)
        choices_service.cancel(adventure_id)
        result = {
            "removed": removed,
            "last_action": remaining[-1] if remaining else None,
            "action_count": len(remaining),
        }
        events_bus.record(
            conn, adventure_id, "undo", {},
            entity_id=str(last["id"]), turn_seq=last["seq"],
        )
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
        events_bus.record(
            conn, action["adventure_id"], "action_edited", edited,
            entity_id=str(action_id), turn_seq=action["seq"],
        )
    # O cache das choices é preso a (id, seq): texto editado não muda a chave.
    choices_service.forget(action["adventure_id"])
    return edited
