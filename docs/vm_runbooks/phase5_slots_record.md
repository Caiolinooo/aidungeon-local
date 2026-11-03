# Fase 5 (G2): slots, livelock #28280 e watchdog — registro

Medido em 2026-10-18 (horário -03) na VM: L4 23034 MiB, 36 vCPU, 49 GB de RAM.
llama-server do fork `Indras-Mirror/llama.cpp-turboq-mtp`, build
`b10281-c8064719f`, modelo `Qwen3.6-35B-A3B-Uncensored-HauhauCS-Aggressive-IQ2_M`
(MoE híbrido com gated delta net). Unit atual e rollback em
[`deploy/vm/README.md`](../../deploy/vm/README.md#llama-server-g2).

## 1. Decisão

| Antes (v0.9.0 / G1) | Depois (G2, no ar desde 15:54) |
|---|---|
| `-np 2`, prompt cache padrão (`-cram 8192`), 32 checkpoints | `-np 5 --kv-unified -cram 0 --ctx-checkpoints 8 --metrics` |
| `-ub 512` | `-ub 1024` |
| embeddings com 646 MiB de VRAM | embeddings com 0 MiB (`CUDA_VISIBLE_DEVICES=` + `--device none`) |
| sem watchdog | `slot-watchdog.timer` a cada 10 s |

Fica o fork: o #28280 continua aberto upstream, reproduzido em `b10533` (CUDA)
e `b10731` (Vulkan), builds mais novas que a nossa, e sem PR de correção
(conferido em 2026-10-18). Testar o upstream só faz sentido quando houver
correção. Nas builds novas o slot preso nem solta quando o cliente desconecta;
na nossa, solta (abaixo).

## 2. O livelock aconteceu duas vezes hoje no app principal

| Quando | Config | O que houve | Quanto durou |
|---|---|---|---|
| 15:04:32 | código anterior à G1, `-np 2`, prompt cache | turno do jogador na Costazul no slot 0 (3157 tokens, 17,6 s); em seguida o `POST /choices` fixou o slot 1 com `cache_prompt=true`; a tarefa 18917 repetiu `prompt processing, n_tokens = 131, progress = 1.06` e `erasing old context checkpoint (pos 674)` a cada ~120 ms | 180 s: 503 no choices às 15:07:32, `cancel task` às 15:07:33 (mesmo padrão de 17/10) |
| 15:41:54 | G1 (no ar desde 15:25), `-np 2`, prompt cache | choices com `cache_prompt=false` no slot 1; o servidor logou `making room for prompt cache entry` e a tarefa 22514 entrou no mesmo laço (`n_tokens = 646`, checkpoint em 2681) | 15 s: o prazo da G1 fechou a conexão, o servidor cancelou a tarefa, o chronicle que esperava na fila rodou (21 s, criou 2 cards) e o retrato saiu às 15:42:45 |

Com `-cram 0`, das 15:54 até o início do soak: 172 tarefas (benchmarks, repro,
o jogador e o próprio soak), 0 linhas `erasing old context checkpoint`, 0
`prompt cache entry`. Os 3 `cancel task` do período são o See do jogador às
16:24 (ver §6) e o repro interrompido.

Por que `cache_prompt=false` não bastou: no `server-context.cpp` do fork, com
prompt cache ligado (`--cache-idle-slots` vem ligado e depende de `-cram`), toda
tarefa nova salva os slots ociosos no cache (e, com `--kv-unified`, os limpa), e
uma requisição fixada cujo prompt compartilha ≤ 10% com o slot faz
`prompt_save` + `prompt_load`. O `cache_prompt` da requisição não desliga isso.
Os relatos do #28280 também só reproduzem com 2+ slots e prompt cache; com
`-np 1` não acontece.

Custo do `-cram 0`: um slot que troca de conversa reprocessa o prompt inteiro.
No app isso já acontece: a narrativa é a única com cache e o topo do prompt
muda quase todo turno no modelo híbrido.

## 3. Repro sintético (`tools/repro_slot_hang.py`)

| Config | Modo | Requisições | Travamentos | Pior |
|---|---|---|---|---|
| `-np 2`, prompt cache (G1) | `bug`, 15 rodadas | 83 | 0 | 29,0 s |
| `-np 5`, `-cram 0` | `bug`, 7 de 12 rodadas (parado: o jogador entrou) | 42 | 0 | 26,7 s |

O repro nunca disparou o bug, nem com `-np 2`, enquanto o tráfego real do app
disparou duas vezes no mesmo dia. Quem reproduz é o tráfego do app: o soak de
200 turnos no staging (§7) é o teste que vale.

## 4. Benchmarks (`tools/bench_slots.py`, `-np 5`)

Narrativa de 4000 tokens no slot 0, 200 tokens gerados; carga = a narrativa com
3 auxiliares de 3000 tokens sem cache ao mesmo tempo (choices no 1, chronicle no
2, image_prompt no 3). 5 repetições, p50/p95. VRAM do llama-server sozinho
(embeddings ainda na GPU durante a varredura).

| `-ub` | prompt tok/s (12,7k) | frio TTFT p50 / p95 | solo TTFT p50 / tok/s | carga TTFT p50 / p95 | carga tok/s | choices p50 / p95 | chronicle p50 / p95 | image_prompt p50 / p95 | VRAM ocioso / pico |
|---|---|---|---|---|---|---|---|---|---|
| 512 | 913 | 5,90 / 5,94 s | 1,22 s / 19,9 | 9,01 / 9,45 s | 10,5 | 15,3 / 25,1 s | 19,6 / 24,6 s | 19,0 / 21,3 s | 12964 / 13428 MiB |
| **1024** | 1291 | 4,59 / 4,73 s | 1,17 s / 21,7 | 5,94 / 7,73 s | 12,2 | 11,0 / 13,0 s | 13,5 / 14,5 s | 15,9 / 17,3 s | 13246 / 13506 MiB |
| 2048 | 1596 | 3,96 / 4,06 s | 1,23 s / 19,9 | 6,64 / 6,87 s | 13,1 | 10,9 / 17,1 s | 12,4 / 19,4 s | 13,6 / 17,9 s | 13808 / 14098 MiB |

Pico de VRAM com SDXL (A1111, 20 passos) e a narrativa juntos, `-ub 1024`,
embeddings fora da GPU: 21253 MiB em 800×720 (dentro da meta de 21,5 GB) e
21937 MiB em 832×1216 (fora). O llama-server cresce ~290 MiB depois de
aquecido. Por isso fica o `-ub 1024`: o 2048 soma ~560 MiB e só volta depois
que a G6 baixar o pico do SD (VAE fp16-fix ou Forge), junto com o preset de
retrato 832×1216.

RAM: o RSS do llama-server caiu de 14,5 GB para 7,1 GB com `-cram 0` (8,4 GB
depois de uma hora de uso). Às 16:32: 21,5 GB disponíveis com o A1111 em 12 GB e
4,7 GB de swap (no começo do dia: ~0 disponível e 17 GB de swap).

## 5. Metas da seção 2 do plano depois da G2

| Meta | Medido | Situação |
|---|---|---|
| narrativa TTFT ≤ 3 s p50 / ≤ 6 s p95 (prompt reprocessado) | 4,59 / 4,73 s | p95 ok; p50 fica para a G7 (`-ub 2048` depois da G6) e prompts menores (K1/K4) |
| narrativa sozinha ≥ 17 tok/s, TTFT com cache | 21,7 tok/s, 1,17 s | ok |
| narrativa ≥ 13 tok/s com 3 auxiliares | 12,2 | G3: no máximo 2 auxiliares durante a narrativa |
| TTFT p95 ≤ 6 s com auxiliares | 7,73 s | G3 (prioridade da narrativa) |
| choices ≤ 6 s p50 / ≤ 10 s p95 | 11,0 / 13,0 s com 3 auxiliares | G3 + C7 (pré-geração logo após o `done`, prompt menor) |
| nenhum livelock; watchdog recupera em ≤ 90 s | ver §7 | — |

## 6. Watchdog (`tools/slot_watchdog.py`)

Instalado às 16:29 (`/usr/local/sbin/slot_watchdog.py`, `slot-watchdog.timer`
a cada 10 s, estado em `/var/lib/slot-watchdog/status.json`). Regras:

- slot ocupado sem passar do próprio máximo de `n_prompt_tokens_processed` e
  `n_decoded` por mais de 45 s, com nenhum outro slot andando nesse tempo:
  reinicia o llama-server. O máximo, e não o último valor, porque no #28280 a
  tarefa presa oscila entre duas posições;
- slot parado enquanto outro anda só espera (o lote de prompt é montado na
  ordem dos slots, então um prompt longo no slot 0 segura os outros), até
  300 s: no #28280 os outros slots seguem servindo;
- `/slots` sem responder por 45 s: reinicia;
- 180 s de intervalo mínimo entre reinícios.

Recuperação no pior caso com o servidor ocioso: 45 s + até 10 s do timer + ~30 s
para carregar o modelo, dentro dos 90 s da meta. O journal da unit só mostra
reinícios e esperas (`LogLevelMax=notice`). Teste a seco com `--dry-run` antes
de ligar o timer; reinício simulado com `/slots` inacessível logou só a linha
do reinício.

O app relê `total_slots` (`GET /props`) depois de um timeout ou de servidor
inacessível (`llm_client._forget_slots`), então um reinício com outro `-np` não
deixa `id_slot` inválido na fila. Está no staging desde 16:36 e vai para o app
principal depois do soak.

## 7. Soak no staging

Aventura `Soak G2 (cópia da Costazul)` no staging (export e import da
Costazul: 27 ações, 11 cards), 200 turnos (`--repeat 50` de 4 turnos `do`,
`say`, `do`, `story`), See a cada 5 turnos, `--yield-to` no app principal com
5 min de silêncio exigido. Com o código da G1 e o `_forget_slots` no staging.

Resultado: _em andamento_.

## 8. Lições

- Teste de carga no llama-server compartilhado atinge o jogador: às 16:24 o See
  dele deu 503 (`image_prompt` estourou 20 s) porque o repro ocupava os slots 0
  e 1 com prompts de 21k e 5k tokens. Benchmark e repro só com o jogador
  parado; o `replay_turn.py` tem `--yield-to` para soaks longos.
- O choices no slot 1 é a vítima de sempre: é a primeira auxiliar depois do
  turno e a única que o jogador espera. A G3 (C7) gera o choices no backend
  logo após o `done`, com prioridade.

## 9. Correção de geração, latência e VRAM (2026-10-29)

Commit validado no staging: `15bf754`. O replay de 20 turnos terminou com
20/20 ações HTTP 200, sem 409 ou `job_timeout`. `say` não produziu chamadas
`turn_check`; `sim_intent`, `turn_gate` e `turn_check` usaram os slots 3, 3 e
4. O browser mostrou `Preparando narrativa` antes do token, os rótulos de fase
e `Narrando` somente depois do primeiro token.

| Métrica (p50/p95) | Baseline | Correção P0/P1 |
|---|---:|---:|
| TTFT | 52,7/83,9 s | 24,7/36,9 s |
| Turno completo | 72,1/98,0 s | 43,0/54,6 s |
| `action_check` | 20,0 s (p50) | 12,0/12,1 s |
| `npc_gate` | 43,9 s (p50) | 12,2/14,6 s |
| `narrative_ttft` | — | 6,4/8,2 s |

As metas TTFT p50 <=2 s e p95 <=5 s não foram atingidas. Choices teve 19
respostas 503 por timeout e uma resposta 200 em 8,5 s; não houve amostras
suficientes para validar a meta de choices. Seis metas de backdrop do replay
terminaram como `superseded` durante as ações seguintes, sem timeout de job.

O retrato legado `b261430a5ccd4acea2f2975af3749874` terminou `done` em
848×816, sem `config_error`. FLUX durante replay terminou `done` em 27,465 s;
o clipe isolado terminou `done` em 18,841 s. O clipe manual durante o replay
contínuo ficou pendente por 600 s enquanto imagens de maior prioridade eram
processadas; a medição durante uma narrativa isolada permanece no soak final.

O piso de 900 MiB livres falhou: mínimos de 100 MiB no retrato, 70 MiB no
FLUX durante replay e 38 MiB no clipe isolado. Portanto `-ub 2048` não foi
ativado e a unidade permanece em `-ub 1024`. `MemAvailable` foi 3.177 MiB,
abaixo dos gates de 12.288 MiB do prompt cache e 6.144 MiB do modelo auxiliar.
Permanecem `-cram 0`, `scheduler.aux_cache_prompt=false`, `llama-aux` inativo e
`scheduler.role_overrides` vazio. Narrador, pesos e parâmetros de qualidade não
foram alterados.

## 10. Fechamento com soak reduzido

- A pedido explícito do usuário, o soak de 200 turnos foi interrompido após **53 turnos concluídos, todos HTTP 200**. O processo remoto foi terminado; log parcial: `/home/caio/aidungeon-bench/soak-200.log`.
- `repro_slot_hang.py --mode app --external --rounds 20`: **120 requisições, zero travamentos**, pior requisição 53,5 s; log `/home/caio/aidungeon-bench/final-repro-slot-hang.log`.
- Clipe durante um turno isolado: `done` em 80,856 s, sem erro de job; turno HTTP 200 e replay com metas funcionais ok. VRAM livre mínima **36 MiB**, RAM disponível mínima 1.997 MiB, swap delta 0; JSON `/home/caio/aidungeon-bench/final-clip-single-narrative.json`.
- Retrato legado após o soak: `done` em 30,55 s, sem `config_error`; VRAM livre mínima **196 MiB**, RAM disponível mínima 2.039 MiB, swap delta 0; JSON `/home/caio/aidungeon-bench/final-portrait-after-soak.json`.
- O piso de 900 MiB continua reprovado nos picos de FLUX/LTXV. Nenhum experimento P2 foi ativado: `-ub 1024`, `-cram 0`, cache desativado, `llama-aux` inativo e overrides vazios permanecem.

## 11. Liberação de RAM/VRAM e nova rodada P2

O Forge legado foi parado por não ser o provider configurado e o embeddings foi
fixado em CPU com `CUDA_VISIBLE_DEVICES=` e `--device none`. O embeddings saiu
de 698 MiB de VRAM e 9,4 GB de RSS para 0 MiB e cerca de 1,7 GB. Com o Forge
foram liberados mais 402 MiB de VRAM e 6,9 GB de RSS. `MemAvailable` passou de
cerca de 3,1 GB para 15–16,5 GB, chegando a 30 GB depois da liberação de caches
e do encerramento do auxiliar.

O ComfyUI passou a reservar 2 GiB com `--vram-headroom 2`, sem trocar pesos,
resolução ou passos. FLUX terminou `done` em 24,802 s com 2.264 MiB livres;
LTXV terminou `done` em 35,528 s com 1.248 MiB livres. JSONs:
`/home/caio/aidungeon-bench/headroom2-flux.json` e
`/home/caio/aidungeon-bench/headroom2-clip.json`. Uma sonda com 3 GiB também
passou (3.136 MiB no FLUX e 1.438 no LTXV), mas o LTXV subiu para 43,938 s;
a reserva de 2 GiB foi restaurada.

O auxiliar Qwen2.5-3B foi carregado somente em CPU, com zero VRAM atribuída e
RAM/swap dentro dos limites. A avaliação cega de 24 ações reprovou a
ativação: candidato com **0/24 JSONs válidos** dentro dos prazos dos papéis
(17 timeouts e 7 respostas degradadas), contra 16/24 do 35B. Relatório sem
prompts nem respostas: `/home/caio/aidungeon-bench/aux-24-eval.json`.
`llama-aux.service` foi desativado e os `role_overrides` permaneceram vazios.

`-ub 2048` não foi ativado: sua estimativa adicional de 592 MiB derrubaria o
mínimo LTXV medido com headroom 2 de 1.248 para cerca de 656 MiB, abaixo do
piso de 900. O narrador continua inalterado em `-ub 1024`.

Com `MemAvailable` acima de 12.288 MiB, o cache foi ativado em `-cram 8192` e
`scheduler.aux_cache_prompt=true`. O repro externo concluiu 120 requisições,
zero travamentos e pior requisição de 30,8 s; log:
`/home/caio/aidungeon-bench/cache-repro-slot-hang.log`.

O soak de 200 turnos foi **reprovado e interrompido no turno 70**, não concluído.
Nos 63 turnos com TTFT válido até esse ponto, o TTFT foi p50 **93,9 s**, p95
**110,0 s** e máximo 122,1 s, contra 24,7 s de p50 antes do experimento. Os
turnos 43–49 incluíram `ConnectError` e `Loading model`. O monitor registrou
VRAM livre mínima 1.176 MiB, RAM disponível mínima 3.667 MiB (abaixo do gate de
4.096) e swap delta -48 MiB. O replay ficou pausado pelo guard do jogador após
o turno 70 e foi terminado; não houve relatório de 200 turnos. Logs:
`/home/caio/aidungeon-bench/cache-soak-200-memory.json.log` e
`/home/caio/aidungeon-bench/cache-soak-200-memory.json`.

O cache foi revertido para `-cram 0` e `aux_cache_prompt=false` nas duas
instâncias; o llama voltou a responder `/health` 200. Um turno posterior ainda
teve TTFT de 93,1 s, agora explicado pela telemetria: a narrativa da aventura
de teste chegou a aproximadamente **63 mil tokens de prompt**, processados a
cerca de **840 tokens/s**. Assim, as latências do início e do fim do soak não
são comparáveis sem considerar o crescimento do histórico.

## 12. Teste A/B de `-ub 2048`

Depois que o usuário autorizou testes abaixo do piso de 900 MiB, um turno real
com See concorrente terminou com TTFT 76,5 s, FLUX e LTXV `done`, VRAM livre
mínima 1.044 MiB, RAM disponível mínima 10.336 MiB e swap delta 0. Relatórios:
`/home/caio/aidungeon-bench/ub2048-turn-see-memory.json` e `.json.log`.

O benchmark direto de prompt, contudo, reprovou a flag: três repetições de
aproximadamente 12,7 mil tokens mediram p50 **870,5 tok/s** com `-ub 2048`.
Depois da restauração para `-ub 1024`, o mesmo benchmark mediu **1.247,2
tok/s** (44 s contra 31 s de wall total). JSONs:
`/home/caio/aidungeon-bench/ub2048-bench-slots.json` e
`/home/caio/aidungeon-bench/ub1024-bench-slots.json`. Portanto `-ub 2048` foi
revertido; a unidade final permanece em `-ub 1024`, `-cram 0` e o teste de
livelock do 2048 não foi executado porque o benchmark já falhou no gate de
melhoria.

## 13. Por que o prefixo narrativo não é reutilizado

A investigação do cliente confirmou que a narrativa já envia
`cache_prompt=true` (`backend/app/llm_client.py:540-543`) e permanece no slot 0.
O `context_builder` mantém system e histórico antes da user final; lore, estado,
vereditos e a ação nova já estão nessa user final. Portanto falta de flag ou
um prefixo dinâmico no início não explicam `cache_n=0`.

O modelo usa Gated DeltaNet. No mecanismo de checkpoint desse llama-server, o
estado recorrente só pode ser restaurado num checkpoint válido dentro do
prefixo comum; sem ele, o servidor zera `n_past` e refaz o prefill. Com
`-cram 8192`, tarefas nos outros slots ainda limpam o slot 0 ocioso, e o
save/load não preserva os checkpoints híbridos necessários. Isso é consistente
com o prefill integral medido: 63 mil tokens a cerca de 840 tok/s.

Não há corte seguro no prompt que preserve o conteúdo: os dados variáveis já
estão na última mensagem. Reutilizar esse prefixo exigiu um llama-server com os
fixes de checkpoint híbrido validados também contra o livelock #28280. A
validação e a promoção estão registradas na seção 14; `-cram 0`,
`aux_cache_prompt=false`, `-ub 1024`, auxiliar 3B desligado e overrides vazios
permanecem.

## 14. Checkpoint híbrido: validação e promoção

Os commits `8c6d89ab0` e `d0e1388a0` do PR upstream `ggml-org/llama.cpp#25592`
foram aplicados sobre o fork de produção `c8064719f`. O binário final é build
**10283 (`d0e1388a0`)**. O patch toca somente
`src/llama-memory-hybrid.cpp`, `src/llama-memory-recurrent.cpp` e
`tools/server/server-context.cpp`: restaura checkpoints recorrentes/híbridos no
prefixo comum e invalida checkpoints divergentes. Não altera sampler, pesos ou
parâmetros de geração.

A sonda determinística fria, depois de recompilar baseline e patch com a mesma
fonte-base e flags, produziu hashes idênticos nos dois casos: curto
`7f416c7f56e6796cef2ef11134ca07b53b26bb0f7d49f8ee961874a6bda74348` e longo
`5fff8b681a72ae9ef81f2ceede41cf9b60928c46f0f154941040a9568446a76a`.
Relatórios: `/home/caio/aidungeon-bench/hybrid-quality-production-baseline.json`
e `hybrid-quality-exact-patch.json`. Assim, a diferença observada contra o
binário antigo não veio do patch de cache.

Dois testes diretos de 60 requisições terminaram com **zero stalls**: um com
crescimento de prefixo e outro alternando conversas e slots. Logs:
`hybrid-exact-cram0-app10.log` e `hybrid-equivalent-cram0-bug10.log`.
No replay real de 20 turnos, todas as ações responderam HTTP 200. Depois da
narrativa fria, `cache_n` avançou de 61.236 para 66.193 tokens e cada turno
processou somente cerca de 3,0–3,3 mil novos tokens, em vez de refazer mais de
64 mil. `narrative_ttft` ficou em p50 **7,2 s** e p95 **13,1 s**.

O TTFT total desse replay ficou em p50 **40,2 s** e p95 **55,0 s**, e o turno em
68,4/85,6 s. Choices teve 18 respostas 503 e duas 200; oito metas de backdrop
falharam. Portanto as metas globais de latência **não foram atingidas** e não se
deve comparar esse TTFT diretamente ao replay antigo: a aventura já tinha um
histórico muito maior. O ganho comprovado do patch é eliminar o prefill integral
crescente, não resolver toda a fila auxiliar.

O binário foi promovido no serviço persistente com `/health` 200 e versão 10283.
A pedido explícito do usuário para encerrar sem mais horas de teste, não foi
aberto outro soak de 200 turnos. No smoke frio, turno e choices responderam
HTTP 200 e backdrop/clipe terminaram `done`; o turno levou 124,0 s e falhou
somente a meta de 120 s, com prefill narrativo frio de 84,0 s. O turno quente
seguinte passou em todas as metas do replay: HTTP 200, TTFT 22,6 s,
`narrative_ttft` 6,2 s, turno 46,6 s e choices 200 em 14,7 s. Logs:
`hybrid-postdeploy-smoke.log` e `hybrid-postdeploy-warm.log`. A meta global de
TTFT continua não atingida. Permanecem `-cram 0`, `-ub 1024`,
`aux_cache_prompt=false`, auxiliar 3B desligado e overrides vazios.
