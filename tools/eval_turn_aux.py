#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
eval_turn_aux.py -- Compara o 35B com o Qwen2.5-3B auxiliar, sem gravar aventura.

Roda as mesmas 24 ações (8 intenção, 8 checagem, 8 gate) nos dois endpoints.
Os payloads são os de sim.intent, action_check e npc_gate; o estado reutiliza
as fichas das fixtures desses testes. Nada é escrito no banco.

Critério, contra o baseline:
  JSON válido nos dois lados em 100% dos casos;
  campos críticos iguais em 100%: kind + alvo + hostilidade; possible + blockers;
  decisão do gate colapsada em allow / redirect (partial) / block (refuse);
  own_beat permanece distinto;
  >= 90% de igualdade nos demais campos já normalizados pelos módulos.

Bloqueadores da checagem comparam como conjunto normalizado (ordem não conta).
A saída é só métrica e status: sem prompt, prosa, resposta ou chave.
Exit 0 quando o critério passa; 1 quando falha ou o endpoint quebra; 2 no uso.

Chave, por lado: --api-key, senão o --env-file desse lado, senão LLM_API_KEY.
Um --env-file não herda a chave do outro lado. A chave nunca é impressa.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from contextlib import contextmanager
from copy import deepcopy
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
BACKEND_DIR = REPO_ROOT / "backend"
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app import action_check, consequences, llm_client, npc_assets, npc_gate, settings_store  # noqa: E402
from app import progression  # noqa: E402
from app.sim import intent  # noqa: E402

EXIT_OK = 0
EXIT_FAIL = 1
EXIT_USAGE = 2
OTHER_RATE_MIN = 0.90
CANDIDATE_SLOTS = {"sim_intent": 0, "turn_check": 0, "turn_gate": 1}
_CRITICAL_KEYS = {
    "intent": {"kind", "target", "hostile"},
    "check": {"possible", "blockers"},
    "gate": {"decision"},
}
_GATE_CRITICAL = {
    "allow": "allow",
    "partial": "redirect",
    "refuse": "block",
    "own_beat": "own_beat",
}

ELARA_ID = "card-elara"
BRAM_ID = "card-bram"
PLAYER_ID = "card-caio"

try:
    sys.stdout.reconfigure(errors="replace")
    sys.stderr.reconfigure(errors="replace")
except Exception:
    pass


def _canon(value):
    """Forma comparável. Não serve para exibir: pode carregar texto do modelo."""
    if isinstance(value, str):
        return " ".join(value.split()).casefold()
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, float):
        return round(value, 6)
    if isinstance(value, dict):
        return tuple(sorted((str(key), _canon(item)) for key, item in value.items()))
    if isinstance(value, (list, tuple)):
        return tuple(_canon(item) for item in value)
    return " ".join(str(value).split()).casefold()


def critical_decision(decision: str) -> str:
    """allow permanece; partial vira redirect; refuse vira block."""
    return _GATE_CRITICAL.get(decision, decision)


def critical_view(kind: str, normalized: dict) -> tuple:
    if kind == "intent":
        return (
            normalized.get("kind"),
            _canon(normalized.get("target")),
            normalized.get("hostile") is True,
        )
    if kind == "check":
        blockers = normalized.get("blockers")
        if not isinstance(blockers, list):
            blockers = []
        return (
            normalized.get("possible") is True,
            tuple(sorted(_canon(item) for item in blockers if isinstance(item, str))),
        )
    return (critical_decision(str(normalized.get("decision") or "")),)


def other_view(kind: str, normalized: dict) -> dict:
    skip = _CRITICAL_KEYS[kind]
    return {key: _canon(value) for key, value in normalized.items() if key not in skip}


def score_case(kind: str, baseline: dict, candidate: dict) -> dict:
    left = other_view(kind, baseline)
    right = other_view(kind, candidate)
    keys = sorted(set(left) | set(right))
    mismatches = [key for key in keys if left.get(key) != right.get(key)]
    return {
        "critical_equal": critical_view(kind, baseline) == critical_view(kind, candidate),
        "other_matches": len(keys) - len(mismatches),
        "other_total": len(keys),
        "mismatch_fields": mismatches,
    }


