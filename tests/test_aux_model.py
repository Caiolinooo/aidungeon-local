"""Papéis auxiliares no 3B: overrides, slot pinado e o avaliador sem rede."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import httpx

from app import action_check, db, llm_client, npc_gate, settings_store
from app.sim import intent

TOOLS_DIR = Path(__file__).resolve().parent.parent / "tools"
if str(TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(TOOLS_DIR))

import eval_turn_aux as aux  # noqa: E402

AUX_URL = "http://127.0.0.1:1240/v1"
MAIN_URL = "http://main/v1"

INTENT = {
    "kind": "attack", "ability": "str", "skill": "Luta", "target": "bandido",
    "toughness": "normal", "hostile": True, "weapon": "espada", "item": "",
    "gold": 0, "difficulty": "medium", "advantage": "", "minutes": 30,
    "activity": "other", "shock": "none", "heal": False,
}
CHECK = {
    "possible": True, "blockers": [], "risks": ["a ronda"],
    "notes": "nada impede", "cost": "folego", "consequence": "mira olha",
}
GATE = {
    "decision": "allow",
    "outcome": "Elara permanece na clareira.",
    "reactions": [{
        "npc": "Elara", "card_id": aux.ELARA_ID,
        "reaction": "Ela observa.", "line": "",
        "emotion": "neutral", "pose": "ainda",
    }],
    "initiative": None,
    "relation_updates": [],
    "cue": "Mantenha Elara presente.",
}


def _reply(role: str, body: dict | None = None) -> str:
    if role == "sim_intent":
        return json.dumps(body or INTENT)
    if role == "turn_check":
        return json.dumps(body or CHECK)
    if role == "turn_gate":
        return json.dumps(body or GATE)
    raise AssertionError(role)


def _install(monkeypatch, handler):
    calls = []

    def fake(cfg, messages, **kwargs):
        calls.append((cfg, messages, kwargs))
        reply = handler(cfg, messages, kwargs)
        if isinstance(reply, Exception):
            raise reply
        return reply

    monkeypatch.setattr(llm_client, "chat_completion", fake)
    return calls


def _argv(*extra: str) -> list[str]:
    return [
        "--baseline-url", "http://baseline.invalid/v1",
        "--candidate-url", AUX_URL,
        "--baseline-model", "qwen3-35b",
        "--candidate-model", "qwen2.5-3b-instruct-q4_k_m",
        *extra,
    ]


def _row(*, valid=True, equal=True, other_matches=1, other_total=1,
         case_id="check:observe", kind="check", error=""):
    return {
        "case_id": case_id,
        "kind": kind,
        "baseline_valid": valid,
        "candidate_valid": valid,
        "baseline_error": error,
        "candidate_error": "",
        "critical_equal": equal and valid,
        "other_matches": other_matches,
        "other_total": other_total,
        "mismatch_fields": [] if other_matches == other_total else ["notes"],
    }


def test_role_overrides_default_empty(tmp_db):
    settings_store.load_file_settings(force=True)
    with db.session() as conn:
        cfg = settings_store.effective_llm_settings(conn)
    assert cfg["scheduler"]["role_overrides"] == {}


def test_role_overrides_come_from_the_file_not_the_db(tmp_db):
    settings_store.load_file_settings(force=True)
    scheduler = settings_store._file_cache["scheduler"]
    previous = scheduler.get("role_overrides")
    had_key = "role_overrides" in scheduler
    scheduler["role_overrides"] = {
        "sim_intent": {
            "base_url": "  http://127.0.0.1:1240/v1  ",
            "model_alias": " qwen2.5-3b-instruct-q4_k_m ",
            "api_key": "sk-aux-secret",
            "slot": "0",
        },
        "turn_check": {"slot": 0, "api_key": "sk-\u20261234"},
        "turn_gate": {"base_url": "http://127.0.0.1:1240/v1", "slot": 1},
        "empty": {},
        "bad": {"slot": True, "base_url": "   "},
        "meta": {"slot": -1},
        "": {"slot": 1},
    }
    try:
        with db.session() as conn:
            db.set_setting(conn, "llm.base_url", json.dumps("http://db-override/v1"))
            cfg = settings_store.effective_llm_settings(conn)
    finally:
        if had_key:
            scheduler["role_overrides"] = previous
        else:
            scheduler.pop("role_overrides", None)
    assert cfg["base_url"] == "http://db-override/v1"
    overrides = cfg["scheduler"]["role_overrides"]
    assert set(overrides) == {"sim_intent", "turn_check", "turn_gate"}
    assert overrides["sim_intent"] == {
        "base_url": "http://127.0.0.1:1240/v1",
        "model_alias": "qwen2.5-3b-instruct-q4_k_m",
        "api_key": "sk-aux-secret",
        "slot": 0,
    }
    assert overrides["turn_check"] == {"slot": 0}
    assert overrides["turn_gate"]["slot"] == 1
    assert "api_key" not in overrides["turn_check"]


def test_role_config_keeps_the_main_endpoint_without_override():
    cfg = {"base_url": MAIN_URL, "model_alias": "big", "api_key": "sk-main",
           "scheduler": {"role_overrides": {}}}
    copied = llm_client.role_config(cfg, "turn_gate")
    assert copied is not cfg
    assert copied["base_url"] == MAIN_URL
    assert copied["model_alias"] == "big"
    assert copied["api_key"] == "sk-main"
    copied["base_url"] = "http://mutated/v1"
    assert cfg["base_url"] == MAIN_URL


def test_three_roles_use_only_their_override(monkeypatch):
    seen = []

    def fake(cfg, messages, **kwargs):
        seen.append((kwargs["role"], cfg["base_url"], cfg["model_alias"], cfg.get("api_key")))
        return _reply(kwargs["role"])

    monkeypatch.setattr(llm_client, "chat_completion", fake)
    cfg = {
        "base_url": MAIN_URL,
        "model_alias": "big",
        "api_key": "sk-main",
        "disable_thinking": True,
        "timeout_seconds": 30,
        "scheduler": {"role_overrides": {
            "sim_intent": {
                "base_url": AUX_URL,
                "model_alias": "qwen2.5-3b-instruct-q4_k_m",
                "api_key": "sk-aux",
                "slot": 0,
            },
            "turn_check": {
                "base_url": AUX_URL,
                "model_alias": "qwen2.5-3b-instruct-q4_k_m",
                "slot": 0,
            },
            "turn_gate": {
                "base_url": AUX_URL,
                "model_alias": "qwen2.5-3b-instruct-q4_k_m",
                "slot": 1,
            },
        }},
    }
    untouched = llm_client.role_config(cfg, "choices")
    assert untouched["base_url"] == MAIN_URL
    assert untouched["model_alias"] == "big"
    assert "slot" not in llm_client.role_config(cfg, "sim_intent")
    classified = intent.classify(
        "olho a sala", {}, [], None, cfg, None, {"max_tokens": 40, "timeout_seconds": 20},
    )
    verdict = action_check.check_action(
        "olho a sala com atenção", "do", {"player": {"name": "Caio"}},
        llm_cfg=cfg, transport=None, cfg={"max_tokens": 40, "timeout_seconds": 20},
    )
    gate = npc_gate._call(cfg, {"action": "oi", "people": []}, None)
    assert classified["kind"] == "attack"
    assert verdict.possible is True
    assert gate["decision"] == "allow"
    assert [row[0] for row in seen] == ["sim_intent", "turn_check", "turn_gate"]
    assert {row[1] for row in seen} == {AUX_URL}
    assert {row[2] for row in seen} == {"qwen2.5-3b-instruct-q4_k_m"}
    assert [row[3] for row in seen] == ["sk-aux", "sk-main", "sk-main"]
    assert cfg["base_url"] == MAIN_URL


def test_explicit_slot_skips_props_even_when_zero():
    llm_client.invalidate_discovery_cache()
    hits = []

    def handler(request: httpx.Request) -> httpx.Response:
        hits.append(request.url.path)
        if request.url.path.endswith("/props"):
            return httpx.Response(200, json={"total_slots": 5})
        if request.url.path.endswith("/chat/completions"):
            body = json.loads(request.content.decode("utf-8"))
            hits.append(body.get("id_slot"))
            return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})
        return httpx.Response(404, json={"error": request.url.path})

    transport = httpx.MockTransport(handler)
    cfg = aux.endpoint_cfg(
        "http://fake-aux:9/v1", "pinned-model", "", slots=aux.CANDIDATE_SLOTS,
    )
    tuned = llm_client.role_config(cfg, "turn_check")
    assert llm_client.slot_for(tuned, "sim_intent", transport) == 0
    assert llm_client.slot_for(tuned, "turn_check", transport) == 0
    assert llm_client.slot_for(tuned, "turn_gate", transport) == 1
    assert hits == []
    llm_client.chat_completion(
        tuned, [{"role": "user", "content": "hi"}],
        role="turn_check", transport=transport, max_tokens=8,
    )
    assert hits == ["/v1/chat/completions", 0]
    llm_client.slot_for(cfg, "choices", transport)
    assert hits[-1] == "/props"


def test_corpus_sends_fixed_actions_and_fixture_state(monkeypatch):
    rows = aux.corpus()
    assert [row["kind"] for row in rows].count("intent") == 8
    assert [row["kind"] for row in rows].count("check") == 8
    assert [row["kind"] for row in rows].count("gate") == 8
    assert len({row["action"] for row in rows}) == 24
    state = aux.check_state(aux.corpus_by_id("check:world_facts"))
    assert state["blocked_items"] == ["lanterna antiga"]
    assert state["blocked_places"] == ["quarto selado"]
    assert state["player"]["stats"]["dex"] == 16
    assert state["present_npcs"][0]["name"] == "Mira"
    impossible = aux.check_state(aux.corpus_by_id("check:impossible"))
    assert impossible["blocked_items"] == []
    assert "parede sólida" in impossible["canon_blob"]
    absent = aux.gate_payload(aux.corpus_by_id("gate:talk_absent"))
    people = {item["name"]: item for item in absent["people"]}
    assert people["Bram"] == {"name": "Bram", "status": "alive", "present": False}
    assert people["Elara"]["present"] is True
    allow = aux.gate_payload(aux.corpus_by_id("gate:allow"))
    elara = next(item for item in allow["people"] if item["name"] == "Elara")
    assert elara["profile"]["obedience"] == "obedient_compelled"
    blocked = aux.gate_payload(aux.corpus_by_id("gate:block"))
    assert blocked["world_check"]["action_blockers"] == ["Não existe: passagem na parede."]
    seen = []

    def fake(cfg, messages, **kwargs):
        seen.append((kwargs["role"], kwargs.get("timeout_s"), messages[-1]["content"]))
        return _reply(kwargs["role"])

    monkeypatch.setattr(llm_client, "chat_completion", fake)
    cfg = {"base_url": MAIN_URL, "model_alias": "big", "scheduler": {}}
    aux._execute(aux.corpus_by_id("intent:melee"), cfg)
    aux._execute(aux.corpus_by_id("check:world_facts"), cfg)
    aux._execute(aux.corpus_by_id("gate:block"), cfg)
    by_role = {role: (timeout, content) for role, timeout, content in seen}
    assert "Ataco o bandido com a espada curta." in by_role["sim_intent"][1]
    assert "lanterna antiga" in by_role["turn_check"][1]
    assert "quarto selado" in by_role["turn_check"][1]
    assert "passagem na parede" in by_role["turn_gate"][1]
    sim_timeout = float(settings_store.load_file_settings()["sim"]["timeout_seconds"])
    check_timeout = float(settings_store.load_file_settings()["action_check"]["timeout_seconds"])
    assert by_role["sim_intent"][0] == sim_timeout
    assert by_role["turn_check"][0] == check_timeout
    gate_timeout = by_role["turn_gate"][0]
    if gate_timeout is None:
        assert llm_client.role_timeout({"role_timeouts": {}}, "turn_gate") == 12.0
    else:
        assert float(gate_timeout) == 45.0


def test_parse_score_and_threshold():
    valid, normalized = aux.parse_reply(
        "intent", '{"kind":"attack","target":" Bandido ","hostile":true,"minutes":5}',
    )
    also_valid, other = aux.parse_reply(
        "intent", '{"kind":"attack","target":"bandido","hostile":true,"minutes":9}',
    )
    assert valid and also_valid
    scored = aux.score_case("intent", normalized, other)
    assert scored["critical_equal"] is True
    assert scored["mismatch_fields"] == ["minutes"]
    assert aux.parse_reply("intent", "RAWLEAK9") == (False, None)
    left = {
        "possible": False, "blockers": ["B", "A"], "risks": [],
        "notes": "", "cost": "", "consequence": "",
    }
    right = {
        "possible": False, "blockers": ["a", "  b "], "risks": ["x"],
        "notes": "", "cost": "", "consequence": "",
    }
    check_score = aux.score_case("check", left, right)
    assert check_score["critical_equal"] is True
    assert check_score["mismatch_fields"] == ["risks"]
    payload = aux.gate_payload(aux.corpus_by_id("gate:greet"))
    ok, allow = aux.parse_reply("gate", json.dumps(GATE), payload)
    _, partial = aux.parse_reply("gate", json.dumps({**GATE, "decision": "partial"}), payload)
    _, refuse = aux.parse_reply("gate", json.dumps({**GATE, "decision": "refuse"}), payload)
    assert ok and allow["decision"] == "allow"
    assert aux.critical_view("gate", partial) == ("redirect",)
    assert aux.critical_view("gate", refuse) == ("block",)
    assert aux.score_case("gate", allow, partial)["critical_equal"] is False
    assert aux.parse_reply("check", "{}")[0] is False
    assert aux.parse_reply("gate", "{", payload)[0] is False
    assert aux.summarize([_row(other_matches=9, other_total=10)])["quality_ok"] is True
    assert aux.summarize([_row(other_matches=8, other_total=10)])["quality_ok"] is False
    leaked = aux.summarize([_row(
        valid=False, equal=False, other_matches=0, other_total=0,
        error="RuntimeError: sk-secret prose",
    )])
    assert leaked["quality_ok"] is False
    assert "sk-secret" not in json.dumps(leaked)
    assert leaked["errors"]["baseline"] == {}


def test_main_passes_without_printing_secrets(monkeypatch, capsys, tmp_path):
    env = tmp_path / ".env"
    env.write_text('LLM_API_KEY="sk-FILE-LEAK-42"\nOTHER=1\n')
    calls = _install(monkeypatch, lambda cfg, messages, kwargs: _reply(kwargs["role"]))
    code = aux.main(_argv(
        "--baseline-env-file", str(env),
        "--candidate-api-key", "sk-FLAG-LEAK-7",
    ))
    captured = capsys.readouterr()
    report = json.loads(captured.out)
    assert code == 0
    assert report["quality_ok"] is True
    assert report["cases"] == 24
    assert report["critical_matches"] == 24
    assert report["other_rate"] == 1.0
    assert len(calls) == 48
    blob = captured.out + captured.err
    assert "sk-FILE-LEAK-42" not in blob
    assert "sk-FLAG-LEAK-7" not in blob
    assert "base_url" not in report
    assert "api_key" not in blob


def test_main_fails_closed_on_invalid_json_critical_and_other_fields(monkeypatch, capsys):
    def invalid(cfg, messages, kwargs):
        if "1240" in cfg["base_url"] and kwargs["role"] == "sim_intent":
            return "RAWLEAK9"
        return _reply(kwargs["role"])

    _install(monkeypatch, invalid)
    code = aux.main(_argv())
    captured = capsys.readouterr()
    report = json.loads(captured.out)
    assert code == 1
    assert report["quality_ok"] is False
    assert report["invalid_cases"] == [row["id"] for row in aux.corpus() if row["kind"] == "intent"]
    assert "RAWLEAK9" not in captured.out + captured.err

    def critical(cfg, messages, kwargs):
        if "1240" in cfg["base_url"] and kwargs["role"] == "turn_check":
            return _reply("turn_check", {**CHECK, "possible": False, "blockers": ["muro secreto"]})
        return _reply(kwargs["role"])

    _install(monkeypatch, critical)
    code = aux.main(_argv())
    captured = capsys.readouterr()
    report = json.loads(captured.out)
    assert code == 1
    assert report["critical_misses"] == [row["id"] for row in aux.corpus() if row["kind"] == "check"]
    assert "muro secreto" not in captured.out + captured.err

    def notes(cfg, messages, kwargs):
        if "1240" in cfg["base_url"] and kwargs["role"] == "turn_check":
            return _reply("turn_check", {**CHECK, "notes": "OUTRA-NOTA-LONGA"})
        return _reply(kwargs["role"])

    _install(monkeypatch, notes)
    code = aux.main(_argv())
    captured = capsys.readouterr()
    report = json.loads(captured.out)
    assert code == 0
    assert report["quality_ok"] is True
    assert report["other_mismatch_fields"] == {"check.notes": 8}
    assert "OUTRA-NOTA-LONGA" not in captured.out + captured.err

    def drifted(cfg, messages, kwargs):
        if "1240" in cfg["base_url"] and kwargs["role"] == "sim_intent":
            return _reply("sim_intent", {**INTENT, "ability": "dex", "gold": 4242, "minutes": 12})
        return _reply(kwargs["role"])

    _install(monkeypatch, drifted)
    code = aux.main(_argv())
    captured = capsys.readouterr()
    report = json.loads(captured.out)
    assert code == 1
    assert report["quality_ok"] is False
    assert report["critical_rate"] == 1.0
    assert report["other_rate"] < aux.OTHER_RATE_MIN
    assert "4242" not in captured.out + captured.err


def test_main_hides_transport_errors_and_rejects_bad_args(monkeypatch, capsys):
    def boom(cfg, messages, kwargs):
        if "1240" in cfg["base_url"] and kwargs["role"] == "turn_gate" and "Declaro que Elara" in messages[-1]["content"]:
            raise RuntimeError("sk-LEAK-999 prose-leak")
        return _reply(kwargs["role"])

    _install(monkeypatch, boom)
    code = aux.main(_argv("--baseline-api-key", "sk-LEAK-999"))
    captured = capsys.readouterr()
    report = json.loads(captured.out)
    assert code == 1
    assert report["errors"]["candidate"] == {"RuntimeError": 1}
    assert "sk-LEAK-999" not in captured.out + captured.err
    assert "prose-leak" not in captured.out + captured.err
    assert aux.main([]) == 2


def test_key_resolution_keeps_each_side_separate(tmp_path, monkeypatch):
    monkeypatch.setenv("LLM_API_KEY", "sk-process")
    env = tmp_path / ".env"
    env.write_text("LLM_API_KEY=sk-file\n")
    assert aux.resolve_key("sk-flag", str(env)) == "sk-flag"
    assert aux.resolve_key("", str(env)) == "sk-file"
    assert aux.resolve_key("", "") == "sk-process"
    empty = tmp_path / "empty.env"
    empty.write_text("OTHER=1\n")
    assert aux.resolve_key("", str(empty)) == ""

