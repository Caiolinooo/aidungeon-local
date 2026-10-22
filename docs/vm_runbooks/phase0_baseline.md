# Phase 0 Runbook: Baseline Measurement & Recording Specification

> **MANDATORY INVARIANT**: Phase 0 must be executed and recorded **before any VM service restart or parameter change**. This record serves as the authoritative rollback point. Nothing advances to Phase 1 without this completed baseline.

---

## 1. Prerequisites & Execution Environment

Connect to the VM:
```bash
ssh caio@vm.groupabz.com
```

Ensure no active user generation is underway before beginning measurements.

---

## 2. Command Lines to Execute on the VM

### Step 2.1 — Service Discovery & Process Command Lines

Record the exact command line and systemd unit definition for `llama-server`:

```bash
# 1. Capture systemd service definition
sudo systemctl cat llama-server.service || sudo systemctl cat llama*.service

# 2. Capture exact runtime command line with null bytes expanded
PID=$(pgrep -f "llama-server" | head -n 1)
echo "PID: $PID"
cat /proc/$PID/cmdline | tr '\0' ' ' ; echo ""

# 3. Capture listening ports and model files
ss -tulpn | grep -E "1234|7860|8000|8443|8081"
ls -lh /opt/models/ || ls -lh ~/models/
```

### Step 2.2 — Memory Measurement (nvidia-smi)

Measure total, used, free memory, and per-process VRAM allocation across three operating states:

#### A. Idle State (No turn or image running)
```bash
nvidia-smi --query-gpu=timestamp,memory.total,memory.used,memory.free --format=csv
nvidia-smi --query-compute-apps=pid,process_name,used_memory --format=csv
nvidia-smi
```

#### B. Mid-Turn State (Active LLM Generation)
Trigger a generation turn (see Step 2.3) and concurrently sample `nvidia-smi`:
```bash
# In background or parallel terminal:
for i in {1..10}; do
  nvidia-smi --query-compute-apps=timestamp,pid,process_name,used_memory --format=csv
  sleep 0.5
done
```

#### C. Mid-Portrait State (Active SDXL Generation)
Trigger a 1024x1024 portrait generation (see Step 2.4) and concurrently sample `nvidia-smi` to capture the peak VRAM surge:
```bash
# Concurrently sample during image generation:
for i in {1..20}; do
  nvidia-smi --query-gpu=timestamp,memory.used,memory.free --format=csv
  sleep 0.5
done
```

---

### Step 2.3 — Fixed Passage Generation Benchmark (tok/s at Temperature 0.85)

Use the reference adventure situation with exact parameters:
- **Model**: `Qwen3.6-35B-A3B-Uncensored-HauhauCS-Aggressive-IQ2_M.gguf`
- **Temperature**: `0.85`
- **Top P**: `0.95`
- **Repetition Penalty**: `1.08`
- **Stop Sequences**: `["\n> You", "\n> you"]`
- **Disable Thinking**: `chat_template_kwargs: {"enable_thinking": false}`

Execute using `curl` or `tools/bench_llm.py`:

```bash
python3 /mnt/DEV/Projeto/aidungeon-local/tools/bench_llm.py \
  --api-key "$LLM_API_KEY" \
  --temperature 0.85 \
  --prompt "You are Caio, an Aasimar sorcerer allied with the Zhentarim, standing at the gates of Calimport at dusk. The agent Thuldan waves at you from the crowded bazaar.\n\n> You greet Thuldan and ask about the sealed scroll."
```

Or via direct raw curl benchmark script:
```bash
curl -X POST http://127.0.0.1:1234/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $LLM_API_KEY" \
  -d '{
    "model": "Qwen3.6-35B-A3B-Uncensored-HauhauCS-Aggressive-IQ2_M.gguf",
    "messages": [
      {"role": "system", "content": "You are an AI storyteller running an interactive text adventure. Continue the story in second person (\"You ...\"), matching the established tone. Write vivid prose with dialogue, sensory detail and consequences for the player'\''s actions. Never decide what the player character says or does beyond their stated action."},
      {"role": "user", "content": "You are Caio, an Aasimar sorcerer allied with the Zhentarim, standing at the gates of Calimport at dusk. The agent Thuldan waves at you from the crowded bazaar.\n\n> You greet Thuldan and ask about the sealed scroll."}
    ],
    "temperature": 0.85,
    "top_p": 0.95,
    "max_tokens": 300,
    "stop": ["\n> You", "\n> you"],
    "chat_template_kwargs": {"enable_thinking": false},
    "stream": true
  }'
```

Record:
- Time to First Token (TTFT in seconds)
- Output tokens generated
- Total generation duration (seconds)
- Tokens per second (`tok/s = tokens / generation_seconds`)

---

### Step 2.4 — SDXL epiCRealism 1024 Portrait Timing Benchmark

Trigger a 1024x1024 portrait generation using the configured parameters:
- **Endpoint**: `http://127.0.0.1:7860/sdapi/v1/txt2img`
- **Checkpoint**: `epicrealismXL_vxviiCrystalclear.safetensors`
- **Resolution**: `1024 x 1024`
- **Steps**: `30`
- **Sampler**: `DPM++ 2M Karras` (or Euler a)
- **Prompt**: `"photorealistic photograph, natural skin texture, real lighting, sharp detail, portrait of an Aasimar sorcerer in robes at dusk in Calimport bazaar"`
- **Negative Prompt**: `"text, watermark, signature, low quality, deformed, blurry, cartoon, anime, illustration, cgi"`

