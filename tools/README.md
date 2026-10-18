# tools — utilitários do LLM (AI Dungeon Local)

Scripts de diagnóstico e benchmark do servidor LLM, mais o setup do **fallback local**.
Todos rodam com Python padrão (sem dependências externas).

| Script | Função |
|---|---|
| `check_llm.py` | Diagnóstico: alcance, auth e modelos do servidor (GETs públicos) |
| `bench_llm.py` | Benchmark de geração: TTFT, tok/s, tokens (stream, exige API key) |
| `replay_turn.py` | Replay de turnos pela API do backend (staging) com as metas de fluidez |
| `bench_slots.py` | Benchmark do llama-server nos papéis do app (C1) contra as metas da seção 2 do plano |
| `repro_slot_hang.py` | Tenta reproduzir o slot preso do llama.cpp #28280 contra o llama-server |
| `slot_watchdog.py` | Oneshot do `slot-watchdog.timer`: reinicia o llama-server com slot preso |
| `setup_llm_local.ps1` | **Windows.** Fallback local: baixa KoboldCpp + GGUF e gera `start_llm_local.bat` |
| `setup_llm_local.sh` | **Linux.** Fallback local: baixa KoboldCpp + GGUF e gera `start_llm_local.sh` |

---

## 1. check_llm.py — diagnóstico

Chama os endpoints **públicos** `GET {base_url}/health` e `GET {base_url}/models`
e imprime alcance, necessidade de auth, modelos descobertos (id, n_params,
n_ctx_train, ftype) e o veredito final.

```powershell
python tools\check_llm.py                                  # usa config\settings.json
python tools\check_llm.py --api-key SUA_CHAVE              # override pontual da chave
python tools\check_llm.py --base-url http://127.0.0.1:5001/v1   # testa o fallback local
python tools\check_llm.py --help
```

Vereditos (e exit codes):

| Veredito | Exit | Significado |
|---|---|---|
| `PRONTO` | 0 | servidor acessível **e** API key configurada |
| `FALTA_API_KEY` | 2 | servidor acessível, falta API key |
| `INALCANÇÁVEL` | 3 | `/health` e `/models` não responderam |

## 2. bench_llm.py — benchmark de geração

Envia um prompt narrativo curto (estilo AI Dungeon, 2ª pessoa) via
`POST {base_url}/chat/completions` com `stream=true` e mede **TTFT** (tempo até
o 1º token), **tok/s** e **total de tokens**. Veredito: `>= 15 tok/s` ÓTIMO ·
`8-15` BOM · `< 8` LENTO.

**Pré-requisito: API key configurada** (o script se recusa a rodar sem chave —
sem chave ele nem toca na rede). A chave do projeto está no `.env` da raiz e é
lida automaticamente.

```powershell
python tools\bench_llm.py                                   # lê a chave do .env
python tools\bench_llm.py --api-key SUA_CHAVE               # override pontual
# ou, na sessão atual do PowerShell:
$env:LLM_API_KEY = "SUA_CHAVE"; python tools\bench_llm.py
python tools\bench_llm.py --max-tokens 256
python tools\bench_llm.py --help
```

Se retornar `HTTP 401`, a chave está errada/ausente — configure por uma das vias abaixo.

## 2.1 replay_turn.py — replay no staging

Joga turnos reais pela API, como o front: `POST /action` (SSE) e, no `done`,
`/choices` e `/scene` em paralelo, com o turno seguinte saindo na hora.
`--see-during-turn` dispara um See no primeiro token. No fim lista os jobs
criados (espera na fila e duração) e, com `--expect-card`, quanto cada card
levou para existir depois do último `done`.

Metas (exit 1 se alguma falhar): nenhum 409; choices em até 16 s ou 503 limpo;
See no meio do turno sem 409; backdrop até 60 s na fila; cards esperados em
até `--card-wait`. O token é mintado com `AUTH_USER`/`AUTH_SECRET` do `.env` e
nunca é impresso.

```bash
# na VM, contra o staging (127.0.0.1:2123). Grava turnos: nunca rode contra o app principal.
python3 tools/replay_turn.py --env-file /home/caio/aidungeon/.env \
    --adventure dd5a6fa4c0394dd3832a1261778ef5a2 \
    --turn "do:saiam todos, quero apenas a clara aqui" --turn "say:clara? tudo bem?" \
    --see-during-turn --expect-card Clara
```

