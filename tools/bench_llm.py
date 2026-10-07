#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
bench_llm.py -- Benchmark de geração do LLM (AI Dungeon Local).

Envia um prompt narrativo curto (estilo AI Dungeon, 2ª pessoa) via
POST {base_url}/chat/completions com stream=true e mede:

  TTFT    tempo até o 1º token (segundos)
  tok/s   tokens de saída por segundo de geração
  total   tokens gerados

Veredito de velocidade:  >= 15 tok/s ÓTIMO · 8-15 BOM · < 8 LENTO

Pré-requisito: API key configurada (--api-key, env LLM_API_KEY, settings
ou aba Config da UI). Sem chave o script NÃO chama o servidor (exit 2).

Apenas biblioteca padrão. Exit codes: 0=ok · 2=FALTA_API_KEY · 3=INALCANÇÁVEL · 1=erro.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

EXIT_OK = 0
EXIT_FALTA_API_KEY = 2
EXIT_INALCANCAVEL = 3
EXIT_ERRO = 1

USER_AGENT = "aidungeon-local-bench-llm/1.0"

SYSTEM_PROMPT = (
    "You are an AI storyteller running an interactive text adventure. Continue the story in "
    "second person (\"You ...\"), matching the established tone. Write vivid prose with dialogue, "
    "sensory detail and consequences for the player's actions. Never decide what the player "
    "character says or does beyond their stated action. Never break the fourth wall."
)

DEFAULT_USER_PROMPT = (
    "You are Caio, an Aasimar sorcerer allied with the Zhentarim, standing at the gates of "
    "Calimport at dusk. The agent Thuldan waves at you from the crowded bazaar.\n\n"
    "> You greet Thuldan and ask about the sealed scroll."
)

try:
    sys.stdout.reconfigure(errors="replace")
except Exception:
    pass


# --------------------------------------------------------------------------- #
# config
# --------------------------------------------------------------------------- #

def load_settings(config_path: Path) -> dict:
    if not config_path.is_file():
        raise FileNotFoundError(f"config não encontrado: {config_path}")
    with config_path.open("r", encoding="utf-8") as fh:
        return json.load(fh)


def resolve_api_key(cli_api_key: str | None, llm_cfg: dict) -> tuple[str, str | None]:
    if cli_api_key:
        return cli_api_key, "--api-key"
    env_name = llm_cfg.get("api_key_env") or "LLM_API_KEY"
    env_val = os.environ.get(env_name, "").strip()
    if env_val:
        return env_val, f"env {env_name}"
    cfg_val = (llm_cfg.get("api_key") or "").strip()
    if cfg_val:
        return cfg_val, "config/settings.json -> llm.api_key"
    return "", None


def discover_model(base_url: str, api_key: str, timeout: float) -> str | None:
    """GET {base_url}/models (público no llama-server) e pega o 1º id."""
    url = base_url.rstrip("/") + "/models"
    for attempt in range(2):
        req = urllib.request.Request(url, headers={
            "Accept": "application/json",
            "User-Agent": USER_AGENT,
        })
        if attempt == 1 and api_key:
            req.add_header("Authorization", f"Bearer {api_key}")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = json.loads(resp.read().decode("utf-8", "replace"))
            items = data.get("data") or data.get("models") or []
            for it in items:
                mid = it.get("id") or it.get("name") or it.get("model")
                if mid:
                    return mid
            return None
        except urllib.error.HTTPError as exc:
            if exc.code in (401, 403) and attempt == 0 and api_key:
                continue
            return None
        except (urllib.error.URLError, OSError, json.JSONDecodeError):
            return None
    return None


# --------------------------------------------------------------------------- #
# benchmark
# --------------------------------------------------------------------------- #

def print_key_help() -> None:
    print(" Configure a API key por UMA das vias e rode de novo:")
    print('   1. env:  $env:LLM_API_KEY = "sua-chave"   (sessão atual do PowerShell)')
    print('   2. backend: PUT /api/settings/llm  {"api_key": "sua-chave"}')
    print("   3. UI: aba Config -> campo API key")
    print(" Ou passe direto:  python tools\\bench_llm.py --api-key SUA_CHAVE")


