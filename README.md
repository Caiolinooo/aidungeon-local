# AI Dungeon Local

Réplica funcional e self-hosted do AI Dungeon: mesmas mecânicas de jogo, mesmo modelo de
dados, importação integral do backup oficial. Backend + SPA rodam em `127.0.0.1`; o LLM
e o gerador de imagens são **endpoints configuráveis** (local ou VM), ambos com API
OpenAI-compatível.

> O plano mestre completo (anatomia do produto, especificação técnica, riscos e roadmap)
> está em [`PLANO.md`](PLANO.md). Este README registra as **decisões de arquitetura** e o
> **mapa do repositório**.

---

## Funcionalidades (v0.9.0)

- **Modo Turbo**: toda chamada manda `cache_prompt: true`. `id_slot` só entra
  se `GET /props` reportar `total_slots >= 2` (narrativa no slot 0, auxiliar
  no 1). Servidor de um slot, ou falha nessa sonda: a chave não vai — não há
  flag no `settings.json`. No fio, a system da posição 0 guarda só a diretiva
  do narrador e `[Plot]`. `[World info]`, `[Memories]`, `[Summary]` e
  `[Player state]` abrem a mensagem user final, para não invalidar o prefixo
  cacheado. A janela recente corta por ação inteira e, quando corta, sobe o
  descarte para um múltiplo de 8. Ações `see` ficam fora da narração.
- **Teto de contexto**: `llm.context_size_limits.max` = 106496. Um valor
  gravado acima disso (o banco da VM tinha 262144) é cortado na leitura;
  `GET /api/settings/llm` também devolve `context_size_max`. Na VM o
  llama-server fica `-np 2 -c 262144` (131072 por slot) — ver
  [`deploy/vm/README.md`](deploy/vm/README.md) §8. O restart do llama-server
  vem antes do backend.
- **See**: o texto da action `see` é a legenda limpa (frase em inglês do
  modelo; o beat cru só se essa frase vier vazia). O prompt SDXL completo
  continua na linha da imagem (`images.prompt`). A tela ainda limpa captions
  antigas que guardavam o prompt inteiro.
- **AI Story Studio**: o rascunho aceita gênero, tom, POV (`second` / `third`
  / `first`), nome/papel/aparência do protagonista, `image_style`,
  `card_count` (3–8) e `web_search_mode` (`auto` / `always` / `off`). O POV
  vira uma diretiva fixa em `ai_instructions` — o montador de contexto não lê
  `third_person`. Dá para refinar um campo ou um card com uma instrução, e
  criar/enriquecer card pela API. Cenário existente: PATCH/PUT, duplicar e
  DELETE (aventuras ligadas ficam com `scenario_id` nulo). Migration
  `009_scenario_image_style.sql`.
- **Pesquisa web (opt-in)**: Wikipedia e DuckDuckGo, sem chave paga. Entra na
  criação de cenário (modo acima), no enriquecimento de cards e no turno
  (`web_search: true` no `POST /action`, toggle na barra). O bloco no prompt
  é `[Web Research & Canon Facts]`. Falha de rede não derruba a geração.
- **Service worker v3** (`aidungeon-local-v3`): JS/CSS da mesma origem são
  network-first (cache só se a rede falhar). `skipWaiting` + `clients.claim`
  e um `controllerchange` recarregam a aba aberta. O backend manda
  `Cache-Control: no-cache` em `index.html`, `sw.js` e `manifest.webmanifest`,
  e `public, max-age=31536000, immutable` em `assets/` (nome com hash do Vite).

## Funcionalidades (v0.8.0)

O que a v0.8.0 já incluía. Teto de contexto, See, Generate with AI e o
service worker descritos aqui foram substituídos pela v0.9.0 acima.

- **Memória vetorial híbrida na CPU + CRUD do Memory Bank (Fase 4)**: memórias do
  `memory_bank` são vetorizadas via `Qwen3-Embedding-0.6B` (GGUF em `llama-server`
  secundário com `--embeddings --n-gpu-layers 0`, zero impacto na VRAM da L4).
  A migração `008_memory_embeddings.sql` armazena `embedding_model` e `embedding_dim`
  ao lado do BLOB para ignorar vetores de outros modelos/dimensões. `select_memories`
  combina similaridade por cosseno com bônus de palavra-chave para nomes próprios,
  mantendo o orçamento de 800 tokens, `top_k=12` e a inclusão obrigatória da memória
  mais recente (com fallback automático para ranking por palavra-chave se o serviço
  estiver offline). Inclui `tools/backfill_embeddings.py` e CRUD completo na API/UI
  (`GET/POST/PATCH/DELETE /api/adventures/{id}/memories` e `POST .../memories/regenerate`).
