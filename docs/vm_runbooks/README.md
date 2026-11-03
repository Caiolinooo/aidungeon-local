# GPU VM Runbooks & Tuning Guides

> **v0.9.0 (atual):** o narrador na VM roda `-np 2 -c 262144` (131072 por slot). O app corta `context_size` em 106496 e liga `id_slot` só quando `GET /props` devolve `total_slots >= 2`. Backup online, restart do llama-server e só então `pkill -f 'uvicorn app.main:app'` estão em [`deploy/vm/README.md`](../../deploy/vm/README.md) §8. As fases 0–4 abaixo são o histórico de tuning, não o alvo atual de slots ou de contexto.

## 1. Executive Summary & Access Status

- **VM Host**: `vm.groupabz.com`
- **Target User**: `caio`
- **Current Access Status**: `BLOCKED_ON_USER_SSH_KEY`
- **Verification Command**:
  ```bash
  ssh -o BatchMode=yes -o ConnectTimeout=5 caio@vm.groupabz.com true
  ```
- **Observed Error**:
  `caio@vm.groupabz.com: Permission denied (publickey,password).` (exit code 255)
- **Security Policy**:
  - STRICT: No passwords may ever be prompted, entered, or committed to git, docs, or logs.
  - Per `ORIGINAL_REQUEST.md § R5`: All VM execution phases requiring SSH are blocked until the user configures key-based SSH.
  - Local development (R2 conversation quality research, R3 UI/UX redesign, R4 memory bank / cards / sandboxed scripting, R5 regression safety) and Phase 4 mocked embeddings proceed uninterrupted.

---

## 2. Remote Infrastructure Specification

The VM runs Ubuntu 24.04 / 22.04 LTS on an **NVIDIA L4 Tensor Core GPU (24 GB VRAM / ~22.5 GiB usable)**.
The services co-located on the GPU VM are:

| Service | Port | Endpoint / Mode | VRAM Footprint (Nominal) | Notes |
|---|---|---|---|---|
| **Narrator LLM** (`llama-server`) | 1234 | `http://vm.groupabz.com:1234/v1` | ~10.6 GB | `Qwen3.6-35B-A3B-Uncensored-HauhauCS-Aggressive-IQ2_M.gguf`, MoE 35B (3B active), `enable_thinking=false` required; 5 slots, `-cram 0`, watchdog (Phase 5) |
| **Image Generation** (ComfyUI / FLUX.2 Klein) | 8188 | `http://127.0.0.1:8188` (local only) | DynamicVRAM com 2 GB de headroom | FLUX.2 Klein ativo; LTXV no mesmo serviço. Forge Neo/epiCRealism permanece instalado apenas como rollback e fica parado. |
| **Voice Stack** (Supertonic + Whisper) | 8000 / 8443 | `https://vm.groupabz.com/voice/v1` | ~1.4 GB | TTS (`supertonic-3`), STT (`whisper-1`) behind Nginx reverse proxy |
| **CPU Embeddings** (`llama-server`) | 8091 | `http://127.0.0.1:8091` (Phase 4) | 0.0 GB (0 MB VRAM) | `Qwen3-Embedding-0.6B-Q8_0.gguf` with `--embeddings --n-gpu-layers 0 --device none` and `CUDA_VISIBLE_DEVICES=` (without them CUDA still reserved 646 MiB) |

On 2026-10-18 a backdrop txt2img OOM'd inside `lowvram.send_me_to_gpu` (20 MiB allocation, 34 MiB free, 211 MiB of fragmented PyTorch cache). The half-moved CLIP token embedding then failed every later request with `wrapper_CUDA__index_select` (`cpu` vs `cuda:0`). `--medvram` stayed: 1344×768 left 21 MiB free, under the 2 GB bar for `--medvram-sdxl`. The unit drop-in and the rollback patch are `deploy/vm/sd-webui-alloc.conf` and `deploy/vm/sd-webui-lowvram-device.patch`.

---

## 3. Phased Tuning Roadmap (Appendix A)

```
+-------------------------------------------------------------------------+
| Phase 0: Measure Baseline (Rollback Anchor)                            |
| - Record nvidia-smi (idle, mid-turn, mid-portrait)                      |
| - Capture exact llama-server cmdline & unit                             |
| - Measure tok/s at temp 0.85 & 1024 portrait latency                    |
+-------------------------------------------------------------------------+
                                    |
                                    v
+-------------------------------------------------------------------------+
| Phase 1: Free VRAM (Context 40960, Slots 1, Batch -b 2048 -ub 512)     |
| - Drop training context (262144 -> 40960), cap UI context at 40960     |
| - Drop slots 4 -> 1; drop mmproj projector                              |
| - Gate: Peak VRAM during portrait <= 21 GB; tok/s >= 95% of baseline   |
+-------------------------------------------------------------------------+
                                    |
                                    v
+-------------------------------------------------------------------------+
| Phase 2: Narrator Quant Upgrade (Go/No-Go Gate: ~4 GB free VRAM)        |
| - If free VRAM during portrait >= 4 GB: test IQ3_XXS / IQ3_S (13-14 GB) |
| - Keep IQ2_M on disk side-by-side; 3-turn prose comparison              |
| - Gate: No OOM on portrait, response time stable, prose improves       |
+-------------------------------------------------------------------------+
                                    |
                                    v
+-------------------------------------------------------------------------+
| Phase 3: SDXL Checkpoint Comparison & Headroom Rules                   |
| - Compare epiCRealism XL vs Juggernaut XL on identical portrait prompt   |
| - Headroom rule: --medvram-sdxl ONLY if >= 2 GB free headroom at peak;   |
|   remain on --medvram if Phase 2 quant was upgraded                     |
+-------------------------------------------------------------------------+
                                    |
                                    v
+-------------------------------------------------------------------------+
| Phase 4: CPU Vector Memory (Qwen3-Embedding-0.6B on CPU Port 8081)      |
| - Systemd unit with --n-gpu-layers 0 (strictly zero GPU VRAM change)    |
| - Cosine ranking + keyword bonus; fallback to keyword if offline        |
+-------------------------------------------------------------------------+
```

---

## 4. Runbook Navigation Index

1. [Phase 0 — Baseline Measurement Runbook](phase0_baseline.md)
2. [Phase 1 — VRAM Liberation Runbook](phase1_vram_liberation.md)
3. [Phase 2 — Narrator Quant Upgrade Runbook](phase2_quant_upgrade.md)
4. [Phase 3 — SDXL Checkpoint Comparison Runbook](phase3_sdxl_comparison.md)
5. [Phase 4 — CPU Embeddings Deployment Runbook](phase4_cpu_embeddings.md)
6. [Mock Baseline Dataset](mock_baseline.json)
7. [Phase 5 — Slots, livelock #28280 e watchdog (G2): registro](phase5_slots_record.md)

---

## 5. Unblocking Procedure for User

To grant SSH access to the agent:
1. Ensure the user's public SSH key is authorized in `/home/caio/.ssh/authorized_keys` on `vm.groupabz.com`.
2. Confirm passwordless login from this workstation:
   ```bash
   ssh -o BatchMode=yes caio@vm.groupabz.com true && echo "SSH Ready"
   ```
3. Once ready, the orchestrator or user can re-trigger live execution of Phases 0-3.
