#!/usr/bin/env bash
# Instala o FLUX.2 Klein 4B distilled oficial, encoder Qwen3 4B e VAE, com hashes pinados.
# Uso na VM: bash deploy/vm/install_flux2_klein.sh
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
  'https://huggingface.co/black-forest-labs/FLUX.2-klein-4B/resolve/main/flux-2-klein-4b-fp8.safetensors?download=true' \
  "$COMFY/models/diffusion_models/flux-2-klein-4b-fp8.safetensors" \
  97ed34fe0567e436200f2faee3939b88f2b5d99f8af2a4dc16532c4245c0ccb6
fetch \
  'https://huggingface.co/Comfy-Org/flux2-klein-4B/resolve/main/split_files/text_encoders/qwen_3_4b.safetensors?download=true' \
  "$COMFY/models/text_encoders/qwen_3_4b.safetensors" \
  6c671498573ac2f7a5501502ccce8d2b08ea6ca2f661c458e708f36b36edfc5a
fetch \
  'https://huggingface.co/Comfy-Org/flux2-klein-4B/resolve/main/split_files/vae/flux2-vae.safetensors?download=true' \
  "$COMFY/models/vae/flux2-vae.safetensors" \
  868fe7b343cc8f3a19dbcfcafbc3d5f888802be3f89bd81b65b3621a066ce8f3

echo 'FLUX.2 Klein instalado. O ComfyUI 0.42+ já fornece os nodes nativos; reinicie comfyui.service.'