def parse_reply(kind: str, raw: str | None, payload: dict | None = None) -> tuple[bool, dict | None]:
    """O mesmo leitor do módulo. Falha de JSON ou de forma vira inválido."""
    try:
        return _parse_reply(kind, raw, payload)
    except Exception:
        return False, None


def _parse_reply(kind: str, raw: str | None, payload: dict | None) -> tuple[bool, dict | None]:
    if not isinstance(raw, str) or not raw.strip():
        return False, None
    if kind == "intent":
        data = npc_assets._extract_json(raw)
        if not isinstance(data, dict):
            return False, None
        return True, intent.normalize(data)
    if kind == "check":
        verdict = action_check._verdict_from_text(raw)
        if verdict is None:
            return False, None
        return True, verdict.to_dict()
    if kind != "gate" or not isinstance(payload, dict):
        return False, None
    data = npc_assets._extract_json(raw)
    if not isinstance(data, dict):
        return False, None
    normalized = npc_gate._normalize_payload(data, payload)
    if not isinstance(normalized, dict):
        return False, None
    return True, normalized


def _actor_view() -> dict:
    """Recorte de app.sim.engine._actor_view, com a ficha curta das fixtures."""
    return {
        "name": "Caio",
        "hp": 11,
        "hp_max": 14,
        "ac": 13,
        "abilities": {"str": 12, "dex": 16, "con": 11, "int": 13, "wis": 10, "cha": 12},
        "skills": {"Atletismo": 1, "Furtividade": 2, "Luta": 1, "Percepção": 1},
        "inventory": ["espada curta", "arco curto", "tocha", "corda"],
        "down": False,
    }


def _check_base() -> dict:
    """Estado no formato de action_check.collect_state, a partir de tests/test_action_check._rich."""
    return {
        "where": "pátio",
        "places": ["pátio", "portão"],
        "items": ["tocha", "corda"],
        "facts_line": "Mira espera no portão.",
        "canon_blob": "O portão de pedra está fechado. A chave está sob a pedra solta.",
        "blocked_items": [],
        "blocked_places": [],
        "resisted": [],
        "absent": [],
        "invented_people": [],
        "invented_places": [],
        "present_npcs": [{
            "name": "Mira",
            "status": "alive",
            "class": "ranger",
            "stats": {"dex": 16, "hp": 20},
            "memory": "She hid the spare key.",
            "canon": "A scout.",
            "bonds": {"trust": 3},
            "mood_line": "Mira — wary",
        }],
        "player": {
            "name": "Caio",
            "class": "ladino",
            "appearance": "scar over the brow",
            "status": "alive",
            "clothing": "grey cloak",
            "stats": {"dex": 16, "hp": 11},
            "canon": "Cânone essencial do jogador no portão.",
            "inventory": [{"name": "Torch", "qty": 1}, {"name": "corda", "qty": 1}],
            "dice": {"last": 17},
            "character": {"scar": "brow"},
            "ui_state": {},
        },
        "recent_actions": [{"type": "do", "text": "You stand before the gate. Mira waits beside you."}],
        "memories": ["The gate key is hidden under the loose stone."],
        "character_rules": "sem magia de fogo",
        "sim": {},
    }


def check_state(case: dict) -> dict:
    state = deepcopy(_check_base())
    for key in ("blocked_items", "blocked_places", "resisted", "invented_people"):
        if key in case:
            state[key] = list(case[key])
    if case["id"] == "check:impossible":
        state["canon_blob"] = "O portão é uma parede sólida de pedra, sem passagem."
    return state


def _sheet(title_status: str, **extra) -> str:
    sheet = {
        "status": title_status,
        "class": extra.pop("class", ""),
        "alignment": extra.pop("alignment", "neutro"),
        "personality": extra.pop("personality", ""),
        "goals": extra.pop("goals", ""),
        "expression": extra.pop("expression", ""),
        "bonds": extra.pop("bonds", {}),
    }
    sheet.update(extra)
    return json.dumps(sheet, ensure_ascii=False)