- **Qualidade de conversação e samplers avançados**: suporte nativo a **Min-P** (`min_p`),
  **DRY repetition penalty** (`dry_multiplier`, `dry_base`, `dry_allowed_length`,
  `dry_penalty_last_n`), `presence_penalty`, `frequency_penalty`, **retry temperature
  stepping** ($T = \min(T_{\text{base}} + 0.05 \times n, 1.15)$) e stop sequences
  dinâmicos com o nome do protagonista + barreiras anti-impersonation no `context_builder`.
  A pesquisa completa está em [`docs/R2_RESEARCH.md`](docs/R2_RESEARCH.md) e o benchmark
  reprodutível de 10 situações em [`tools/eval_conversation.py`](tools/eval_conversation.py).
- **Redesign visual completo, temas de leitura e PWA**: sistema de design com 3 temas
  (**Obsidian**, **Parchment**, **OLED Midnight**), modal *Reader Preferences* (família
  tipográfica serif/sans/mono, tamanho 14–22px, espaçamento e largura da coluna), layout
  *two-pane* no desktop (`1440×900`, feed de leitura + inspetor lateral acoplado) e barra
  inferior / *drawer* otimizado para toque no celular (`390×844`, alvos $\ge 44\text{px}$).
  Página dedicada `/settings` com teto de contexto em `40.960` tokens (Fase 1). Instalável
  como **PWA** (`manifest.webmanifest`, `sw.js` com precache da *app shell* e banner de
  estado offline / backend inacessível).
- **Busca, filtros e ações em lote nos Story Cards & NPCs**: filtragem por texto (`q`),
  tipo (`type`) e fixação (`pinned`) em `GET /api/adventures/{id}/cards`, mais endpoint e
  barra de ações em lote (`POST /api/adventures/{id}/cards/bulk`: `delete`, `pin`, `unpin`,
  `retype`).
- **Scripting Hooks em sandbox**: modificadores estilo AI Dungeon (`input_modifier`,
  `context_modifier`, `output_modifier`) em [`backend/app/scripting.py`](backend/app/scripting.py),
  configuráveis em `GET/PUT /api/settings/scripting` e na página `/settings`. Executam com
  validação estática de AST (bloqueando `import`, atributos `__dunder__` e builtins de
  I/O/rede), isolamento em subprocesso e *timeout* estrito de `1.0s` com fallback seguro
  para o texto original em caso de erro.
- **Runbooks da VM GPU L4 (Fases 0–4)**: procedimentos detalhados e scripts de medição
  em [`docs/vm_runbooks/`](docs/vm_runbooks/README.md) cobrindo Fase 0 (baseline e rollback),
  Fase 1 (liberação de VRAM: `-c 40960 -np 1 -b 2048 -ub 512` sem `mmproj`), Fase 2
  (avaliação de quant `IQ3_XXS`/`IQ3_S`), Fase 3 (comparação SDXL `epiCRealism` vs
  `Juggernaut XL`) e Fase 4 (`llama-server` CPU para `Qwen3-Embedding-0.6B`).
- **Controles dinâmicos definidos pela IA**: cada aventura ganha um painel de
  controles desenhado pelo LLM a partir do cenário — tabs do InputBar com
  rótulos do gênero (ex.: Action/Dialogue/Narrate) e widgets de **dados**
  (rolagem local, enviar como ação), **stats** (HP/Mana com barras),
  **inventário** (chips, toque insere no input) e **ficha do personagem**
  (edição inline). O manifesto fica em `adventures.ui_profile` e o estado dos
  widgets em `adventures.ui_state` (edição do jogador, PUT com debounce); o
  estado entra no contexto como bloco `[Player state]` — a IA não narra dano
  ou tesouro no escuro. `enabled=false` em `config/settings.json` (`ui_profile`)
  volta ao InputBar padrão.
