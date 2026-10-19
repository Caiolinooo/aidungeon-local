#!/usr/bin/env bash
# instalar.sh — setup local do AI Dungeon Local no Linux (idempotente).
#
# 1. Verifica Python 3.12+ e Node.js 20+
# 2. Cria o venv e instala backend/requirements.txt
# 3. npm ci + npm run build em frontend/ (gera frontend/dist)
# 4. Cria .env a partir de .env.example (nunca sobrescreve)
# 5. Diagnostico do LLM via tools/check_llm.py (aviso, nao bloqueia)
#
# Uso:  bash instalar.sh
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/common.sh
source "$ROOT/scripts/common.sh"
aidl_require_linux
cd "$ROOT"

ok()   { printf '[ OK ] %s\n' "$1"; }
warn() { printf '[AVISO] %s\n' "$1"; }
fail() { printf '[ERRO ] %s\n' "$1" >&2; }

echo ""
echo "== AI Dungeon Local - instalacao (Linux) =="
echo ""

pick_python() {
  local cmd ver
  for cmd in python3.13 python3.12 python3 python; do
    command -v "$cmd" >/dev/null 2>&1 || continue
    ver="$("$cmd" -c 'import sys; print("%d.%d" % sys.version_info[:2])' 2>/dev/null || true)"
    [[ "$ver" == *. ]] || true
    if [[ "$ver" =~ ^([0-9]+)\.([0-9]+)$ ]]; then
      if (( ${BASH_REMATCH[1]} > 3 || (${BASH_REMATCH[1]} == 3 && ${BASH_REMATCH[2]} >= 12) )); then
        printf '%s\n' "$cmd"
        return 0
      fi
    fi
  done
  return 1
}

if ! PY_CMD="$(pick_python)"; then
  fail "Python 3.12+ nao encontrado no PATH."
  echo "       Debian/Ubuntu: sudo apt install python3 python3-venv python3-pip"
  echo "       Fedora:        sudo dnf install python3"
  echo "       Depois rode:   bash instalar.sh"
  exit 1
fi
py_ver="$("$PY_CMD" -c 'import sys; print("%d.%d.%d" % sys.version_info[:3])')"
ok "Python $py_ver (via $PY_CMD)"

NODE_RAW="$(node --version 2>/dev/null || true)"
NODE_MAJOR="${NODE_RAW#v}"
NODE_MAJOR="${NODE_MAJOR%%.*}"
if [[ -z "${NODE_MAJOR}" || ! "${NODE_MAJOR}" =~ ^[0-9]+$ || "${NODE_MAJOR}" -lt 20 ]]; then
  fail "Node.js 20+ nao encontrado (detectado: ${NODE_RAW:-nenhum})."
  echo "       Instale o LTS de https://nodejs.org/ (ou via nvm) e rode este script novamente."
  exit 1
fi
ok "Node.js $(node --version) / npm $(npm --version)"

VENV_DIR="$(aidl_venv_dir "$ROOT")"
VENV_PY="$VENV_DIR/bin/python"
if [[ ! -x "$VENV_PY" ]]; then
  if ! "$PY_CMD" -c 'import venv' >/dev/null 2>&1; then
    fail "O modulo venv nao esta instalado."
    echo "       Debian/Ubuntu: sudo apt install python3-venv"
    echo "       Fedora:        sudo dnf install python3"
    exit 1
  fi
  if [[ -e "$ROOT/backend/.venv/Scripts/python.exe" && "$VENV_DIR" == *venv-linux ]]; then
    warn "backend/.venv e o venv do Windows; criando backend/.venv-linux"
  fi
  echo "Criando ${VENV_DIR#"$ROOT"/} ..."
  "$PY_CMD" -m venv "$VENV_DIR"
  ok "${VENV_DIR#"$ROOT"/} criado"
else
  ok "${VENV_DIR#"$ROOT"/} ja existe (mantido)"
fi

echo "Instalando dependencias Python (backend/requirements.txt) ..."
"$VENV_PY" -m pip install --disable-pip-version-check --quiet --upgrade pip
"$VENV_PY" -m pip install --disable-pip-version-check --quiet -r "$ROOT/backend/requirements.txt"
ok "dependencias Python instaladas"

echo "Instalando dependencias do frontend (npm ci) ..."
(
  cd "$ROOT/frontend"
  npm ci --no-audit --no-fund
  echo "Gerando build de producao (npm run build) ..."
  npm run build
)
ok "frontend buildado em frontend/dist"

if [[ -f "$ROOT/.env" ]]; then
  ok ".env ja existe (mantido — nao sobrescrevo segredos)"
else
  cp "$ROOT/.env.example" "$ROOT/.env"
  chmod 600 "$ROOT/.env" || true
  warn ".env criado a partir de .env.example"
  warn "PREENCHA os 4 valores antes de jogar — os comentarios no arquivo"
  warn "ensinam a gerar AUTH_PASSWORD_HASH e AUTH_SECRET."
fi

echo ""
echo "Testando o servidor LLM (tools/check_llm.py) ..."
set +e
"$VENV_PY" "$ROOT/tools/check_llm.py" --timeout 8
llm_code=$?
set -e
case "$llm_code" in
  0) ok "LLM da VM acessivel e com API key" ;;
  2) warn "VM acessivel, mas falta LLM_API_KEY no .env" ;;
  3) warn "VM do LLM inalcancavel agora — o jogo so funciona com ela no ar" ;;
  *) warn "check_llm retornou codigo $llm_code (veja a saida acima)" ;;
esac

echo ""
echo "== Instalacao concluida =="
echo ""
echo "Proximos passos:"
echo "  1. Se o .env foi criado agora: edite-o e preencha LLM_API_KEY,"
echo "     AUTH_PASSWORD_HASH e AUTH_SECRET (instrucoes dentro do arquivo)."
echo "  2. bash iniciar.sh — sobe o backend e abre o navegador."
echo "  3. Login com o usuario/senha do .env. Para encerrar: bash parar.sh."
echo ""
