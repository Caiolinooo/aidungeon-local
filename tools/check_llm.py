#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
check_llm.py -- Diagnóstico CLI do servidor LLM (AI Dungeon Local).

Lê a configuração efetiva (config\\settings.json; override de api_key por
variável de ambiente, arquivo .env na raiz ou --api-key), chama os endpoints
PÚBLICOS GET {base_url}/health e GET {base_url}/models e imprime tabela com:
alcance, auth necessária?, modelos descobertos (id, n_params, n_ctx_train,
ftype) e o veredito final:

  PRONTO          servidor acessível E API key configurada
  FALTA_API_KEY   servidor acessível, mas nenhuma API key configurada
  INALCANÇÁVEL    servidor não respondeu (VM fora / rede)

Apenas biblioteca padrão (nenhuma dependência externa).

Exit codes: 0=PRONTO · 2=FALTA_API_KEY · 3=INALCANÇÁVEL · 1=erro de uso/config.
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

EXIT_PRONTO = 0
EXIT_FALTA_API_KEY = 2
EXIT_INALCANCAVEL = 3
EXIT_ERRO = 1

USER_AGENT = "aidungeon-local-check-llm/1.0"
TABLE_W = 74

try:  # nunca quebrar por causa de encoding do console
    sys.stdout.reconfigure(errors="replace")
except Exception:
    pass


# --------------------------------------------------------------------------- #
# config efetiva
# --------------------------------------------------------------------------- #

def load_settings(config_path: Path) -> dict:
    if not config_path.is_file():
        raise FileNotFoundError(f"config não encontrado: {config_path}")
    with config_path.open("r", encoding="utf-8") as fh:
        return json.load(fh)


def load_dotenv_key(env_name: str, dotenv_path: Path) -> str:
    """Le env_name de um .env simples (KEY=valor), sem dependências.
    Nunca imprime o valor; retorna '' se ausente."""
    try:
        if not dotenv_path.is_file():
            return ""
        for line in dotenv_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            k = k.strip()
            if k.startswith("export "):
                k = k[len("export "):].strip()
            if k == env_name:
                return v.strip().strip('"').strip("'").strip()
    except OSError:
        pass
    return ""


def resolve_api_key(cli_api_key: str | None, llm_cfg: dict, dotenv_path: Path) -> tuple[str, str | None]:
    """Precedência: --api-key > env real > .env da raiz > settings."""
    if cli_api_key:
        return cli_api_key, "--api-key"
    env_name = llm_cfg.get("api_key_env") or "LLM_API_KEY"
    env_val = os.environ.get(env_name, "").strip()
    if env_val:
        return env_val, f"env {env_name}"
    dot_val = load_dotenv_key(env_name, dotenv_path)
    if dot_val:
        return dot_val, f".env ({env_name})"
    cfg_val = (llm_cfg.get("api_key") or "").strip()
    if cfg_val:
        return cfg_val, "config/settings.json -> llm.api_key"
    return "", None


# --------------------------------------------------------------------------- #
# HTTP (stdlib)
# --------------------------------------------------------------------------- #

class HttpResult:
    def __init__(self, url: str):
        self.url = url
        self.ok = False
        self.status: int | None = None
        self.elapsed = 0.0
        self.body = b""
        self.error: str | None = None
        self.auth_required = False
        self.auth_used = False


def http_get(url: str, api_key: str = "", timeout: float = 20.0) -> HttpResult:
    """GET sem auth; se 401/403 e houver chave, tenta de novo COM a chave."""
    res = HttpResult(url)
    for attempt in range(2):
        req = urllib.request.Request(url, headers={
            "Accept": "application/json",
            "User-Agent": USER_AGENT,
        })
        if attempt == 1:
            req.add_header("Authorization", f"Bearer {api_key}")
            res.auth_used = True
        t0 = time.perf_counter()
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                res.body = resp.read()
                res.status = resp.status
                res.elapsed = time.perf_counter() - t0
                res.ok = 200 <= resp.status < 300
                return res
        except urllib.error.HTTPError as exc:
            res.elapsed = time.perf_counter() - t0
            res.status = exc.code
            try:
                res.body = exc.read() or b""
            except Exception:
                pass
            if exc.code in (401, 403):
                res.auth_required = True
                if attempt == 0 and api_key:
                    continue  # retry autenticado
            res.error = f"HTTP {exc.code}"
            return res
        except (urllib.error.URLError, OSError) as exc:
            res.elapsed = time.perf_counter() - t0
            reason = getattr(exc, "reason", exc)
            res.error = str(reason)
            return res
    return res


def endpoint_urls(base_url: str) -> tuple[str, list[str]]:
    """models = {base}/models; health = tenta na raiz do servidor e no próprio base."""
    b = base_url.rstrip("/")
    root = b
    for suffix in ("/v1", "/api"):
        if root.lower().endswith(suffix):
            root = root[: -len(suffix)]
            break
    health = []
    for u in (root + "/health", b + "/health"):
        if u not in health:
            health.append(u)
    return b + "/models", health