def _elara_sheet() -> str:
    """A ficha ferida de tests/test_npc_gate.test_decide_sends_every_person_body_and_memories."""
    return json.dumps({
        "status": "alive",
        "class": "guarda",
        "alignment": "desconfiado",
        "personality": "desconfiada de todos",
        "goals": "proteger a clareira",
        "expression": "guarded",
        "bonds": {"relationship": 40, "trust": 10, "fear": 8, "desire": 0, "boldness": 30},
        "body_state": {"v": 1, "pain": 6, "conditions": [{
            "id": "c1", "kind": "cut", "location": "left cheek",
            "severity": 3, "description": "corte no rosto",
            "visual_en": "small cut on the left cheek", "active": True,
            "portrait_visible": True, "scene_visible": True,
        }]},
    }, ensure_ascii=False)


def _card(card_id: str, title: str, entry: str, sheet: str) -> dict:
    return {
        "id": card_id,
        "type": "character",
        "title": title,
        "entry": entry,
        "sheet": sheet,
    }


def gate_payload(case: dict) -> dict:
    """Mesma forma de npc_gate._payload, montada em memória."""
    setup = case.get("setup") or "present"
    elara_entry = "Guarda ferida na clareira."
    if setup == "allow":
        elara_entry = "Elara te obedece e permanece a seu serviço."
    elara = _card(ELARA_ID, "Elara", elara_entry, _elara_sheet())
    bram = _card(BRAM_ID, "Bram", "Mercador que observa.", _sheet(
        "alive", **{"class": "mercador", "alignment": "neutro"},
    ))
    player = _card(PLAYER_ID, "Caio", "", _sheet(
        "alive", **{"class": "ladino", "appearance": "scar over the brow"},
    ))
    scene_text = "Elara segura a faca na clareira."
    if setup != "absent":
        scene_text = "Elara segura a faca. Bram observa ao lado."
    present_ids = {ELARA_ID}
    if setup != "absent":
        present_ids.add(BRAM_ID)
    people = []
    for card in (elara, bram):
        if card["id"] in present_ids:
            item = npc_gate._card_context(card, present=True)
            item["memories"] = [{
                "kind": "event",
                "text": npc_assets._clip(
                    "O jogador já tentou passar por ela.",
                    npc_gate._GATE_MEMORY_CHARS,
                ),
            }][:npc_gate._GATE_MEMORY_KEEP]
            people.append(item)
        else:
            people.append(npc_gate._absent_context(card))
    adventure = {
        "title": "Adjudicação",
        "premise": "",
        "plot_essentials": "",
        "ui_state": {},
    }
    payload = {
        "action": case["action"],
        "mode": "player_action",
        "scene_tail": npc_gate._scene_of([{"type": "start", "text": scene_text}]),
        "adventure": {
            "title": adventure["title"],
            "premise": "",
            "plot_essentials": "",
            "consequences": consequences.effect_context(adventure),
        },
        "player": npc_gate._player_context(player, "hero"),
        "people": people,
        "relationships_between_npcs": progression.relationship_context([elara, bram]),
        "language": "pt-BR",
    }
    if setup == "block":
        payload["world_check"] = {
            "possible": False,
            "blockers": ["parede"],
            "action_blockers": ["Não existe: passagem na parede."],
        }
    return payload


