# Correção de imagens, latência dos turnos e orçamento de VRAM

## Contexto

Implementar todo o P0–P2 autorizado: eliminar o erro real `860x820` dos retratos FLUX.2, impedir que clipes e jobs aparentemente travados bloqueiem imagens, reduzir a espera antes da narrativa e medir cada fase. O narrador Qwen3.6-35B-A3B, os pesos FLUX.2 Klein/LTXV e seus parâmetros de qualidade permanecem inalterados. Toda mudança experimental de infra/modelo só permanece se passar em correção, qualidade, estabilidade e memória, com **mínimo de 900 MiB de VRAM livre em todas as amostras**.

## Medições e fatores já confirmados

- A unidade `/etc/systemd/system/llama-server.service` usa `-np 5 --kv-unified -cram 0 -c 262144 -ctk q8_0 -ctv q8_0 -b 2048 -ub 1024`; o slot 4 existe e está sem papel fixo.
- `docs/vm_runbooks/phase5_slots_record.md:68-76`: `-ub 2048` acrescenta 592 MiB ao pico do llama e aumenta o prompt medido de 1.291 para 1.596 tok/s.
- Em 2026-10-29, a VM ociosa tinha 16.930 MiB de VRAM usados e 5.636 livres, mas somente 2.868 MiB de RAM disponível e 18.546 MiB de swap em uso. O `nvidia-smi` histórico reporta `used + free = 22.566`; aceite usa diretamente o `memory.free` reportado, não o total nominal de 23.034 MiB.
- Não há medição de pico do Klein/LTXV no estado atual. `-cram 0` evita o livelock já reproduzido; cache não será reativado sem soak e folga de RAM.
- Falha real no banco principal: aventura `e17f7abe1731489c9104b502d9584dc9`, cards `b261430a5ccd4acea2f2975af3749874` (Ladrão de Jibóia), `f04a36c71a4c404fae796e667ef4af18` e `823548f64daf43a1ac1c3ec3d0b6d832`, erro `config_error: flux2 exige width e height múltiplos de 16 (recebido 860x820)`.
- Staging ativo em `127.0.0.1:2123`; ele e o principal compartilham llama/ComfyUI. Testes que escrevem ficam exclusivamente no staging e mudanças do llama exigem jogador principal inativo.

## Abordagem

### 1. Instrumentar antes de otimizar e criar medições repetíveis

1. Em `backend/app/events.py:199-213`, tratar `turn_phase` como efêmero junto de `token`; não gravar esses eventos no histórico SQLite.
2. Em `backend/app/game_engine.py`, adicionar um helper de fase que emite:
   - início: `{"phase":"<nome>","state":"start"}`;
   - fim: `{"phase":"<nome>","state":"end","elapsed_ms":<inteiro>}`.
   Usar os nomes `intent`, `action_check`, `npc_gate`, `narrative_wait`, `narrative_ttft`, `correction`. Não incluir prompts, prosa ou chaves. Envolvendo chamadas existentes, o helper sempre emite `end` em `finally`.
3. Em `backend/app/routes/health.py:150-180`, acrescentar `llm.recent_calls = llm_client.recent_calls(200)` ao payload existente. Os registros já contêm `role`, `slot`, `outcome`, `waited_s`, `ttft_s`, `prompt_n`, `cache_n`, `prompt_per_second` e `predicted_per_second`.
4. Em `tools/replay_turn.py:149-190,260-278,408-413`, coletar os `turn_phase` com `state=end` e agregar cada fase em `self.stats["phase:<nome>"]`; o relatório existente passa a mostrar p50/p95/máximo dessas fases além de TTFT/turno/choices.
5. Criar `tools/bench_generation_vram.py`, somente stdlib, porque nenhuma ferramenta atual combina geração real com memória. Opções: `--base`, `--env-file`, `--adventure`, `--mode flux|portrait|clip`, `--card`, `--image`, `--sample-ms 250`, `--timeout`, `--output`.
   - `flux`: executa o grafo Klein real via `image_client._generate_comfyui`, 1024×1024, sem alterar pesos; registra bytes e dimensões.
   - `portrait`: chama `POST /api/adventures/{aid}/cards/{cid}/portrait` e faz polling do job.
   - `clip`: chama `POST /api/adventures/{aid}/clips` com prompt único por execução e faz polling.
   - Durante qualquer modo, amostra `nvidia-smi --query-gpu=memory.used,memory.free --format=csv,noheader,nounits`, `/proc/meminfo` e swap. O JSON de saída contém duração, status/erro, dimensões quando aplicável, `vram_used_max_mib`, `vram_free_min_mib`, `ram_available_min_mib` e `swap_used_delta_mib`. Falha de job ou `vram_free_min_mib < 900` retorna exit 1.
