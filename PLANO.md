# PLANO MESTRE — AI Dungeon Local (cópia funcional self-hosted)

> **Objetivo:** rodar em casa uma réplica funcional do AI Dungeon (mesmas mecânicas de jogo,
> mesmo modelo de dados, importando 100% do backup real), usando **LLM 100% local**, sem
> depender de nenhum serviço externo.
>
> **Backup de referência:** `D:\Projeto\AI_Dungeon_CAIO` (aventura "Faerûn: Caio" — 201 ações,
> 236 story cards, 25 memórias).
>
> **Observação de escopo:** réplica **funcional** — mesmas mecânicas e formato de dados, com UI
> e código próprios (sem copiar marca, assets ou código proprietário da Latitude).

---

## 1. Como o AI Dungeon funciona (anatomia do produto)

### 1.1 Loop de jogo
1. Jogador escolhe/cria um **Scenario** (mundo, premissa, prompt inicial).
2. O jogo instancia uma **Adventure** a partir do scenario.
3. A cada turno o jogador envia uma **Action**; a IA gera a continuação narrando em
   **2ª pessoa** ("You are... You see...").
4. O jogador pode **Retry** (regenerar a última resposta), **Undo** (voltar turnos),
   **Edit** (editar qualquer texto, inclusive respostas da IA) e **Continue** (IA segue sozinha).

### 1.2 Tipos de ação (confirmado no backup)
| Tipo | Significado | Formatação no texto |
|---|---|---|
| `start` | Texto de abertura do scenario | prosa pura |
| `do` | Ação física do personagem | `> You <verbo>...` |
| `say` | Fala do personagem | `> You say "..."` |
| `story` | Narração direta injetada pelo jogador | prosa pura |
| `continue` | Continuação gerada pela IA | prosa pura |
| `see`* | Gera imagem da cena (AI Dungeon "See") | — *(fase opcional)* |

### 1.3 Sistema de contexto e memória (o coração do produto)
O AI Dungeon não manda a história inteira ao modelo. Ele monta um "sanduíche" de contexto:

```
┌─ AI Instructions (prompt do scenario / instruções de estilo)
├─ Plot Essentials ("Memory") — bloco fixo: quem é você, mundo, regras
├─ Story Cards disparados — lorebook: entram quando uma palavra-chave
│   aparece no texto recente (ex.: mencionar "Baldur's Gate" injeta o card da cidade)
├─ Memory Bank — resumos automáticos de eventos antigos, recuperados por relevância
├─ Story Summary — resumo rolante de tudo que saiu da janela recente
├─ Janela recente — últimos N mil tokens da história, verbatim
├─ Author's Note — nota de estilo/tom injetada PERTO DO FIM (alta influência por recência)
└─ Última ação do jogador ("> You do ..." / '> You say "..."') → modelo continua
```

### 1.4 Features do produto (checklist da réplica)
- [x] Scenarios (criar/editar/clonar), tags, privacidade — **local: sempre privado**
- [x] Adventures com histórico completo de ações
- [x] Actions: Do / Say / Story / Continue / Retry / Undo / Edit
- [x] Plot Essentials (Memory) editável
- [x] Author's Note editável
- [x] Story Cards (título, tipo, triggers, texto, pin, "usar na criação de personagem")
- [x] Memory Bank automático (resumos gerados pela própria IA)
- [x] Story Summary rolante
- [x] Biblioteca de scenarios + busca por tag
- [x] Export/Import de aventura (mesmo formato do backup original — round-trip)
- [ ] "See" (imagens) — fase opcional (hardware fraco, ver §3)
- [ ] Multiplayer / TTS / app mobile — fora do escopo v1 (roadmap §10)

---

## 2. Análise do backup real (o que temos e como mapear)

Arquivos em `D:\Projeto\AI_Dungeon_CAIO`:

| Arquivo | Conteúdo | Uso |
|---|---|---|
| `faern-caio-backup.zip` → `metadata.json` | adventure + state completos | importação primária |
| `faern-caio-backup.zip` → `actions-001.json` | 201 ações (`partNumber/totalParts/actions`) | histórico verbatim |
| `adventure-...-story-cards-236.json` | 236 story cards | redundante com metadata (validar) |
| `faern-caio.md` | export legível | referência humana / teste de sanidade |

