# Briefing do Grok: Plataforma e Performance

Você cuida de concorrência, filas, execução de LLM e SD, deploy, segurança e da
VM. O Kimi K3 cuida do que o jogador lê e vê. Os contratos entre vocês estão em
[CONTRATOS.md](CONTRATOS.md) e são lei: não mude assinatura sem PR de contrato.

## Ambiente

- VM: `vm.groupabz.com`, usuário `caio`, acesso **só por chave SSH** (peça a sua
  ao usuário; nunca use nem grave senha em prompt, doc ou commit).
- GPU NVIDIA L4 24 GB, 36 vCPUs (Xeon Gold 6138), 49 GB de RAM, 82 GB livres em `/`.
- App: `/home/caio/aidungeon` (cópia, não é git), `aidungeon.service`, uvicorn na
  porta 2122, nginx TLS em `https://vm.groupabz.com:8443` → 2122.
- LLM: `llama-server.service` (root), fork `Indras-Mirror/llama.cpp-turboq-mtp`
  build b10281, porta 1234, modelo
  `Qwen3.6-35B-A3B-Uncensored-HauhauCS-Aggressive-IQ2_M.gguf` + `--mmproj` f16.
  Flags atuais: `-t 8 --threads-batch 16 -np 2 -c 262144 -ctk q8_0 -ctv q8_0
  -b 2048 -ub 512 -rea auto -fa on --tools all --jinja --n-gpu-layers 99
  -ncmoe 24 --no-mmap --image-min-tokens 1024`.
- Embeddings: `llama-embeddings.service`, CPU, porta 8091.
- SD: `sd-webui.service` (A1111 em `/home/caio/sd-webui`, `--medvram`), porta 7860.
- Outros clientes do `:1234`: open-webui e tarefas do hermes.
- Testes locais: `python -m pytest -q --ignore=tests/e2e_opaque` (339 verdes na Fase 0).

## Pode e deve mexer

- VM: units systemd (llama-server, SD, embeddings, app, staging), nginx, firewall, chaves.
- `deploy/vm/`, `docs/vm_runbooks/`, `tools/`.
- Backend: `llm_client.py`, `llm_scheduler.py` (novo), `jobs.py`,
  `image_client.py`, `events.py` (novo), `routes/events.py` (novo),
  `game_engine.py` (locks, pós-turno, ganchos), `routes/generation.py`,
  `routes/choices.py`, `routes/jobs.py`, `routes/settings.py`,
  `settings_store.py`, `routes/health.py`.
- `config/settings.json`, só as chaves `llm.*`, `scheduler.*`,
  `image.providers.*`, `image.presets.*`, `choices.*`.

## Não pode mexer

Prompts narrativos, lógica de cards, cena e likeness, frontend,
`context_builder.py`. Precisa de algo lá? Abra pedido de contrato.

## Tarefas em ordem

### G1 Hotfix "destravar" (primeiro deploy)
- Auxiliares com `cache_prompt=false` e slot ≠ 0 (C1).
- Tirar `acquire_generation_lock` de `post_choices` e `post_see`.
- Timeout curto por chamada auxiliar.
- Stubs no-op de `meta_commands` e `output_sanitizer` ligados no `game_engine` (C3).
- Reiniciar o A1111 e pôr na unit `MemoryHigh=12G`, `MemoryMax=14G`, `Restart=always`.
- Aceite: replay do turno das 23:18 de 17/10 sem 409; choices em ≤ 15 s ou falha
  limpa; backdrop sem espera de 180 s.

### G2 llama-server com 4 slots e sem livelock
- `tools/repro_slot_hang.py`: slot fixado, prompt longo em cache, depois prompt curto.
- Medir com o repro e com `tools/bench_llm.py`: (a) fork atual com a G1;
  (b) upstream mais recente com o mesmo GGUF; (c) `--ctx-checkpoints 4..8`.
- Flags alvo: `-np 4 --kv-unified`, `-c 262144 -ctk q8_0 -ctv q8_0`, `-ub` 1024
  ou 2048 (medir), `-cram 2048 --ctx-checkpoints 8 --metrics`, `--api-key-file`,
  bind em 127.0.0.1.
- `tools/slot_watchdog.py` + timer systemd: lê `GET /slots`; slot parado > 45 s
  reinicia o llama-server e publica `llm_degraded`.
- Invalidar o `_slot_cache` do app quando o servidor reiniciar.
- Aceite: soak de 200 turnos sem livelock, registrado em
  `docs/vm_runbooks/phase5_slots_record.md`.

