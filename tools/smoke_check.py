#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
smoke_check.py -- Confere uma instância do backend depois de um deploy (G9).

1. Espera até --wait-s o GET /api/adventures sem token responder 401 (no ar,
   auth ligada).
2. Com o token mintado de AUTH_USER/AUTH_SECRET do --env-file (nunca
   impresso): GET /api/adventures 200 e GET /api/health/llm 200. O status do
   LLM só é impresso: servidor LLM fora do ar não é falha do deploy.
3. --db: migrations registradas em schema_migrations e user_version.

Com --player-idle-min N --idle-db BANCO não sobe nada: sai 0 se o jogador
está parado há N minutos ou mais (mesma regra do --yield-db do
replay_turn.py), 1 se não está.

Apenas biblioteca padrão.
Exit codes: 0 = ok · 1 = falhou · 2 = erro de uso.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

from replay_turn import db_quiet_s, mint_token, read_dotenv

EXIT_OK, EXIT_FAIL, EXIT_USAGE = 0, 1, 2


def get(url: str, token: str | None = None, timeout: float = 30.0) -> tuple[int, bytes]:
    req = urllib.request.Request(url)
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(req, timeout=timeout) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


def wait_up(base: str, wait_s: float) -> bool:
    deadline = time.monotonic() + wait_s
    while True:
        try:
            status, _ = get(f"{base}/api/adventures", timeout=5)
            if status == 401:
                return True
            print(f"  GET /api/adventures sem token: HTTP {status} (esperado 401)")
            return False
        except (OSError, urllib.error.URLError):
            pass
        if time.monotonic() > deadline:
            print(f"  sem resposta em {wait_s:.0f}s")
            return False
        time.sleep(1)


def check_api(base: str, token: str) -> bool:
    status, _ = get(f"{base}/api/adventures", token)
    print(f"  GET /api/adventures: HTTP {status}")
    if status != 200:
        return False
    status, raw = get(f"{base}/api/health/llm", token)
    try:
        health = json.loads(raw)
    except ValueError:
        health = {}
    breaker = (health.get("scheduler") or {}).get("breaker") or {}
    slots = (health.get("slots") or {}).get("total")
    print(f"  GET /api/health/llm: HTTP {status} status={health.get('status')} "
          f"modelo={health.get('model_discovered')} slots={slots} breaker={breaker.get('state')}"
          + (f" ({health.get('details')})" if health.get("details") else ""))
    if health.get("status") not in ("ok", "degraded"):
        print("  aviso: o LLM não está ok; o app subiu, então o deploy segue")
    return status == 200 and "status" in health


def check_db(path: str) -> bool:
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        applied = [n for n, in conn.execute("SELECT number FROM schema_migrations ORDER BY number")]
        conn.close()
    except sqlite3.Error as exc:
        print(f"  banco {path}: {exc}")
        return False
    print(f"  banco: user_version={version} migrations={applied[0] if applied else '-'}"
          f"..{applied[-1] if applied else '-'} ({len(applied)})")
    return True


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Smoke test de uma instância do backend.")
    p.add_argument("--base", default="http://127.0.0.1:2123", help="URL da instância")
    p.add_argument("--env-file", default="/home/caio/aidungeon/.env")
    p.add_argument("--wait-s", type=float, default=60.0, help="espera o serviço subir")
    p.add_argument("--db", help="banco da instância (lido só para leitura)")
    p.add_argument("--player-idle-min", type=float,
                   help="só confere se o jogador está parado há esses minutos")
    p.add_argument("--idle-db", help="banco do app principal, para --player-idle-min")
    return p


def main() -> int:
    args = build_parser().parse_args()
    if args.player_idle_min is not None:
        if not args.idle_db:
            print("--player-idle-min exige --idle-db", file=sys.stderr)
            return EXIT_USAGE
        quiet = db_quiet_s(args.idle_db)
        if quiet is None:
            print(f"não abriu {args.idle_db}")
            return EXIT_FAIL
        print(f"jogador parado há {quiet / 60:.1f} min (mínimo {args.player_idle_min:g})")
        return EXIT_OK if quiet >= args.player_idle_min * 60 else EXIT_FAIL

    dotenv = read_dotenv(Path(args.env_file))
    user, secret = dotenv.get("AUTH_USER", ""), dotenv.get("AUTH_SECRET", "")
    if not user or not secret:
        print(f"AUTH_USER/AUTH_SECRET ausentes em {args.env_file}", file=sys.stderr)
        return EXIT_USAGE
    base = args.base.rstrip("/")
    print(f"smoke {base}")
    ok = wait_up(base, args.wait_s) and check_api(base, mint_token(user, secret, ttl_s=600))
    if args.db:
        ok = check_db(args.db) and ok
    print("  OK" if ok else "  FALHOU")
    return EXIT_OK if ok else EXIT_FAIL


if __name__ == "__main__":
    sys.exit(main())