6. Cobrir parsing/exit do benchmark com mocks e eventos efêmeros/fases com testes de SSE. Rodar o subconjunto de testes afetado antes de seguir.

### 2. Capturar o baseline no staging

1. Atualizar o staging somente com a instrumentação usando `cd /home/caio/aidungeon-git && tools/deploy.sh --target staging --ref origin/fix/ai-generation-latency-vram --frontend`. Renovando o banco do staging pela cópia SQLite documentada em `deploy/vm/README.md:127-136`, preservar os IDs da aventura real acima.
2. Rodar 20 turnos (`--repeat 4`) com estes cinco turnos:
   - `do:observo a saída e escuto com atenção`
   - `say:Ladrão, precisamos sair agora.`
   - `do:espero`
   - `story:O vento atravessa as árvores.`
   - `do:peço ao Ladrão que me acompanhe`
   Comando base: `python3 tools/replay_turn.py --env-file /home/caio/aidungeon/.env --base http://127.0.0.1:2123 --adventure e17f7abe1731489c9104b502d9584dc9 --repeat 4 --see-every 5 --yield-to http://127.0.0.1:2122` seguido dos cinco `--turn`. Guardar a saída como baseline.
3. Medir `flux` e `clip` separadamente e depois cada um durante outro replay de 20 turnos. Guardar JSON antes de alterar `-ub`, prompts ou papéis. Se o baseline já violar 900 MiB, interromper mudanças que aumentem memória e aplicar primeiro as correções que não aumentam VRAM.

### 3. Corrigir o canvas FLUX.2 e a identidade do cache

1. Em `backend/app/image_client.py:453-457,706-744`, criar `_flux2_canvas(width, height, cfg) -> tuple[int,int]`. Depois de resolver `None`/`0` pelos defaults, somente para `_comfy_image_engine(cfg) == "flux2_klein"`, calcular cada dimensão como `max(16, (int(value) // 16) * 16)`. Assim `860x820 -> 848x816`; dimensões já válidas são identidade.
2. Usar o helper antes de montar `EmptyFlux2LatentImage` e manter `_validate_flux2_canvas` como invariante. Usar o mesmo helper em `request_key` (`image_client.py:784-787`) para que cache e execução nunca divergam. Não aplicar snap ao engine Qwen nem ao clipe.
3. `generate_image` já devolve `width`/`height`; garantir que devolva os valores efetivamente normalizados para que a próxima âncora persistida nasça alinhada.
4. Em `tests/test_image_comfyui.py`, substituir o teste que espera rejeição de 1000×1024 por assertivas de `860x820 -> 848x816`, identidade para 1024×1024/832×1216/1344×768/800×720, mesmo `request_key` para 860 e 848, e Qwen inalterado. Adicionar regressão do retrato cuja âncora legada é 860×820, verificando o grafo e os metadados 848×816.

### 4. Tornar a fila recuperável e colocar clipes automáticos por último

1. Em `backend/app/db.py:1728-1743,1817-1850`:
   - `job_to_dict` expõe `heartbeat_at` lido de `payload.heartbeat_at` apenas enquanto ativo;
   - criar `touch_job(conn, job_id: str, heartbeat_at: str) -> bool` com `json_set(payload,'$.heartbeat_at',?)` e `WHERE id=? AND status='running'`;
   - restringir `finish_job` a `status IN ('preparing','running')`, para que timeout/cancelamento nunca seja sobrescrito por publicação tardia;
   - em `claim_next_job`, não escolher um `scene_clip` com `json_extract(payload,'$.automatic')=1` enquanto existir qualquer outro job `pending`/`preparing` que não seja clipe automático. Se só restarem clipes automáticos, eles executam normalmente; clipe manual não sofre o gate;
   - em `jobs.enqueue`, ao enfileirar `character_portrait`, `scene_backdrop`, `see_image` ou `location_image`, cancelar também clipes automáticos `running`, com motivo `preempted by queued image`. O callback de progresso do clipe consulta o status a cada 5 s e chama `interrupt_image(job, transport)` depois que o commit torna o cancelamento visível; a finalização tardia continua bloqueada.
