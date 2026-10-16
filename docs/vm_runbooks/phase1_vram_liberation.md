# Phase 1 Runbook: VRAM Liberation Without Model Change

## 1. Objective & Scope

Free substantial VRAM without changing the text model by eliminating wasteful context allocation, excessive slots, oversized batch buffers, and the unused multimodal vision projector.

| Parameter | Baseline (Current) | Target (Phase 1 — Max Context + Multimodal Kept) | Rationale |
|---|---|---|---|
| **Server Context (`-c`)** | `262144` (4 slots = 64k/slot) | `262144` (1 slot = full 256k/slot) | Because Qwen3.6-35B-A3B uses Hybrid Gated DeltaNet (30/40 layers $O(1)$ state) + only 10 GQA layers (2 KV heads), 1 slot at 256k with `q8_0` KV cache uses only `~1.35 GB` of VRAM! |
| **KV Cache Quantization** | `f16` (unquantized) | `--flash-attn -ctk q8_0 -ctv q8_0` | Cuts KV cache VRAM by 50% with near-zero perplexity loss, enabling full 256k context on 1 slot while freeing ~4 GB from the old 4-slot f16 allocation. |
| **Slots (`--parallel` / `-np`)** | `4` | `1` | Dedicates 100% of the 262,144-token context pool to the active slot instead of splitting across 4 slots. |
| **Batch (`-b`)** | `8192` | `2048` | Reduces scratch buffer memory overhead. |
| **Micro-batch (`-ub`)** | `4096` | `512` | Aligns evaluation chunks with typical turn prompt sizes. |
| **Vision Projector (`--mmproj`)** | Loaded (~0.9 GB) | **Kept Loaded (`--mmproj`)** | **Preserved** because this `llama-server` instance is shared with another project that requires visual/multimodal input. |
| **UI Context Cap (`context_size_limits.max`)** | `131072` | `262144` | Allows scaling up to the full 262,144 native context. |

---

## 2. Pre-Change Checks

Before modifying files or restarting services:
1. Verify Phase 0 baseline is fully recorded in `docs/vm_runbooks/phase0_baseline_record.md`.
2. Confirm no active generations are running:
   ```bash
   # Check if GPU compute processes are active
   nvidia-smi --query-compute-apps=pid,process_name,used_memory --format=csv
   ```
3. Backup existing service file:
   ```bash
   sudo cp /etc/systemd/system/llama-server.service /etc/systemd/system/llama-server.service.phase0.bak
   ```

---

## 3. Step-by-Step Execution on VM

### Step 3.1 — Edit `llama-server.service`

Locate and edit `/etc/systemd/system/llama-server.service`:
```bash
sudo nano /etc/systemd/system/llama-server.service
```

Update `ExecStart` parameters (keeping the existing `--mmproj` flag intact for multimodal compatibility):
```ini
[Service]
ExecStart=/usr/local/bin/llama-server \
  -m /opt/models/Qwen3.6-35B-A3B-Uncensored-HauhauCS-Aggressive-IQ2_M.gguf \
  --mmproj /opt/models/mmproj-Qwen3.6-35B-A3B-f16.gguf \
  --port 1234 \
  --host 0.0.0.0 \
  -c 262144 \
  --parallel 1 \
  --flash-attn \
  -ctk q8_0 \
  -ctv q8_0 \
  -b 2048 \
  -ub 512 \
  --api-key-file /opt/aidungeon/.llama_api_key
```
*(Preserve the exact `--mmproj` path from your original service file)*

### Step 3.2 — Reload & Restart llama-server

```bash
sudo systemctl daemon-reload
sudo systemctl restart llama-server
sudo systemctl status llama-server --no-pager
```

### Step 3.3 — Verify Server Parameters

Query the public models endpoint to verify context size and multimodal capability:
```bash
curl -s http://127.0.0.1:1234/v1/models | jq '.data[0].meta.n_ctx, .models[0].capabilities'
```
- Expect `n_ctx`: `262144`
- Expect `capabilities`: `["completion", "multimodal"]` (preserving `--mmproj` for the other project)

### Step 3.4 — Local Repo Configuration (Already Applied)

In `/mnt/DEV/Projeto/aidungeon-local/config/settings.json`:
1. `context.context_size` and `llm.context_size` default to `65536` (64k tokens — compatible both before and after Phase 1).
2. `llm.context_size_limits.max` is unlocked to `262144` (256k tokens), with quick presets (`32k`, `64k`, `128k`, `256k`) in `/settings`.

---

## 4. Post-Change Verification & Measurements

Repeat the Step 2 benchmarks from Phase 0:

1. **Idle VRAM**:
   ```bash
   nvidia-smi --query-gpu=memory.total,memory.used,memory.free --format=csv
   ```
2. **Mid-Turn tok/s**:
   Run `tools/bench_llm.py` with identical prompt and temperature `0.85`.
   Measure TTFT, total tokens, tok/s.
3. **Mid-Portrait Peak VRAM**:
   Trigger 1024x1024 portrait on `http://127.0.0.1:7860/sdapi/v1/txt2img`.
   Monitor peak VRAM during the 30 steps.

---

## 5. Phase 1 Acceptance Gate

| Gate Metric | Condition | Pass/Fail Action |
|---|---|---|
| **Peak VRAM during portrait** | **≤ 21.0 GB (≤ 21,504 MiB)** | If > 21 GB: **ROLLBACK IMMEDIATELY** to Phase 0. |
| **Generation Speed (tok/s)** | **≥ 95% of Phase 0 tok/s** | If < 95%: Investigate or rollback. |
| **Portrait Generation** | **Completes successfully without CUDA OOM** | If OOM: Rollback. |

### Rollback Procedure (If Gate Fails)
```bash
sudo cp /etc/systemd/system/llama-server.service.phase0.bak /etc/systemd/system/llama-server.service
sudo systemctl daemon-reload
sudo systemctl restart llama-server
# Revert config/settings.json context_size_limits.max back to 131072 if changed
```
Document the failure reason and measured VRAM peak in `docs/vm_runbooks/phase1_record.md`.
