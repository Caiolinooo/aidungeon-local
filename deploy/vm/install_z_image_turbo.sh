#!/usr/bin/env bash
# Instala o Z-Image-Turbo Q4_K_M (GGUF), o encoder FP8 e o VAE ae.
# Não baixa BF16, NVFP4 nem INT8. Não move o checkout do ComfyUI-GGUF.
# Uso na VM: bash deploy/vm/install_z_image_turbo.sh
set -euo pipefail

COMFY=${COMFY_HOME:-/home/caio/ComfyUI}

if ! rg -n "lumina2" "$COMFY/custom_nodes/ComfyUI-GGUF/loader.py" >/dev/null \
   || ! rg -n "class ZImage" "$COMFY/comfy/supported_models.py" >/dev/null; then
  echo 'ComfyUI-GGUF or ZImage support is missing; do not download' >&2
  exit 1
fi

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
  'https://huggingface.co/unsloth/Z-Image-Turbo-GGUF/resolve/main/z-image-turbo-Q4_K_M.gguf' \
  "$COMFY/models/diffusion_models/z-image-turbo-Q4_K_M.gguf" \
  4ab6bceb1f0b82c9e725f123848018eac662a6fc1d467c019e13e701afb7b55c
fetch \
  'https://huggingface.co/Comfy-Org/z_image_turbo/resolve/main/split_files/text_encoders/qwen_3_4b_fp8_mixed.safetensors' \
  "$COMFY/models/text_encoders/qwen_3_4b_fp8_mixed.safetensors" \
  72450b19758172c5a7273cf7de729d1c17e7f434a104a00167624cba94f68f15
fetch \
  'https://huggingface.co/Comfy-Org/z_image_turbo/resolve/main/split_files/vae/ae.safetensors' \
  "$COMFY/models/vae/ae.safetensors" \
  afc8e28272cd15db3919bacdb6918ce9c1ed22e96cb12c4d5ed0fba823529e38

echo 'Z-Image-Turbo Q4_K_M instalado. O checkout do ComfyUI-GGUF não foi alterado.'
