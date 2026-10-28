#!/usr/bin/env bash
# Instala o LTXV 2B 0.9.6 distilled e seu encoder T5 FP8, com hashes pinados.
# Uso na VM: bash deploy/vm/install_ltxv_video.sh
set -euo pipefail

COMFY=${COMFY_HOME:-/home/caio/ComfyUI}

fetch() {
  local url=$1 dest=$2 sha=$3
  if [[ -f "$dest" ]] && echo "$sha  $dest" | sha256sum -c -; then
    return
  fi
  mkdir -p "$(dirname "$dest")"
  curl -fL --retry 3 "$url" -o "$dest.part"
  echo "$sha  $dest.part" | sha256sum -c -
  mv "$dest.part" "$dest"
}

fetch \
  'https://huggingface.co/Lightricks/LTX-Video/resolve/main/ltxv-2b-0.9.6-distilled-04-25.safetensors?download=true' \
  "$COMFY/models/checkpoints/ltxv-2b-0.9.6-distilled-04-25.safetensors" \
  94891bd4bd08de30d484befbfc54fdcffe6d1596a131baad700b9baa5e1de86b
fetch \
  'https://huggingface.co/Comfy-Org/mochi_preview_repackaged/resolve/main/split_files/text_encoders/t5xxl_fp8_e4m3fn_scaled.safetensors?download=true' \
  "$COMFY/models/text_encoders/t5xxl_fp8_e4m3fn_scaled.safetensors" \
  a498f0485dc9536735258018417c3fd7758dc3bccc0a645feaa472b34955557a

echo 'LTXV instalado. O ComfyUI atual já fornece os nodes nativos; reinicie comfyui.service.'