- **Prosa legível**: o prompt pede frases completas, com artigos e parágrafos.
  A tela só quebra bloco telegráfico na leitura; o texto gravado não muda.
- **Faixa da cena**: a pintura e o elenco ficam numa faixa no topo. O texto
  fica em fundo sólido.
- **Crônica dos NPCs**: depois de cada resposta, quem entrou ou mudou atualiza
  a ficha. A aparência já gravada só muda se o texto marcar essa mudança.
- **Celular**: a altura acompanha a área visível (o teclado não cobre o campo)
  e os botões da barra têm área de toque maior.
- **Voz (TTS/STT)**: narração e falas de NPC em áudio via stack OpenAI-compatível
  da VM — **Supertonic** (TTS, vozes M1–M3/F1–F3) e **Whisper** (STT, fale a ação
  pelo microfone). `POST /api/tts` devolve bytes `audio/mpeg` em passthrough;
  `POST /api/stt` recebe multipart e devolve `{"text": ...}`. Voz por NPC na ficha
  (aba NPCs), voz do narrador e endpoint/chave em `GET/PUT /api/settings/voice`
  (chave via `VOICE_API_KEY` no `.env`, sempre mascarada na API).
- **Avatares 3D (TalkingHead)**: aba "Avatars" no painel lateral — avatar GLB
  (Ready Player Me / VRoid convertido, via `@met4citizen/talkinghead` sobre
  three.js; presets em `frontend/public/avatars/` com licença) por NPC, com
  customização persistida na ficha (`vrm_id` em JSON — nome da coluna mantido
  por compatibilidade): modelo (preset ou URL GLB custom), corpo (M/F) e mood
  (8 moods nativos do TalkingHead, com lipsync pt-BR/en para narração TTS) +
  badges de voz e emoção no dock lateral.
- **Pipeline assíncrono de assets**: ao criar uma aventura a partir de um cenário,
  os retratos dos NPCs já começam a gerar em background (fila de jobs + worker no
  backend) — você joga enquanto os assets ficam prontos e a UI aplica cada um ao
  concluir (polling de `GET .../jobs`).
- **Aba NPCs**: ficha por personagem (story cards `type=character`) com retrato,
  descrição editável, prompt do retrato (vazio = o LLM escreve um a partir da
  descrição + contexto da história) e voz TTS (M1–M3, F1–F3). Botão de regenerar
  por NPC ou "Generate all portraits" para a aventura inteira.
- **Jogo completo**: ações Do/Say/Story, Continue, Retry, Undo, edição de qualquer ação,
  streaming SSE token a token — paridade com as mecânicas do AI Dungeon.
- **Generate with AI**: descreva uma ideia ("pirata steampunk caçando um leviatã") e o
  assistente gera o cenário inteiro — título, AI instructions, plot essentials, author's
  note, abertura em 2ª pessoa, tags e 3–8 story cards. Revise cada campo (com botão de
  regenerar individual) antes de criar a aventura.
- **See (geração de imagens)**: ilustre o que acabou de acontecer, com visualização em
  tela cheia, cópia rápida do prompt gerado e download direto em PNG. Na VM o checkpoint
  é o **epiCRealism XL CrystalClear** (SDXL fotorealista, sem filtro): retrato e See em
  1024×1024, faixa da cena em 1344×768. Provedores: **sdwebui** ou **openai_compatible**.
- **Escolhas múltiplas**: após cada turno, o LLM sugere 3 próximas ações clicáveis
  (chips acima da entrada) — desligável em `config/settings.json` (`choices.enabled`).
- **Múltiplas histórias**: aventuras ilimitadas na Home, cada uma com memória, story
  cards e estilo visual próprios; importação/exportação de backups do AI Dungeon
  (round-trip, incl. `imageStyle`).
- **Configuração pela UI** (`/settings` e aba Config): endpoint/chave/modelo da LLM,
  samplers (temperature, top_p, min_p, DRY, presence/frequency/repetition penalties,
  max tokens), contexto (até `40.960`), imagem, voz, memória vetorial e scripting hooks.
  Overrides ficam no banco e prevalecem sobre `config/settings.json`.
- **Auth local**: login Bearer HMAC (credenciais no `.env`), middleware protege toda
  `/api`.