2. Em `backend/app/image_client.py:654-703,706-744,1194-1261,1305+`, acrescentar callback opcional `progress=None` a `_comfy_run`, `_generate_comfyui`, `generate_image` e `generate_clip`; chamar após submit e a cada poll. Todos os cinco callers atuais continuam compatíveis porque o default é `None`; os callers de `jobs.py` passam o heartbeat.
3. Em `backend/app/jobs.py`, criar heartbeat com escrita no máximo a cada 5 s por job, chamado no início de `_run_job` e pelo callback de imagem/clipe. Criar `reap_stalled_jobs(transport, now=None)` e `reaper_loop()` independentes de `process_pending`, rodando a cada 5 s desde `start_worker` até `stop_worker`.
   - Prazos desde `started_at`: imagem/ver/local `max(360, image.timeout_seconds+60)`; clipe `max(1860, video.timeout_seconds+60)`; `card_assets` `max(180, role_timeout(assets)+60)`; `npc_day_tick` 900 s.
   - Ao expirar: `_finish(...,"error",result={"code":"job_timeout"},error="job exceeded its deadline")` e `interrupt_image(job, transport)`. O update condicional impede `done` tardio. O heartbeat é evidência de progresso na UI, não um timer independente que finge atividade.
4. Limitar a preparação de retratos a uma thread global em `jobs.py:627-731`: fila FIFO interna, `_arm_portrait_prepare` apenas enfileira se houver preparação ativa ou `game_engine.is_any_generation_active()`; o `finally` de cada preparação inicia no máximo a próxima quando não há narrativa. Chamar `jobs.resume_portrait_prepare()` a partir de `release_generation_lock` (`game_engine.py:147-150`) com import local, inclusive no caminho de erro.
5. Em `tests/test_jobs_priority.py`, `tests/test_job_terminal.py` e `tests/test_auto_clips.py`, provar: still pendente vence clipe automático; enfileirar still cancela e interrompe um clipe automático running; clipe manual não é adiado nem cancelado; clipe roda com fila vazia; heartbeat persiste; reaper independente marca timeout; publicação tardia não altera erro; somente uma preparação roda e ela é adiada durante narrativa.

### 5. Corrigir o painel de jobs

Em `frontend/src/components/AdventureJobs.jsx:157-184`, `canRetry` exige `result.code !== "config_error"`; `job_timeout` continua recuperável. Para `config_error`, mostrar `Corrija a configuração de imagem antes de gerar novamente.` e nenhum botão de retry. Durante `running`, mostrar tempo desde `started_at` e `sem progresso há ...` quando `heartbeat_at` estiver há mais de 15 s. Reutilizar o poll de 1,5 s existente; não criar outro polling.

### 6. Separar papéis do turno e remover trabalho LLM desnecessário

1. Em `backend/app/llm_client.py:58-76,364-424` e `backend/app/llm_scheduler.py:1-28`, adicionar `sim_intent`, `turn_check`, `turn_gate` a `ROLES`, com timeouts 20/12/12 s e prioridades 1/1/1. Manter `meta=45` para os demais consumidores e prioridade 2.
2. Mapa de slots:
   - 5+: `sim_intent=3`, `turn_gate=3`, `turn_check=4`;
   - 4: `3`, `3`, `2` respectivamente;
   - 3: `2`, `2`, `1`;
   - 2: `1`, `1`, `1`;
   - 1: todos 0.
   Os demais mapas permanecem como estão. Em 5 slots, check e gate usam 4 e 3; `image_prompt` continua 3 com prioridade menor.