### 2.1 Mapeamento campo a campo (backup → clone)
```
metadata.json
├─ adventure.title / description / tags / thirdPerson / contentRating → adventures.*
├─ adventure.memory        → adventures.plot_essentials
├─ adventure.authorsNote   → adventures.authors_note
├─ state.instructions.scenario → adventures.ai_instructions
├─ state.storySummary      → adventures.story_summary
├─ state.storyCards[]      → story_cards
│    .keys (", Baldur's Gate ,") → triggers (split por vírgula, trim, ignorar vazios)
│    .value/.description   → entry
│    .type (location/character/...)  .title  .isPinned  .useForCharacterCreation
├─ state.memories[]        → memories (já vêm resumidos — importar direto!)
└─ totalActionCount / totalParts → paginação do histórico

actions-NNN.json (concatenar por partNumber)
└─ actions[]: {id, type, text, createdAt}
   ⚠ ids NÃO são contíguos (0,1,2,...,211 com buracos) — usar ORDEM DO ARRAY como sequência
```

### 2.2 Estado da aventura importada (critério de sucesso nº 1)
Após importar, abrir "Faerûn: Caio" no clone deve mostrar: as 201 ações na ordem, o
personagem (Caio, Aasimar Sorcerer, Zhentarim, Calimport), os 236 cards (Baldur's Gate,
Zhentarim, etc.), as 25 memórias, o Plot Essentials com a ficha do Caio e o Author's Note
de tom "mundo vivo". **E continuar a história do ponto exato onde parou.**

---

## 3. Hardware e stack (decisões fundamentais)

### 3.1 Hardware detectado
- CPU: i5-1145G7 (4C/8T, Tiger Lake) · RAM: 16 GB · GPU: Iris Xe integrada (sem CUDA)

**Consequência:** inferência por CPU com modelos pequenos quantizados (GGUF Q4/Q5).
Expectativa realista: **5–12 tok/s** com 7B Q4 — jogável, pois respostas são streamadas
(experiência similar ao AI Dungeon original).

### 3.2 Modelo de linguagem (escolha com fallback)
| Papel | Recomendado | Alternativas | RAM aprox. |
|---|---|---|---|
| Narrador principal | **Qwen2.5-7B-Instruct Q4_K_M** | Llama-3.1-8B Q4_K_M, Mistral-7B-v0.3 Q4 | ~5 GB |
| Modo rápido | Qwen2.5-3B-Instruct Q5_K_M | Gemma-2-2B, Phi-3.5-mini Q4 | ~2,5 GB |
| Resumidor (memória) | o próprio modelo principal (temp baixa) | — | 0 |
| Embeddings (fase 4) | bge-small-en-v1.5 GGUF (~33 MB) | fallback: busca por palavra-chave | ~0,1 GB |

> Critério: 7B Q4_K_M + contexto 8k cabe folgado em 16 GB (pesos 4,7 GB + KV cache ~2 GB).
> Se a máquina do usuário final for outra, o config permite trocar o modelo sem mudar código.

### 3.3 Runtime de LLM
**KoboldCpp** (recomendado): um único `.exe`, otimizado para CPU, feito para storytelling,
expõe **API compatível com OpenAI** (`http://127.0.0.1:5001/v1/chat/completions` com streaming).
Alternativas plugáveis via config: `llama-server` (llama.cpp) ou Ollama.

### 3.4 Stack do clone
```
┌────────────────────────────────────────────────────────┐
│  FRONTEND  React + Vite + Tailwind (SPA)               │
│  réplica da UI: feed da história, tabs Do/Say/Story    │
├────────────────────────────────────────────────────────┤
│  BACKEND   Python 3.12 + FastAPI + SQLite              │
│  game engine · montador de contexto · memória · SSE    │
├────────────────────────────────────────────────────────┤
│  LLM       KoboldCpp (OpenAI-compatible, streaming)    │
│  modelo GGUF local, zero nuvem                         │
└────────────────────────────────────────────────────────┘
```
Tudo roda em `127.0.0.1`. Um `iniciar.bat` sobe LLM + backend + abre o navegador.

---

## 4. Especificação técnica

