#!/usr/bin/env bash
# Fallback local do LLM no Linux: baixa KoboldCpp + GGUF e gera start_llm_local.sh.
#
# SOMENTE quando a VM (http://vm.groupabz.com:1234/v1) estiver fora do ar.
# O servidor nao e iniciado por este script.
#
# Uso:
#   bash tools/setup_llm_local.sh
#   bash tools/setup_llm_local.sh --model qwen2.5-3b-instruct-q5_k_m --force
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck source=../scripts/common.sh
source "$ROOT/scripts/common.sh"
aidl_require_linux

MODEL="qwen2.5-7b-instruct-q4_k_m"
FORCE=0

usage() {
  cat <<EOF
Uso: bash tools/setup_llm_local.sh [--model CHAVE] [--force]

  --model   chave em config/models.json -> local_fallbacks
            (padrao: qwen2.5-7b-instruct-q4_k_m)
  --force   refaz os downloads mesmo que os arquivos ja existam
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --model)
      MODEL="${2:-}"
      shift 2
      ;;
    --force) FORCE=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Argumento desconhecido: $1" >&2; usage; exit 1 ;;
  esac
done

command -v curl >/dev/null 2>&1 || { echo "curl e necessario." >&2; exit 1; }
command -v python3 >/dev/null 2>&1 || { echo "python3 e necessario." >&2; exit 1; }

MODEL_DIR="$ROOT/data/models"
KOB_DIR="$ROOT/data/koboldcpp"
mkdir -p "$MODEL_DIR" "$KOB_DIR"

echo "============================================================"
echo " AI Dungeon Local - setup do fallback local (KoboldCpp)"
echo "============================================================"

if ! parsed="$(
  MODEL="$MODEL" CFG="$ROOT/config/models.json" python3 - <<'PY'
import json, os, shlex, sys
cfg_path = os.environ["CFG"]
model = os.environ["MODEL"]
with open(cfg_path, encoding="utf-8") as f:
    cfg = json.load(f)
fallbacks = cfg.get("local_fallbacks") or {}
fb = fallbacks.get(model)
if not isinstance(fb, dict) or "file" not in fb:
    keys = ", ".join(k for k, v in fallbacks.items() if isinstance(v, dict) and "file" in v)
    sys.exit(f"Modelo {model!r} nao existe em local_fallbacks. Opcoes: {keys}")
def emit(key, value):
    print(f"{key}={shlex.quote(str(value))}")
emit("GGUF_FILE", fb["file"])
emit("GGUF_REPO", fb["hf_repo"])
emit("CTX", fb["context_size"])
emit("THREADS", fb["threads"])
emit("MODEL_NAME", fb.get("name", model))
emit("QUANT", fb.get("quant", ""))
emit("RAM", fb.get("ram_estimate_gb", ""))
emit("ROLE", fb.get("role", ""))
PY
)"; then
  exit 1
fi
eval "$parsed"

GGUF_PATH="$MODEL_DIR/$GGUF_FILE"
GGUF_URL="https://huggingface.co/${GGUF_REPO}/resolve/main/${GGUF_FILE}"

echo " modelo  : $MODEL_NAME $QUANT  (~$RAM GB RAM, $ROLE)"
echo " destino : $GGUF_PATH"

KOB_BIN="$KOB_DIR/koboldcpp"
if [[ -x "$KOB_BIN" && "$FORCE" -eq 0 ]]; then
  echo ""
  echo "[1/3] KoboldCpp ja existe - pulando (use --force p/ atualizar)"
  echo "      $KOB_BIN"
else
  echo ""
  echo "[1/3] Consultando release estavel do KoboldCpp no GitHub..."
  arch="$(uname -m)"
  case "$arch" in
    x86_64|amd64) asset_name="koboldcpp-linux-x64" ;;
    aarch64|arm64) asset_name="koboldcpp-linux-arm64" ;;
    *)
      echo "Arquitetura nao suportada pelo fallback: $arch" >&2
      exit 1
      ;;
  esac
  if ! asset_url="$(
    ASSET="$asset_name" python3 - <<'PY'
import json, os, urllib.request, sys
req = urllib.request.Request(
    "https://api.github.com/repos/LostRuins/koboldcpp/releases/latest",
    headers={"User-Agent": "aidungeon-local", "Accept": "application/vnd.github+json"},
)
with urllib.request.urlopen(req, timeout=60) as resp:
    rel = json.load(resp)
want = os.environ["ASSET"]
for asset in rel.get("assets") or []:
    if asset.get("name") == want:
        print(asset["browser_download_url"])
        sys.exit(0)
names = ", ".join(a.get("name", "") for a in rel.get("assets") or [])
sys.exit(f"Asset {want!r} nao encontrado no release {rel.get('tag_name')}. Assets: {names}")
PY
  )"; then
    exit 1
  fi
  echo "      baixando $asset_name ..."
  curl -fL --retry 3 --retry-delay 2 -o "$KOB_BIN" "$asset_url"
  chmod +x "$KOB_BIN"
  echo "      OK -> $KOB_BIN"
fi

if [[ -f "$GGUF_PATH" && "$FORCE" -eq 0 ]]; then
  echo ""
  echo "[2/3] GGUF ja existe - pulando (use --force p/ refazer)"
  echo "      $GGUF_PATH"
else
  echo ""
  echo "[2/3] Baixando GGUF (arquivo grande, pode demorar bastante)..."
  echo "      $GGUF_URL"
  curl -fL --retry 3 --retry-delay 2 -o "$GGUF_PATH" "$GGUF_URL"
  echo "      OK -> $GGUF_PATH"
fi

START="$ROOT/start_llm_local.sh"
cat >"$START" <<EOF
#!/usr/bin/env bash
# start_llm_local.sh - gerado por tools/setup_llm_local.sh
# Fallback local: KoboldCpp com API OpenAI-compativel em http://127.0.0.1:5001/v1
set -euo pipefail
ROOT="\$(cd "\$(dirname "\${BASH_SOURCE[0]}")" && pwd)"
exec "\$ROOT/data/koboldcpp/koboldcpp" \\
  --model "\$ROOT/data/models/${GGUF_FILE}" \\
  --port 5001 \\
  --contextsize ${CTX} \\
  --threads ${THREADS}
EOF
chmod +x "$START"
echo ""
echo "[3/3] Gerado: $START"

echo ""
echo "============================================================"
echo " Concluido. Proximos passos:"
echo "============================================================"
echo "  1. Suba o servidor local:   bash start_llm_local.sh"
echo "  2. Aponte o backend p/ ele (uma das opcoes):"
echo "       - UI aba Config: base_url = http://127.0.0.1:5001/v1"
echo "       - PUT /api/settings/llm com {\"base_url\":\"http://127.0.0.1:5001/v1\"}"
echo "  3. Valide:  python3 tools/check_llm.py --base-url http://127.0.0.1:5001/v1"
echo "  4. Bench :  python3 tools/bench_llm.py --base-url http://127.0.0.1:5001/v1"
echo "     (KoboldCpp nao exige API key por padrao)"