3. Trocar `role="meta"` por `sim_intent` em `backend/app/sim/intent.py`, `turn_check` em `backend/app/action_check.py` e `turn_gate` em `backend/app/npc_gate.py` (inclusive `own_beat`). Atualizar testes que fixam os mapas antigos.
4. Em `action_check.py:93-125`, tornar `_hard_blockers` público como `hard_blockers` e acrescentar a `check_action` os opcionais `allow_llm: bool=True` e `blockers: list[str]|None=None`. Bloqueadores fornecidos evitam recomputação; `allow_llm=False` retorna o bloqueio determinístico ou um veredito `possible=True` vazio, sem rede.
5. Em `game_engine`, para `say`, chamar a checagem com `allow_llm=False`; para `do`, manter o modelo. Assim `world_facts` e bloqueios duros continuam valendo para fala.

### 7. Executar check e gate em paralelo, preservando seus vereditos

1. Depois do sim e de `_action_check_state`, calcular uma vez `hard_blockers(state)`. Em `game_engine.py:834-896`, usar `ThreadPoolExecutor(max_workers=2)` somente quando check e gate existirem: um future executa `_apply_action_check`; o outro executa `_npc_gate_world_check`/`own_beat`. Sem os dois trabalhos, manter chamada direta.
2. Acrescentar `action_blockers: list[str]|None=None` a `_npc_gate_world_check` e incluir `payload["world_check"]["action_blockers"] = action_blockers[:8]`; o prompt do gate passa a distinguir fatos conhecidos de ficção permitida sem receber o veredito LLM do check.
3. Aguardar ambos antes de `stream_chat_completion`; injetar no narrador o mesmo `verdict_block` e `adjudication` de hoje. Compartilhar o `cancel` existente e propagar `LlmCancelled`. Não alterar `_correct_narration`.
4. Testes com eventos controlados devem provar overlap real (o segundo entra antes de o primeiro sair), ausência de chamada LLM em `say`, bloqueio duro preservado e narrador recebendo ambos os vereditos.

### 8. Reduzir prompts auxiliares sem apagar fatos dos presentes

1. Em `backend/app/action_check.py:225-288`, reutilizar `npc_assets._clip` para limitar: `ui_state` JSON 1.200 chars, `canon` JSON 1.200, inventário 1.200, `character_rules` 1.200, memórias às 4 primeiras ×160, ações às últimas 6 ×240, ausentes a nome/status e no máximo 20. Manter player, presentes, stats e sim completos; dados de dados só o último resultado.
2. Em `backend/app/npc_gate.py:406-445`: presentes conservam a ficha; ausentes viram somente `{name,status,present:false}`; memórias somente dos presentes, 4 ×160; `scene_tail` 1.600. Manter `allow` e veredito estruturado.
3. Testes devem verificar os limites e que presentes/stats/bloqueadores essenciais continuam no payload; não testar apenas constantes.

### 9. Mostrar a fase real no frontend

Em `frontend/src/pages/Play.jsx:995-1142,1740-1753`, consumir `turn_phase` já encaminhado por `frontend/src/sse.js`. Rótulos: `intent=Interpretando ação`, `action_check=Checando regras`, `npc_gate=Consultando o mundo`, `narrative_wait=Preparando narrativa`, `correction=Revisando narração`. No primeiro `token`, voltar a `Narrando`; em `done/error/refused`, limpar como hoje. Um `end` só limpa a fase se ainda for a fase correspondente, para eventos fora de ordem não apagarem uma fase mais nova.

### 10. Validar P0+P1 no staging antes dos experimentos P2

1. Rodar localmente:
   ```bash
   python3 -m pytest tests/test_image_comfyui.py tests/test_jobs_priority.py tests/test_job_terminal.py tests/test_auto_clips.py tests/test_action_check.py tests/test_npc_gate.py tests/test_llm_roles.py tests/test_health_llm.py tests/test_bench_slots.py -q
   python3 -m pytest -q
   cd frontend && npm run build
   ```
2. Publicar no staging e repetir exatamente o replay e os benchmarks do passo 2. Aceite funcional:
   - retry do card `b261430a5ccd4acea2f2975af3749874` termina `done`, gera 848×816 e não contém `config_error`;
   - 20 turnos sem 409, `job_timeout` indevido ou slot preso;
   - `say` não tem chamada `turn_check` no `llm.recent_calls`;
   - check/gate aparecem concorrentes e choices p95 ≤16 s;
   - meta de latência: TTFT p50 ≤2 s e p95 ≤5 s, choices p95 ≤12 s;
   - cada amostra de VRAM livre ≥900 MiB.
