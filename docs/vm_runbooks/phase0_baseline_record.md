# Phase 0 Baseline Record (Measured Live on `vm.groupabz.com`)

**Date**: 2026-10-16  
**Host**: `vm.groupabz.com` (`pop-os`, NVIDIA L4 23,034 MiB)  
**Status**: Recorded prior to any service modification or restart.

---

## 1. GPU & Process VRAM (`nvidia-smi`)

| State | Total VRAM | Used VRAM | Free VRAM | Notes |
| :--- | :---: | :---: | :---: | :--- |
| **Idle** | `23,034 MiB` | `16,787 MiB` | `5,779 MiB` | `llama-server` (`14,546 MiB`), `abz-voice` (`1,426 MiB`), `sd-webui` (`316 MiB`), desktop/X (`~300 MiB`) |
| **Mid-Turn (LLM Generation)** | `23,034 MiB` | `16,787 MiB` | `5,779 MiB` | 220 tokens generated at `temperature=0.85` |
| **Mid-Portrait (SDXL 1024×1024)** | `23,034 MiB` | **`22,487 MiB`** | **`79 MiB`** | 30 steps `epiCRealism XL` at `1024×1024` (dangerously close to 23 GB ceiling!) |

### Per-Process VRAM at Baseline Idle
```text
pid, process_name, used_gpu_memory [MiB]
343960, /home/caio/llama.cpp-turboq-mtp/build/bin/llama-server, 14546 MiB
369675, venv/bin/python3 (abz-voice-audio), 1426 MiB
915356, venv/bin/python (sd-webui), 316 MiB
```

---

## 2. Exact `llama-server` Command Line (Phase 0 Rollback Point)

```bash
/home/caio/llama.cpp-turboq-mtp/build/bin/llama-server \
  --host 0.0.0.0 \
  --port 1234 \
  -t 8 \
  --threads-batch 16 \
  -b 8192 \
  -ub 4096 \
  --api-key [REDACTED_FROM_ENV] \
  -rea auto \
  -fa on \
  --tools all \
  --jinja \
  -m Qwen3.6-35B-A3B-Uncensored-HauhauCS-Aggressive-IQ2_M.gguf \
  --mmproj mmproj-Qwen3.6-35B-A3B-Uncensored-HauhauCS-Aggressive-f16.gguf \
  --n-gpu-layers 99 \
  -ncmoe 24 \
  --no-mmap \
  --image-min-tokens 1024
```
* **`/props` inspection**: `total_slots: 4`, `n_ctx: 262144`, unquantized `f16` KV cache.

---

## 3. Fixed Benchmark Sample Results (Phase 0)

* **LLM Turn (`temperature=0.85`, `max_tokens=220`)**:
  * **Throughput**: **`18.37 tok/s`** (`220 tokens` in `11.98s`)
  * **Sample Output**:
    > *"The air in the lantern-lit bazaar is thick enough to chew, smelling of roasted meat, exotic spices, and the metallic tang of dried blood. Torches sputter in iron sconces, casting long, flickering shadows that seem to dance with a life of their own..."*
* **SDXL Portrait (`1024×1024`, 30 steps, `epiCRealism XL`, seed `424242`)**:
  * **Latency**: **`27.03s`**
  * **Peak VRAM**: **`22,487 MiB`** (`79 MiB` free)
