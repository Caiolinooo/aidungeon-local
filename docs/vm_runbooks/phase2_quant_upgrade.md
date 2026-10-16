# Phase 2 Runbook: Narrator Quantization Upgrade Evaluation

## 1. Objective & Go/No-Go Decision Gate

Upgrade the narrator model by one quantization step to improve prose richness and instruction adherence, provided sufficient VRAM headroom was freed in Phase 1.

### The Go/No-Go Gate
- **Condition**: Must have **≈ 4 GB (4,096 MiB) of FREE VRAM during an active SDXL 1024 portrait** in Phase 1.
- **Decision Logic**:
  - If Free VRAM at Portrait Peak < 3.5 GB: **NO-GO**. Stay with `IQ2_M`. Do NOT download or deploy IQ3. Document the decision.
  - If Free VRAM at Portrait Peak ≥ 3.8 GB: **GO**. Proceed to Step 2.

---

## 2. Model Candidates & Storage Rules

- **Current Baseline File**:
  `/opt/models/Qwen3.6-35B-A3B-Uncensored-HauhauCS-Aggressive-IQ2_M.gguf`
  *(File size: ~11.65 GB, ~2.7 bpw)*
- **STRICT CONSTRAINT**: **NEVER DELETE `IQ2_M`**. It must remain side-by-side on disk as the immediate fallback.
- **Primary Candidate**:
  `Qwen3.6-35B-A3B-Uncensored-HauhauCS-Aggressive-IQ3_XXS.gguf` (~13.0 GB) or `IQ3_S.gguf` (~14.1 GB).

### Download Command (Side-by-Side)
```bash
cd /opt/models/
# Download without removing IQ2_M:
huggingface-cli download HauhauCS/Qwen3.6-35B-A3B-Uncensored-GGUF \
  Qwen3.6-35B-A3B-Uncensored-HauhauCS-Aggressive-IQ3_XXS.gguf \
  --local-dir /opt/models/ --local-dir-use-symlinks False
```

---

## 3. Deployment Procedure

1. Verify no generations are running (`nvidia-smi`).
2. Update `/etc/systemd/system/llama-server.service`:
   Change model path to:
   `-m /opt/models/Qwen3.6-35B-A3B-Uncensored-HauhauCS-Aggressive-IQ3_XXS.gguf`
3. Reload and restart:
   ```bash
   sudo systemctl daemon-reload
   sudo systemctl restart llama-server
   sudo systemctl status llama-server --no-pager
   ```
4. Verify via `/v1/models`:
   ```bash
   curl -s http://127.0.0.1:1234/v1/models | jq '.data[0].id, .data[0].meta.ftype'
   ```

---

## 4. Evaluation Protocol

Execute these 3 evaluation tests:

### Test 4.1 — 3-Turn Prose Comparison

Run 3 consecutive story turns using the reference adventure scenario. Compare output quality between `IQ2_M` and `IQ3`:

**Turn 1: Narrative Entry**
- *Prompt*: `You are Caio, an Aasimar sorcerer allied with the Zhentarim, standing at the gates of Calimport at dusk. The agent Thuldan waves at you from the crowded bazaar.\n\n> You greet Thuldan and ask about the sealed scroll.`
- *Inspect*: Sensory atmosphere, NPC dialogue naturalness, no player-impersonation.

**Turn 2: Complex Action**
- *Prompt*: `Thuldan slides the wax-sealed cylinder into your sleeve, warning that the Pasha's vizier has doubled the guard at the docks.\n\n> You examine the sigil with your magical sight while keeping an eye on the shadowed archway.`
- *Inspect*: Arcane description coherence, tactical awareness, tone consistency.

**Turn 3: Consequence & Branching**
- *Prompt*: `The sigil pulses with faint evocation wards. A city watch patrol rounds the corner, torches flaring in the evening gloom.\n\n> You whisper an incantation to conceal the aura and step into the spice vendor's stall.`
- *Inspect*: Consequence handling, narrative pacing, vocabulary diversity.

### Test 4.2 — Fixed Passage tok/s Speed Check
Measure tok/s on Turn 1 prompt at temperature 0.85. Compare against Phase 0 / Phase 1 baseline.

### Test 4.3 — Concurrent SDXL Portrait Run
Trigger a 1024x1024 portrait generation in SDWebUI while the IQ3 model is loaded. Monitor peak VRAM.

---

## 5. Decision Gates & Rollback

| Metric | Criterion | Action |
|---|---|---|
| **Portrait Memory Failure** | Any CUDA OOM in SDWebUI or llama-server crash | **ROLLBACK to IQ2_M immediately**. |
| **tok/s Speed Degradation** | Significant throughput drop (>25% slower than IQ2_M) | **ROLLBACK to IQ2_M**. |
| **Prose Quality** | IQ3 does NOT show noticeable improvement across 3 turns | **ROLLBACK to IQ2_M**. |
| **Next Alternative (if IQ3 fails)** | Separate test: dense 14B model in Q5_K_M or Q6_K | Evaluated as a distinct experiment; one model swap at a time. |

### Rollback Command to IQ2_M
```bash
sudo sed -i 's/IQ3_XXS/IQ2_M/g' /etc/systemd/system/llama-server.service
sudo systemctl daemon-reload
sudo systemctl restart llama-server
```