### 4.1 Banco de dados (SQLite, `aidungeon.db`)
```sql
CREATE TABLE scenarios(
  id TEXT PRIMARY KEY, title TEXT NOT NULL, description TEXT DEFAULT '',
  ai_instructions TEXT DEFAULT '', plot_essentials TEXT DEFAULT '',
  authors_note TEXT DEFAULT '', opening_text TEXT DEFAULT '',
  tags TEXT DEFAULT '[]', third_person INTEGER DEFAULT 0,
  created_at TEXT, updated_at TEXT);

CREATE TABLE adventures(
  id TEXT PRIMARY KEY, scenario_id TEXT REFERENCES scenarios(id),
  title TEXT NOT NULL, description TEXT DEFAULT '',
  ai_instructions TEXT DEFAULT '', plot_essentials TEXT DEFAULT '',
  authors_note TEXT DEFAULT '', story_summary TEXT DEFAULT '',
  tags TEXT DEFAULT '[]', third_person INTEGER DEFAULT 0,
  source TEXT DEFAULT 'new',            -- 'new' | 'import'
  created_at TEXT, updated_at TEXT);

CREATE TABLE actions(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  adventure_id TEXT NOT NULL REFERENCES adventures(id),
  seq INTEGER NOT NULL,                  -- ordem absoluta (não o id do backup!)
  type TEXT NOT NULL CHECK(type IN ('start','continue','do','say','story','see')),
  text TEXT NOT NULL, created_at TEXT, edited_at TEXT,
  UNIQUE(adventure_id, seq));

CREATE TABLE story_cards(
  id TEXT PRIMARY KEY, adventure_id TEXT NOT NULL REFERENCES adventures(id),
  title TEXT DEFAULT '', type TEXT DEFAULT 'other',
  triggers TEXT DEFAULT '[]',            -- JSON array de palavras-chave
  entry TEXT DEFAULT '', pinned INTEGER DEFAULT 0,
  use_for_character_creation INTEGER DEFAULT 0,
  created_at TEXT, updated_at TEXT);

CREATE TABLE memories(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  adventure_id TEXT NOT NULL REFERENCES adventures(id),
  text TEXT NOT NULL, seq_start INTEGER, seq_end INTEGER,
  embedding BLOB,                        -- fase 4 (pode ser NULL)
  created_at TEXT);

CREATE TABLE settings(key TEXT PRIMARY KEY, value TEXT);
```

### 4.2 Montador de contexto (componente crítico)
Ordem **exata** de montagem e orçamento de tokens (contexto 8192):

| Bloco | Ordem | Budget (tokens) |
|---|---|---|
| System: AI instructions + "narrador em 2ª pessoa" | 1 | ≤ 700 |
| System: Plot Essentials | 2 | ≤ 400 |
| System: Story Cards (pinned + disparados, por relevância) | 3 | ≤ 1200 |
| System: Memory Bank (top-k relevantes) | 4 | ≤ 600 |
| System: Story Summary | 5 | ≤ 400 |
| User: janela recente verbatim (com `>` nas ações do jogador) | 6 | ~3800 |
| System(final): Author's Note | 7 | ≤ 300 |
| User(final): última ação do jogador formatada | 8 | ≤ 200 |
| **Reserva p/ resposta** | | 512 |

Regras:
- Formatação do jogador: `do` → `\n> You {texto}\n`; `say` → `\n> You say "{texto}"\n`
  (é exatamente assim no backup — manter byte a byte).
- Truncar a janela recente **por ação inteira** (nunca cortar no meio de uma ação).
- Stop sequences: `\n> You`, `\n> you` — impede a IA de jogar pelo jogador.
- Resposta: streaming SSE; ao final, gravar action `continue`.

### 4.3 Prompts (templates exatos)
**System base:**
```
You are an AI storyteller running an interactive text adventure. Continue the story in
second person ("You ..."), matching the established tone. Write vivid prose with dialogue,
sensory detail and consequences for the player's actions. Never decide what the player
character says or does beyond their stated action. Never break the fourth wall.
```
**Gatilho de Story Card:** varrer os últimos ~1000 caracteres da janela (case-insensitive);
se qualquer trigger do card aparecer → injetar `entry`. Pinned = sempre. Budget por card 150 tk.
**Resumo (Memory Bank):** a cada ~1500 tokens novos, resumir o trecho (temp 0.3, ≤120 palavras,
foco em fatos/decisões/NPCs), gravar em `memories`. O backup prova o formato (ver `state.memories`).
**Story Summary:** reescrever incrementalmente o resumo global sempre que a janela desliza
(descarta ações antigas) — sempre caber em 400 tokens.

