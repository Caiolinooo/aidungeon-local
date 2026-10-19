#!/usr/bin/env bash
# Helpers compartilhados pelos scripts de Linux (instalar/iniciar/parar).
# Nao execute este arquivo; os scripts da raiz fazem source dele.

aidl_require_linux() {
  case "$(uname -s)" in
    Linux) ;;
    *)
      echo "[ERRO ] Este script e para Linux ($(uname -s))." >&2
      echo "        No Windows use o equivalente .ps1 / .bat." >&2
      exit 1
      ;;
  esac
}

# Diretorio do venv deste sistema. Se backend/.venv for o venv do Windows
# (Scripts/python.exe), o Linux usa backend/.venv-linux para nao misturar.
aidl_venv_dir() {
  local root="$1"
  local win="$root/backend/.venv"
  local linux="$root/backend/.venv-linux"
  if [[ -x "$win/bin/python" ]]; then
    printf '%s\n' "$win"
    return 0
  fi
  if [[ -e "$win/Scripts/python.exe" || -x "$linux/bin/python" ]]; then
    printf '%s\n' "$linux"
    return 0
  fi
  printf '%s\n' "$win"
}

aidl_venv_python() {
  local root="$1"
  local py
  py="$(aidl_venv_dir "$root")/bin/python"
  if [[ -x "$py" ]]; then
    printf '%s\n' "$py"
    return 0
  fi
  return 1
}

# Qualquer resposta HTTP (inclusive 401) significa que o uvicorn esta no ar.
aidl_backend_up() {
  local code
  code="$(curl -s -o /dev/null --max-time 2 -w '%{http_code}' \
    http://127.0.0.1:8000/api/health/llm 2>/dev/null || true)"
  [[ -n "$code" && "$code" != "000" ]]
}
