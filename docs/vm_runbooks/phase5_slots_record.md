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
