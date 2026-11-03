#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
bench_generation_vram.py -- Gera uma imagem ou um clipe e mede VRAM/RAM.

Modos:
  flux      grafo Klein real via image_client._generate_comfyui, 1024x1024,
            pesos inalterados. Roda no processo local contra a config efetiva
            do banco (AIDUNGEON_DB_PATH ou data/aidungeon.db). Não grava PNG.
  portrait  POST /api/adventures/{aid}/cards/{cid}/portrait e polling do job.
            Exige --card.
  clip      POST /api/adventures/{aid}/clips com prompt genérico + id único
            e polling do job. Exige --image (PNG de origem da aventura).

Durante a geração amostra nvidia-smi (memory.used, memory.free) e
/proc/meminfo (MemAvailable, SwapTotal, SwapFree). A saída JSON traz só
métricas, dimensões, bytes e status/erro sanitizado — nunca prompts, tokens,
chaves ou corpo de resposta.

Exit codes: 0 = ok e vram_free_min_mib >= 900 · 1 = geração/job falhou ou
VRAM livre < 900 · 2 = uso, acesso ou amostra impossível.
Apenas biblioteca padrão, exceto o cliente httpx já exigido por image_client
no modo flux.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

TOOLS_DIR = Path(__file__).resolve().parent
REPO_ROOT = TOOLS_DIR.parent
BACKEND_DIR = REPO_ROOT / "backend"
if str(TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(TOOLS_DIR))

import replay_turn  # noqa: E402

EXIT_OK = 0
EXIT_FAIL = 1
EXIT_USAGE = 2
VRAM_FREE_FLOOR_MIB = 900
FLUX_PROMPT = "wide still scene, natural light, no text"
CLIP_PROMPT = "gentle camera drift, natural motion, no text"
TERMINAL = ("done", "error", "canceled")
_SENSITIVE = ("token", "bearer", "api_key", "authorization", "prompt", "secret")

try:
    sys.stdout.reconfigure(errors="replace")
except Exception:
    pass


def parse_nvidia_smi(text: str) -> tuple[int, int]:
    """Soma used e free de cada GPU. CSV noheader nounits, uma GPU por linha."""
    used_total = 0
    free_total = 0
    rows = 0
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        parts = [part.strip() for part in line.split(",")]
        if len(parts) < 2:
            raise ValueError("nvidia-smi: linha sem used,free")
        used_total += int(parts[0])
        free_total += int(parts[1])
        rows += 1
    if rows == 0:
        raise ValueError("nvidia-smi: saída vazia")
    return used_total, free_total


def parse_meminfo(text: str) -> tuple[int, int]:
    """MemAvailable e swap usado, em MiB. SwapTotal/SwapFree vêm em kB."""
    values: dict[str, int] = {}
    for raw in text.splitlines():
        key, sep, rest = raw.partition(":")
        if not sep:
            continue
        number = rest.strip().split()
        if not number:
            continue
        try:
            values[key] = int(number[0])
        except ValueError:
            continue
    missing = [name for name in ("MemAvailable", "SwapTotal", "SwapFree") if name not in values]
    if missing:
        raise ValueError("meminfo sem " + ",".join(missing))
    available_mib = values["MemAvailable"] // 1024
    swap_used_mib = max(0, values["SwapTotal"] - values["SwapFree"]) // 1024
    return available_mib, swap_used_mib


def sanitize_error(message: str | None, limit: int = 160) -> str | None:
    if not message:
        return None
    text = " ".join(str(message).split())
    lowered = text.lower()
    if any(word in lowered for word in _SENSITIVE):
        return "generation failed"
    return text[:limit]


