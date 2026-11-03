#!/usr/bin/env bash
# Instala o MiniMax H3 reduzido: GGUF Q4_0, encoder NVFP4 e VAE de vídeo INT8.
# Não baixa o DiT FP8 nem o BF16. Não move o pin do ComfyUI-GGUF
# (6ea2651e7df66d7585f6ffee804b20e92fb38b8a) e não reinicia llama-server.
# Uso na VM: bash deploy/vm/install_h3_video.sh
set -euo pipefail

COMFY=${COMFY_HOME:-/home/caio/ComfyUI}
NODE="$COMFY/custom_nodes/ComfyUI-H3-Multishot"
PIN=d7d197709fc93c1afb125e3db1dfd4ac0ae28c60

if [[ ! -d "$NODE/.git" ]]; then
  git clone https://github.com/jlucasmcrell/ComfyUI-H3-Multishot.git "$NODE"
fi
git -C "$NODE" fetch origin "$PIN"
git -C "$NODE" checkout "$PIN"
if ! rg -n "minimax_h3" "$NODE" >/dev/null; then
  echo 'ComfyUI-H3-Multishot checkout has no minimax_h3; do not download' >&2
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
  'https://huggingface.co/joeygambino/MiniMax-H3-curve-GGUF/resolve/main/fl2va/MiniMax-H3-fl2va-curve-Q4_0.gguf' \
  "$COMFY/models/diffusion_models/MiniMax-H3-fl2va-curve-Q4_0.gguf" \
  226c83f6b971077f61f4ccf4ffaaed5d7ccf914642b7c25359da1da2f13dcd1a
fetch \
  'https://huggingface.co/Comfy-Org/MiniMax-H3/resolve/main/text_encoders/qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors' \
  "$COMFY/models/text_encoders/qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors" \
  35a88d51044231fe332301d7a62aa81e3f2cba62febeb446e2c1e3e0ef76f2c6
fetch \
  'https://huggingface.co/Comfy-Org/MiniMax-H3/resolve/main/vae/minimax_h3_video_vae_int8_convrot.safetensors' \
  "$COMFY/models/vae/minimax_h3_video_vae_int8_convrot.safetensors" \
  52a2c8c73583c86e4f41cdcce3a6ad0ea562987bc0bf3d60a0cef5f5c8e60c0e

echo 'Reinicie comfyui.service para o cgroup novo (MemoryMax=36G, MemorySwapMax=8G). Não reinicie llama-server.'