### 4.4 API REST (FastAPI)
```
POST   /api/scenarios                          criar scenario
GET    /api/scenarios  |  GET /api/scenarios/{id}
POST   /api/adventures                         nova aventura (de scenario ou vazia)
GET    /api/adventures                         lista (home)
GET    /api/adventures/{id}                    estado completo p/ UI
POST   /api/adventures/{id}/action             {type, text} → SSE stream da resposta
POST   /api/adventures/{id}/continue           IA segue sozinha (SSE)
POST   /api/adventures/{id}/retry              regenera última resposta (SSE)
POST   /api/adventures/{id}/undo               remove último turno
PATCH  /api/actions/{id}                       editar texto de qualquer ação
CRUD   /api/adventures/{id}/cards              story cards
GET/PUT /api/adventures/{id}/memory            plot_essentials + authors_note
POST   /api/import/aidungeon                   upload do .zip do backup
GET    /api/adventures/{id}/export             exporta NO MESMO FORMATO do backup
GET    /api/health/llm                         status do KoboldCpp + modelo carregado
```

### 4.5 Frontend (réplica da experiência)
**Tela 1 — Home:** grid "Suas Aventuras" (card com título, último trecho, data) + botão
"Nova Aventura" + "Importar backup do AI Dungeon".
**Tela 2 — Jogo (a principal):**
- Feed central rolável: prosa da IA em texto corrido; ações do jogador em destaque
  (estilo citação, como no original: `**You do** / **You say**` + blockquote).
- Barra de entrada com tabs **[ Do | Say | Story ]**, caixa de texto, botão enviar.
- Toolbar: **↻ Retry · ↩ Undo · ▶ Continue**; hover numa ação → ✏ Edit.
- Drawer lateral (hambúrguer): abas **Story Cards | Memory (Plot Essentials) | Author's
  Note | Detalhes | Config** (temperatura, tamanho da resposta, modelo).
- Estado "IA escrevendo..." com texto streamando token a token.
**Tela 3 — Editor de Scenario** (form completo + opening text).
Sem login (single-user local). Tema escuro padrão, como o original.

### 4.6 Importador (`importer.py`)
1. Abre o `.zip`, lê `metadata.json` + todos `actions-*.json` (ordena por `partNumber`).
2. Cria scenario implícito + adventure com campos mapeados (§2.1).
3. Insere ações na **ordem do array** (seq 0..N), preservando `type` e `created_at`.
4. Insere story cards (triggers = split/trim de `keys`) e memories (como estão).
5. Aceita também o JSON solto de story cards para merge/validação.
6. **Teste de round-trip:** exportar a aventura importada e comparar semanticamente com o
   backup original (mesmas ações na ordem, mesmos cards, mesmas memórias).

### 4.7 Sampler (defaults inspirados no AI Dungeon)
`temperature 0.85 · top_p 0.95 · repetition_penalty 1.08 · max_tokens 300 (slider 100–600)`
Todos expostos na aba Config por aventura.

---

## 5. Plano de execução com agentes especializados

Execução orquestrada pelo Claude Code: cada área recebe um **subagente dedicado** (Agent tool)
com escopo, arquivos e critérios de aceite próprios. Dependências definem a ordem.

### 5.1 Elenco de agentes

| # | Agente | Missão | Entregáveis | Depende de |
|---|---|---|---|---|
| 1 | **arquiteto** | Fundação e contratos | repo `AIDungeon_Local/`, schema.sql, openapi.yaml, README de decisões | — |
| 2 | **engenheiro-llm** | Runtime do modelo local | `tools/setup_llm.ps1` (baixa KoboldCpp + GGUF), `config/models.json`, `bench.py` (tok/s + smoke test) | 1 |
| 3 | **engenheiro-backend** | API + game engine | FastAPI: DB, CRUDs, `context_builder.py`, `game_engine.py` (action/retry/undo/edit/continue), SSE | 1 |
| 4 | **engenheiro-memoria** | Cards + memória | `memory/triggers.py`, `memory/summarizer.py`, rolling summary, retrieval (keyword → embeddings fase 4) | 3 |
| 5 | **engenheiro-importador** | Backup → banco | `importer.py` + `exporter.py` (round-trip) + `tests/test_import.py` rodando o backup REAL | 3 |
| 6 | **engenheiro-frontend** | UI réplica | React/Vite/Tailwind: Home, Play, Editor; streaming SSE no feed | 3 (API) |
| 7 | **engenheiro-qa** | Qualidade | testes unitários, teste E2E de replay (rejeita as 201 ações contra mock-LLM), checklist de coerência | 3,4,5 |
| 8 | **engenheiro-devops** | Experiência 1-clique | `instalar.ps1`, `iniciar.bat`, README de uso, logs rotativos | 2,3,6 |
| 9 | **engenheiro-imagem** *(opcional)* | ação "See" | Stable Diffusion CPU (SD1.5/TinySD) + endpoint `/see` — lento neste hardware, feature flag off | 3,6 |