Execute via curl and record wall-clock elapsed time:
```bash
time curl -X POST http://127.0.0.1:7860/sdapi/v1/txt2img \
  -H "Content-Type: application/json" \
  -d '{
    "prompt": "photorealistic photograph, natural skin texture, real lighting, sharp detail, portrait of an Aasimar sorcerer in robes at dusk in Calimport bazaar",
    "negative_prompt": "text, watermark, signature, low quality, deformed, blurry, cartoon, anime, illustration, cgi",
    "steps": 30,
    "width": 1024,
    "height": 1024,
    "override_settings": {
      "sd_model_checkpoint": "epicrealismXL_vxviiCrystalclear.safetensors"
    }
  }' -o /tmp/phase0_portrait.json
```

Record:
- Total execution time (seconds)
- Resulting image verification (base64 length > 0)
- Peak VRAM during generation

---

## 3. Authoritative Baseline Recording Template

When live data is captured, fill in and commit this table into `docs/vm_runbooks/phase0_baseline_record.md`:

```markdown
# Phase 0 Baseline Measurement Record

**Date Recorded**: [YYYY-MM-DD HH:MM:SS UTC]
**Operator**: [caio / agent]
**GPU Hardware**: NVIDIA L4 (24,117 MiB total capacity)
**Driver Version**: [e.g. 535.183.01 / 550.54.15]
**CUDA Version**: [e.g. 12.2 / 12.4]

### 1. Process Command Lines
- **llama-server PID**: [PID]
- **Command Line**:
  `llama-server -m /path/to/Qwen3.6-35B-A3B-Uncensored-HauhauCS-Aggressive-IQ2_M.gguf -c 262144 -b 8192 -ub 4096 --parallel 4 --mmproj /path/to/mmproj.gguf ...`
- **SDWebUI PID**: [PID]
- **Command Line**:
  `python launch.py --medvram --no-half-vae ...`
- **Voice Stack PID**: [PID]

### 2. VRAM Measurements (nvidia-smi)
| Operational State | Total VRAM (MiB) | Used VRAM (MiB) | Free VRAM (MiB) | llama-server VRAM | SDWebUI VRAM | Voice VRAM |
|---|---|---|---|---|---|---|
| **Idle** | 24,117 | [e.g. 16,100] | [e.g. 8,017] | [e.g. 14,500] | [e.g. 200] | [e.g. 1,400] |
| **Mid-Turn (LLM)** | 24,117 | [e.g. 16,800] | [e.g. 7,317] | [e.g. 15,200] | [e.g. 200] | [e.g. 1,400] |
| **Mid-Portrait (SDXL)** | 24,117 | [e.g. 20,400] | [e.g. 3,717] | [e.g. 14,500] | [e.g. 4,500] | [e.g. 1,400] |

### 3. Generation Performance
- **Fixed Passage Prompt**: Calimport bazaar / Thuldan scroll
- **TTFT (Time to First Token)**: [e.g. 0.42 s]
- **Tokens Generated**: [e.g. 185 tokens]
- **Total Generation Time**: [e.g. 8.8 s]
- **tok/s**: [e.g. 21.0 tok/s]

### 4. Image Generation Performance
- **Checkpoint**: `epicrealismXL_vxviiCrystalclear.safetensors`
- **Resolution**: 1024x1024 (30 steps, DPM++ 2M Karras)
- **Generation Time**: [e.g. 14.8 s]
- **Peak VRAM Surge**: [e.g. +4,300 MiB]
```

## Pendência: modelo do LLM

A troca do modelo fica fora do plano de interface e de peso de jogo. O registro abaixo é o estado verificado, para quando a troca for feita.

- Estado verificado em 2026-10-21: `GET http://vm.groupabz.com:1234/v1/models` responde 200 sem autenticação e devolve `Qwen3.6-35B-A3B-Uncensored-HauhauCS-Aggressive-IQ2_M.gguf`, `ftype "IQ2_M - 2.7 bpw"`, `n_ctx 262144`, `n_params 34660610888`.
- Consequência: quantização de 2 bits + fine-tune de RP degradam a aderência a JSON e a qualidade narrativa — é a causa primária de "a IA é imprecisa".
- O backend não lê `config/models.json`; o modelo efetivo vem de `llm.model_alias` (`"auto"`) → `GET {base_url}/models` (`backend/app/llm_client.py:261-275`).
- O `POST /v1/chat/completions` do servidor exige `LLM_API_KEY` (401 "Invalid API Key" sem ela); a chave vive no `.env` da VM (`deploy/vm/aidungeon-backend.service`, `.env.example`).
- Recomendação para quando a troca for feita: modelo de instrução com ≥ 14B em Q4_K_M ou melhor, com `response_format`/`grammar` JSON habilitado no llama-server (o `llm_client.chat_completion` ainda não envia `response_format` — `backend/app/llm_client.py:855-899`).
