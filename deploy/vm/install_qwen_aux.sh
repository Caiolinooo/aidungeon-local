#!/usr/bin/env bash
# Baixa o Qwen2.5-3B-Instruct Q4_K_M para o llama-aux em CPU.
# Não instala, não habilita e não inicia llama-aux.service.
# Uso na VM: bash deploy/vm/install_qwen_aux.sh
set -euo pipefail

DEST=${QWEN_AUX_GGUF:-/home/caio/qwen2.5-3b-instruct-q4_k_m.gguf}
URL='https://huggingface.co/Qwen/Qwen2.5-3B-Instruct-GGUF/resolve/main/qwen2.5-3b-instruct-q4_k_m.gguf?download=true'
SHA=626b4a6678b86442240e33df819e00132d3ba7dddfe1cdc4fbb18e0a9615c62d
SIZE=2104932768

fetch() {
  local url=$1 dest=$2 sha=$3 size=$4
  if [[ -f "$dest" ]]; then
    local actual
    actual=$(stat -c%s "$dest")
    if [[ "$actual" == "$size" ]] && echo "$sha  $dest" | sha256sum -c -; then
      return
    fi
    rm -f "$dest"
  fi
  mkdir -p "$(dirname "$dest")"
  curl -fL --retry 3 --retry-delay 2 "$url" -o "$dest.part"
  local got
  got=$(stat -c%s "$dest.part")
  if [[ "$got" != "$size" ]]; then
    echo "tamanho inesperado: ${got} bytes (esperado ${size})" >&2
    exit 1
  fi
  echo "$sha  $dest.part" | sha256sum -c -
  mv "$dest.part" "$dest"
}

fetch "$URL" "$DEST" "$SHA" "$SIZE"

echo "Qwen2.5-3B-Instruct Q4_K_M em $DEST. llama-aux.service não foi habilitado."