### G3 Scheduler de LLM (`llm_scheduler.py`, C1)
- Fila com prioridade; no máximo 2 auxiliares durante a narrativa.
- Cancelamento real (stream interno; cancelar fecha a conexão).
- Circuit breaker: 3 timeouts → `LlmDegraded` por 60 s.
- Fallback: TTFT da narrativa > 30 s → retry sem slot fixo e aciona o watchdog.
- Métricas por chamada: `prompt_n`, `cache_n`, tok/s.
- Pós-turno: choices e chronicle em paralelo; summary depois do chronicle; ação
  nova cancela choices e summary, nunca o chronicle.
- `web_search.research_context` usa role `assistant`, rede ≤ 5 s.
- Aceite: testes com transport mock (prioridade, cancelamento, breaker, < 4 slots).

### G4 Event bus e SSE (C2)
- Pub/sub em memória por aventura, buffer curto para reconexão.
- Aceite: teste de integração da ordem dos eventos e da reconexão sem perda.

### G5 Jobs com prioridade e See assíncrono (C5, C6)
- Job `see_image`; prompt LLM antes da fila do SD; executor chama os planejadores C4.
- Aceite: `POST /see` < 300 ms; jogador age enquanto pinta; backdrop velho cancelado.

### G6 Stack de imagem rápida e fiel
- A1111 → Forge (mesma API `/sdapi/v1/*`, ControlNet/IP-Adapter/InstantID).
  Manter `/home/caio/sd-webui` para rollback. Plano B: ComfyUI.
- DMD2 (`dmd2_sdxl_4step_lora_fp16`), LCM, 4–8 steps, CFG 1–1,5. A/B em 10
  prompts fixos contra 20 steps DPM++ 2M Karras CFG 5. Com CFG 1 o negative não age.
- Presets: portrait 832×1216, see 1216×832, backdrop 1344×768. Remover o
  override 800×720 do DB.
- `render(plan)` com `face_refs` → ControlNet (IP-Adapter FaceID Plus v2 SDXL ou
  InstantID), ordem dos args por `GET /sdapi/v1/script-info`.
- Aceite: retrato ≤ 8 s, See ≤ 12 s, VRAM ≤ 21,5 GB com 4 slots, RSS do SD
  estável por 24 h.

### G7 Orçamento de memória e velocidade
- Embeddings com `--device none`. Medir `-ncmoe` 24/20/16, `-ub`, `-t` 8/12/16/24,
  `context_size` 32768. Opcional: `--spec-type ngram-*` (MTP fora: GGUF sem
  tensores MTP), IQ3 conforme `docs/vm_runbooks/phase2_quant_upgrade.md`.
- Aceite: medições e decisão no runbook.

### G8 Segurança
- API key nova (≠ senha) via `--api-key-file` (root, 640). SSH só por chave.
- llama-server em 127.0.0.1; PC acessa por túnel SSH ou nginx TLS. ufw.
- Atualizar open-webui, hermes, `.env` do app. Identificar a origem dos
  "Invalid API Key" (1.746 no log de 18/10).
- `PUT /settings/llm` ignora `api_key` com "…" ou não ASCII.
- Aceite: `ps` sem a chave; porta 1234 fechada para fora; teste do PUT verde.

### G9 Deploy reprodutível
- VM vira checkout git em `/home/caio/aidungeon-git` (migrar `.env` e `data/`
  com backup; manter o diretório antigo para rollback).
- `tools/deploy.sh`: pull, venv, migrations, build do front, restart, smoke.
- Staging: `aidungeon-staging.service` na porta 2123 com cópia do DB.
- Aceite: deploy e rollback testados e documentados.

### G10 Observabilidade
- `/api/health/llm` com slots, breaker, fila de jobs e RAM/VRAM.
- Log por turno com TTFT, tok/s e `cache_n`.
- Aceite: degradação visível em ≤ 5 s.

## Regras

- Branch por tarefa a partir da branch da Fase 0; PR pequeno com `pytest` verde.
- Staging antes do ambiente principal (porta 2122), em janela combinada com o usuário.
- Proibido: force-push; commitar `.env`, DB, `data/images`, `data/references`,
  chaves ou senhas; apagar o modelo IQ2_M; desligar o guardrail de pessoa real.
- Consulte docs atuais (Context7: llama.cpp server, Forge/ControlNet) antes de
  usar flag ou API nova.
- Atualize `tasks.md` e os runbooks no mesmo PR.
