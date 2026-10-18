#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
repro_slot_hang.py -- Reproduz o livelock do llama.cpp #28280 (slot fixado +
prompt cache em modelo híbrido) e confere se uma configuração escapa dele.

Sintoma (relatos no #28280): o slot fica `is_processing` com
`n_prompt_tokens_processed` parado e o log repete "erasing old context
checkpoint" na mesma posição. Pede 2+ slots e prompt cache.

Modos:
  bug  um slot fixado alterna uma conversa longa (prefixo crescente) e duas
       curtas, todas com cache_prompt=true, enquanto outro slot trabalha.
  app  o tráfego do app depois da G1: narrativa no slot 0 com cache e
       auxiliares nos outros slots sem cache (mapa do contrato C1), mais um
       cliente externo sem id_slot (open-webui, hermes) com cache.

Detecção: uma thread lê GET /slots a cada 2 s. Slot ocupado sem progresso
(mesma tarefa, sem passar do maior n_prompt_tokens_processed nem do maior
n_decoded; regra de slot_watchdog.advance) por --stall-s conta como travado:
o script fecha a conexão e mede se o slot solta sozinho.

API key: --api-key > env LLM_API_KEY > --env-file / <repo>/.env. Nunca impressa.
Apenas biblioteca padrão; precisa de slot_watchdog.py na mesma pasta.
Exit codes: 0 = nada travou · 3 = travou · 1 = erro de uso/conexão.
"""

from __future__ import annotations

import argparse
import http.client
import json
import os
import random
import socket
import sys
import threading
import time
import urllib.parse
from pathlib import Path

from slot_watchdog import advance

REPO_ROOT = Path(__file__).resolve().parent.parent
EXIT_OK = 0
EXIT_ERROR = 1
EXIT_STALL = 3

WORDS = (
    "a cidade dorme sob a chuva enquanto o vento empurra as cortinas pesadas "
    "do terraço e a música distante pulsa contra as janelas de vidro escuro "
    "ela atravessa o salão devagar com o vestido vermelho arrastando no chão "
    "o médico observa em silêncio o copo vazio na mão e a luz baixa da suíte "
    "the guard checks the gate twice before the convoy rolls into the yard"
).split()

try:
    sys.stdout.reconfigure(errors="replace")
except Exception:
    pass


def read_dotenv_key(path: Path, name: str) -> str:
    if not path.is_file():
        return ""
    for line in path.read_text(encoding="utf-8").splitlines():
        key, sep, value = line.strip().partition("=")
        if sep and key.strip().removeprefix("export ").strip() == name:
            return value.strip().strip('"').strip("'")
    return ""


def filler(rng: random.Random, n_words: int) -> str:
    return " ".join(rng.choice(WORDS) for _ in range(n_words))


class Stalled(Exception):
    pass


class Server:
    def __init__(self, base_url: str, api_key: str, timeout: float) -> None:
        url = urllib.parse.urlparse(base_url)
        self.host = url.hostname or "127.0.0.1"
        self.port = url.port or 80
        self.headers = {"Content-Type": "application/json"}
        if api_key:
            self.headers["Authorization"] = f"Bearer {api_key}"
        self.timeout = timeout

    def get(self, path: str) -> object:
        conn = http.client.HTTPConnection(self.host, self.port, timeout=10)
        try:
            conn.request("GET", path, headers=self.headers)
            resp = conn.getresponse()
            body = resp.read()
            if resp.status != 200:
                raise RuntimeError(f"GET {path}: HTTP {resp.status} {body[:200]!r}")
            return json.loads(body)
        finally:
            conn.close()

    def open_chat(self, payload: dict) -> http.client.HTTPConnection:
        conn = http.client.HTTPConnection(self.host, self.port, timeout=self.timeout)
        conn.request("POST", "/v1/chat/completions", body=json.dumps(payload),
                     headers=self.headers)
        return conn


class SlotWatch(threading.Thread):
    """Lê /slots e marca há quanto tempo cada slot ocupado não progride."""

    def __init__(self, server: Server, interval: float = 2.0) -> None:
        super().__init__(daemon=True)
        self.server = server
        self.interval = interval
        self.lock = threading.Lock()
        self.marks: dict[int, dict] = {}
        self.snapshot: dict[int, dict] = {}
        self.stop = threading.Event()

    def run(self) -> None:
        while not self.stop.is_set():
            try:
                slots = self.server.get("/slots")
            except Exception:
                slots = []
            now = time.monotonic()
            with self.lock:
                for slot in slots or []:
                    sid = slot["id"]
                    self.snapshot[sid] = slot
                    self.marks[sid] = advance(self.marks.get(sid), slot, now)
            self.stop.wait(self.interval)

    def stalled_for(self, sid: int) -> float:
        with self.lock:
            slot = self.snapshot.get(sid) or {}
            if not slot.get("is_processing"):
                return 0.0
            now = time.monotonic()
            return now - (self.marks.get(sid) or {}).get("since", now)

    def busy(self, sid: int) -> bool:
        with self.lock:
            return bool((self.snapshot.get(sid) or {}).get("is_processing"))

    def describe(self, sid: int) -> dict:
        with self.lock:
            slot = dict(self.snapshot.get(sid) or {})
        slot.pop("params", None)
        return slot


class Repro:
    def __init__(self, server: Server, args: argparse.Namespace) -> None:
        self.server = server
        self.args = args
        self.watch = SlotWatch(server)
        self.rng = random.Random(args.seed)
        self.t0 = time.monotonic()
        self.stalls: list[str] = []
        self.results: list[dict] = []
        self.results_lock = threading.Lock()

    def log(self, message: str) -> None:
        print(f"[{time.monotonic() - self.t0:7.1f}s] {message}", flush=True)

    def chat(self, label: str, messages: list[dict], *, slot: int | None,
             cache: bool, max_tokens: int = 8) -> dict:
        payload: dict = {
            "messages": messages, "max_tokens": max_tokens, "temperature": 0.0,
            "stream": False, "cache_prompt": cache,
            "chat_template_kwargs": {"enable_thinking": False},
        }
        if slot is not None:
            payload["id_slot"] = slot
        started = time.monotonic()
        conn = self.server.open_chat(payload)
        box: dict = {}

        def read() -> None:
            try:
                resp = conn.getresponse()
                box["status"] = resp.status
                box["body"] = resp.read()
            except Exception as exc:
                box["error"] = exc

        reader = threading.Thread(target=read, daemon=True)
        reader.start()
        while reader.is_alive():
            reader.join(1.0)
            if slot is not None and self.watch.stalled_for(slot) > self.args.stall_s:
                self.on_stall(label, slot, conn, started)
                raise Stalled(label)
        conn.close()
        elapsed = time.monotonic() - started
        if "error" in box or box.get("status") != 200:
            raise RuntimeError(f"{label}: {box.get('error') or box.get('status')} "
                               f"{(box.get('body') or b'')[:200]!r}")
        timings = json.loads(box["body"]).get("timings") or {}
        result = {"label": label, "slot": slot, "elapsed": elapsed,
                  "prompt_n": timings.get("prompt_n"), "cache_n": timings.get("cache_n")}
        with self.results_lock:
            self.results.append(result)
        self.log(f"{label:<22} slot {slot if slot is not None else '-'} {elapsed:6.1f}s "
                 f"prompt_n {result['prompt_n']} cache_n {result['cache_n']}")
        return result

    def on_stall(self, label: str, slot: int, conn: http.client.HTTPConnection,
                 started: float) -> None:
        detail = self.watch.describe(slot)
        self.stalls.append(label)
        self.log(f"TRAVOU: {label} no slot {slot} após {time.monotonic() - started:.0f}s "
                 f"sem progresso há {self.watch.stalled_for(slot):.0f}s: {json.dumps(detail)}")
        try:
            if conn.sock is not None:
                conn.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        conn.close()
        cut = time.monotonic()
        while time.monotonic() - cut < 30:
            if not self.watch.busy(slot):
                self.log(f"slot {slot} soltou {time.monotonic() - cut:.1f}s depois de fechar a conexão")
                return
            time.sleep(1)
        self.log(f"slot {slot} continua preso 30s depois de fechar a conexão: precisa reiniciar")

    # ------------------------------------------------------------ cenários
    def conversation(self, n_turns: int, words: int, tag: str) -> list[dict]:
        rng = random.Random(f"{self.args.seed}-{tag}")
        messages = [{"role": "system", "content": f"[{tag}] You are a narrator. " + filler(rng, 60)}]
        for _ in range(n_turns):
            messages.append({"role": "user", "content": filler(rng, words)})
            messages.append({"role": "assistant", "content": filler(rng, words // 2)})
        messages.append({"role": "user", "content": filler(rng, 40)})
        return messages

    def background(self, slot: int, stop: threading.Event) -> None:
        turn = 0
        while not stop.is_set():
            turn += 1
            try:
                self.chat(f"slot{slot} lote {turn}", self.conversation(8 + turn % 5, 220, f"batch{turn % 3}"),
                          slot=slot, cache=True)
            except Stalled:
                stop.set()
            except Exception as exc:
                self.log(f"slot{slot} lote {turn}: {exc}")
                stop.wait(2)

    def run_bug(self, total_slots: int) -> None:
        slot = self.args.slot
        stop = threading.Event()
        workers = []
        if total_slots > 1:
            other = (slot + 1) % total_slots
            workers.append(threading.Thread(target=self.background, args=(other, stop), daemon=True))
        for worker in workers:
            worker.start()
        try:
            for rnd in range(1, self.args.rounds + 1):
                if stop.is_set():
                    break
                self.chat(f"r{rnd} longa", self.conversation(self.args.long_turns + rnd, 300, "agent"),
                          slot=slot, cache=True)
                self.chat(f"r{rnd} curta A", self.conversation(4, 250, "chatA"), slot=slot, cache=True)
                self.chat(f"r{rnd} curta B", self.conversation(5, 250, "chatB"), slot=slot, cache=True)
        finally:
            stop.set()
            for worker in workers:
                worker.join(timeout=self.args.stall_s + 40)

    def run_app(self, total_slots: int) -> None:
        aux = {"choices": 1, "chronicle": 2, "summary": 2, "image_prompt": 3}
        if total_slots == 3:
            aux = {"choices": 1, "chronicle": 2, "summary": 2, "image_prompt": 2}
        elif total_slots == 2:
            aux = dict.fromkeys(aux, 1)
        elif total_slots < 2:
            aux = dict.fromkeys(aux, None)
        for rnd in range(1, self.args.rounds + 1):
            self.chat(f"r{rnd} narrativa", self.conversation(self.args.long_turns + rnd, 300, "story"),
                      slot=0 if total_slots > 1 else None, cache=True, max_tokens=48)
            threads = [
                threading.Thread(target=self._safe, args=(f"r{rnd} {role}", self.conversation(3, 200, role),
                                                          slot), daemon=True)
                for role, slot in aux.items()
            ]
            if self.args.external:
                threads.append(threading.Thread(target=self._external, args=(rnd,), daemon=True))
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
            if self.stalls:
                return

    def _safe(self, label: str, messages: list[dict], slot: int | None) -> None:
        try:
            self.chat(label, messages, slot=slot, cache=False)
        except Stalled:
            pass
        except Exception as exc:
            self.log(f"{label}: {exc}")

    def _external(self, rnd: int) -> None:
        try:
            self.chat(f"r{rnd} externo", self.conversation(6 + rnd % 4, 220, f"webui{rnd % 2}"),
                      slot=None, cache=True)
        except Exception as exc:
            self.log(f"r{rnd} externo: {exc}")

    def run(self) -> int:
        try:
            props = self.server.get("/props")
        except Exception as exc:
            print(f"servidor inacessível: {exc}")
            return EXIT_ERROR
        total = int(props.get("total_slots") or 1)
        self.log(f"build {props.get('build_info')} total_slots {total} modo {self.args.mode}")
        if self.args.mode == "bug" and self.args.slot >= total:
            print(f"--slot {self.args.slot} não existe (total_slots {total})")
            return EXIT_ERROR
        self.watch.start()
        try:
            if self.args.mode == "bug":
                self.run_bug(total)
            else:
                self.run_app(total)
        except Stalled:
            pass
        except Exception as exc:
            self.log(f"erro: {exc}")
            if not self.stalls:
                return EXIT_ERROR
        finally:
            self.watch.stop.set()
        worst = max((r["elapsed"] for r in self.results), default=0.0)
        print(f"\n{len(self.results)} requisições, pior {worst:.1f}s, travamentos {len(self.stalls)}")
        return EXIT_STALL if self.stalls else EXIT_OK


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Repro do livelock llama.cpp #28280.")
    p.add_argument("--base-url", default="http://127.0.0.1:1234",
                   help="raiz do llama-server, sem /v1 (padrão http://127.0.0.1:1234)")
    p.add_argument("--mode", choices=("bug", "app"), default="bug")
    p.add_argument("--slot", type=int, default=0, help="slot fixado no modo bug (padrão 0)")
    p.add_argument("--rounds", type=int, default=20, help="rodadas (padrão 20)")
    p.add_argument("--long-turns", type=int, default=30,
                   help="turnos iniciais da conversa longa, ~450 tokens cada (padrão 30)")
    p.add_argument("--stall-s", type=float, default=45.0,
                   help="segundos sem progresso que contam como travado (padrão 45)")
    p.add_argument("--external", action="store_true",
                   help="modo app: cliente externo sem id_slot com cache a cada rodada")
    p.add_argument("--timeout", type=float, default=600.0, help="timeout de socket (padrão 600 s)")
    p.add_argument("--seed", type=int, default=7)
    p.add_argument("--api-key", help="override pontual (senão env LLM_API_KEY ou .env)")
    p.add_argument("--env-file", help="caminho do .env (padrão AIDUNGEON_ENV_PATH ou <repo>/.env)")
    return p


def main() -> int:
    args = build_parser().parse_args()
    env_path = Path(args.env_file or os.environ.get("AIDUNGEON_ENV_PATH") or REPO_ROOT / ".env")
    api_key = args.api_key or os.environ.get("LLM_API_KEY") or read_dotenv_key(env_path, "LLM_API_KEY")
    return Repro(Server(args.base_url, api_key, args.timeout), args).run()


if __name__ == "__main__":
    sys.exit(main())
