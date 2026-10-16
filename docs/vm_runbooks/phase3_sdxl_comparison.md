# Phase 3 Runbook: SDXL Checkpoint Comparison & Headroom Rules

## 1. Objective & Scope

Evaluate the two available SDXL checkpoints on disk without downloading any new models:
1. `epiCRealism XL CrystalClear` (`epicrealismXL_vxviiCrystalclear.safetensors`)
2. `Juggernaut XL` (already present on VM disk)

Select the best visual performer for character portraits and scene backdrops in dark fantasy interactive fiction, and establish whether to switch from `--medvram` to `--medvram-sdxl` based on measured headroom.

---

## 2. Checkpoint Comparison Protocol

### Rule: Single Model Loaded
Only one checkpoint is loaded in VRAM at any time. Checkpoints are switched via the A1111 WebUI API.

### Common Benchmark Parameters
- **Resolution**: `1024 x 1024`
- **Steps**: `30`
- **Sampler**: `DPM++ 2M Karras`
- **CFG Scale**: `7.0`
- **VAE**: `sdxl-vae-fp16-fix`
- **Positive Prompt**:
  `photorealistic photograph, natural skin texture, real lighting, sharp detail, portrait of an Aasimar sorcerer with glowing golden eyes in ornate dark robes at dusk in Calimport bazaar`
- **Negative Prompt**:
  `text, watermark, signature, low quality, deformed, blurry, cartoon, anime, illustration, cgi, 3d render`

---

## 3. Execution Commands on the VM

### Step 3.1 — Switch to epiCRealism XL & Generate
```bash
# 1. Select checkpoint
curl -X POST http://127.0.0.1:7860/sdapi/v1/options \
  -H "Content-Type: application/json" \
  -d '{"sd_model_checkpoint": "epicrealismXL_vxviiCrystalclear.safetensors"}'

# 2. Generate portrait
curl -X POST http://127.0.0.1:7860/sdapi/v1/txt2img \
  -H "Content-Type: application/json" \
  -d '{
    "prompt": "photorealistic photograph, natural skin texture, real lighting, sharp detail, portrait of an Aasimar sorcerer with glowing golden eyes in ornate dark robes at dusk in Calimport bazaar",
    "negative_prompt": "text, watermark, signature, low quality, deformed, blurry, cartoon, anime, illustration, cgi, 3d render",
    "steps": 30,
    "width": 1024,
    "height": 1024,
    "sampler_name": "DPM++ 2M Karras",
    "cfg_scale": 7.0
  }' > /tmp/portrait_epicrealism.json

# 3. Extract and save image
jq -r '.images[0]' /tmp/portrait_epicrealism.json | base64 -d > docs/assets/portrait_epicrealism_1024.png
```

### Step 3.2 — Switch to Juggernaut XL & Generate
```bash
# 1. Select checkpoint
curl -X POST http://127.0.0.1:7860/sdapi/v1/options \
  -H "Content-Type: application/json" \
  -d '{"sd_model_checkpoint": "juggernautXL_v9Rundiffusion.safetensors"}'

# 2. Generate portrait with identical seed & prompt
curl -X POST http://127.0.0.1:7860/sdapi/v1/txt2img \
  -H "Content-Type: application/json" \
  -d '{
    "prompt": "photorealistic photograph, natural skin texture, real lighting, sharp detail, portrait of an Aasimar sorcerer with glowing golden eyes in ornate dark robes at dusk in Calimport bazaar",
    "negative_prompt": "text, watermark, signature, low quality, deformed, blurry, cartoon, anime, illustration, cgi, 3d render",
    "steps": 30,
    "width": 1024,
    "height": 1024,
    "sampler_name": "DPM++ 2M Karras",
    "cfg_scale": 7.0
  }' > /tmp/portrait_juggernaut.json

# 3. Extract and save image
jq -r '.images[0]' /tmp/portrait_juggernaut.json | base64 -d > docs/assets/portrait_juggernaut_1024.png
```

---

## 4. Headroom Evaluation: `--medvram` vs `--medvram-sdxl`

### The Decision Rule (from Appendix A)
1. **Rule 1**: If Phase 2 upgraded the text model quantization (e.g. to IQ3), **REMAIN ON `--medvram`**. Do not use `--medvram-sdxl`.
2. **Rule 2**: If Phase 2 was a NO-GO (stayed on IQ2_M), inspect the measured free VRAM at peak portrait generation from Phase 1.
   - If Free VRAM at Peak ≥ 2.0 GB (2,048 MiB): `--medvram-sdxl` may be enabled to reduce VRAM offloading latency.
   - If Free VRAM at Peak < 2.0 GB: **REMAIN ON `--medvram`**.

---

## 5. Artifacts to Commit

- `docs/assets/portrait_epicrealism_1024.png`
- `docs/assets/portrait_juggernaut_1024.png`
- Written decision and justification in `docs/vm_runbooks/phase3_record.md`:
  - Visual fidelity comparison (facial details, skin texture, lighting, fantasy elements).
  - Selected checkpoint name for `config/settings.json -> image.providers.sdwebui.sd_model_checkpoint`.
  - Final `--medvram` flag configuration.
