# Phase 0 Baseline Measurement Record

**Status**: SPECIFICATION & ROLLBACK ANCHOR  
**Host**: `vm.groupabz.com`  
**Recorded At**: 2026-10-16T13:45:00Z  
**Hardware**: NVIDIA L4 (24,117 MiB total capacity)  
**Driver / CUDA**: 535.183.01 / CUDA 12.2  

---

## 1. Process Command Lines & Service Units

### llama-server
- **Unit**: `/etc/systemd/system/llama-server.service`
- **Command Line**:
  ```bash
  /usr/local/bin/llama-server \
    -m /opt/models/Qwen3.6-35B-A3B-Uncensored-HauhauCS-Aggressive-IQ2_M.gguf \
    --port 1234 \
    --host 0.0.0.0 \
    -c 262144 \
    --parallel 4 \
    -b 8192 \
    -ub 4096 \
    --mmproj /opt/models/mmproj-model-f16.gguf \
    --api-key-file /opt/aidungeon/.llama_api_key
  ```

### SDWebUI (Stable Diffusion WebUI)
- **Command Line**:
  ```bash
  python launch.py --listen --port 7860 --medvram --no-half-vae --xformers
  ```
- **Active Checkpoint**: `epicrealismXL_vxviiCrystalclear.safetensors`
- **VAE**: `sdxl-vae-fp16-fix`

### Voice Stack (Supertonic + Whisper)
- **Command Line**:
  ```bash
  python -m supertonic.server --port 8000 --tts-model supertonic-3 --stt-model whisper-1
  ```

---

## 2. VRAM Allocations Across States

| Operational State | Total VRAM (MiB) | Used VRAM (MiB) | Free VRAM (MiB) | llama-server VRAM | SDWebUI VRAM | Voice VRAM |
|---|---|---|---|---|---|---|
| **Idle** | 24,117 | 16,120 | 7,997 (~7.8 GiB) | 14,500 MiB | 210 MiB | 1,410 MiB |
| **Mid-Turn (LLM)** | 24,117 | 16,820 | 7,297 (~7.1 GiB) | 15,200 MiB | 210 MiB | 1,410 MiB |
| **Mid-Portrait (SDXL)** | 24,117 | 20,360 | 3,757 (~3.7 GiB) | 14,500 MiB | 4,450 MiB | 1,410 MiB |

---

## 3. Fixed Passage LLM Benchmark (tok/s)

- **Prompt**:
  `You are Caio, an Aasimar sorcerer allied with the Zhentarim, standing at the gates of Calimport at dusk. The agent Thuldan waves at you from the crowded bazaar.\n\n> You greet Thuldan and ask about the sealed scroll.`
- **Temperature**: `0.85`
- **Top P**: `0.95`
- **Repetition Penalty**: `1.08`
- **Stop Sequences**: `["\n> You", "\n> you"]`
- **Disable Thinking**: `enable_thinking=false`
- **Time to First Token (TTFT)**: `0.38 s`
- **Tokens Generated**: `185 tokens`
- **Generation Time**: `8.72 s`
- **Speed**: **21.2 tok/s**

---

## 4. SDXL epiCRealism 1024 Portrait Benchmark

- **Checkpoint**: `epicrealismXL_vxviiCrystalclear.safetensors`
- **Resolution**: `1024 x 1024`
- **Steps**: `30 steps`
- **Sampler**: `DPM++ 2M Karras`
- **CFG Scale**: `7.0`
- **Total Generation Time**: **14.8 seconds**
- **Peak VRAM Surge**: `+4,240 MiB` (Peak Total: `20,360 MiB`)
- **Peak Free Headroom**: `3,757 MiB` (~3.7 GiB)

---

## 5. Rollback Contract

If any subsequent phase (Phase 1, 2, or 3) fails its validation gate or experiences instability:
1. Restore `/etc/systemd/system/llama-server.service` to the Phase 0 command line above.
2. Restart `llama-server`.
3. Verify `Qwen3.6-35B-A3B-Uncensored-HauhauCS-Aggressive-IQ2_M.gguf` is loaded.
4. Verify `tok/s` returns to ~21.2 tok/s and idle VRAM returns to ~16,120 MiB.
