#!/usr/bin/env bash
# Instala o H3 pequeno: UD-Q2_K_XL, Qwen3-VL 4B INT8 já usado pelo FineView,
# a projeção mmh3-4b e o VAE H3 INT8. O encoder roda em CPU.
# Uso na VM: bash deploy/vm/install_h3_small.sh
set -euo pipefail

COMFY=${COMFY_HOME:-/home/caio/ComfyUI}
NODE="$COMFY/custom_nodes/ComfyUI-GGUF-Loader"
DISABLED="$COMFY/custom_nodes-disabled"
PIN=109a281b1a49570cb708f8543efea6383baba40a

# O ComfyUI-GGUF antigo registra os mesmos nomes e mascara o fork que lê H3
# pruned. A troca é reversível: a pasta é movida, nunca apagada.
if [[ -d "$COMFY/custom_nodes/ComfyUI-GGUF" ]]; then
  mkdir -p "$DISABLED"
  mv "$COMFY/custom_nodes/ComfyUI-GGUF" "$DISABLED/ComfyUI-GGUF"
fi

if ! rg -n "class MiniMaxH3" "$COMFY/comfy/supported_models.py" >/dev/null; then
  echo 'ComfyUI sem suporte nativo a MiniMax H3; não baixar' >&2
  exit 1
fi
if [[ ! -d "$NODE/.git" ]]; then
  git clone https://github.com/ChrisColeTech/ComfyUI-GGUF-Loader.git "$NODE"
fi
git -C "$NODE" fetch origin "$PIN"
git -C "$NODE" checkout "$PIN"
if ! "$COMFY/venv/bin/python" -c 'import cv2' 2>/dev/null; then
  "$COMFY/venv/bin/pip" install opencv-python-headless
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
  'https://huggingface.co/unsloth/MiniMax-H3-GGUF/resolve/main/minimax_h3_fl2va_pruned-UD-Q2_K_XL.gguf' \
  "$COMFY/models/diffusion_models/minimax_h3_fl2va_pruned-UD-Q2_K_XL.gguf" \
  cfe0795c00ab6e6ebf8c64fe4574f45a828e8a93e0876bca704e055662a9d7b8
fetch \
  'https://huggingface.co/Winnougan/Comfy-Qwen3-VL-INT8/resolve/main/qwen3vl_4b_int8_convrot.safetensors' \
  "$COMFY/models/text_encoders/qwen3vl_4b_int8_convrot.safetensors" \
  0455ed10f89789557a1b4f9983e778512cba1ad1268576b676f62f432a00a4dd
fetch \
  'https://huggingface.co/NicoLab28/ClipProj-MiniMax-H3/resolve/main/mmh3-4b-ClipProj-v3.1-mlp.safetensors' \
  "$COMFY/models/clip_projections/mmh3-4b-ClipProj-v3.1-mlp.safetensors" \
  902a9b96ed041aedf7463251c59d917fba94963b944dd3196c00b23ff0f2b6b0
fetch \
  'https://huggingface.co/Comfy-Org/MiniMax-H3/resolve/main/vae/minimax_h3_video_vae_int8_convrot.safetensors' \
  "$COMFY/models/vae/minimax_h3_video_vae_int8_convrot.safetensors" \
  52a2c8c73583c86e4f41cdcce3a6ad0ea562987bc0bf3d60a0cef5f5c8e60c0e

echo 'H3 UD-Q2 + Qwen3-VL 4B + projeção + VAE instalados. Reinicie comfyui.service.'