def run_bench(base_url: str, api_key: str, model: str, messages: list[dict],
              max_tokens: int, temperature: float, top_p: float,
              timeout: float) -> dict:
    url = base_url.rstrip("/") + "/chat/completions"
    body = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
        "top_p": top_p,
        "max_tokens": max_tokens,
        "stream": True,
        "stream_options": {"include_usage": True},
    }
    req = urllib.request.Request(url, data=json.dumps(body).encode("utf-8"), method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("Accept", "text/event-stream")
    req.add_header("User-Agent", USER_AGENT)
    if api_key:
        req.add_header("Authorization", f"Bearer {api_key}")

    t0 = time.perf_counter()
    try:
        resp = urllib.request.urlopen(req, timeout=timeout)
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode("utf-8", "replace")[:300]
        except Exception:
            pass
        return {"ok": False, "http_status": exc.code, "error": f"HTTP {exc.code}", "detail": detail}
    except (urllib.error.URLError, OSError) as exc:
        return {"ok": False, "http_status": None,
                "error": str(getattr(exc, "reason", exc)), "detail": ""}

    ttft = None
    t_first = None
    chunks = 0
    parts: list[str] = []
    usage = None
    finish_reason = None
    t_end = None

    with resp:
        while True:
            raw = resp.readline()
            if not raw:
                break
            line = raw.decode("utf-8", "replace").strip()
            if not line or line.startswith(":") or not line.startswith("data:"):
                continue
            payload = line[len("data:"):].strip()
            if payload == "[DONE]":
                t_end = time.perf_counter()
                break
            try:
                evt = json.loads(payload)
            except json.JSONDecodeError:
                continue
            if evt.get("usage"):
                usage = evt["usage"]
            for choice in evt.get("choices") or []:
                if choice.get("finish_reason"):
                    finish_reason = choice["finish_reason"]
                delta = choice.get("delta") or {}
                piece = delta.get("content") or choice.get("text") or ""
                if piece:
                    now = time.perf_counter()
                    if t_first is None:
                        t_first = now
                        ttft = now - t0
                    chunks += 1
                    parts.append(piece)

    if t_end is None:
        t_end = time.perf_counter()

    completion_tokens = (usage or {}).get("completion_tokens") or chunks
    token_source = "usage" if (usage or {}).get("completion_tokens") else "chunks SSE (estimativa)"
    gen_time = (t_end - t_first) if t_first else 0.0
    tok_s = (completion_tokens / gen_time) if gen_time > 0 else 0.0

    return {
        "ok": True,
        "ttft": ttft,
        "gen_time": gen_time,
        "total_time": t_end - t0,
        "completion_tokens": completion_tokens,
        "prompt_tokens": (usage or {}).get("prompt_tokens"),
        "token_source": token_source,
        "tok_s": tok_s,
        "finish_reason": finish_reason,
        "text": "".join(parts),
    }


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #

def build_parser() -> argparse.ArgumentParser:
    epilog = (
        "veredito de velocidade:  >= 15 tok/s ÓTIMO · 8-15 BOM · < 8 LENTO\n"
        "\n"
        "exemplos:\n"
        "  python tools\\bench_llm.py --api-key SUA_CHAVE\n"
        "  $env:LLM_API_KEY = \"SUA_CHAVE\"; python tools\\bench_llm.py\n"
        "  python tools\\bench_llm.py --base-url http://127.0.0.1:5001/v1   (fallback local, sem key)\n"
        "  python tools\\bench_llm.py --max-tokens 256\n"
    )
    p = argparse.ArgumentParser(
        prog="bench_llm.py",
        description="Benchmark de geração do LLM: TTFT, tok/s e total de tokens (stream SSE).",
        epilog=epilog,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--base-url", help="sobrescreve llm.base_url do settings")
    p.add_argument("--api-key", help="sobrescreve a api_key (senão: env LLM_API_KEY > settings)")
    p.add_argument("--model", help="id do modelo (padrão: llm.model_alias ou autodescoberta via GET /models)")
    p.add_argument("--max-tokens", type=int, default=128, help="tokens a gerar (padrão: 128)")
    p.add_argument("--temperature", type=float, default=None,
                   help="padrão: sampler.temperature do settings (0.85)")
    p.add_argument("--prompt", help="prompt customizado (padrão: cena curta estilo AI Dungeon)")
    p.add_argument("--config", default=str(Path(__file__).resolve().parent.parent / "config" / "settings.json"),
                   help="caminho do settings.json")
    p.add_argument("--timeout", type=float, default=None,
                   help="timeout em segundos (padrão: llm.timeout_seconds do settings, 180)")
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)

    try:
        settings = load_settings(Path(args.config))
    except (FileNotFoundError, json.JSONDecodeError) as exc:
        print(f"ERRO: {exc}")
        return EXIT_ERRO

    llm_cfg = settings.get("llm") or {}
    sampler = settings.get("sampler") or {}
    base_url = (args.base_url or llm_cfg.get("base_url") or "").strip()
    if not base_url:
        print("ERRO: base_url vazio (nem --base-url nem llm.base_url no settings).")
        return EXIT_ERRO

    timeout = args.timeout or float(llm_cfg.get("timeout_seconds") or 180)
    temperature = args.temperature if args.temperature is not None else float(sampler.get("temperature", 0.85))
    top_p = float(sampler.get("top_p", 0.95))

    # --- api key: sem chave, NÃO toca na rede ------------------------------- #
    api_key, key_src = resolve_api_key(args.api_key, llm_cfg)
    print("=" * 72)
    print(" AI Dungeon Local - benchmark do LLM (stream)")
    print("=" * 72)
    print(f" base_url : {base_url}")
    if not api_key:
        print(" api_key  : NÃO configurada")
        print("-" * 72)
        print(" VEREDITO: FALTA_API_KEY")
        print(" /chat/completions exige API key neste servidor (401 sem chave).")
        print_key_help()
        return EXIT_FALTA_API_KEY
    print(f" api_key  : configurada ({key_src}, valor oculto)")

    # --- modelo -------------------------------------------------------------- #
    model = args.model or (llm_cfg.get("model_alias") or "auto")
    if model == "auto":
        print(" modelo   : auto -> consultando GET /models ...")
        model = discover_model(base_url, api_key, timeout=min(timeout, 20.0))
        if not model:
            print(" ERRO: não foi possível descobrir o modelo via GET /models.")
            print("       O servidor está no ar? (rode tools\\check_llm.py)")
            print("       Ou informe explicitamente: --model ID_DO_MODELO")
            return EXIT_INALCANCAVEL
    print(f" modelo   : {model}")
    print(f" sampler  : temperature={temperature} top_p={top_p} max_tokens={args.max_tokens}")
    print("-" * 72)

    # --- benchmark ----------------------------------------------------------- #
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": args.prompt or DEFAULT_USER_PROMPT},
    ]
    print(" Gerando (stream)...")
    r = run_bench(base_url, api_key, model, messages, args.max_tokens,
                  temperature, top_p, timeout)

    if not r["ok"]:
        print("-" * 72)
        if r.get("http_status") == 401:
            print(" VEREDITO: FALTA_API_KEY (HTTP 401 Unauthorized)")
            print(" A chave configurada foi recusada pelo servidor.")
            print_key_help()
            return EXIT_FALTA_API_KEY
        if r.get("http_status") is None:
            print(f" VEREDITO: INALCANÇÁVEL ({r['error']})")
            print(" Se a VM estiver fora, veja o fallback local em tools\\README.md.")
            return EXIT_INALCANCAVEL
        print(f" ERRO: {r['error']}  {r.get('detail', '')}")
        return EXIT_ERRO

    # --- resultados ----------------------------------------------------------- #
    print("-" * 72)
    print(" RESULTADO")
    ttft_txt = f"{r['ttft']:.2f}s" if r["ttft"] is not None else "n/d (stream vazio)"
    print(f"   TTFT (1º token)  : {ttft_txt}")
    print(f"   tokens gerados   : {r['completion_tokens']}  (fonte: {r['token_source']})")
    if r.get("prompt_tokens"):
        print(f"   tokens de prompt : {r['prompt_tokens']}")
    print(f"   tempo de geração : {r['gen_time']:.2f}s   (total c/ request: {r['total_time']:.2f}s)")
    print(f"   velocidade       : {r['tok_s']:.2f} tok/s")
    if r.get("finish_reason"):
        print(f"   finish_reason    : {r['finish_reason']}")
    amostra = " ".join((r["text"] or "").split())[:240]
    if amostra:
        print(f"   amostra          : \"{amostra}\"")
    print("-" * 72)

    tps = r["tok_s"]
    if tps >= 15:
        veredito = "ÓTIMO (>= 15 tok/s)"
    elif tps >= 8:
        veredito = "BOM (8-15 tok/s)"
    else:
        veredito = "LENTO (< 8 tok/s)"
    print(f" VEREDITO: {veredito}")
    if tps < 8:
        print(" Dica: respostas <= 300 tokens + streaming mantêm a experiência jogável;")
        print("       se persistir lento, avalie o fallback local (tools\\README.md).")
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