_CORPUS: list[dict] = [
    {"id": "intent:melee", "kind": "intent", "action": "Ataco o bandido com a espada curta."},
    {"id": "intent:ranged", "kind": "intent", "action": "Disparo o arco curto contra o bandido."},
    {"id": "intent:skill", "kind": "intent", "action": "Escalo a muralha usando furtividade."},
    {"id": "intent:buy", "kind": "intent", "action": "Compro uma tocha do mercador."},
    {"id": "intent:rest", "kind": "intent", "action": "Sento e descanso para recuperar o fôlego."},
    {"id": "intent:speech", "kind": "intent", "action": "Digo ao bandido que não quero lutar."},
    {"id": "intent:item", "kind": "intent", "action": "Acendo a tocha que carrego."},
    {"id": "intent:explore", "kind": "intent", "action": "Observo a sala e sigo pelo corredor."},
    {"id": "check:observe", "kind": "check", "action": "Observo o portão e a trilha com atenção."},
    {"id": "check:traverse", "kind": "check", "action": "Atravesso o pátio até o portão."},
    {"id": "check:jump", "kind": "check", "action": "Salto o muro baixo do portão."},
    {"id": "check:attack", "kind": "check", "action": "Ataco Mira com a tocha acesa."},
    {"id": "check:persuade", "kind": "check", "action": "Peço a Mira que abra o portão."},
    {"id": "check:item", "kind": "check", "action": "Uso a tocha para iluminar o portão."},
    {
        "id": "check:world_facts", "kind": "check",
        "action": "Pego a lanterna antiga e entro no quarto selado.",
        "blocked_items": ["lanterna antiga"],
        "blocked_places": ["quarto selado"],
    },
    {"id": "check:impossible", "kind": "check", "action": "Atravesso a parede sólida do portão."},
    {"id": "gate:greet", "kind": "gate", "action": "Cumprimento Elara na clareira.", "setup": "present"},
    {"id": "gate:ask_present", "kind": "gate", "action": "Peço a Elara que me deixe passar.", "setup": "present"},
    {"id": "gate:talk_absent", "kind": "gate", "action": "Falo com Bram, que não está na clareira.", "setup": "absent"},
    {"id": "gate:declared_death", "kind": "gate", "action": "Declaro que Elara está morta.", "setup": "death"},
    {"id": "gate:travel", "kind": "gate", "action": "Viajo com Elara até a cidade.", "setup": "present"},
    {"id": "gate:allow", "kind": "gate", "action": "Elara, fique de guarda aqui.", "setup": "allow"},
    {"id": "gate:redirect", "kind": "gate", "action": "Empurro Elara e passo pela trilha.", "setup": "present"},
    {"id": "gate:block", "kind": "gate", "action": "Atravesso a parede e saio da clareira.", "setup": "block"},
]


def corpus() -> list[dict]:
    rows = _CORPUS
    ids = [row["id"] for row in rows]
    counts: dict[str, int] = {}
    for row in rows:
        counts[row["kind"]] = counts.get(row["kind"], 0) + 1
    if len(rows) != 24 or len(set(ids)) != 24 or counts != {"intent": 8, "check": 8, "gate": 8}:
        raise RuntimeError("corpus auxiliar deve ter 24 ações fixas")
    return rows


def corpus_by_id(case_id: str) -> dict:
    for row in corpus():
        if row["id"] == case_id:
            return row
    raise KeyError(case_id)


def _file_block(name: str) -> dict:
    try:
        raw = settings_store.load_file_settings().get(name) or {}
    except Exception:
        raw = {}
    return dict(raw) if isinstance(raw, dict) else {}


def _capture(run) -> dict:
    """Uma chamada já embrulhada pelo módulo. Guarda a classe do erro, nunca a mensagem."""
    box: dict = {"raw": None, "error": ""}
    real = llm_client.chat_completion

    def wrapped(cfg, messages, **kwargs):
        try:
            raw = real(cfg, messages, **kwargs)
        except Exception as exc:
            box["error"] = type(exc).__name__
            raise
        box["raw"] = raw if isinstance(raw, str) else ""
        box["error"] = ""
        return raw

    llm_client.chat_completion = wrapped
    try:
        run()
    except Exception as exc:
        if not box["error"]:
            box["error"] = type(exc).__name__
    finally:
        llm_client.chat_completion = real
    return box


def _execute(case: dict, llm_cfg: dict) -> tuple[dict, dict | None]:
    payload = None
    if case["kind"] == "intent":
        sim_cfg = _file_block("sim")

        def run():
            intent.classify(
                case["action"], _actor_view(), ["bandido"], None,
                llm_cfg, None, sim_cfg,
            )
    elif case["kind"] == "check":
        state = check_state(case)
        check_cfg = _file_block("action_check")

        def run():
            action_check.check_action(
                case["action"], "do", state,
                llm_cfg=llm_cfg, transport=None, cfg=check_cfg,
                allow_llm=True, blockers=[],
            )
    else:
        payload = gate_payload(case)

        def run():
            npc_gate._call(llm_cfg, payload, None)

    return _capture(run), payload