---

## Decisões de arquitetura

### Stack

```
┌────────────────────────────────────────────────────────┐
│  FRONTEND  React + Vite + Tailwind (SPA, tema escuro)  │
├────────────────────────────────────────────────────────┤
│  BACKEND   Python 3.12 + FastAPI + SQLite              │
│  game engine · montador de contexto · memória · SSE    │
│  assistente de cenários · prompt builder de imagens    │
├────────────────────────────────────────────────────────┤
│  LLM       endpoint OpenAI-compatível configurável     │
│  (padrão: llama.cpp na VM; alternativas: KoboldCpp,    │
│   Ollama, LM Studio — basta trocar llm.base_url)       │
├────────────────────────────────────────────────────────┤
│  IMAGENS   sdwebui (A1111/Forge/SD.Next) ou endpoint   │
│  OpenAI-compatible de imagens — local, LAN ou VM       │
└────────────────────────────────────────────────────────┘
```

Single-user self-hosted: login local (Bearer HMAC, credenciais no `.env`), sem conta em
serviço externo. O build estático do frontend é servido pelo próprio FastAPI; o
`iniciar.bat` (Windows) ou `iniciar.sh` (Linux) sobe o backend e abre o navegador.

### Por que API OpenAI-compatível (LLM e imagens)

O backend nunca fala com um runtime específico: tanto a LLM (`llm_client`) quanto a
geração de imagens (`image_client`) usam protocolos padrão — `/v1/chat/completions` com
streaming e `/sdapi/v1/txt2img` ou `/v1/images/generations`. Trocar de runtime é editar
**uma URL** na aba Config (ou em `config/settings.json`), sem tocar em código:

- **LLM**: padrão atual é o `llama.cpp` da VM (`http://vm.groupabz.com:1234/v1`, modelo
  descoberto em runtime via `GET /models`). Vale KoboldCpp, Ollama, LM Studio ou
  qualquer servidor OpenAI-compatível — local ou remoto. KoboldCpp continua sendo a
  opção local recomendada: um único `.exe`, otimizado para CPU (nosso hardware é
  i5-1145G7 + Iris Xe, sem CUDA) e feito para *storytelling*. Modelos de referência em
  `config/models.json` (Qwen2.5-7B Q4_K_M; fallback 3B "modo rápido").
- **Imagens**: `sdwebui` (A1111/Forge/SD.Next com `--api`, local ou LAN) ou
  `openai_compatible` (qualquer `POST {base_url}/images/generations` — ex. um servidor
  de difusão na VM). Endpoint, resolução, steps, negative prompt e estilo são
  configuráveis pela UI.

Nota Qwen3.x: modelos híbridos exigem `chat_template_kwargs.enable_thinking=false` em
toda chamada (sem isso `content` volta vazio) — o `llm_client` já envia sempre que
`llm.disable_thinking` está ativo (padrão).

### Por que SQLite

- **Zero-configuração e um único arquivo** (`data/aidungeon.db`) — backup = copiar o arquivo.
- Transacional e rápido de sobra para o volume real (o backup de referência tem 201 ações,
  236 story cards, 25 memórias — ordens de grandeza abaixo de qualquer limite prático).
- Sem servidor de banco para o usuário instalar; schema versionado em `backend/schema.sql`
  (espelho exato do PLANO.md §4.1).

### Ordem do "sanduíche" de contexto

O coração do produto: nunca enviamos a história inteira ao modelo. O `context_builder`
monta o prompt nesta ordem lógica (budgets de `config/settings.json`):

| # | Bloco | Onde vai no fio | Budget (tokens) |
|---|---|---|---|
| 1 | AI instructions + diretiva de narrador | system (posição 0) | ≤ 700 |
| 2 | `[Plot]` (Plot Essentials) | system (posição 0) | ≤ 400 |
| 3 | `[World info]` — cards pinned + keyword | `<lore>` no topo da user final | ≤ 1200 |
| 4 | `[Memories]` | `<lore>` no topo da user final | ≤ 600 |
| 5 | `[Summary]` (resumo rolante) | `<lore>` no topo da user final | ≤ 400 |
| 5b | `[Player state]` | `<lore>` no topo da user final | — |
| 6 | Janela recente verbatim (`> You ...`) | uma mensagem user POR AÇÃO | dinâmico |
| 7 | `[Author's note: …]` | user final, depois do `<lore>` | ≤ 300 |
| 8 | STYLE/CANON/LORE cues + `[Facts]` + última ação | user final | ≤ 200 |
| — | Reserva para a resposta | — | 512 |

