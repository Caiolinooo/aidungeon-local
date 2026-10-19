#!/usr/bin/env bash
# parar.sh — encerra o backend (uvicorn) que escuta a porta 8000.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/common.sh
source "$ROOT/scripts/common.sh"
aidl_require_linux

PIDFILE="$ROOT/logs/backend.pid"
found=0

kill_pid() {
  local pid="$1"
  if [[ -n "$pid" && "$pid" =~ ^[0-9]+$ ]] && kill -0 "$pid" 2>/dev/null; then
    echo "Encerrando backend (PID $pid)..."
    kill "$pid" 2>/dev/null || true
    sleep 0.5
    if kill -0 "$pid" 2>/dev/null; then
      kill -9 "$pid" 2>/dev/null || true
    fi
    found=1
  fi
}

if [[ -f "$PIDFILE" ]]; then
  kill_pid "$(tr -d '[:space:]' <"$PIDFILE")"
  rm -f "$PIDFILE"
fi

collect_port_pids() {
  local line pid
  if command -v ss >/dev/null 2>&1; then
    while IFS= read -r line; do
      pid="$(sed -n 's/.*pid=\([0-9]\+\).*/\1/p' <<<"$line")"
      [[ -n "$pid" ]] && printf '%s\n' "$pid"
    done < <(ss -lptnH 'sport = :8000' 2>/dev/null || true)
  elif command -v lsof >/dev/null 2>&1; then
    lsof -tiTCP:8000 -sTCP:LISTEN 2>/dev/null || true
  fi
}

while IFS= read -r pid; do
  kill_pid "$pid"
done < <(collect_port_pids | sort -u)

if (( found == 1 )); then
  echo "Backend encerrado."
else
  echo "Nenhum backend escutando a porta 8000."
fi