class MemorySampler(threading.Thread):
    """Amostra GPU e RAM até stop. Falha de leitura fica em self.error."""

    def __init__(self, sample_ms: int, *, runner=None, meminfo_reader=None) -> None:
        super().__init__(daemon=True)
        self.interval_s = max(0.05, sample_ms / 1000)
        self.runner = runner or subprocess.run
        self.meminfo_reader = meminfo_reader or (lambda: Path("/proc/meminfo").read_text(
            encoding="utf-8", errors="replace"))
        self.stop = threading.Event()
        self.error: str | None = None
        self.vram_used: list[int] = []
        self.vram_free: list[int] = []
        self.ram_available: list[int] = []
        self.swap_used: list[int] = []

    def sample_once(self) -> None:
        proc = self.runner(
            ["nvidia-smi", "--query-gpu=memory.used,memory.free",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=5,
        )
        if proc.returncode != 0:
            raise RuntimeError("nvidia-smi falhou")
        used, free = parse_nvidia_smi(proc.stdout or "")
        available, swap = parse_meminfo(self.meminfo_reader())
        self.vram_used.append(used)
        self.vram_free.append(free)
        self.ram_available.append(available)
        self.swap_used.append(swap)

    def run(self) -> None:
        while not self.stop.is_set():
            try:
                self.sample_once()
            except Exception as exc:
                self.error = sanitize_error(f"{type(exc).__name__}: {exc}") or "sample failed"
                self.stop.set()
                return
            self.stop.wait(self.interval_s)


def _job_view(body: dict | None) -> dict:
    if not isinstance(body, dict):
        return {}
    result = body.get("result") if isinstance(body.get("result"), dict) else {}
    image = result.get("image") if isinstance(result.get("image"), dict) else {}
    code = result.get("code")
    return {
        "status": body.get("status"),
        "error": body.get("error") or (str(code) if code else None),
        "width": result.get("width") or image.get("width"),
        "height": result.get("height") or image.get("height"),
    }


def poll_job(api: replay_turn.Api, adventure_id: str, job_id: str,
             timeout_s: float) -> dict:
    deadline = time.monotonic() + timeout_s
    last = "timeout"
    while time.monotonic() < deadline:
        res = api.call("GET", f"/api/adventures/{adventure_id}/jobs/{job_id}")
        if res["status"] != 200:
            return {"status": "error", "error": res["error"] or f"HTTP {res['status']}",
                    "width": None, "height": None}
        view = _job_view(res["body"])
        status = str(view["status"] or "")
        if status in TERMINAL:
            return view
        last = status or "pending"
        time.sleep(min(1.0, max(0.05, deadline - time.monotonic())))
    return {"status": "error", "error": f"job poll timeout ({last})",
            "width": None, "height": None}


def run_flux(timeout_s: float) -> dict:
    if str(BACKEND_DIR) not in sys.path:
        sys.path.insert(0, str(BACKEND_DIR))
    import httpx
    from app import db, image_client

    db.init_db()
    with db.session() as conn:
        cfg = image_client.effective_settings(conn)
    client_timeout = float(cfg.get("timeout_seconds") or timeout_s)
    with httpx.Client(timeout=httpx.Timeout(client_timeout, connect=min(10.0, client_timeout)),
                      trust_env=False) as client:
        content, width, height = image_client._generate_comfyui(
            cfg, client, FLUX_PROMPT, 1024, 1024)
    return {"status": "done", "error": None, "width": int(width), "height": int(height),
            "bytes": len(content)}


def run_portrait(api: replay_turn.Api, adventure_id: str, card_id: str,
                 timeout_s: float) -> dict:
    res = api.call("POST", f"/api/adventures/{adventure_id}/cards/{card_id}/portrait", {})
    if res["status"] not in (200, 202) or not isinstance(res["body"], dict):
        return {"status": "error", "error": res["error"] or f"HTTP {res['status']}",
                "width": None, "height": None}
    job_id = str(res["body"].get("id") or "")
    if not job_id:
        return {"status": "error", "error": "portrait response missing job id",
                "width": None, "height": None}
    return poll_job(api, adventure_id, job_id, timeout_s)


def run_clip(api: replay_turn.Api, adventure_id: str, image_id: str,
             timeout_s: float) -> dict:
    prompt = f"{CLIP_PROMPT} [{uuid.uuid4().hex}]"
    res = api.call("POST", f"/api/adventures/{adventure_id}/clips",
                   {"image_id": image_id, "prompt": prompt})
    if res["status"] not in (200, 202) or not isinstance(res["body"], dict):
        return {"status": "error", "error": res["error"] or f"HTTP {res['status']}",
                "width": None, "height": None}
    job_id = str(res["body"].get("job_id") or "")
    if not job_id:
        return {"status": "error", "error": "clip response missing job id",
                "width": None, "height": None}
    return poll_job(api, adventure_id, job_id, timeout_s)


def metrics_from(sampler: MemorySampler, started: float, outcome: dict) -> dict:
    used = sampler.vram_used
    free = sampler.vram_free
    ram = sampler.ram_available
    swap = sampler.swap_used
    report = {
        "duration_s": round(time.monotonic() - started, 3),
        "status": outcome.get("status") or "error",
        "error": sanitize_error(outcome.get("error")),
        "vram_used_max_mib": max(used) if used else None,
        "vram_free_min_mib": min(free) if free else None,
        "ram_available_min_mib": min(ram) if ram else None,
        "swap_used_delta_mib": (swap[-1] - swap[0]) if len(swap) >= 2 else (0 if swap else None),
    }
    if outcome.get("width") is not None and outcome.get("height") is not None:
        report["width"] = int(outcome["width"])
        report["height"] = int(outcome["height"])
    if outcome.get("bytes") is not None:
        report["bytes"] = int(outcome["bytes"])
    return report


def classify(report: dict, sample_error: str | None) -> int:
    failed = report["status"] != "done" or report["error"]
    if failed or report["vram_free_min_mib"] is not None \
            and report["vram_free_min_mib"] < VRAM_FREE_FLOOR_MIB:
        return EXIT_FAIL
    if sample_error or report["vram_free_min_mib"] is None:
        report["status"] = "error"
        report["error"] = sanitize_error(sample_error or "no memory samples")
        return EXIT_USAGE
    return EXIT_OK


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Mede VRAM/RAM de uma geração Klein, retrato ou clipe.")
    parser.add_argument("--base", default="http://127.0.0.1:2123")
    parser.add_argument("--env-file")
    parser.add_argument("--adventure", required=True)
    parser.add_argument("--mode", required=True, choices=("flux", "portrait", "clip"))
    parser.add_argument("--card", help="card id (obrigatório em portrait)")
    parser.add_argument("--image", help="image id de origem (obrigatório em clip)")
    parser.add_argument("--sample-ms", type=int, default=250)
    parser.add_argument("--timeout", type=float, default=300.0)
    parser.add_argument("--output", help="grava o mesmo JSON impresso")
    parser.add_argument("--token", help=argparse.SUPPRESS)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.sample_ms < 1:
        print(json.dumps({"status": "error", "error": "sample-ms must be >= 1"}))
        return EXIT_USAGE
    if args.mode == "portrait" and not args.card:
        print(json.dumps({"status": "error", "error": "portrait requires --card"}))
        return EXIT_USAGE
    if args.mode == "clip" and not args.image:
        print(json.dumps({"status": "error", "error": "clip requires --image"}))
        return EXIT_USAGE

    api = None
    if args.mode != "flux":
        try:
            token = replay_turn.resolve_token(args)
        except SystemExit as exc:
            print(json.dumps({"status": "error", "error": sanitize_error(str(exc)) or "auth"}))
            return EXIT_USAGE
        api = replay_turn.Api(args.base, token, args.timeout)

    sampler = MemorySampler(args.sample_ms)
    sampler.start()
    started = time.monotonic()
    try:
        if args.mode == "flux":
            outcome = run_flux(args.timeout)
        elif args.mode == "portrait":
            outcome = run_portrait(api, args.adventure, args.card, args.timeout)
        else:
            outcome = run_clip(api, args.adventure, args.image, args.timeout)
    except Exception as exc:
        outcome = {"status": "error", "error": f"{type(exc).__name__}: {exc}"}
    finally:
        sampler.stop.set()
        sampler.join(timeout=max(2.0, args.sample_ms / 1000 + 1))

    report = metrics_from(sampler, started, outcome)
    code = classify(report, sampler.error)
    payload = json.dumps(report, ensure_ascii=True)
    if args.output:
        Path(args.output).write_text(payload + "\n", encoding="utf-8")
    print(payload)
    return code


if __name__ == "__main__":
    sys.exit(main())
