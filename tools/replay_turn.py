#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
replay_turn.py -- Replay de turnos contra uma instância do backend (staging).

Joga turnos reais pela API, do jeito que o front faz, e mede o que trava o
jogo: 409 na ação seguinte, choices lento, See recusado no meio do turno,
espera dos jobs de fundo na fila e o card que não aparece.

Cada turno:
  1. POST /action (SSE): TTFT, total e tokens. Com --see-during-turn, um
     POST /see sai assim que chega o primeiro token.
  2. No `done`, POST /choices e POST /scene em paralelo (como o Play.jsx).
  3. O turno seguinte sai logo depois do `done`, sem esperar o choices.
No fim: os jobs criados durante o replay (espera na fila e duração) e, com
--expect-card, quanto tempo cada card levou para existir.

Token: mintado com AUTH_USER/AUTH_SECRET do .env, no formato de
backend/app/auth.py, e nunca impresso. Ou --token / env AIDUNGEON_TOKEN.

Apenas biblioteca padrão.
Exit codes: 0 = metas ok · 1 = alguma meta falhou · 2 = erro de uso/acesso.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import json
import os
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
EXIT_OK = 0
EXIT_FAIL = 1
EXIT_USAGE = 2

try:
    sys.stdout.reconfigure(errors="replace")
except Exception:
    pass


# --------------------------------------------------------------------------- #
# token
# --------------------------------------------------------------------------- #