O template Qwen3 exige a system no início e rejeita system depois da posição 0.
Os blocos voláteis (3–5b) abrem a user final dentro do `<lore>` — assim o prefixo
cacheado do slot pinado sobrevive a card, memória, resumo e estado. A janela (6)
vai uma mensagem user por ação: o checkpoint de KV do slot vale para o turno
seguinte (modelo híbrido Gated DeltaNet só restaura checkpoints perto do fim do
prompt). Quando a janela não cabe, descarta ações inteiras em múltiplos de 8
(WINDOW_DROP_CHUNK). Ações `see` não entram em mensagem nenhuma.

Regras inegociáveis:
- Formatação do jogador **byte a byte como no backup**: `do` → `\n> You {texto}\n`;
  `say` → `\n> You say "{texto}"\n`.
- Truncar a janela recente **por ação inteira**, nunca no meio de uma ação.
- Stop sequences `\n> You` / `\n> you` — a IA nunca joga pelo jogador.
- Geração sempre com **streaming SSE**; ao final, a resposta é gravada como action
  `continue`.
- POV (1ª / 2ª / 3ª pessoa) só chega ao narrador pelo texto de `ai_instructions`.

### Contratos antes de código

`schema.sql`, `docs/openapi.yaml` e `config/*.json` são a fonte da verdade — backend,
frontend e QA são desenvolvidos contra essas interfaces (ver PLANO.md §5). O schema do
banco evolui por **migrations versionadas** (`backend/migrations/00X_*.sql`, controladas
por `PRAGMA user_version`): o runner aplica só o que falta, em DBs novos ou antigos.

---

## Pré-requisitos

- **Python 3.12+** (no Debian/Ubuntu, também `python3-venv`)
- **Node.js 20+** (build do frontend)
- **curl** (scripts de Linux e o health check)
- **~6 GB de disco livres** para o modelo GGUF 7B Q4_K_M (mais ~2,5 GB se quiser o 3B)
- Windows 10/11 x64 **ou** Linux x64/arm64
- 16 GB de RAM recomendados (hardware de referência: i5-1145G7, Iris Xe — sem CUDA)

## Como rodar

A instalação é a mesma nos dois sistemas: confere Python 3.12+ e Node 20+, cria o
venv, instala as deps Python, roda `npm ci` + `npm run build` no frontend, cria
`.env` a partir de [`.env.example`](.env.example) (preencha os 4 valores — os
comentários no arquivo ensinam a gerar cada um) e testa o LLM da VM.

**Windows**

1. **Instalação (uma vez):**
   `powershell -ExecutionPolicy Bypass -File instalar.ps1`
2. **Uso diário:** duplo-clique em **`iniciar.bat`** — sobe o backend
   (`uvicorn` em `127.0.0.1:8000`, log em `logs\backend-AAAAMMDD.log`) e abre o
   navegador já na página de **login** (usuário/senha do `.env`).
   Para encerrar: **`parar.bat`**.

**Linux**

1. **Instalação (uma vez):** `bash instalar.sh`
2. **Uso diário:** `bash iniciar.sh` — sobe o backend em background
   (log em `logs/backend-AAAAMMDD.log`, PID em `logs/backend.pid`) e abre o
   navegador. `AIDL_NO_BROWSER=1 bash iniciar.sh` sobe sem abrir o browser.
   Para encerrar: `bash parar.sh`.

Se `backend/.venv` já for o venv do Windows, o Linux usa `backend/.venv-linux`
para não misturar os dois interpretadores.

O backend serve o build da SPA (`frontend/dist`) na mesma origem — não é preciso
`npm run dev` para jogar. Logs de erro do backend ficam em `logs/`.

### Deploy (opcional)

- **Frontend na Vercel** — SPA estática com `VITE_API_BASE_URL` apontando para o
  backend: passo a passo em [`docs/DEPLOY_VERCEL.md`](docs/DEPLOY_VERCEL.md).