### 5.2 Ordem de spawn
```
Onda 1 (sequencial):  arquiteto
Onda 2 (paralela):    engenheiro-llm  ┆  engenheiro-backend
Onda 3 (paralela):    engenheiro-memoria ┆ engenheiro-importador ┆ engenheiro-frontend
Onda 4:               engenheiro-qa (transversal)
Onda 5:               engenheiro-devops  (+ opcional: engenheiro-imagem)
```

### 5.3 Prompts prontos para spawn (copiar no Agent tool)
> Cada prompt abaixo é auto-contido; colar como `prompt` do Agent tool na hora de executar.

**arquiteto:**
> Crie o esqueleto do projeto em D:\Projeto\AIDungeon_Local conforme PLANO.md §4:
> estrutura de pastas (backend/, frontend/, memory/, tools/, tests/, data/), schema.sql
> exato do §4.1, openapi.yaml cobrindo §4.4, config padrão (config/settings.json com
> sampler do §4.7 e budgets do §4.2) e README.md de decisões. Não implemente lógica.

**engenheiro-llm:**
> Em D:\Projeto\AIDungeon_Local\tools, escreva setup_llm.ps1 que baixa KoboldCpp (release
> estável Windows x64) e o GGUF Qwen2.5-7B-Instruct Q4_K_M em data\models, gera
> start_llm.bat (porta 5001, contexto 8192, threads=8), e bench.py que mede tok/s e valida
> /v1/chat/completions com streaming. Não execute downloads; apenas entregue os scripts.

**engenheiro-backend:**
> Implemente backend/ (FastAPI + SQLite) conforme PLANO.md §4.1–4.4: todos os endpoints,
> context_builder.py montando o sanduíche na ordem e budgets exatos, game_engine.py com
> action/do/say/story/continue/retry/undo/edit, cliente httpx p/ KoboldCpp (OpenAI-compat,
> SSE), stop sequences ['\n> You','\n> you']. Testes unitários do context builder.

**engenheiro-memoria:**
> Implemente memory/ conforme PLANO.md §4.3: triggers.py (scan case-insensitive dos últimos
> 1000 chars, pinned sempre, budget 150 tk/card), summarizer.py (memory bank a cada ~1500
> tokens, temp 0.3, ≤120 palavras) e rolling story_summary (≤400 tokens). Integre ao
> context_builder via interface injetável. Testes com fixtures.

**engenheiro-importador:**
> Implemente importer.py + exporter.py conforme PLANO.md §2.1 e §4.6, rodando contra o
> backup real em D:\Projeto\AI_Dungeon_CAIO\faern-caio-backup.zip (trate como somente
> leitura; extraia em temp dir). seq = ordem do array; concatenar actions-*.json por
> partNumber; triggers = split(','/trim) de keys. Teste de round-trip: importar → exportar
> → comparar ações/cards/memórias semanticamente. Entregue relatório da importação real.

