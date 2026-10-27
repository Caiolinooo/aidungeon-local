#!/usr/bin/env bash
# Instala o Wan 2.2 TI2V 5B Q8 sem baixar o pacote fp16 de 16,9 GiB.
# Uso na VM: bash deploy/vm/install_wan_video.sh
set -euo pipefail

COMFY=${COMFY_HOME:-/home/caio/ComfyUI}
GGUF_COMMIT=6ea2651e7df66d7585f6ffee804b20e92fb38b8a

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
  'https://huggingface.co/QuantStack/Wan2.2-TI2V-5B-GGUF/resolve/57437632ddd08bdcbd1508c866aa22e126ed51d2/Wan2.2-TI2V-5B-Q8_0.gguf?download=true' \
  "$COMFY/models/diffusion_models/Wan2.2-TI2V-5B-Q8_0.gguf" \
  57bece983817ab2f957546683bb670f13be7d99022d45674840cd999a050ea8f
fetch \
  'https://huggingface.co/Comfy-Org/Wan-2.2/resolve/main/split_files/text_encoders/umt5_xxl_fp8_e4m3fn_scaled.safetensors?download=true' \
  "$COMFY/models/text_encoders/umt5_xxl_fp8_e4m3fn_scaled.safetensors" \
  c3355d30191f1f066b26d93fba017ae9809dce6c627dda5f6a66eaa651204f68
fetch \
  'https://huggingface.co/Comfy-Org/Wan-2.2/resolve/main/split_files/vae/wan2.2_vae.safetensors?download=true' \
  "$COMFY/models/vae/wan2.2_vae.safetensors" \
  e40321bd36b9709991dae2530eb4ac303dd168276980d3e9bc4b6e2b75fed156

if [[ ! -d "$COMFY/custom_nodes/ComfyUI-GGUF/.git" ]]; then
  git clone https://github.com/city96/ComfyUI-GGUF.git "$COMFY/custom_nodes/ComfyUI-GGUF"
fi
git -C "$COMFY/custom_nodes/ComfyUI-GGUF" fetch origin "$GGUF_COMMIT"
git -C "$COMFY/custom_nodes/ComfyUI-GGUF" checkout "$GGUF_COMMIT"
"$COMFY/venv/bin/pip" install -r "$COMFY/custom_nodes/ComfyUI-GGUF/requirements.txt"

echo 'Wan instalado. Reinicie comfyui.service para carregar o node GGUF.'