Soak: `--repeat N` repete a lista de `--turn`, `--see-every K` dispara o See a
cada K turnos e turno acima de `--slow-turn` (120 s) conta como falha. No fim
saem p50/p95 de TTFT, turno e choices, mais quantos choices deram 503.
Staging e app principal dividem o llama-server e o A1111: com `--yield-to`, o
replay só manda o próximo turno depois de nenhuma aventura do app principal
mudar por `--idle-s` (300 s).

```bash
python3 tools/replay_turn.py --env-file /home/caio/aidungeon/.env \
    --adventure <id de uma aventura de teste no staging> \
    --turn "do:olho em volta" --turn "say:e agora?" --repeat 100 --see-every 5 \
    --yield-to http://127.0.0.1:2122
```

## 2.2 repro_slot_hang.py e slot_watchdog.py — slot preso (#28280)

No #28280 um slot de modelo híbrido (GDN, caso do Qwen3.6-35B-A3B) fica
`is_processing` para sempre, com o log repetindo "erasing old context
checkpoint" na mesma posição. Os relatos pedem 2+ slots, prompt cache e um
slot fixado trocando de conversa.

`repro_slot_hang.py` fala direto com o llama-server (não grava nada no app).
`--mode bug` repete o cenário dos relatos: o slot fixado alterna uma conversa
longa de prefixo crescente e duas curtas, todas com cache, enquanto outro slot
trabalha. `--mode app` repete o tráfego do app (narrativa no slot 0 com cache,
auxiliares sem cache nos slots do contrato C1); `--external` soma um cliente
sem `id_slot`. Slot ocupado sem passar do próprio máximo de progresso por
`--stall-s` conta como travado: o script fecha a conexão e mede se o slot
solta. Exit 0 sem travamento, 3 com. Precisa de `slot_watchdog.py` na mesma
pasta.

```bash
python3 tools/repro_slot_hang.py --env-file /home/caio/aidungeon/.env --mode bug --rounds 15 --stall-s 90
python3 tools/repro_slot_hang.py --env-file /home/caio/aidungeon/.env --mode app --external --rounds 20
```

`slot_watchdog.py` roda como root pelo `slot-watchdog.timer` (a cada 10 s) e
reinicia o `llama-server` quando um slot passa de `--stall-s` (45 s) sem
progresso e nenhum outro slot andou nesse tempo, ou quando `/slots` fica esse
tempo sem responder, com `--cooldown` de 180 s entre reinícios. Slot parado
atrás do prompt longo de outro slot (o lote é montado na ordem dos slots) só
espera, até `--hard-stall-s` (300 s): no #28280 os outros slots seguem
servindo, e tráfego contínuo adiaria o reinício para sempre. O estado legível pelo app fica em
`/var/lib/slot-watchdog/status.json`. Instalação em `deploy/vm/README.md`.

Com `--hint-file` (um por instância do app: `data/llm_stall_hint.json`) o
watchdog lê a dica que o app grava quando a narrativa fica sem token e sem
`prompt_progress` e é repetida em outro slot. Dica de até `--hint-max-age`
(120 s) com o slot dela ainda ocupado sem progresso por `--hint-stall-s`
(20 s) reinicia sem a espera dos 300 s; só aguarda o fallback terminar
(`--hint-quiet-s`, 10 s sem nenhum slot andando) até `--hint-defer-s` (60 s)
depois da dica.

## 2.3 bench_slots.py — llama-server nos papéis do app

Fala direto com o llama-server, nos slots do contrato C1. Cenários (cada um
`--repeat` vezes, com p50/p95):

| Cenário | O que mede |
|---|---|
| `pp` | prompt longo sem cache num slot auxiliar: prompt tok/s do servidor |
| `frio` | narrativa inteira reprocessada no slot 0 (topo do prompt mudou): TTFT |
| `solo` | narrativa só acrescentando turnos no slot 0: TTFT e tok/s de geração |
| `carga` | a narrativa do `solo` com `--aux` chamadas sem cache ao mesmo tempo: TTFT, tok/s e latência das auxiliares |

Com `nvidia-smi` no PATH registra o pico de VRAM de cada cenário. Exit 0 com
as metas ok, 4 com alguma meta falhando. Não grava nada no app, mas ocupa os
slots: rode com o jogador parado.

```bash
python3 tools/bench_slots.py --env-file /home/caio/aidungeon/.env --json /tmp/bench.json
python3 tools/bench_slots.py --env-file /home/caio/aidungeon/.env --scenarios carga --aux 2
```

## 3. Onde configurar a API key

