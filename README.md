# AI Dungeon Local

Réplica funcional e 100% local do AI Dungeon: mesmas mecânicas de jogo, mesmo modelo de
dados, importação integral do backup oficial — sem nenhum serviço externo. Tudo roda em
`127.0.0.1` com um LLM local em GGUF.

> O plano mestre completo (anatomia do produto, especificação técnica, riscos e roadmap)
> está em [`PLANO.md`](PLANO.md). Este README registra as **decisões de arquitetura** e o
> **mapa do repositório**.

---

## Decisões de arquitetura

### Stack

```
┌────────────────────────────────────────────────────────┐
│  FRONTEND  React + Vite + Tailwind (SPA, tema escuro)  │
├────────────────────────────────────────────────────────┤
│  BACKEND   Python 3.12 + FastAPI + SQLite              │
│  game engine · montador de contexto · memória · SSE    │
├────────────────────────────────────────────────────────┤
│  LLM       KoboldCpp (API OpenAI-compatível, streaming)│
│  modelo GGUF local, zero nuvem                         │
└────────────────────────────────────────────────────────┘
```

Single-user local: sem login, sem rede externa. O build estático do frontend é servido
pelo próprio FastAPI; um `iniciar.bat` (fase DevOps) sobe LLM + backend e abre o navegador.

### Por que KoboldCpp

- **Um único `.exe`**, sem toolchain de compilação — crítico num Windows sem CUDA.
- **Otimizado para CPU** (nosso hardware é i5-1145G7 + Iris Xe, sem GPU dedicada) e feito
  para *storytelling* (sampler com repetition penalty, stop sequences, contexto grande).
- Expõe **API compatível com OpenAI** (`/v1/chat/completions` com streaming), então o
  backend fala um protocolo padrão e o runtime pode ser trocado por `llama-server` ou
  Ollama alterando apenas `config/settings.json` (`llm.base_url`), sem tocar em código.
- Modelo padrão: **Qwen2.5-7B-Instruct Q4_K_M** (~4,7 GB; pesos + KV cache cabem folgados
  nos 16 GB de RAM com contexto 8k). Fallback configurável: Qwen2.5-3B Q5_K_M ("modo
  rápido"). Ver `config/models.json`.

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

A fundação (esta fase) entrega apenas estrutura e contratos — `schema.sql`,
`docs/openapi.yaml`, `config/*.json` — para que backend, memória, importador, frontend e
QA sejam desenvolvidos em paralelo contra as mesmas interfaces (ver PLANO.md §5).

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
│  ├─ schema.sql             DDL exata do PLANO.md §4.1 (aplicada na 1ª execução)
│  ├─ app\                   FastAPI: main, db, game_engine, context_builder,
│  │  │                      llm_client (a implementar — fase backend)
│  │  └─ routes\             routers da API (contratos em docs/openapi.yaml)
│  ├─ memory\                triggers.py, summarizer.py, retrieval.py (fase memória)
│  ├─ importer.py            backup AI Dungeon → banco (fase importador)
│  └─ exporter.py            banco → backup AI Dungeon, round-trip (fase importador)
├─ frontend\                 React + Vite + Tailwind (fase frontend; build servido pelo FastAPI)
├─ config\
│  ├─ settings.json          sampler, budgets de contexto, LLM provider, stop sequences,
│  │                         memory bank, story cards (valores do PLANO.md §4.2/§4.3/§4.7)
│  └─ models.json            modelos GGUF disponíveis + qual está ativo
├─ data\
│  ├─ aidungeon.db           banco SQLite (git-ignored)
│  ├─ models\*.gguf          pesos dos modelos (git-ignored)
│  └─ koboldcpp\             runtime do LLM (git-ignored)
├─ tools\                    setup_llm.ps1, bench.py (fase LLM)
├─ tests\                    suíte de testes (fase QA)
├─ docs\
│  └─ openapi.yaml           contrato REST completo (PLANO.md §4.4), incl. SSE
├─ instalar.ps1 · iniciar.bat · parar.bat   (fase DevOps)
```

## Backup de referência

`D:\Projeto\AI_Dungeon_CAIO` — aventura "Faerûn: Caio" (201 ações, 236 story cards,
25 memórias). Tratado como **somente leitura** pelo importador; o critério de aceite nº 1
é importá-lo 100% e **continuar a história do ponto exato onde parou** (PLANO.md §2.2).
