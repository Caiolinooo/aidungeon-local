#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
bench_slots.py -- Mede o llama-server nos papéis do app (contrato C1) contra
as metas da seção 2 do plano.

Cenários, cada um repetido --repeat vezes (p50/p95):
  pp     prompt longo sem cache num slot auxiliar: prompt tok/s do servidor.
  frio   narrativa inteira reprocessada no slot 0 (o que o app faz hoje
         quando o topo do prompt muda): TTFT.
  solo   narrativa só acrescentando turnos no slot 0 (cache aproveitado):
         TTFT e tok/s de geração.
  carga  a narrativa do "solo" com --aux chamadas sem cache ao mesmo tempo
         nos slots do C1 (choices, chronicle, image_prompt): TTFT e tok/s da
         narrativa e latência das auxiliares.

Com nvidia-smi no PATH, registra o pico de memória da GPU em cada cenário.
Os tok/s vêm dos `timings` do servidor; sem eles, da contagem de chunks.

API key: --api-key > env LLM_API_KEY > --env-file / <repo>/.env. Nunca impressa.
Apenas biblioteca padrão.
Exit codes: 0 = metas ok · 4 = alguma meta falhou · 1 = erro de uso/conexão.
"""

from __future__ import annotations

import argparse
import http.client
import json
import os
import random
import shutil
import statistics
import subprocess
import sys
import threading
import time
import urllib.parse
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
EXIT_OK = 0
EXIT_ERROR = 1
EXIT_TARGETS = 4
TOKENS_PER_WORD = 1.25

WORDS = (
    "a cidade dorme sob a chuva enquanto o vento empurra as cortinas pesadas "
    "do terraço e a música distante pulsa contra as janelas de vidro escuro "
    "ela atravessa o salão devagar com o vestido vermelho arrastando no chão "
    "o médico observa em silêncio o copo vazio na mão e a luz baixa da suíte "
    "os guardas conferem o portão duas vezes antes de o comboio entrar no pátio"
).split()

SYSTEM = (
    "Você é o narrador de uma aventura interativa em português do Brasil. "
    "Continue a história em segunda pessoa, com prosa viva, diálogos e "
    "consequências para as ações do jogador. Nunca decida o que o jogador diz."
)

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


def text(rng: random.Random, tokens: int) -> str:
    return " ".join(rng.choice(WORDS) for _ in range(max(1, int(tokens / TOKENS_PER_WORD))))


def pct(values: list[float], q: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, max(0, round(q * (len(ordered) - 1))))]


def slot_map(total: int) -> dict[str, int | None]:
    if total >= 4:
        return {"narrative": 0, "choices": 1, "chronicle": 2, "image_prompt": 3}
    if total == 3:
        return {"narrative": 0, "choices": 1, "chronicle": 2, "image_prompt": 2}
    if total == 2:
        return {"narrative": 0, "choices": 1, "chronicle": 1, "image_prompt": 1}
    return dict.fromkeys(("narrative", "choices", "chronicle", "image_prompt"))


class Server:
    def __init__(self, base_url: str, api_key: str, timeout: float) -> None:
        url = urllib.parse.urlparse(base_url)
        self.host = url.hostname or "127.0.0.1"
        self.port = url.port or 80
        self.headers = {"Content-Type": "application/json"}
        if api_key:
            self.headers["Authorization"] = f"Bearer {api_key}"
        self.timeout = timeout

    def get(self, path: str) -> dict:
        conn = http.client.HTTPConnection(self.host, self.port, timeout=15)
        try:
            conn.request("GET", path, headers=self.headers)
            resp = conn.getresponse()
            body = resp.read()
            if resp.status != 200:
                raise RuntimeError(f"GET {path}: HTTP {resp.status} {body[:200]!r}")
            return json.loads(body)
        finally:
            conn.close()

    def chat(self, messages: list[dict], *, slot: int | None, cache: bool, max_tokens: int,
             stream: bool) -> dict:
        payload: dict = {
            "messages": messages, "max_tokens": max_tokens, "temperature": 0.8,
            "stream": stream, "cache_prompt": cache,
            "chat_template_kwargs": {"enable_thinking": False},
        }
        if slot is not None:
            payload["id_slot"] = slot
        conn = http.client.HTTPConnection(self.host, self.port, timeout=self.timeout)
        started = time.monotonic()
        try:
            conn.request("POST", "/v1/chat/completions", body=json.dumps(payload),
                         headers=self.headers)
            resp = conn.getresponse()
            if resp.status != 200:
                raise RuntimeError(f"HTTP {resp.status} {resp.read()[:200]!r}")
            if not stream:
                body = json.loads(resp.read())
                result = {"timings": body.get("timings") or {}, "ttft": None,
                          "text": ((body.get("choices") or [{}])[0].get("message") or {}).get("content") or ""}
            else:
                result = self._read_stream(resp, started)
        finally:
            conn.close()
        result["elapsed"] = time.monotonic() - started
        return result

    @staticmethod
    def _read_stream(resp: http.client.HTTPResponse, started: float) -> dict:
        ttft = None
        chunks = 0
        parts: list[str] = []
        timings: dict = {}
        first = last = None
        for raw in resp:
            line = raw.decode("utf-8", "replace").strip()
            if not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if data == "[DONE]":
                break
            try:
                event = json.loads(data)
            except ValueError:
                continue
            timings = event.get("timings") or timings
            for choice in event.get("choices") or []:
                piece = (choice.get("delta") or {}).get("content") or ""
                if piece:
                    now = time.monotonic()
                    if ttft is None:
                        ttft = now - started
                        first = now
                    last = now
                    chunks += 1
                    parts.append(piece)
        if not timings.get("predicted_per_second") and first and last and last > first:
            timings = dict(timings, predicted_n=chunks,
                           predicted_per_second=(chunks - 1) / (last - first))
        return {"ttft": ttft, "timings": timings, "text": "".join(parts)}


class VramPeak(threading.Thread):
    """Pico de memória da GPU (MiB) via nvidia-smi enquanto o cenário roda."""

    def __init__(self) -> None:
        super().__init__(daemon=True)
        self.binary = shutil.which("nvidia-smi")
        self.peak: int | None = None
        self.stop = threading.Event()

    def sample(self) -> int | None:
        if not self.binary:
            return None
        try:
            out = subprocess.run([self.binary, "--query-gpu=memory.used",
                                  "--format=csv,noheader,nounits"],
                                 capture_output=True, text=True, timeout=5).stdout
            return sum(int(v) for v in out.split())
        except (OSError, ValueError, subprocess.SubprocessError):
            return None

    def run(self) -> None:
        while not self.stop.is_set():
            used = self.sample()
            if used is not None:
                self.peak = max(self.peak or 0, used)
            self.stop.wait(0.5)


class Bench:
    def __init__(self, server: Server, args: argparse.Namespace) -> None:
        self.server = server
        self.args = args
        self.rng = random.Random(args.seed)
        self.report: dict = {}

    def narrative_base(self) -> list[dict]:
        rng = random.Random(f"{self.args.seed}-story")
        return [{"role": "system", "content": SYSTEM},
                {"role": "user", "content": text(rng, self.args.narrative_tokens)}]

    def aux_messages(self, role: str) -> list[dict]:
        return [{"role": "system", "content": f"Tarefa auxiliar: {role}. Responda curto."},
                {"role": "user", "content": text(self.rng, self.args.aux_tokens)}]

    def scenario(self, name: str, body) -> None:
        vram = VramPeak()
        vram.start()
        started = time.monotonic()
        try:
            data = body()
        finally:
            vram.stop.set()
            vram.join()
        data["vram_peak_mib"] = vram.peak
        data["wall_s"] = round(time.monotonic() - started, 1)
        self.report[name] = data
        print(f"\n[{name}] {json.dumps(data, ensure_ascii=False)}", flush=True)

    def run_pp(self) -> dict:
        slot = slot_map(self.total)["image_prompt"]
        speeds, sizes = [], []
        for i in range(self.args.repeat):
            rng = random.Random(f"{self.args.seed}-pp-{i}")
            msgs = [{"role": "user", "content": text(rng, self.args.pp_tokens)}]
            res = self.server.chat(msgs, slot=slot, cache=False, max_tokens=1, stream=False)
            t = res["timings"]
            speeds.append(float(t.get("prompt_per_second") or 0.0))
            sizes.append(int(t.get("prompt_n") or 0))
            print(f"  pp {i + 1}: {sizes[-1]} tokens a {speeds[-1]:.0f} tok/s", flush=True)
        return {"slot": slot, "prompt_n": int(statistics.median(sizes)),
                "prompt_tok_s_p50": round(pct(speeds, 0.5), 1)}

    def run_cold(self) -> dict:
        slot = slot_map(self.total)["narrative"]
        ttfts, speeds = [], []
        for i in range(self.args.repeat):
            msgs = self.narrative_base() + [{"role": "user", "content": f"[{i}] " + text(self.rng, 40)}]
            res = self.server.chat(msgs, slot=slot, cache=False, max_tokens=8, stream=True)
            ttfts.append(res["ttft"] or res["elapsed"])
            speeds.append(float(res["timings"].get("prompt_per_second") or 0.0))
            print(f"  frio {i + 1}: ttft {ttfts[-1]:.2f}s "
                  f"({res['timings'].get('prompt_n')} tokens a {speeds[-1]:.0f} tok/s)", flush=True)
        return {"slot": slot, "ttft_p50": round(pct(ttfts, 0.5), 2), "ttft_p95": round(pct(ttfts, 0.95), 2),
                "prompt_tok_s_p50": round(pct(speeds, 0.5), 1)}

    def narrate(self, history: list[dict], turn: int) -> dict:
        history.append({"role": "user", "content": f"> ação {turn}: " + text(self.rng, 30)})
        res = self.server.chat(history, slot=slot_map(self.total)["narrative"], cache=True,
                               max_tokens=self.args.gen_tokens, stream=True)
        history.append({"role": "assistant", "content": res["text"] or "..."})
        return res

    def run_solo(self, history: list[dict]) -> dict:
        ttfts, speeds, cached = [], [], []
        for i in range(self.args.repeat):
            res = self.narrate(history, i)
            t = res["timings"]
            ttfts.append(res["ttft"] or res["elapsed"])
            speeds.append(float(t.get("predicted_per_second") or 0.0))
            cached.append(int(t.get("cache_n") or 0))
            print(f"  solo {i + 1}: ttft {ttfts[-1]:.2f}s gen {speeds[-1]:.1f} tok/s "
                  f"prompt_n {t.get('prompt_n')} cache_n {t.get('cache_n')}", flush=True)
        return {"ttft_p50": round(pct(ttfts, 0.5), 2), "ttft_p95": round(pct(ttfts, 0.95), 2),
                "gen_tok_s_p50": round(pct(speeds, 0.5), 1), "cache_n_p50": int(statistics.median(cached))}

    def run_load(self, history: list[dict]) -> dict:
        roles = ["choices", "chronicle", "image_prompt"][: self.args.aux]
        slots = slot_map(self.total)
        ttfts, speeds = [], []
        aux_lat: dict[str, list[float]] = {role: [] for role in roles}
        errors: list[str] = []
        for i in range(self.args.repeat):
            box: dict = {}

            def aux(role: str) -> None:
                try:
                    res = self.server.chat(self.aux_messages(role), slot=slots[role], cache=False,
                                           max_tokens=self.args.aux_gen, stream=False)
                    aux_lat[role].append(res["elapsed"])
                except Exception as exc:
                    errors.append(f"{role}: {exc}")

            def narrative() -> None:
                try:
                    box["res"] = self.narrate(history, 100 + i)
                except Exception as exc:
                    errors.append(f"narrativa: {exc}")

            threads = [threading.Thread(target=narrative, daemon=True)]
            threads += [threading.Thread(target=aux, args=(role,), daemon=True) for role in roles]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
            res = box.get("res")
            if res:
                ttfts.append(res["ttft"] or res["elapsed"])
                speeds.append(float(res["timings"].get("predicted_per_second") or 0.0))
                print(f"  carga {i + 1}: ttft {ttfts[-1]:.2f}s gen {speeds[-1]:.1f} tok/s | "
                      + " ".join(f"{r} {aux_lat[r][-1]:.1f}s" for r in roles if aux_lat[r]), flush=True)
        out = {"aux": roles, "ttft_p50": round(pct(ttfts, 0.5) or 0, 2),
               "ttft_p95": round(pct(ttfts, 0.95) or 0, 2),
               "gen_tok_s_p50": round(pct(speeds, 0.5) or 0, 1), "errors": errors}
        for role, values in aux_lat.items():
            out[f"{role}_p50"] = round(pct(values, 0.5) or 0, 1)
            out[f"{role}_p95"] = round(pct(values, 0.95) or 0, 1)
        return out

    def targets(self) -> list[tuple[str, bool]]:
        r = self.report
        checks = []
        if "frio" in r:
            checks += [("narrativa TTFT p50 <= 3 s reprocessando o prompt", r["frio"]["ttft_p50"] <= 3.0),
                       ("narrativa TTFT p95 <= 6 s reprocessando o prompt", r["frio"]["ttft_p95"] <= 6.0)]
        if "solo" in r:
            checks += [("narrativa TTFT p50 <= 3 s (solo)", r["solo"]["ttft_p50"] <= 3.0),
                       ("narrativa TTFT p95 <= 6 s (solo)", r["solo"]["ttft_p95"] <= 6.0),
                       ("narrativa >= 17 tok/s sozinha", r["solo"]["gen_tok_s_p50"] >= 17.0)]
        if "carga" in r:
            checks += [(f"narrativa >= 13 tok/s com {len(r['carga']['aux'])} auxiliares",
                        r["carga"]["gen_tok_s_p50"] >= 13.0),
                       ("narrativa TTFT p95 <= 6 s com auxiliares", r["carga"]["ttft_p95"] <= 6.0),
                       ("sem erro com auxiliares", not r["carga"]["errors"])]
            if "choices" in r["carga"]["aux"]:
                checks += [("choices p50 <= 6 s com carga", r["carga"]["choices_p50"] <= 6.0),
                           ("choices p95 <= 10 s com carga", r["carga"]["choices_p95"] <= 10.0)]
        return checks

    def run(self) -> int:
        try:
            props = self.server.get("/props")
        except Exception as exc:
            print(f"servidor inacessível: {exc}")
            return EXIT_ERROR
        self.total = int(props.get("total_slots") or 1)
        idle = VramPeak().sample()
        self.report["server"] = {"build": props.get("build_info"), "total_slots": self.total,
                                 "n_ctx": (props.get("default_generation_settings") or {}).get("n_ctx"),
                                 "vram_idle_mib": idle}
        print(f"build {props.get('build_info')} total_slots {self.total} vram_idle {idle} MiB", flush=True)
        wanted = self.args.scenarios.split(",")
        try:
            if "pp" in wanted:
                self.scenario("pp", self.run_pp)
            if "frio" in wanted:
                self.scenario("frio", self.run_cold)
            history = self.narrative_base()
            if "solo" in wanted or "carga" in wanted:
                self.narrate(history, -1)
            if "solo" in wanted:
                self.scenario("solo", lambda: self.run_solo(history))
            if "carga" in wanted:
                self.scenario("carga", lambda: self.run_load(history))
        except Exception as exc:
            print(f"erro: {exc}")
            return EXIT_ERROR
        checks = self.targets()
        print()
        for label, ok in checks:
            print(f"  {'ok   ' if ok else 'FALHA'} {label}")
        if self.args.json:
            Path(self.args.json).write_text(json.dumps(
                dict(self.report, targets={label: ok for label, ok in checks}),
                indent=2, ensure_ascii=False), encoding="utf-8")
        return EXIT_OK if all(ok for _, ok in checks) else EXIT_TARGETS


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Benchmark do llama-server nos papéis do app (C1).")
    p.add_argument("--base-url", default="http://127.0.0.1:1234",
                   help="raiz do llama-server, sem /v1 (padrão http://127.0.0.1:1234)")
    p.add_argument("--scenarios", default="pp,frio,solo,carga",
                   help="lista separada por vírgula (padrão pp,frio,solo,carga)")
    p.add_argument("--repeat", type=int, default=5, help="repetições por cenário (padrão 5)")
    p.add_argument("--pp-tokens", type=int, default=12000, help="tamanho do prompt do pp (padrão 12000)")
    p.add_argument("--narrative-tokens", type=int, default=4000,
                   help="prompt inicial da narrativa (padrão 4000, o tamanho dos turnos reais)")
    p.add_argument("--gen-tokens", type=int, default=200, help="tokens da narrativa (padrão 200)")
    p.add_argument("--aux", type=int, default=3, choices=(0, 1, 2, 3),
                   help="auxiliares em paralelo no cenário carga (padrão 3)")
    p.add_argument("--aux-tokens", type=int, default=3000, help="prompt das auxiliares (padrão 3000)")
    p.add_argument("--aux-gen", type=int, default=120, help="tokens das auxiliares (padrão 120)")
    p.add_argument("--timeout", type=float, default=300.0)
    p.add_argument("--seed", type=int, default=11)
    p.add_argument("--json", help="grava o relatório neste arquivo")
    p.add_argument("--api-key", help="override pontual (senão env LLM_API_KEY ou .env)")
    p.add_argument("--env-file", help="caminho do .env (padrão AIDUNGEON_ENV_PATH ou <repo>/.env)")
    return p


def main() -> int:
    args = build_parser().parse_args()
    env_path = Path(args.env_file or os.environ.get("AIDUNGEON_ENV_PATH") or REPO_ROOT / ".env")
    api_key = args.api_key or os.environ.get("LLM_API_KEY") or read_dotenv_key(env_path, "LLM_API_KEY")
    return Bench(Server(args.base_url, api_key, args.timeout), args).run()


if __name__ == "__main__":
    sys.exit(main())