3. Abrir o staging real no browser: executar um turno e observar os rótulos antes do primeiro token; abrir `AdventureJobs` com um running e um `config_error` e confirmar elapsed/heartbeat e ausência de retry. Não usar teste de fonte como substituto dessa verificação.
4. Se a correção passar mas a meta TTFT não for atingida, manter as correções seguras e continuar para os experimentos P2; não reduzir qualidade do narrador para fechar a métrica.

### 11. P2 — testar `-ub 2048` com rollback objetivo

1. Com o app principal inativo, alterar somente `-ub 1024` para `-ub 2048` em `/etc/systemd/system/llama-server.service`, `daemon-reload` e restart; atualizar o valor documentado em `deploy/vm/README.md` depois de validado.
2. Repetir 20 turnos, `tools/bench_slots.py`, `tools/repro_slot_hang.py`, FLUX e LTXV concorrentes com narrativa. Permanecer apenas se não houver livelock/OOM, `vram_free_min_mib >=900`, e TTFT/prompt tok/s melhorar sem choices p95 >12 s. Caso contrário restaurar `-ub 1024`, restart e repetir health/replay curto.

### 12. P2 — prompt cache somente se couber na RAM e sobreviver ao soak

1. Pré-condição obrigatória antes de mexer na unidade: `MemAvailable >= 12.288 MiB`. Se não houver, manter `-cram 0` e `scheduler.aux_cache_prompt=false`, registrar que o experimento não foi ativado por RAM e seguir para o modelo auxiliar.
2. Se couber, aplicar `-cram 8192` e `aux_cache_prompt=true`, mantendo o watchdog. Rodar `repro_slot_hang.py` e replay dos cinco turnos com `--repeat 40` (200 turnos), mais See a cada 5. Aceitar somente com zero livelock/409/timeout, `MemAvailable >=4.096 MiB`, crescimento de swap <512 MiB, VRAM ≥900 e redução do TTFT p50 em pelo menos 15% contra o passo 11.
3. Em qualquer falha, restaurar imediatamente `-cram 0`, `aux_cache_prompt=false`, restart e replay curto. Não tentar contornar o livelock reiniciando repetidamente.

### 13. P2 — modelo auxiliar pequeno em CPU, condicionado a equivalência

1. Adicionar `deploy/vm/install_qwen_aux.sh` seguindo o `fetch` com hash de `install_flux2_klein.sh`: baixar `https://huggingface.co/Qwen/Qwen2.5-3B-Instruct-GGUF/resolve/main/qwen2.5-3b-instruct-q4_k_m.gguf` para `/home/caio/qwen2.5-3b-instruct-q4_k_m.gguf`, exigir 2.104.932.768 bytes e SHA-256 `626b4a6678b86442240e33df819e00132d3ba7dddfe1cdc4fbb18e0a9615c62d`.
2. Adicionar `deploy/vm/llama-aux.service`: binário `/home/caio/llama.cpp-turboq-mtp/build/bin/llama-server`, host 127.0.0.1, porta 1240, `-ngl 0 -t 8 -c 8192 -np 2 -b 512 -ub 512`, alias `qwen2.5-3b-instruct-q4_k_m`, `CUDA_VISIBLE_DEVICES=""`, `MemoryMax=6G`; sem `--no-mmap` e sem GPU.
3. Em `settings_store.effective_llm_settings`, expor `scheduler.role_overrides`. Em `llm_client`, criar `role_config(cfg, role) -> dict`: cópia rasa do cfg com `base_url`, `model_alias`, `api_key` do override; `slot_for` usa o `slot` explícito do override antes de consultar `/props`. Aplicar `role_config` somente em `sim/intent.py`, `action_check.py` e `npc_gate.py`. Configuração de ativação:
   ```json
   {
     "sim_intent": {"base_url":"http://127.0.0.1:1240/v1","model_alias":"qwen2.5-3b-instruct-q4_k_m","slot":0},
     "turn_check": {"base_url":"http://127.0.0.1:1240/v1","model_alias":"qwen2.5-3b-instruct-q4_k_m","slot":0},
     "turn_gate": {"base_url":"http://127.0.0.1:1240/v1","model_alias":"qwen2.5-3b-instruct-q4_k_m","slot":1}
   }
   ```
   Sem override, todo comportamento atual permanece.