def read_dotenv(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip().removeprefix("export ").strip()
        values[key] = value.strip().strip('"').strip("'")
    return values


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def mint_token(user: str, secret: str, ttl_s: int = 7200) -> str:
    exp = int(time.time()) + ttl_s
    sig = hmac.new(secret.encode("utf-8"), f"{user}.{exp}".encode("utf-8"),
                   hashlib.sha256).hexdigest()
    return ".".join((_b64url(user.encode("utf-8")), _b64url(str(exp).encode("ascii")), sig))


def resolve_token(args: argparse.Namespace) -> str:
    if args.token:
        return args.token
    if os.environ.get("AIDUNGEON_TOKEN"):
        return os.environ["AIDUNGEON_TOKEN"]
    env_path = Path(args.env_file or os.environ.get("AIDUNGEON_ENV_PATH") or REPO_ROOT / ".env")
    dotenv = read_dotenv(env_path)
    user = os.environ.get("AUTH_USER") or dotenv.get("AUTH_USER", "")
    secret = os.environ.get("AUTH_SECRET") or dotenv.get("AUTH_SECRET", "")
    if not user or not secret:
        raise SystemExit(f"sem token: AUTH_USER/AUTH_SECRET ausentes (env ou {env_path})")
    return mint_token(user, secret)


# --------------------------------------------------------------------------- #
# HTTP
# --------------------------------------------------------------------------- #

def _error_code(raw: bytes) -> str:
    try:
        body = json.loads(raw or b"null")
    except ValueError:
        return raw.decode("utf-8", "replace")[:120]
    if isinstance(body, dict):
        err = body.get("error")
        if isinstance(err, dict):
            return str(err.get("code") or err.get("message") or body)[:120]
        return str(body.get("code") or body.get("detail") or body)[:120]
    return str(body)[:120]


class Api:
    def __init__(self, base: str, token: str, timeout: float) -> None:
        self.base = base.rstrip("/")
        self.headers = {"Authorization": f"Bearer {token}",
                        "Content-Type": "application/json"}
        self.timeout = timeout
        # Proxy do ambiente nunca: o alvo é a própria VM (127.0.0.1).
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def call(self, method: str, path: str, body: dict | None = None) -> dict:
        data = None if body is None else json.dumps(body).encode("utf-8")
        req = urllib.request.Request(self.base + path, data=data, method=method,
                                     headers=self.headers)
        started = time.monotonic()
        try:
            with self.opener.open(req, timeout=self.timeout) as resp:
                status, raw = resp.status, resp.read()
        except urllib.error.HTTPError as exc:
            status, raw = exc.code, exc.read()
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            return {"status": None, "elapsed_s": time.monotonic() - started,
                    "error": f"{type(exc).__name__}: {exc}", "body": None}
        elapsed = time.monotonic() - started
        if status >= 400:
            return {"status": status, "elapsed_s": elapsed, "error": _error_code(raw),
                    "body": None}
        try:
            payload = json.loads(raw or b"null")
        except ValueError:
            payload = None
        return {"status": status, "elapsed_s": elapsed, "error": None, "body": payload}

    def stream_action(self, adventure_id: str, type_: str, text: str,
                      on_first_token=None) -> dict:
        body = json.dumps({"type": type_, "text": text}).encode("utf-8")
        req = urllib.request.Request(
            f"{self.base}/api/adventures/{adventure_id}/action", data=body,
            method="POST", headers={**self.headers, "Accept": "text/event-stream"})
        result: dict = {"status": None, "ttft_s": None, "total_s": None, "tokens": 0,
                        "seq": None, "error": None}
        started = time.monotonic()
        try:
            resp = self.opener.open(req, timeout=self.timeout)
        except urllib.error.HTTPError as exc:
            result.update(status=exc.code, error=_error_code(exc.read()),
                          total_s=time.monotonic() - started)
            return result
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            result.update(error=f"{type(exc).__name__}: {exc}",
                          total_s=time.monotonic() - started)
            return result
        result["status"] = resp.status
        event = ""
        with resp:
            for raw in resp:
                line = raw.decode("utf-8", "replace").rstrip("\r\n")
                if line.startswith("event:"):
                    event = line[6:].strip()
                    continue
                if not line.startswith("data:"):
                    continue
                data = json.loads(line[5:].strip() or "null") or {}
                if event == "token":
                    result["tokens"] += 1
                    if result["ttft_s"] is None:
                        result["ttft_s"] = time.monotonic() - started
                        if on_first_token is not None:
                            on_first_token()
                elif event == "done":
                    result["seq"] = (data.get("action") or {}).get("seq")
                elif event == "error":
                    result["error"] = data.get("message")
        result["total_s"] = time.monotonic() - started
        return result


# --------------------------------------------------------------------------- #
# replay
# --------------------------------------------------------------------------- #

def parse_turn(raw: str) -> tuple[str, str]:
    type_, sep, text = raw.partition(":")
    type_ = type_.strip().lower()
    if not sep or type_ not in ("do", "say", "story") or not text.strip():
        raise argparse.ArgumentTypeError(f"turno inválido {raw!r} (use do:texto, say:texto, story:texto)")
    return type_, text.strip()


def _iso_seconds(value: str | None) -> float | None:
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()


def _fmt(seconds: float | None) -> str:
    return "-" if seconds is None else f"{seconds:6.1f}s"


class Replay:
    def __init__(self, api: Api, args: argparse.Namespace) -> None:
        self.api = api
        self.args = args
        self.adv = args.adventure
        self.pool = ThreadPoolExecutor(max_workers=8)
        self.failures: list[str] = []
        self.t0 = time.monotonic()
        self.last_done: float | None = None

    def log(self, message: str) -> None:
        print(f"[{time.monotonic() - self.t0:7.1f}s] {message}", flush=True)

    def fail(self, message: str) -> None:
        self.failures.append(message)
        self.log(f"FALHA: {message}")

    def run(self) -> int:
        state = self.api.call("GET", f"/api/adventures/{self.adv}")
        if state["status"] != 200:
            print(f"aventura inacessível: HTTP {state['status']} {state['error']}")
            return EXIT_USAGE
        adventure = state["body"]
        actions = adventure.get("actions") or []
        last_seq = actions[-1]["seq"] if actions else 0
        known_cards = {c["title"] for c in adventure.get("story_cards") or []}
        self.log(f"aventura {adventure.get('title')!r}: {len(actions)} ações, "
                 f"último seq {last_seq}, {len(known_cards)} cards")
        started_at = time.time() - 2

        pending: list[tuple[str, Future]] = []
        for index, (type_, text) in enumerate(self.args.turn, start=1):
            see: list[Future] = []
            see_seq = last_seq

            def fire_see(seq: int = see_seq, bucket: list = see) -> None:
                bucket.append(self.pool.submit(
                    self.api.call, "POST", f"/api/adventures/{self.adv}/see",
                    {"action_seq": seq}))

            gap = None if self.last_done is None else time.monotonic() - self.last_done
            turn = self.api.stream_action(
                self.adv, type_, text,
                on_first_token=fire_see if self.args.see_during_turn else None)
            gap_note = "" if gap is None else f" (saiu {gap:.1f}s após o done anterior)"
            self.log(f"turno {index} {type_}: HTTP {turn['status']} ttft {_fmt(turn['ttft_s'])} "
                     f"total {_fmt(turn['total_s'])} tokens {turn['tokens']} seq {turn['seq']}"
                     f"{gap_note}" + (f" erro {turn['error']}" if turn["error"] else ""))
            if turn["status"] == 409:
                self.fail(f"turno {index}: 409 {turn['error']}")
            elif turn["status"] != 200 or turn["error"] or turn["seq"] is None:
                self.fail(f"turno {index}: HTTP {turn['status']} {turn['error']}")
            else:
                last_seq = turn["seq"]
                self.last_done = time.monotonic()
                pending.append((f"choices após turno {index}", self.pool.submit(
                    self.api.call, "POST", f"/api/adventures/{self.adv}/choices", {})))
                pending.append((f"scene após turno {index}", self.pool.submit(
                    self.api.call, "POST", f"/api/adventures/{self.adv}/scene", {})))
            pending.extend((f"see durante turno {index}", fut) for fut in see)
            if self.args.see_during_turn and not see:
                self.log(f"turno {index}: sem token, o See não saiu")

        for label, fut in pending:
            self.check_call(label, fut.result())
        if self.args.expect_card:
            self.wait_cards(known_cards)
        self.report_jobs(started_at)
        self.pool.shutdown(wait=True)

        if self.failures:
            print(f"\nRESULTADO: {len(self.failures)} meta(s) falharam")
            for item in self.failures:
                print(f"  - {item}")
            return EXIT_FAIL
        print("\nRESULTADO: todas as metas ok")
        return EXIT_OK

    def check_call(self, label: str, res: dict) -> None:
        detail = f"HTTP {res['status']} em {_fmt(res['elapsed_s'])}"
        if res["error"]:
            detail += f" ({res['error']})"
        if label.startswith("choices") and res["status"] == 200:
            detail += f" {len((res['body'] or {}).get('choices') or [])} opções"
        if label.startswith("scene") and res["status"] == 200:
            backdrop = (res["body"] or {}).get("backdrop") or {}
            detail += f" fundo {backdrop.get('status')}"
        self.log(f"{label}: {detail}")
        if res["status"] == 409:
            self.fail(f"{label}: 409 {res['error']}")
        elif label.startswith("choices"):
            if res["status"] == 200 and res["elapsed_s"] > self.args.choices_max:
                self.fail(f"{label}: {res['elapsed_s']:.1f}s > {self.args.choices_max:.0f}s")
            elif res["status"] not in (200, 503):
                self.fail(f"{label}: HTTP {res['status']} {res['error']}")
        elif res["status"] not in (200, 503):
            self.fail(f"{label}: HTTP {res['status']} {res['error']}")

    def wait_cards(self, known: set[str]) -> None:
        wanted = {name.lower(): name for name in self.args.expect_card}
        found: dict[str, float] = {}
        origin = self.last_done or time.monotonic()
        while time.monotonic() - origin < self.args.card_wait and len(found) < len(wanted):
            state = self.api.call("GET", f"/api/adventures/{self.adv}")
            for card in (state["body"] or {}).get("story_cards") or []:
                title = str(card.get("title") or "")
                for key in wanted:
                    if key in title.lower() and key not in found:
                        found[key] = time.monotonic() - origin
                        when = "já existia" if title in known else f"{found[key]:.1f}s"
                        portrait = "sim" if card.get("portrait_image_id") else "não"
                        self.log(f"card {title!r}: {when}, retrato {portrait}")
            if len(found) < len(wanted):
                time.sleep(3)
        for key, name in wanted.items():
            if key not in found:
                self.fail(f"card {name!r} não apareceu em {self.args.card_wait:.0f}s")

    def report_jobs(self, started_at: float) -> None:
        deadline = time.monotonic() + self.args.jobs_wait
        while True:
            res = self.api.call("GET", f"/api/adventures/{self.adv}/jobs")
            jobs = [j for j in res["body"] or []
                    if (_iso_seconds(j.get("created_at")) or 0) >= started_at]
            open_jobs = [j for j in jobs if j.get("status") in ("pending", "running")]
            if not open_jobs or time.monotonic() > deadline:
                break
            time.sleep(4)
        if not jobs:
            self.log("jobs: nenhum criado durante o replay")
            return
        for job in jobs:
            created = _iso_seconds(job.get("created_at"))
            started = _iso_seconds(job.get("started_at"))
            finished = _iso_seconds(job.get("finished_at"))
            wait = None if created is None or started is None else started - created
            took = None if started is None or finished is None else finished - started
            self.log(f"job {job['type']}: {job['status']} fila {_fmt(wait)} execução {_fmt(took)}"
                     + (f" erro {job['error']}" if job.get("error") else ""))
            if job["type"] == "scene_backdrop":
                if wait is None:
                    self.fail(f"backdrop {job['id']} não começou em {self.args.jobs_wait:.0f}s")
                elif wait > self.args.backdrop_max_wait:
                    self.fail(f"backdrop esperou {wait:.0f}s na fila "
                              f"(meta {self.args.backdrop_max_wait:.0f}s)")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Replay de turnos pela API (staging) com as metas de fluidez.")
    p.add_argument("--base", default="http://127.0.0.1:2123",
                   help="URL do backend (padrão: staging na VM, 127.0.0.1:2123)")
    p.add_argument("--adventure", required=True, help="id da aventura")
    p.add_argument("--turn", action="append", type=parse_turn, required=True,
                   metavar="TIPO:TEXTO", help="turno do jogador (repetível): do:..., say:..., story:...")
    p.add_argument("--see-during-turn", action="store_true",
                   help="dispara POST /see no primeiro token de cada turno")
    p.add_argument("--expect-card", action="append", default=[], metavar="NOME",
                   help="card que deve existir depois do replay (repetível)")
    p.add_argument("--card-wait", type=float, default=60.0,
                   help="espera máxima por card, em segundos (padrão 60)")
    p.add_argument("--choices-max", type=float, default=16.0,
                   help="choices com 200 acima disso falha (padrão 16 s: prazo de 15 s + rede)")
    p.add_argument("--backdrop-max-wait", type=float, default=60.0,
                   help="espera máxima de um backdrop na fila (padrão 60 s)")
    p.add_argument("--jobs-wait", type=float, default=240.0,
                   help="quanto esperar os jobs do replay terminarem (padrão 240 s)")
    p.add_argument("--timeout", type=float, default=300.0,
                   help="timeout de leitura HTTP por chamada (padrão 300 s)")
    p.add_argument("--token", help="Bearer pronto (senão mintado do .env)")
    p.add_argument("--env-file", help="caminho do .env (padrão: AIDUNGEON_ENV_PATH ou <repo>/.env)")
    return p


def main() -> int:
    args = build_parser().parse_args()
    try:
        token = resolve_token(args)
    except SystemExit as exc:
        print(exc)
        return EXIT_USAGE
    return Replay(Api(args.base, token, args.timeout), args).run()


if __name__ == "__main__":
    sys.exit(main())