def _side_result(case: dict, llm_cfg: dict) -> dict:
    try:
        box, payload = _execute(case, llm_cfg)
    except Exception as exc:
        return {"valid": False, "normalized": None, "error": type(exc).__name__}
    if box.get("error"):
        return {"valid": False, "normalized": None, "error": box["error"]}
    valid, normalized = parse_reply(case["kind"], box.get("raw"), payload)
    return {"valid": valid, "normalized": normalized, "error": ""}


@contextmanager
def _quiet():
    logger = logging.getLogger("aidungeon")
    previous = logger.level
    logger.setLevel(logging.CRITICAL)
    try:
        yield
    finally:
        logger.setLevel(previous)


def evaluate(baseline_cfg: dict, candidate_cfg: dict) -> dict:
    """Roda o corpus. O relatório não contém cfg, prompt nem resposta."""
    rows = []
    with _quiet():
        for case in corpus():
            baseline = _side_result(case, baseline_cfg)
            candidate = _side_result(case, candidate_cfg)
            row = {
                "case_id": case["id"],
                "kind": case["kind"],
                "baseline_valid": baseline["valid"],
                "candidate_valid": candidate["valid"],
                "baseline_error": baseline["error"],
                "candidate_error": candidate["error"],
                "critical_equal": False,
                "other_matches": 0,
                "other_total": 0,
                "mismatch_fields": [],
            }
            if baseline["valid"] and candidate["valid"]:
                scored = score_case(case["kind"], baseline["normalized"], candidate["normalized"])
                row.update(scored)
            rows.append(row)
    return summarize(rows)


def _public_name(value: str) -> str:
    """Só identificadores. Mensagem de exceção, prosa e chave ficam de fora."""
    name = str(value or "")
    return name if name.isidentifier() else ""


def summarize(rows: list[dict]) -> dict:
    cases = len(rows)
    baseline_valid = sum(1 for row in rows if row["baseline_valid"])
    candidate_valid = sum(1 for row in rows if row["candidate_valid"])
    critical_matches = sum(1 for row in rows if row["critical_equal"])
    other_matches = sum(int(row["other_matches"]) for row in rows)
    other_total = sum(int(row["other_total"]) for row in rows)
    mismatches: dict[str, int] = {}
    for row in rows:
        kind = row.get("kind") if row.get("kind") in _CRITICAL_KEYS else ""
        for field in row.get("mismatch_fields") or []:
            if not kind or not isinstance(field, str) or not field.isidentifier():
                continue
            key = f"{kind}.{field}"
            mismatches[key] = mismatches.get(key, 0) + 1
    errors: dict[str, dict[str, int]] = {"baseline": {}, "candidate": {}}
    for row in rows:
        for side in ("baseline", "candidate"):
            name = _public_name(row.get(f"{side}_error") or "")
            if name:
                bucket = errors[side]
                bucket[name] = bucket.get(name, 0) + 1
    other_rate = (other_matches / other_total) if other_total else 0.0
    valid_ok = cases > 0 and baseline_valid == cases and candidate_valid == cases
    critical_ok = cases > 0 and critical_matches == cases
    other_ok = other_total > 0 and other_rate >= OTHER_RATE_MIN
    return {
        "cases": cases,
        "baseline_valid": baseline_valid,
        "candidate_valid": candidate_valid,
        "valid_json_rate": {
            "baseline": round(baseline_valid / cases, 4) if cases else 0.0,
            "candidate": round(candidate_valid / cases, 4) if cases else 0.0,
        },
        "critical_matches": critical_matches,
        "critical_rate": round(critical_matches / cases, 4) if cases else 0.0,
        "other_matches": other_matches,
        "other_total": other_total,
        "other_rate": round(other_rate, 4),
        "quality_ok": bool(valid_ok and critical_ok and other_ok),
        "invalid_cases": [
            row["case_id"] for row in rows
            if not row["baseline_valid"] or not row["candidate_valid"]
        ],
        "critical_misses": [
            row["case_id"] for row in rows
            if row["baseline_valid"] and row["candidate_valid"] and not row["critical_equal"]
        ],
        "other_mismatch_fields": dict(sorted(mismatches.items())),
        "errors": errors,
    }


