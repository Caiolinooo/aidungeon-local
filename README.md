# AI Dungeon Local

Réplica funcional e self-hosted do AI Dungeon: mesmas mecânicas de jogo, mesmo modelo de
dados, importação integral do backup oficial. Backend + SPA rodam em `127.0.0.1`; o LLM
e o gerador de imagens são **endpoints configuráveis** (local ou VM), ambos com API
OpenAI-compatível.

> O plano mestre completo (anatomia do produto, especificação técnica, riscos e roadmap)
> está em [`PLANO.md`](PLANO.md). Este README registra as **decisões de arquitetura** e o
> **mapa do repositório**.

---

## Funcionalidades (v0.7.0)

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
  (8 moods nativos do TalkingHead, com lipsync pt-BR/en para narração TTS).
  O modo "seguir a história" detecta o mood do NPC a partir da narrativa mais
  recente (menção do nome + palavras de emoção PT/EN); override manual vence
  por 60s.
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
- **See (geração de imagens)**: ilustre o que acabou de acontecer. O prompt
  começa pela ação recente e pelo visual travado do card (e do jogador, quando
  o texto diz "you"). Se essa pessoa já tem retrato, a cena parte dessa foto.
  Nudez descrita no momento não vai para o negativo. O estilo visual fica no
  fim do prompt. Na VM o checkpoint é o **epiCRealism XL CrystalClear**
  (SDXL fotorealista, sem filtro): retrato e See em 1024×1024, faixa da cena
  em 1344×768. Provedores: **sdwebui** ou **openai_compatible**.
- **Escolhas múltiplas**: após cada turno, o LLM sugere 3 próximas ações clicáveis
  (chips acima da entrada) — desligável em `config/settings.json` (`choices.enabled`).
- **Múltiplas histórias**: aventuras ilimitadas na Home, cada uma com memória, story
  cards e estilo visual próprios; importação/exportação de backups do AI Dungeon
  (round-trip, incl. `imageStyle`).
- **Configuração pela UI** (aba Config): endpoint/chave/modelo da LLM, sampler
  (temperature, top_p, repetition penalty, max tokens), contexto e todas as opções de
  imagem. Overrides ficam no banco e prevalecem sobre `config/settings.json`.
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
`iniciar.bat` sobe o backend e abre o navegador.

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
monta o prompt nesta ordem exata (PLANO.md §4.2, contexto 8192 tokens):

| # | Bloco | Budget (tokens) |
|---|---|---|
| 1 | System: AI instructions + "narrador em 2ª pessoa" | ≤ 700 |
| 2 | System: Plot Essentials ("Memory") | ≤ 400 |
| 3 | System: Story Cards (pinned + disparados por keyword) | ≤ 1200 |
| 4 | System: Memory Bank (top-k relevantes) | ≤ 600 |
| 5 | System: Story Summary (resumo rolante) | ≤ 400 |
| 6 | User: janela recente verbatim (`> You ...` nas ações do jogador) | ~3800 |
| 7 | System(final): Author's Note — perto do fim, alta influência por recência | ≤ 300 |
| 8 | User(final): última ação do jogador formatada | ≤ 200 |
| — | Reserva para a resposta | 512 |

Regras inegociáveis:
- Formatação do jogador **byte a byte como no backup**: `do` → `\n> You {texto}\n`;
  `say` → `\n> You say "{texto}"\n`.
- Truncar a janela recente **por ação inteira**, nunca no meio de uma ação.
- Stop sequences `\n> You` / `\n> you` — a IA nunca joga pelo jogador.
- Geração sempre com **streaming SSE**; ao final, a resposta é gravada como action
  `continue`.

### Contratos antes de código

`schema.sql`, `docs/openapi.yaml` e `config/*.json` são a fonte da verdade — backend,
frontend e QA são desenvolvidos contra essas interfaces (ver PLANO.md §5). O schema do
banco evolui por **migrations versionadas** (`backend/migrations/00X_*.sql`, controladas
por `PRAGMA user_version`): o runner aplica só o que falta, em DBs novos ou antigos.

---

## Pré-requisitos