**engenheiro-frontend:**
> Implemente frontend/ (React+Vite+Tailwind) conforme PLANO.md §4.5: Home (lista/nova/
> importar), Play (feed, tabs Do/Say/Story, Retry/Undo/Continue/Edit, streaming SSE, drawer
> com Story Cards/Memory/Author's Note/Config), Editor de Scenario. Tema escuro. Consumir
> openapi.yaml. Build estático servido pelo FastAPI.

**engenheiro-qa:**
> Conforme PLANO.md §6: suíte de testes (unit → integração → E2E de replay com mock-LLM
> rejeitando as 201 ações do backup), lint, e RELATORIO_QA.md com cobertura e falhas.

**engenheiro-devops:**
> Conforme PLANO.md §7: instalar.ps1 (python venv, deps, npm build), iniciar.bat (sobe
> KoboldCpp, backend, abre navegador), parar.bat, README de uso e solução de problemas.

---

## 6. Fases e critérios de aceite

| Fase | Marco | Critério de aceite |
|---|---|---|
| 0 | Fundação | repo + contratos (schema/openapi/config) aprovados |
| 1 | LLM local | bench.py ≥ 4 tok/s com 7B Q4; streaming OK |
| 2 | Engine MVP | criar aventura e jogar 10 turnos via API (sem UI) |
| 3 | **Importação** | "Faerûn: Caio" importada 100% e **continuável** do ponto exato |
| 4 | UI | jogar completo no navegador com Retry/Undo/Edit/SSE |
| 5 | Memória | cards disparam por keyword; memory bank resume sozinho após N turnos |
| 6 | QA/DevOps | replay E2E verde; `iniciar.bat` sobe tudo com 1 clique |
| 7 (opc) | "See" | imagem da cena (CPU, feature-flag) |

**Teste de aceite final:** abrir `iniciar.bat` → Home mostra "Faerûn: Caio" → abrir →
história inteira presente → enviar `do` "You hand Thuldan the sealed scroll" → IA continua
em 2ª pessoa, coerente com Zhentarim/Calimport, streamando, sem falar pelo jogador →
Retry gera alternativa → Undo volta → fechar tudo e reabrir → estado intacto.

---

## 7. Riscos e mitigações

| Risco | Impacto | Mitigação |
|---|---|---|
| Velocidade do 7B na CPU | latência alta | streaming (parece vivo), modo 3B "rápido", resposta ≤300 tk |
| Qualidade narrativa < AI Dungeon (modelos deles são maiores) | imersão | Qwen2.5-7B é forte em prosa; temperatura/author's note ajustáveis; aceitar troca de GGUF |
| RAM (16 GB com Windows) | swap | fechar apps pesados, ctx 8k, KV cache quantizado q8 |
| Drift de lore em histórias longas | incoerência | story cards + memory bank + rolling summary (fase 5) |
| Importação quebrar em backup futuro | dados | importer validado por round-trip + fixtures |
| Escopo explodir (multiplayer/app) | atraso | fora da v1, roadmap §10 |

---

## 8. Estrutura de pastas alvo
```
D:\Projeto\AIDungeon_Local\
├─ PLANO.md                 ← este arquivo
├─ backend\
│  ├─ app\ (main.py, db.py, game_engine.py, context_builder.py, llm_client.py, routes\)
│  ├─ memory\ (triggers.py, summarizer.py, retrieval.py)
│  └─ importer.py, exporter.py
├─ frontend\ (React+Vite+Tailwind → build servido pelo FastAPI)
├─ config\ (settings.json, models.json)
├─ data\ (aidungeon.db, models\*.gguf, koboldcpp\)
├─ tools\ (setup_llm.ps1, bench.py)
├─ tests\
├─ instalar.ps1 · iniciar.bat · parar.bat
└─ docs\ (openapi.yaml, decisions.md)
```

---

## 9. Como executar este plano
1. Aprovar o plano (e o modelo 7B padrão, ou escolher 3B para velocidade).
2. Rodar **Onda 1** (arquiteto) → revisar contratos.
3. Rodar **Onda 2** em paralelo; enquanto isso o usuário executa `tools\setup_llm.ps1`
   (download ~4,7 GB, única etapa manual de rede).
4. Ondas 3–5; a cada fase, validar o critério de aceite da §6.
5. Jogar.

## 10. Roadmap pós-v1 (opcional)
- **"See"**: SD1.5/TinySD em CPU (~2–4 min/img neste hardware) ou integração com GPU futura.
- **Embeddings** no memory bank (bge-small GGUF) para recuperação semântica.
- **TTS** local (Piper) para narração em voz alta.
- **PWA** p/ jogar do celular na mesma rede Wi-Fi.
- **Multiplayer LAN** (salas compartilhadas via rede local).
- **Modo "scripting"** (hooks de input/output como o AI Dungeon Scripts).

---

*Plano gerado em 2026-10-07 a partir da análise do backup real (201 ações, 236 cards, 25
memórias) e do hardware local (i5-1145G7 · 16 GB · Iris Xe).*