def read_dotenv_key(path: Path, name: str) -> str:
    if not path.is_file():
        return ""
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return ""
    for line in lines:
        key, sep, value = line.strip().partition("=")
        if sep and key.strip().removeprefix("export ").strip() == name:
            return value.strip().strip('"').strip("'")
    return ""


def resolve_key(explicit: str, env_file: str) -> str:
    """Chave do lado. Arquivo explícito não cai na env do processo."""
    if explicit:
        return explicit
    if env_file:
        return read_dotenv_key(Path(env_file), "LLM_API_KEY")
    return os.environ.get("LLM_API_KEY") or ""


def endpoint_cfg(base_url: str, model_alias: str, api_key: str, *,
                 slots: dict | None) -> dict:
    timeouts: dict = {}
    aux_cache = False
    timeout_seconds = 180
    try:
        file_cfg = settings_store.load_file_settings()
        llm = file_cfg.get("llm") if isinstance(file_cfg.get("llm"), dict) else {}
        scheduler = file_cfg.get("scheduler") if isinstance(file_cfg.get("scheduler"), dict) else {}
        raw_timeouts = scheduler.get("timeouts") if isinstance(scheduler.get("timeouts"), dict) else {}
        timeouts = dict(raw_timeouts)
        aux_cache = bool(scheduler.get("aux_cache_prompt", False))
        timeout_seconds = int(llm.get("timeout_seconds", 180) or 180)
    except Exception:
        pass
    overrides = {}
    if slots:
        overrides = {role: {"slot": slot} for role, slot in slots.items()}
    return {
        "provider": "llamacpp",
        "base_url": base_url.rstrip("/"),
        "model_alias": model_alias or "auto",
        "api_key": api_key or None,
        "disable_thinking": True,
        "timeout_seconds": timeout_seconds,
        "aux_cache_prompt": aux_cache,
        "role_timeouts": timeouts,
        "scheduler": {"role_overrides": overrides},
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Compara o baseline 35B com o auxiliar 3B nas 24 ações fixas.",
    )
    parser.add_argument("--baseline-url", required=True)
    parser.add_argument("--candidate-url", required=True)
    parser.add_argument("--baseline-model", default="auto")
    parser.add_argument("--candidate-model", default="qwen2.5-3b-instruct-q4_k_m")
    parser.add_argument("--baseline-api-key", default="")
    parser.add_argument("--candidate-api-key", default="")
    parser.add_argument("--baseline-env-file", default="")
    parser.add_argument("--candidate-env-file", default="")
    parser.add_argument("--output", default="")
    parser.add_argument(
        "--no-candidate-slots", action="store_true",
        help="não pina slot 0/0/1 no candidato; o mapa consulta /props",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        code = exc.code
        return EXIT_USAGE if code is None else int(code)
    slots = None if args.no_candidate_slots else dict(CANDIDATE_SLOTS)
    baseline = endpoint_cfg(
        args.baseline_url, args.baseline_model,
        resolve_key(args.baseline_api_key, args.baseline_env_file),
        slots=None,
    )
    candidate = endpoint_cfg(
        args.candidate_url, args.candidate_model,
        resolve_key(args.candidate_api_key, args.candidate_env_file),
        slots=slots,
    )
    try:
        report = evaluate(baseline, candidate)
    except Exception as exc:
        print(json.dumps({"quality_ok": False, "error": type(exc).__name__}), file=sys.stderr)
        return EXIT_FAIL
    text = json.dumps(report, ensure_ascii=False, sort_keys=True)
    print(text)
    if args.output:
        try:
            Path(args.output).write_text(text + "\n", encoding="utf-8")
        except OSError:
            print(json.dumps({"quality_ok": False, "error": "OSError"}), file=sys.stderr)
            return EXIT_FAIL
    return EXIT_OK if report["quality_ok"] else EXIT_FAIL


if __name__ == "__main__":
    raise SystemExit(main())