- **Python 3.12**
- **Node.js 20+** (build do frontend)
- **~6 GB de disco livres** para o modelo GGUF 7B Q4_K_M (mais ~2,5 GB se quiser o 3B)
- Windows 10/11 x64 para os scripts `tools/*.ps1` / `*.bat` (o restante é portável)
- 16 GB de RAM recomendados (hardware de referência: i5-1145G7, Iris Xe — sem CUDA)

## Como rodar

1. **Instalação (uma vez):**
   `powershell -ExecutionPolicy Bypass -File instalar.ps1`
   — verifica Python 3.12+ e Node 20+, cria `backend\.venv`, instala as deps Python,
   roda `npm ci` + `npm run build` no frontend, cria `.env` a partir de
   [`.env.example`](.env.example) (preencha os 4 valores — os comentários no arquivo
   ensinam a gerar cada um) e testa o LLM da VM.
2. **Uso diário:** duplo-clique em **`iniciar.bat`** — sobe o backend
   (`uvicorn` em `127.0.0.1:8000`, log em `logs\backend-AAAAMMDD.log`) e abre o
   navegador já na página de **login** (usuário/senha do `.env`).
   Para encerrar: **`parar.bat`**.
3. O backend serve o build da SPA (`frontend\dist`) na mesma origem — não é preciso
   `npm run dev` para jogar. Logs de erro do backend ficam em `logs\`.

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
│  │                         002 imagens/image_style · 003 story_cards de cenário
│  │                         · 004 jobs + assets de NPC
│  ├─ app\                   FastAPI: main, db, game_engine, context_builder,
│  │  │                      llm_client, image_client, assistant, settings_store, auth,
│  │  │                      jobs (worker do pipeline assíncrono de assets)
│  │  └─ routes\             routers da API (contratos em docs/openapi.yaml):
│  │                         generation (SSE), images (See), assistant, choices,
│  │                         settings (llm + image), cards, jobs (assets), memory,
│  │                         import/export…
│  ├─ memory\                triggers.py, summarizer.py, retrieval.py
│  ├─ importer.py            backup AI Dungeon → banco
│  └─ exporter.py            banco → backup AI Dungeon, round-trip
├─ frontend\                 React + Vite + Tailwind (build servido pelo FastAPI)
│  └─ src\                   páginas Home/Play/Login/ScenarioEditor, drawer
│                            (Cards/NPCs/Memory/Config/Details), GenerateAdventureModal
├─ config\
│  ├─ settings.json          sampler, budgets de contexto, LLM, imagens, assistente,
│  │                         escolhas, memory bank, story cards, stop sequences
│  └─ models.json            modelos GGUF de referência + qual está ativo
├─ data\
│  ├─ aidungeon.db           banco SQLite (git-ignored)
│  ├─ images\                imagens geradas (See + retratos de NPC, git-ignored)
│  ├─ models\*.gguf          pesos dos modelos (git-ignored)
│  └─ koboldcpp\             runtime local do LLM (git-ignored)
├─ tools\                    check_llm.py, bench_llm.py, setup_llm_local.ps1
├─ tests\                    suíte pytest (181 testes: engine, contexto, memória, auth,
│                           replay E2E do backup, smoke VM, v2 imagens/assistente,
│                           v0.3.0 jobs/retratos, v0.4.0 voz TTS/STT,
│                           v0.5.0 crônica, See e prosa,
│                           v0.5.1 epiCRealism XL,
│                           v0.7.0 ui-profile/controles dinâmicos)
├─ docs\
│  ├─ openapi.yaml           contrato REST completo (PLANO.md §4.4), incl. SSE
│  └─ DEPLOY_VERCEL.md       deploy do frontend na Vercel
├─ deploy\vm\                pacote de deploy do backend na VM (systemd)
├─ instalar.ps1 · iniciar.bat · parar.bat
```

## Backup de referência

`D:\Projeto\AI_Dungeon_CAIO` — aventura "Faerûn: Caio" (201 ações, 236 story cards,
25 memórias). Tratado como **somente leitura** pelo importador; o critério de aceite nº 1
é importá-lo 100% e **continuar a história do ponto exato onde parou** (PLANO.md §2.2).
