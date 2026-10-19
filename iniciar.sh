#!/usr/bin/env bash
# iniciar.sh — sobe o backend e abre o navegador.
#
# Uso: bash iniciar.sh
# Log em logs/backend-AAAAMMDD.log. Para encerrar: bash parar.sh.
# Defina AIDL_NO_BROWSER=1 para nao abrir o navegador.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/common.sh
source "$ROOT/scripts/common.sh"
aidl_require_linux
cd "$ROOT"

if ! command -v curl >/dev/null 2>&1; then
  echo "[ERRO] curl nao encontrado. Instale curl e rode de novo."
  exit 1
fi

if [[ ! -f "$ROOT/.env" ]]; then
  echo "[AVISO] .env nao encontrado. Rode antes: bash instalar.sh"
fi

if ! VENV_PY="$(aidl_venv_python "$ROOT")"; then
  echo "[ERRO] venv do Linux nao encontrado."
  echo "       Rode antes:  bash instalar.sh"
  exit 1
fi

if aidl_backend_up; then
  echo "Backend JA esta rodando em http://127.0.0.1:8000"
else
  mkdir -p "$ROOT/logs"
  LOGFILE="$ROOT/logs/backend-$(date +%Y%m%d).log"
  PIDFILE="$ROOT/logs/backend.pid"
  echo "Subindo backend... log: $LOGFILE"
  (
    cd "$ROOT/backend"
    nohup "$VENV_PY" -m uvicorn app.main:app --host 0.0.0.0 --port 8000 \
      >>"$LOGFILE" 2>&1 &
    echo $! >"$PIDFILE"
  )

  tries=0
  until aidl_backend_up; do
    tries=$((tries + 1))
    if (( tries >= 60 )); then
      echo "[ERRO] Backend nao respondeu em 60s. Veja o log:"
      echo "       $LOGFILE"
      exit 1
    fi
    sleep 1
  done
  echo "Backend no ar em http://0.0.0.0:8000"
  echo "Login: usuario/senha definidos no .env. Para encerrar: bash parar.sh"
fi

if [[ "${AIDL_NO_BROWSER:-}" == "1" ]]; then
  exit 0
fi

url="http://127.0.0.1:8000"
if command -v xdg-open >/dev/null 2>&1; then
  xdg-open "$url" >/dev/null 2>&1 || true
elif command -v sensible-browser >/dev/null 2>&1; then
  sensible-browser "$url" >/dev/null 2>&1 || true
else
  echo "Abra no navegador: $url"
fi