Precedência usada pelos dois scripts: **`--api-key` > env `LLM_API_KEY` > `.env` (raiz do repo) > `config\settings.json` (`llm.api_key`)**.
O valor da chave nunca é impresso nos logs nem versionado (`.env` está no `.gitignore`).

| Via | Como |
|---|---|
| `.env` na raiz (recomendado) | linha `LLM_API_KEY=sua-chave` — lido automaticamente pelos scripts e pelo backend |
| Variável de ambiente | `$env:LLM_API_KEY = "sua-chave"` (sessão atual) ou `setx LLM_API_KEY "sua-chave"` (permanente, abrir novo terminal) |
| Backend (persistente) | `PUT /api/settings/llm` com corpo `{"api_key": "sua-chave"}` |
| UI | Aba **Config** → campo **API key** |

> O nome da variável de ambiente é configurável em `settings.json` → `llm.api_key_env` (padrão `LLM_API_KEY`).

### 3.1 `llm.disable_thinking` (OBRIGATÓRIO p/ o modelo da VM)

O modelo servido na VM é um **Qwen3.x híbrido (thinking)**. Sem o parâmetro
`chat_template_kwargs={"enable_thinking": false}` em cada chamada a
`/chat/completions`, o modelo consome os tokens como `reasoning_content` e
devolve `content` **VAZIO** (validado contra a VM em 2026-10-07).

- `config\settings.json` → `llm.disable_thinking: true` (padrão) é o contrato do projeto:
  todo cliente (backend `llm_client`, `bench_llm.py`) **deve** enviar
  `chat_template_kwargs={"enable_thinking": false}` quando o campo estiver `true`.
- `bench_llm.py` faz isso automaticamente; `--thinking on` desliga o override (só p/ experimentos).
- `check_llm.py` avisa no relatório quando detecta modelo `qwen3*` no servidor.

## 4. Fallback local (quando a VM estiver fora)

Cenário padrão: LLM remoto na VM (`http://vm.groupabz.com:1234/v1`, llama.cpp).
Se a VM ficar indisponível, rode um servidor local com **KoboldCpp** + GGUF em
`data\models\` (conforme `config\models.json` → `local_fallbacks`):

| Perfil | Modelo | Quant | Arquivo em `data\models\` | Fonte (HF) | RAM | ctx | threads |
|---|---|---|---|---|---|---|---|
| Fallback local | Qwen2.5-7B-Instruct | Q4_K_M | `Qwen2.5-7B-Instruct-Q4_K_M.gguf` | `bartowski/Qwen2.5-7B-Instruct-GGUF` | ~5 GB | 8192 | 8 |
| Fallback rápido | Qwen2.5-3B-Instruct | Q5_K_M | `Qwen2.5-3B-Instruct-Q5_K_M.gguf` | `bartowski/Qwen2.5-3B-Instruct-GGUF` | ~2,5 GB | 8192 | 8 |

Passo a passo:

```powershell
# 1. baixa KoboldCpp (release estável Win x64) + GGUF e gera start_llm_local.bat
powershell -ExecutionPolicy Bypass -File tools\setup_llm_local.ps1
#    (para o modelo rápido: ... -Model qwen2.5-3b-instruct-q5_k_m ; -Force refaz downloads)

# 2. sobe o servidor local na porta 5001 (ctx 8192, 8 threads)
.\start_llm_local.bat

# 3. aponta o backend para o LLM local (uma das opções):
#    - UI aba Config:  base_url = http://127.0.0.1:5001/v1
#    - ou via API:
curl -X PUT http://127.0.0.1:8000/api/settings/llm -H "Content-Type: application/json" -d "{\"base_url\":\"http://127.0.0.1:5001/v1\"}"

# 4. valida e mede
python tools\check_llm.py --base-url http://127.0.0.1:5001/v1
python tools\bench_llm.py --base-url http://127.0.0.1:5001/v1   # KoboldCpp não exige key por padrão
```

No Linux o equivalente é `bash tools/setup_llm_local.sh` (binário `koboldcpp-linux-x64`
ou `koboldcpp-linux-arm64`, gravado em `data/koboldcpp/koboldcpp`). Ele gera
`start_llm_local.sh`; o restante (porta 5001, `base_url`, `check_llm.py`) é o mesmo.
`--model` e `--force` espelham `-Model` e `-Force`.

Para voltar à VM, restaure `base_url = http://vm.groupabz.com:1234/v1` pela mesma via.

> Expectativa de velocidade no hardware local (i5-1145G7, CPU): ~5-12 tok/s com
> 7B Q4_K_M — jogável graças ao streaming. Para partidas mais ágeis, use o 3B.
