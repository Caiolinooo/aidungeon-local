#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
slot_watchdog.py -- Vigia os slots do llama-server e reinicia o serviço
quando um slot trava (llama.cpp #28280).

Roda como oneshot a cada 10 s (slot-watchdog.timer). Guarda em
<state-dir>/state.json, por slot, a tarefa (id_task), o maior
n_prompt_tokens_processed e o maior n_decoded vistos nela e desde quando
nenhum dos dois passa do máximo. Slot ocupado sem progresso por mais de
--stall-s, sem nenhum outro slot andando nesse tempo, ou /slots sem responder
por esse tempo, reinicia o serviço (com --cooldown entre reinícios).

O lote de prompt é montado na ordem dos slots: um prompt longo num slot
deixa os slots seguintes parados até acabar. Por isso slot parado enquanto
outro anda só espera, até --hard-stall-s: no #28280 os outros slots seguem
servindo, e tráfego contínuo adiaria o reinício para sempre.

Na build atual fechar a conexão já solta o slot, e o app faz isso no prazo de
cada role. O watchdog cobre o caso do #28280 em que nem isso resolve.

Dica do app (--hint-file, repetível): quando a narrativa fica sem token e sem
prompt_progress, o app larga a conexão, repete a narrativa em outro slot e
grava {ts, slot, reason}. Com dica de até --hint-max-age segundos e o slot
dela ainda ocupado sem progresso por --hint-stall-s, o watchdog reinicia sem
esperar os outros slots pararem: só espera o slot do fallback terminar
(--hint-quiet-s sem nenhum slot andando) até --hint-defer-s depois da dica.

<state-dir>/status.json (legível pelo app, ver /api/health/llm):
  checked_at, slots [{id, busy, stalled_s}], unreachable_s, restarts,
  last_restart {at, reason}, hint {slot, at, age_s} (dica em vigor).

API key: --api-key-file (uma por linha, '#' comenta) > env LLM_API_KEY >
LLM_API_KEY do --env-file. Nunca impressa.
Apenas biblioteca padrão.
Exit codes: 0 = ok ou reiniciou · 1 = erro de uso.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

SERVER_KEY = "server"
WARNING, NOTICE = 4, 5


def say(level: int, message: str) -> None:
    """No journal o prefixo <n> vira a prioridade (a unit usa LogLevelMax=notice)."""
    prefix = f"<{level}>" if os.environ.get("JOURNAL_STREAM") else ""
    print(prefix + message, flush=True)


def read_key(args: argparse.Namespace) -> str:
    if args.api_key_file:
        for line in Path(args.api_key_file).read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                return line
    if os.environ.get("LLM_API_KEY"):
        return os.environ["LLM_API_KEY"]
    if args.env_file and Path(args.env_file).is_file():
        for line in Path(args.env_file).read_text(encoding="utf-8").splitlines():
            key, sep, value = line.strip().partition("=")
            if sep and key.strip().removeprefix("export ").strip() == "LLM_API_KEY":
                return value.strip().strip('"').strip("'")
    return ""


def fetch_slots(base_url: str, api_key: str, timeout: float) -> list[dict] | None:
    """Lista de slots, [] se o servidor recusou a conexão (subindo ou parado),
    None se não respondeu a tempo."""
    req = urllib.request.Request(base_url.rstrip("/") + "/slots")
    if api_key:
        req.add_header("Authorization", f"Bearer {api_key}")
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(req, timeout=timeout) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        say(WARNING, f"GET /slots: HTTP {exc.code} (API key? --no-slots?)")
        return []
    except (TimeoutError, urllib.error.URLError) as exc:
        reason = getattr(exc, "reason", exc)
        if isinstance(reason, ConnectionRefusedError):
            return []
        if isinstance(reason, TimeoutError) or isinstance(exc, TimeoutError):
            return None
        return []
    except (OSError, ValueError):
        return []


def progress(slot: dict) -> list:
    nxt = slot.get("next_token")
    if isinstance(nxt, list):
        nxt = nxt[0] if nxt else {}
    return [slot.get("id_task"), slot.get("n_prompt_tokens_processed"),
            (nxt or {}).get("n_decoded")]


def advance(mark: dict | None, slot: dict, now: float) -> dict:
    """Registro de progresso do slot depois de uma leitura de /slots.

    Só conta como progresso uma tarefa nova ou um contador acima do maior já
    visto na tarefa: no #28280 a tarefa presa pode oscilar entre duas posições.
    """
    task, *counters = progress(slot)
    old = mark or {}
    if not slot.get("is_processing") or old.get("task") != task:
        return {"task": task, "best": counters, "since": now}
    best = list(old.get("best") or [None, None])
    moved = False
    for i, value in enumerate(counters):
        if value is not None and (best[i] is None or value > best[i]):
            best[i] = value
            moved = True
    return {"task": task, "best": best, "since": now if moved else old.get("since", now)}


def load(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save(path: Path, data: dict) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    tmp.chmod(0o644)
    tmp.replace(path)


def iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).isoformat(timespec="seconds")


def fresh_hint(args: argparse.Namespace, state: dict, now: float) -> dict | None:
    """Dica mais nova ainda válida: dentro de --hint-max-age, depois do último
    reinício e ainda não usada."""
    newest = None
    floor = max(float(state.get("last_restart_ts", 0.0)), float(state.get("hint_used_ts", 0.0)))
    for raw in args.hint_file or []:
        hint = load(Path(raw))
        try:
            ts = float(hint["ts"])
            slot = str(int(hint["slot"]))
        except (KeyError, TypeError, ValueError):
            continue
        if ts <= floor or now - ts > args.hint_max_age or now < ts:
            continue
        if newest is None or ts > newest["ts"]:
            newest = {"ts": ts, "slot": slot}
    return newest


def check(args: argparse.Namespace, now: float) -> int:
    state_dir = Path(args.state_dir)
    state_dir.mkdir(parents=True, exist_ok=True)
    state = load(state_dir / "state.json")
    status = load(state_dir / "status.json")
    marks: dict = state.get("marks", {})

    slots = fetch_slots(args.base_url, read_key(args), args.request_timeout)
    stalled: list[tuple[str, float]] = []
    report = []
    if slots is None:
        mark = marks.get(SERVER_KEY) or {"since": now}
        marks = {SERVER_KEY: mark}
        stalled.append((SERVER_KEY, now - mark["since"]))
    else:
        fresh = {}
        for slot in slots:
            key = str(slot.get("id"))
            fresh[key] = advance(marks.get(key), slot, now)
            busy = bool(slot.get("is_processing"))
            stalled_s = now - fresh[key]["since"] if busy else 0.0
            report.append({"id": slot.get("id"), "busy": busy, "stalled_s": round(stalled_s, 1)})
            if busy:
                stalled.append((key, stalled_s))
                if stalled_s == 0.0:
                    state["moved_at"] = now
        marks = fresh

    status.update(checked_at=iso(now), slots=report,
                  unreachable_s=round(stalled[0][1], 1) if slots is None else 0.0)
    worst = max(stalled, key=lambda item: item[1], default=(None, 0.0))
    last_restart = state.get("last_restart_ts", 0.0)
    quiet_s = now - state.get("moved_at", 0.0)
    hint = fresh_hint(args, state, now)
    status["hint"] = ({"slot": int(hint["slot"]), "at": iso(hint["ts"]),
                       "age_s": round(now - hint["ts"], 1)} if hint else None)
    hinted_s = dict(stalled).get(hint["slot"], 0.0) if hint else 0.0
    reason = None
    if hint and hinted_s > args.hint_stall_s:
        waited_s = now - hint["ts"]
        if quiet_s <= args.hint_quiet_s and waited_s < args.hint_defer_s:
            say(NOTICE, f"slot {hint['slot']} parado há {hinted_s:.0f}s (dica do app); "
                        f"outro slot andou há {quiet_s:.0f}s")
        elif now - last_restart < args.cooldown:
            say(NOTICE, f"slot {hint['slot']} parado há {hinted_s:.0f}s (dica do app); "
                        "reinício recente, aguardando cooldown")
        else:
            reason = (f"slot {hint['slot']} sem progresso há {hinted_s:.0f}s "
                      "(dica do app: narrativa parada)")
            state["hint_used_ts"] = hint["ts"]
    elif worst[0] is not None and worst[1] > args.stall_s:
        if worst[0] != SERVER_KEY and quiet_s <= args.stall_s and worst[1] <= args.hard_stall_s:
            say(NOTICE, f"slot {worst[0]} parado há {worst[1]:.0f}s; outro slot andou há {quiet_s:.0f}s")
        elif now - last_restart < args.cooldown:
            say(NOTICE, f"slot {worst[0]} parado há {worst[1]:.0f}s; reinício recente, aguardando cooldown")
        else:
            reason = (f"/slots sem resposta há {worst[1]:.0f}s" if worst[0] == SERVER_KEY
                      else f"slot {worst[0]} sem progresso há {worst[1]:.0f}s")
    if reason:
        say(WARNING, f"REINICIANDO {args.service}: {reason}")
        if not args.dry_run:
            subprocess.run(["systemctl", "restart", args.service], check=False)
        state["last_restart_ts"] = now
        marks = {}
        status["restarts"] = int(status.get("restarts", 0)) + 1
        status["last_restart"] = {"at": iso(now), "reason": reason,
                                  "dry_run": bool(args.dry_run)}
        status["hint"] = None
    state["marks"] = marks
    save(state_dir / "state.json", state)
    save(state_dir / "status.json", status)
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Watchdog de slots do llama-server (#28280).")
    p.add_argument("--base-url", default="http://127.0.0.1:1234",
                   help="raiz do llama-server, sem /v1 (padrão http://127.0.0.1:1234)")
    p.add_argument("--service", default="llama-server", help="unit a reiniciar")
    p.add_argument("--stall-s", type=float, default=45.0,
                   help="segundos sem progresso até reiniciar (padrão 45)")
    p.add_argument("--hard-stall-s", type=float, default=300.0,
                   help="reinicia mesmo com outro slot andando (padrão 300 s: nenhum prompt "
                        "cabe em 262144 tokens e leva tanto)")
    p.add_argument("--cooldown", type=float, default=180.0,
                   help="intervalo mínimo entre reinícios (padrão 180 s)")
    p.add_argument("--request-timeout", type=float, default=10.0,
                   help="timeout do GET /slots; o servidor só responde entre dois "
                        "lotes de prompt (padrão 10 s)")
    p.add_argument("--hint-file", action="append", default=[], metavar="CAMINHO",
                   help="dica do app (scheduler.stall_hint_path), repetível: um por instância")
    p.add_argument("--hint-max-age", type=float, default=120.0,
                   help="idade máxima da dica em segundos (padrão 120)")
    p.add_argument("--hint-stall-s", type=float, default=20.0,
                   help="com dica, segundos sem progresso no slot dela até reiniciar (padrão 20)")
    p.add_argument("--hint-quiet-s", type=float, default=10.0,
                   help="com dica, espera os outros slots ficarem parados esse tempo (padrão 10)")
    p.add_argument("--hint-defer-s", type=float, default=60.0,
                   help="com dica, reinicia mesmo com outro slot andando depois disso (padrão 60)")
    p.add_argument("--state-dir", default="/var/lib/slot-watchdog")
    p.add_argument("--api-key-file")
    p.add_argument("--env-file", help=".env com LLM_API_KEY (ex.: /home/caio/aidungeon/.env)")
    p.add_argument("--dry-run", action="store_true", help="registra, mas não reinicia")
    return p


def main() -> int:
    args = build_parser().parse_args()
    return check(args, time.time())


if __name__ == "__main__":
    sys.exit(main())