- **Backend na VM** (`vm.groupabz.com`, FastAPI persistente + systemd + TLS opcional):
  gerar o pacote com `deploy\vm\package_backend.ps1` e seguir
  [`deploy/vm/README.md`](deploy/vm/README.md).

## Onde cada coisa vive

```
AIDungeon_Local\
├─ PLANO.md                  plano mestre (fonte da verdade)
├─ README.md                 este arquivo — decisões e mapa
├─ backend\
│  ├─ schema.sql             DDL base (aplicada na 1ª execução)
│  ├─ migrations\            migrations versionadas (PRAGMA user_version):
│  │                         002 imagens · 003 story_cards · 004 jobs/assets
│  │                         · 005 npc_sheet · 006 portrait_anchor · 007 ui_profile
│  │                         · 008 memory_embeddings (modelo + dimensão do vetor)
│  │                         · 009 scenario image_style
│  ├─ app\                   FastAPI: main, db, game_engine, context_builder,
│  │  │                      llm_client, image_client, embeddings_client, scripting,
│  │  │                      assistant, web_search, settings_store, auth, jobs
│  │  └─ routes\             routers da API (contratos em docs/openapi.yaml):
│  │                         generation (SSE), images (See), assistant, choices,
│  │                         settings (llm/image/voice/embeddings/scripting),
│  │                         cards (busca + bulk), jobs, memory (CRUD + regenerate),
│  │                         import/export…
│  ├─ memory\                triggers.py, summarizer.py, retrieval.py (híbrido cosseno+keyword)
│  ├─ importer.py            backup AI Dungeon → banco
│  └─ exporter.py            banco → backup AI Dungeon, round-trip
├─ frontend\                 React + Vite + Tailwind (PWA servida pelo FastAPI)
│  ├─ public\                manifest.webmanifest, sw.js (offline app shell), icons/, avatars/
│  └─ src\                   páginas Home/Play/Login/ScenarioEditor/Settings,
│                            themeContext (Obsidian/Parchment/OLED), ReaderPreferencesModal,
│                            OfflineBanner, drawer (Cards/NPCs/Memory/Controls/Avatars/Config/Details)
├─ config\
│  ├─ settings.json          sampler (incl. Min-P, DRY, retry stepping), budgets de contexto,
│  │                         LLM (teto 106496), imagens, voz, embeddings, scripting
│  └─ models.json            modelos GGUF de referência + qual está ativo
├─ tools\                    check_llm.py, bench_llm.py, backfill_embeddings.py,
│                            eval_conversation.py, setup_llm_local.ps1 / .sh
├─ benchmark\                conversation_situations.json (10 situações de referência),
│                            scored_blind_eval.md (avaliação cega A/B R2)
├─ tests\                    suíte pytest (487 testes: engine, contexto, memória vetorial,
│                            auth, replay E2E do backup, cards bulk, scripting sandbox,
│                            eval de conversação, suíte E2E opaca de 4 camadas)
├─ docs\
│  ├─ openapi.yaml           contrato REST completo (PLANO.md §4.4), incl. SSE
│  ├─ R2_RESEARCH.md         pesquisa de qualidade narrativa (14 técnicas ranqueadas)
│  ├─ UI_AUDIT_RUBRIC.md     avaliação da UI/UX (rubrica 1–5 + acessibilidade + PWA)
│  ├─ screenshots\           capturas before/ e after/ (1440×900 e 390×844)
│  ├─ vm_runbooks\           runbooks das Fases 0–4 na GPU L4 (baseline, VRAM, quant, SDXL, CPU embeddings)
│  └─ DEPLOY_VERCEL.md       deploy do frontend na Vercel
├─ deploy\vm\                pacote de deploy do backend na VM (systemd)
├─ instalar.ps1 · iniciar.bat · parar.bat    (Windows)
├─ instalar.sh · iniciar.sh · parar.sh        (Linux)
```

## Backup de referência

`D:\Projeto\AI_Dungeon_CAIO` — aventura "Faerûn: Caio" (201 ações, 236 story cards,
25 memórias). Tratado como **somente leitura** pelo importador; o critério de aceite nº 1
é importá-lo 100% e **continuar a história do ponto exato onde parou** (PLANO.md §2.2).
