# Phase 4 Runbook: CPU Vector Memory Deployment & Infrastructure

## 1. Architectural Overview & Hardware Policy

The memory bank requires multilingual semantic embedding capability (supporting Portuguese and English narrative facts) without consuming any VRAM on the NVIDIA L4 GPU.

### Model Selection
- **Selected Model**: `Qwen3-Embedding-0.6B` (GGUF format, ~0.6B parameters).
- **Rejected Alternative**: `bge-small-en-v1.5` (English only, fails on Portuguese narrative).
- **GPU Invariant**: **Zero VRAM Delta**. The model must run with `--n-gpu-layers 0` exclusively on the VM host CPU and system RAM.

---

## 2. VM Deployment Procedure

### Step 2.1 — Download Qwen3-Embedding-0.6B GGUF

```bash
cd /opt/models/
# Download GGUF
huggingface-cli download Qwen/Qwen3-Embedding-0.6B-GGUF \
  qwen3-embedding-0.6b-f16.gguf \
  --local-dir /opt/models/ --local-dir-use-symlinks False
```

### Step 2.2 — Create Systemd Unit (`aidungeon-embeddings.service`)

Create `/etc/systemd/system/aidungeon-embeddings.service`:

```ini
[Unit]
Description=AI Dungeon Local - CPU Embedding Server (llama-server)
After=network.target

[Service]
Type=simple
User=aidungeon
Group=aidungeon
ExecStart=/usr/local/bin/llama-server \
  -m /opt/models/qwen3-embedding-0.6b-f16.gguf \
  --host 127.0.0.1 \
  --port 8081 \
  --embeddings \
  --n-gpu-layers 0 \
  -c 2048 \
  --parallel 2
Restart=on-failure
RestartSec=5

NoNewPrivileges=true
ProtectSystem=full
ProtectHome=true
PrivateTmp=true

[Install]
WantedBy=multi-user.target
```

### Step 2.3 — Enable and Start Service

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now aidungeon-embeddings.service
sudo systemctl status aidungeon-embeddings.service --no-pager
```

### Step 2.4 — Verification of GPU Invariance (Zero VRAM Delta)

Inspect GPU processes and compute allocations:
```bash
# Verify no PID for port 8081 appears in nvidia-smi
nvidia-smi --query-compute-apps=pid,process_name,used_memory --format=csv
```
Expected output: Port 8081 process (`llama-server`) does **NOT** appear in compute apps. VRAM delta = `0 MiB`.

### Step 2.5 — Health & Embedding Test via Curl

```bash
curl -X POST http://127.0.0.1:8081/embeddings \
  -H "Content-Type: application/json" \
  -d '{
    "model": "qwen3-embedding-0.6b-f16.gguf",
    "input": "O amuleto foi escondido na tumba antiga."
  }'
```
Expected output: JSON containing `"data": [{"embedding": [ ... ], "index": 0}]` with vector dimension 1024.

---

## 3. Backend Integration Contract (for Milestone 2)

1. **Settings Block in `config/settings.json`**:
   ```json
   "embeddings": {
     "enabled": true,
     "base_url": "http://127.0.0.1:8081",
     "model": "qwen3-embedding-0.6b-f16.gguf",
     "dimension": 1024,
     "timeout_seconds": 5
   }
   ```
2. **Database Migration `008_memory_embeddings.sql`**:
   - `ALTER TABLE memories ADD COLUMN embedding_model TEXT;`
   - `ALTER TABLE memories ADD COLUMN embedding_dim INTEGER;`
   - (`embedding BLOB` already exists in `schema.sql`).
3. **Hybrid Retrieval**:
   - Cosine similarity between query vector and stored memory vectors.
   - Small keyword bonus for exact matching terms (e.g. proper nouns).
   - Strict 800 token ceiling and newest-memory inclusion invariant.
   - Graceful fallback: If embedding service is down (503 / connection refused), seamlessly fall back to exact keyword ranking.