# --------------------------------------------------------------------------- #
# parsing / formatação
# --------------------------------------------------------------------------- #

def parse_models(body: bytes) -> list[dict]:
    data = json.loads(body.decode("utf-8", "replace"))
    items = data.get("data") or data.get("models") or []
    # llama-server também devolve um array estilo Ollama com "capabilities"
    caps_by_name = {}
    for m in data.get("models") or []:
        nm = m.get("name") or m.get("model")
        if nm:
            caps_by_name[nm] = m.get("capabilities") or []
    out = []
    for it in items:
        mid = it.get("id") or it.get("name") or it.get("model") or "?"
        meta = it.get("meta") or {}
        out.append({
            "id": mid,
            "n_params": meta.get("n_params"),
            "n_ctx_train": meta.get("n_ctx_train") or meta.get("n_ctx"),
            "ftype": meta.get("ftype"),
            "owned_by": it.get("owned_by"),
            "capabilities": it.get("capabilities") or caps_by_name.get(mid) or [],
        })
    return out


def fmt_params(n) -> str:
    if not n:
        return "-"
    n = float(n)
    if n >= 1e9:
        return f"{n / 1e9:.2f}B"
    if n >= 1e6:
        return f"{n / 1e6:.1f}M"
    return str(int(n))


def fmt_ctx(n) -> str:
    return str(int(n)) if n else "-"


def trunc(s: str, width: int) -> str:
    s = str(s)
    return s if len(s) <= width else s[: width - 1] + "…"


def hr(ch: str = "-") -> str:
    return ch * TABLE_W


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #

def build_parser() -> argparse.ArgumentParser:
    epilog = (
        "vereditos:\n"
        "  PRONTO (exit 0)         servidor acessível E api_key configurada\n"
        "  FALTA_API_KEY (exit 2)  servidor acessível, falta api_key\n"
        "  INALCANÇÁVEL (exit 3)   /health e /models não responderam\n"
        "\n"
        "exemplos:\n"
        "  python tools\\check_llm.py\n"
        "  python tools\\check_llm.py --api-key SUA_CHAVE\n"
        "  python tools\\check_llm.py --base-url http://127.0.0.1:5001/v1   (fallback local)\n"
    )
    p = argparse.ArgumentParser(
        prog="check_llm.py",
        description="Diagnóstico do servidor LLM (GETs públicos /health e /models).",
        epilog=epilog,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--base-url", help="sobrescreve llm.base_url do settings (ex.: http://127.0.0.1:5001/v1)")
    p.add_argument("--api-key", help="sobrescreve a api_key (senão: env LLM_API_KEY > settings)")
    p.add_argument("--config", default=str(Path(__file__).resolve().parent.parent / "config" / "settings.json"),
                   help="caminho do settings.json (padrão: ..\\config\\settings.json)")
    p.add_argument("--timeout", type=float, default=20.0, help="timeout por requisição em segundos (padrão: 20)")
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)

    try:
        settings = load_settings(Path(args.config))
    except (FileNotFoundError, json.JSONDecodeError) as exc:
        print(f"ERRO: {exc}")
        return EXIT_ERRO

    llm_cfg = settings.get("llm") or {}
    base_url = (args.base_url or llm_cfg.get("base_url") or "").strip()
    if not base_url:
        print("ERRO: base_url vazio (nem --base-url nem llm.base_url no settings).")
        return EXIT_ERRO

    api_key, key_src = resolve_api_key(args.api_key, llm_cfg,
                                       Path(__file__).resolve().parent.parent / ".env")
    models_url, health_urls = endpoint_urls(base_url)

    print("=" * TABLE_W)
    print(" AI Dungeon Local - diagnostico do servidor LLM".ljust(TABLE_W - 1))
    print("=" * TABLE_W)
    print(f" base_url : {base_url}")
    print(f" config   : {Path(args.config)}")
    print(f" api_key  : {('configurada (' + key_src + ', valor oculto)') if api_key else 'NÃO configurada'}")
    print(hr())

    # ---- GET /health ------------------------------------------------------- #
    health_res: HttpResult | None = None
    for hu in health_urls:
        r = http_get(hu, api_key=api_key, timeout=args.timeout)
        if r.ok or r.status is not None:  # qualquer resposta HTTP = alcançável
            health_res = r
            break
        health_res = health_res or r
    print(" ENDPOINT / ALCANCE / AUTH")
    if health_res and health_res.status is not None:
        body_txt = health_res.body.decode("utf-8", "replace").strip().replace("\n", " ")
        state = "OK" if health_res.ok else f"HTTP {health_res.status}"
        auth_txt = "sim" if health_res.auth_required else "não"
        print(f" GET {health_res.url}")
        print(f"   alcance: {state} em {health_res.elapsed:.2f}s | auth necessária: {auth_txt}"
              f" | resposta: {trunc(body_txt, 40)}")
    else:
        print(f" GET {health_urls[0]}")
        print(f"   alcance: FALHOU ({health_res.error if health_res else 'sem resposta'})")

    # ---- GET /models ------------------------------------------------------- #
    models_res = http_get(models_url, api_key=api_key, timeout=args.timeout)
    models: list[dict] = []
    if models_res.ok:
        try:
            models = parse_models(models_res.body)
        except (json.JSONDecodeError, KeyError) as exc:
            print(f" GET {models_url}")
            print(f"   alcance: HTTP 200 mas JSON inválido ({exc})")
            models = []
    if models_res.status is not None:
        state = "OK" if models_res.ok else f"HTTP {models_res.status}"
        auth_txt = "sim" if models_res.auth_required else "não"
        extra = f" | {len(models)} modelo(s)" if models_res.ok else ""
        print(f" GET {models_url}")
        print(f"   alcance: {state} em {models_res.elapsed:.2f}s | auth necessária: {auth_txt}{extra}")
    else:
        print(f" GET {models_url}")
        print(f"   alcance: FALHOU ({models_res.error})")

    reachable = bool((health_res and health_res.status is not None) or models_res.status is not None)

    # ---- tabela de modelos ------------------------------------------------- #
    if models:
        print(hr())
        print(" MODELOS DESCOBERTOS")
        print(f" {'#':<2} {'id':<44} {'n_params':>9} {'n_ctx_train':>12}  ftype")
        for i, m in enumerate(models, 1):
            caps = f" [{', '.join(m['capabilities'])}]" if m.get("capabilities") else ""
            print(f" {i:<2} {trunc(m['id'], 44):<44} {fmt_params(m['n_params']):>9} "
                  f"{fmt_ctx(m['n_ctx_train']):>12}  {m.get('ftype') or '-'}{caps}")

    # ---- aviso: modelo híbrido Qwen3 (thinking) ---------------------------- #
    qwen3_hybrid = any("qwen3" in str(m.get("id", "")).lower() for m in models)
    disable_thinking = bool(llm_cfg.get("disable_thinking", True))

    print(hr())
    print(" NOTA: /v1/chat/completions e /props exigem API key (não testados aqui;")
    print("       o benchmark autenticado é feito por tools\\bench_llm.py).")
    if qwen3_hybrid:
        print(hr("!"))
        print(" AVISO: modelo Qwen3.x HÍBRIDO (thinking) detectado no servidor.")
        if disable_thinking:
            print("   llm.disable_thinking=true no settings: clientes DEVEM enviar")
            print('   chat_template_kwargs={"enable_thinking": false} em TODA chamada a')
            print("   /chat/completions. Sem isso os tokens viram reasoning_content e")
            print("   content volta VAZIO (validado contra esta VM em 2026-10-07).")
            print("   tools\\bench_llm.py já envia esse parâmetro automaticamente.")
        else:
            print("   ATENÇÃO: llm.disable_thinking está FALSE no settings - sem")
            print('   chat_template_kwargs={"enable_thinking": false} o content volta')
            print("   VAZIO (tokens viram reasoning_content). Reative o campo.")
    print(hr())

    # ---- veredito ---------------------------------------------------------- #
    if not reachable:
        print(" VEREDITO: INALCANÇÁVEL")
        print(" O servidor não respondeu em /health nem em /models.")
        print(" Verifique: VM ligada? DNS vm.groupabz.com resolve? porta 1234 aberta?")
        print(" Se a VM estiver fora, veja o fallback local em tools\\README.md")
        print(" (tools\\setup_llm_local.ps1 + start_llm_local.bat).")
        return EXIT_INALCANCAVEL

    if not api_key:
        print(" VEREDITO: FALTA_API_KEY")
        print(" Servidor acessível, mas nenhuma API key configurada.")
        print(" Configure por UMA das vias:")
        print("   1. .env na raiz do repo:  LLM_API_KEY=sua-chave   (gitignored, lido automaticamente)")
        print("   2. env:  $env:LLM_API_KEY = \"sua-chave\"   (sessão atual do PowerShell)")
        print("   3. backend: PUT /api/settings/llm  {\"api_key\": \"sua-chave\"}")
        print("   4. UI: aba Config -> campo API key")
        print(" Ou teste já:  python tools\\check_llm.py --api-key SUA_CHAVE")
        return EXIT_FALTA_API_KEY

    print(" VEREDITO: PRONTO")
    print(f" Servidor acessível e API key configurada ({key_src}).")
    print(" Próximo passo: python tools\\bench_llm.py")
    return EXIT_PRONTO


if __name__ == "__main__":
    raise SystemExit(main())