4. Criar `tools/eval_turn_aux.py`: executar os mesmos payloads diretamente contra o 35B baseline e o 3B candidato, sem gravar aventuras. Corpus fixo de 24 ações: 8 de intenção (ataque corpo a corpo, ataque à distância, perícia, compra, descanso, fala, item, exploração), 8 de check (observação, travessia, salto, ataque, persuasão, item, contradição de world_facts, ação impossível) e 8 de gate (saudação, pedido a presente, fala com ausente, morte declarada, viagem, obediência a allow, redirect, block). Usar os estados das fixtures de `tests/test_action_check.py`/`test_npc_gate.py`; ações são fornecidas no próprio script, não geradas por LLM.
5. Pré-condição de ativação: `MemAvailable >=6.144 MiB`; depois de carregar, `MemAvailable >=2.048 MiB`, crescimento de swap <256 MiB e zero VRAM atribuído ao processo. Qualidade contra o 35B: JSON válido em 100%, equivalência de 100% nos campos críticos (`kind`, alvo/hostilidade; `possible`, blockers; decisão allow/redirect/block) e ≥90% de igualdade nos demais campos normalizados. Falhou: desabilitar a unit, esvaziar `role_overrides` e manter o 35B.
6. Passou: ativar os três overrides, repetir 20 turnos e a avaliação cega. Manter somente se TTFT p50 ≤2 s/p95 ≤5 s, choices p95 ≤12 s, sem fallback/erro estruturado, e memórias dentro dos limites. Caso contrário voltar aos overrides vazios e desligar `llama-aux`.

### 14. Soak final e promoção

1. Com a combinação que passou nos gates, executar 200 turnos no staging e `repro_slot_hang.py`; depois um retrato da âncora legada e um clipe durante narrativa, medindo VRAM. Exigir zero slots presos, zero retratos `config_error`, zero job terminal sobrescrito e toda amostra ≥900 MiB livre.
2. Comparar o relatório final ao baseline por fase; registrar valores antes/depois e quais experimentos P2 ficaram ativos ou foram revertidos.
3. Promover somente o commit testado: `tools/deploy.sh --target main --ref <commit-testado> --frontend` (sem `--force` se o jogador estiver ativo). O script já faz backup, smoke e rollback de código. Se o smoke ou replay curto pós-deploy falhar, usar o backup impresso pelo deploy com `tools/deploy.sh --target main --rollback <backup>` e restaurar separadamente qualquer flag de unidade revertida pelos passos anteriores.

## Arquivos e âncoras críticos

- `backend/app/game_engine.py:719-1039` — ordem real sim → check/gate → narrador e ponto da paralelização/telemetria.
- `backend/app/image_client.py:453-457,654-744` — invariante múltiplo de 16 e polling onde o heartbeat deve nascer.
- `backend/app/db.py:1728-1916` — claim, heartbeat e proteção contra finalização tardia.
- `backend/app/jobs.py:627-731,1180-1298,1532-1640` — preparação, clipes automáticos, worker e reaper.
- `tools/replay_turn.py:149-190,260-278,408-413` — comparação antes/depois por fase.

## Assunções e contingências

- O limite de 900 MiB é um piso por amostra, inclusive durante narrativa + FLUX/LTXV; uma média acima de 900 não compensa um pico abaixo.
- As metas de latência são p50 TTFT ≤2 s, p95 TTFT ≤5 s e choices p95 ≤12 s. Se a combinação segura não atingir esses valores, entregar as correções aprovadas e relatar a meta não atingida; não trocar o narrador, reduzir passos ou resolução para mascarar o resultado.
- Cache e modelo auxiliar não são obrigatórios quando suas pré-condições falham. O fallback já decidido é manter `-cram 0`/`aux_cache_prompt=false` e/ou overrides vazios com o 35B; nunca ativar ambos enquanto a soma não passar nos mesmos limites de RAM e swap.
- Nenhuma medição, replay ou benchmark pode imprimir tokens, API keys ou conteúdo privado das aventuras; relatórios guardam somente métricas, IDs e status.
