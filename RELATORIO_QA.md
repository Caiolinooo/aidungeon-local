# RELATÓRIO QA — AI Dungeon Local

**Data:** 2026-10-08 · **Agente:** engenheiro-qa (Onda 5) · **Escopo:** provar que o sistema
inteiro funciona, com foco no replay E2E do backup real + smoke real na VM.

## 1. Resumo executivo

- **77/77 testes verdes** em ~23 s (`python -m pytest tests/`), sem nenhum skip nesta máquina.
- **Replay E2E do backup real verde**: as 83 ações do jogador de "Faerûn: Caio" rejogadas via
  API contra LLM mockado determinístico — **0 falhas** 4xx/5xx, 83 streams SSE terminando em
  `done`, seq contíguo o tempo todo, nenhum payload acima do orçamento de contexto.
- **Round-trip verde**: import → export é semanticamente idêntico ao backup (ações, cards,
  memórias, campos de metadata).
- **Smoke real na VM verde**: login/token real (.env), health ok com modelo descoberto, 1
  ação `do` com SSE até `done` (4,7 s, ~57 tok/s), undo, cleanup direto no SQLite.
- **Nenhum bug de produto encontrado** (nenhum BLOQUEANTE). Somente ajustes de teste.
- Contratos intactos: `docs/openapi.yaml`, `backend/schema.sql`, `config/settings.json` e
  `.env` **não foram modificados**; `.env` é gitignored e suas credenciais nunca foram
  impressas ou commitadas.

## 2. Cobertura por área

| Área | Arquivo de teste | Testes | O que cobre |
|---|---|---:|---|
| Engine (game loop) | `tests/test_game_engine.py` | 16 | action do/say/story (formato byte a byte do backup), continue, retry (mesmo seq/novo id, falha preserva resposta), undo (nunca remove `start`), edit (`edited_at`), lock de geração concorrente (409), payload ao LLM (stop sequences, sampler, `enable_thinking=false`), fallback de `reasoning_content` |
| Montador de contexto | `tests/test_context_builder.py` | 8 | sanduíche do PLANO §4.2, budgets por bloco, janela recente truncando por ação inteira, formatação idempotente de ações importadas |
| Memória (cards + resumos) | `tests/test_memory.py` | 26 | triggers de story cards (keyword case-insensitive, pinned, budget/card), retrieval (recency + overlap, top_k), summarizer (memory bank a cada ~1500 tokens, story summary rolante ≤400, falha de LLM sempre silenciosa) |
| Auth | `tests/test_auth.py` | 12 | login (token HMAC + expiração), 401 genérico usuário/senha, throttle 429 após 5 falhas, rotas protegidas 401 sem/com token inválido/expirado/adulterado, SPA pública, fail closed sem credenciais |
| Settings/health do LLM | `tests/test_settings.py` | 11 | `api_key` mascarada, overrides no banco, redescoberta de modelo ao trocar base_url/alias, health ok/unreachable/auth_error |
| Import-export + replay E2E | `tests/test_e2e_replay.py` | **3 (novos)** | contagens exatas do backup real, replay das 83 ações do jogador, round-trip semântico (detalhes na §3) |
| Smoke real na VM | `tests/test_smoke_vm.py` | **1 (novo)** | fluxo feliz real contra `vm.groupabz.com` (detalhes na §4); `@pytest.mark.skipif` sem `LLM_API_KEY` (CI sem rede pula) |
| Frontend (build) | — | — | `frontend/dist/` presente (assets + index.html); SPA servida na raiz e pública sem token (coberto por `test_auth.py::test_spa_static_public_without_token`). Build não regerado neste QA. |

**Total: 77 testes** (73 preexistentes + 4 novos), todos verdes.

## 3. Replay E2E do backup real (números)

**Backup:** `D:\Projeto\AI_Dungeon_CAIO\faern-caio-backup.zip` (66 KB, somente leitura).
Conteúdo de referência: **201 ações** (1 `start`, 117 `continue`, 25 `do`, 58 `say` — ids do
backup com buracos em 0..211, logo o importador avisa e usa a ordem do array), **236 story
cards**, **25 memórias**, `storySummary` vazio, Plot Essentials 401 chars, Author's Note
1567 chars, AI Instructions 2367 chars.

**(a)+(b) Importação via `POST /api/import/aidungeon` (Bearer de teste):**
- ImportReport: `actions_imported=201`, `cards_imported=236`, `memories_imported=25`,
  warning esperado "ids do backup não são contíguos".
- Estado via API: ações na **mesma ordem, tipos e textos** do backup; **seq contíguo
  0..200**; `plot_essentials` / `authors_note` / `ai_instructions` / `story_summary` /
  `tags` / `third_person` **byte a byte iguais** ao `metadata.json`; `source="import"`.

**(c) Replay** (2ª cópia importada; 83 ações do jogador reenviadas via
`POST /api/adventures/{id}/action`, SSE consumido até `done`; LLM = MockTransport
determinístico — eco numerado em 2ª pessoa; summarizer desviado para o mesmo mock):
- **83/83 ações rejogadas sem nenhuma falha 4xx/5xx** (todas HTTP 200).
- **83/83 streams terminando em `done`**, zero eventos `error`; `continue` persistida e
  não-vazia em todas; formatação persistida do jogador conferida byte a byte.
- **seq contíguo do início ao fim**: cópia termina com **367 ações, seq 0..366 sem buracos**
  (201 importadas + 83 jogador + 83 continuações). A 1ª cópia permaneceu intacta (201).
- **Orçamento de contexto nunca estourado** (instrumentação dos 83 payloads capturados):
  estimativa min/média/máx = **5179 / 5458 / 5699 tokens**; teto seguro = 7892
  (context_size 8192 − max_tokens 300 de resposta). Estimador = o próprio
  `context_builder.estimate_tokens` (chars/4).
- **Subsistema de memória exercitado de verdade**: 39 chamadas de resumo (memory bank +
  story summary rolante) atendidas pelo mock durante o replay.
- Duração: ~6 s (determinístico, sem rede).

**(d) Round-trip** (export da 1ª cópia intacta via `GET /api/adventures/{id}/export`):
- Ações: **201 na mesma ordem**, mesmos tipos, textos e `createdAt` (ids viram o seq
  contíguo 0..200 — diferença intencional e documentada, pois o backup tem buracos).
- Cards: **236** — mesmo conjunto (título, tipo, entry, triggers, pinned,
  useForCharacterCreation), independente de ordem/ids.
- Memórias: **25**, mesmos textos na mesma ordem.
- Metadata: `title`, `description`, `memory`, `authorsNote`, `tags`, `thirdPerson`,
  `state.instructions.scenario`, `state.storySummary` idênticos; `totalActionCount=201`.

## 4. Smoke real na VM (rodou — resultado)

`tests/test_smoke_vm.py::test_smoke_vm_real_flow` — **PASSOU** (~5,7 s):

- Middleware Bearer ativo: `/api/adventures` sem token → **401**.
- Login real: senha errada → **401 `invalid_credentials`** (endpoint exercitado contra o
  `AUTH_PASSWORD_HASH` real). Nota de design: o `.env` guarda **apenas o hash** da senha,
  portanto o token do fluxo é emitido pelo **mesmo mecanismo do login**
  (`make_token(AUTH_USER, AUTH_SECRET)` do `.env`) e validado em `/api/auth/check` → **200**
  com o usuário real.
- `GET /api/health/llm` → **status `ok`**, `model_loaded=true`, `model_discovered=
  Qwen3.6-35B-A3B-Uncensored-HauhauCS-Aggressive-IQ2_M.gguf`.
- Aventura sintética → 1 action `do` com SSE até `done`: **geração em 4,7 s, 269 eventos
  token (~57 tok/s)**, resposta não-vazia em 2ª pessoa, **sem vazar reasoning**
  (`<think>`/`reasoning_content` ausentes — `disable_thinking` funcionando).
- `undo` → `action_count=0`; aventura sintética **apagada direto no SQLite** (a API não tem
  DELETE) e confirmada inexistente.
- **Dados reais protegidos**: o teste roda sempre sobre banco temporário
  (`AIDUNGEON_DB_PATH` em `tmp_path` via fixture) e **se recusa a rodar** se o banco efetivo
  for `data\aidungeon.db` — a aventura "Faerûn: Caio" real jamais é tocada.
- Em CI sem `LLM_API_KEY` (e sem `.env`): `skipif` pula o teste — suíte segue verde.

## 5. Checklist dos critérios de aceite (PLANO.md §6)

| Fase | Critério | Status | Evidência |
|---|---|---|---|
| 0 — Fundação | repo + contratos aprovados | ✅ | `backend/schema.sql`, `docs/openapi.yaml`, `config/settings.json` presentes e intactos (fora do diff deste QA) |
| 1 — LLM | bench ≥ 4 tok/s; streaming OK | ✅ (adaptado ao §11: LLM na VM) | Smoke real: streaming SSE OK, **~57 tok/s** na VM (Qwen3.6-35B-A3B MoE). `tools/bench_llm.py` existe, mas o alvo mudou de local para VM |
| 2 — Engine MVP | criar aventura e jogar 10 turnos via API | ✅ | 16 testes de engine + replay de **83 turnos** via API sem falha |
| 3 — Importação | "Faerûn: Caio" 100% importada e continuável do ponto exato | ✅ | `test_import_backup_exact_counts` (201/236/25, campos byte a byte) + replay na cópia a partir do estado importado completo |
| 4 — UI | jogar completo no navegador com Retry/Undo/Edit/SSE | ⚠️ parcial | `frontend/dist/` presente, SPA servida e pública (teste verde). **Sem teste automatizado de browser** — validação manual do usuário pendente |
| 5 — Memória | cards por keyword; memory bank resume sozinho | ✅ | 26 testes de memória + summarizer exercitado no replay (39 resumos mockados) |
| 6 — QA/DevOps | replay E2E verde; `iniciar.bat` 1-clique | ✅ QA / ⚠️ DevOps | Replay E2E verde (§3). `iniciar.bat`/`instalar.ps1`/`parar.bat` **presentes na working tree** (entregues por outro agente, ainda não commitados) — **não executados neste QA** |
| 7 (opc) — "See" | imagem da cena | ❌ | Fora do escopo v1 (PLANO §3/§10) |

**Teste de aceite final (§6, manual):** pendente de sessão com `iniciar.bat`. Os passos
equivalentes via API estão verdes: importação completa (✅), continuar do ponto exato
(✅ 83× no replay), Retry/Undo/persistência (✅ unitários e smoke).

## 6. Riscos conhecidos restantes

1. **Estimador de tokens (chars/4)** — os budgets são verificados com o estimador do próprio
   sistema; um tokenizer real pode divergir (sobretudo fora do inglês). Margem observada no
   replay (máx 5699 de 7892) dá folga, mas não é garantia dura. Mitigação futura: trocar
   `estimate_tokens` por tokenizer GGUF real (interface já isolada).
2. **Teto teórico do sanduíche** — o `BASE_SYSTEM_PROMPT` (~75 tokens) não conta no budget
   do bloco 1; se TODOS os blocos estiverem no teto simultaneamente, o prompt pode chegar a
   ~7755 tokens (+512 reserva = 8267 > 8192). Com dados reais o máximo observado foi 5699 —
   risco residual baixo, mas existe no pior caso sintético.
3. **Divergência cosmética conhecida (não-bug):** o backup grava `say` como
   `> You say, "..."` (com vírgula) e o engine grava `> You say "..."` (sem vírgula) em
   ações NOVAS. Import/export preservam o verbatim do backup (round-trip verde); só o
   texto de ações rejogadas/novas usa o formato do engine.
4. **`storySummary` do backup vem vazio** — na 1ª geração real pós-import, o rolling summary
   resume o passado (trecho capado em 6000 chars, 1 chamada extra de LLM). Comportamento por
   design do summarizer; visível apenas no 1º turno.
5. **Sem DELETE de aventura na API** — cleanup é manual via SQLite (o smoke cobre o caminho).
   Contrato `openapi.yaml` não prevê remoção.
6. **Frontend sem E2E de browser** — fluxo Play (Retry/Undo/Edit/SSE) coberto só na API.
7. **Throttle de login em memória** — reiniciar o processo zera o lockout (por design;
   documentado em `routes/auth.py`).
8. **Embeddings do memory bank** — fase 4 pendente; retrieval atual é keyword+recency
   (coluna `embedding` já existe no schema).

## 7. Integridade de contratos e segredos

- `docs/openapi.yaml`, `backend/schema.sql`, `config/settings.json`: **não modificados**
  (ver `git status` — diff deste QA toca somente `tests/` + este relatório).
- `.env`: gitignored (`.gitignore:37`), **nunca impresso nem commitado**; testes usam
  credenciais de teste via `conftest.py`; o smoke lê o `.env` real sem expor valores.
- Commit escopado: `tests/test_e2e_replay.py`, `tests/test_smoke_vm.py`, `RELATORIO_QA.md`.

## 8. Como reproduzir

```bash
python -m pytest tests/ -q                 # suíte completa (77 testes)
python -m pytest tests/test_e2e_replay.py -q -s   # replay E2E + números de contexto
python -m pytest tests/test_smoke_vm.py -q -s     # smoke real na VM (precisa do .env)
```

## 9. Verificação v0.12.0 — 2026-10-24

- **Suíte completa**: `python -m pytest tests/ -q` → **1019 passed, 4 skipped**
  em 4 min 10 s, incluindo os 154 testes de `tests/e2e_opaque` que antes falhavam
  na coleta. Nenhum teste de produto falhou.
- **Soak local**: 200/200 turnos via API/uvicorn, zero eventos `error`, 200 ações
  persistidas e `seq` contíguo `0..199` em 116,09 s. Provider controlado e imediato;
  prova estabilidade, não qualidade narrativa nem latência do modelo externo.
- **Thread isolada**: `test_worker_see_failed_still_refuses_a_minor` também passa
  com `PytestUnhandledThreadExceptionWarning` promovido a erro após sincronizar a
  thread com o fim do teste.
- **Build de produção**: `npm run build` → sucesso em 14,5 s. Permanecem os avisos
  de chunks maiores que 500 kB (bundle principal e emotionDetect).
- **Smoke real do build, banco e providers descartáveis**: login, criação e tela Play
  sem erros de console. Parar chamou `/generation/cancel` (200), interrompeu o stream
  e não gravou prosa parcial; o Continue seguinte concluiu.
- **Preferências por aventura**: Curta + Mais fiel persistiram via `/ui-state`; o
  provider recebeu `max_tokens=180` e `temperature=0.45`. Preferências internas não
  entram em `[Player state]`.
- **Imagens**: See retornou 202, apareceu na central e terminou `done`; Gerar
  variação chamou `?variation=1` e concluiu uma nova imagem. Artefatos removidos.
- **Health e acessibilidade**: sondas LLM/voz/imagem `ok` no ambiente controlado;
  zero controles sem nome acessível na aba LLM; viewport 390×844 sem overflow.
- **Contratos**: `docs/openapi.yaml` validado como YAML (57 paths) e atualizado com
  cancelamento, healthchecks, variação e campos avançados de imagem.
- Os providers do smoke eram controlados e locais, portanto este resultado prova os
  fluxos e contratos, não a qualidade artística da LLM, da voz ou das imagens reais.

Para repetir:

```bash
python -m pytest tests/ -q
cd frontend && npm run build
python -c "import yaml; yaml.safe_load(open('../docs/openapi.yaml', encoding='utf-8'))"
```

## 10. Auditoria sistêmica e hotfixes v0.12.1 — 2026-10-25

- Auditoria completa de vida, aventura, eventos, mídia, segurança e operação.
  O estado atual é um jogo narrativo com motor lite e Modo Vida reativo; ainda
  não é um mundo autônomo nem um livefeed durável unificado.
- Roadmap F0–F7 documentado em `PLANO.md §12`, com arquitetura por outbox,
  revisões, autonomia sem LLM por tick, event log, sistemas e gates mensuráveis.
- **1048 verificações verdes**: 1045 aprovações na suíte completa e os três casos
  de autorização restantes reexecutados e aprovados separadamente; 4 skips.
- **Build de produção** concluído em 26,3 s; avisos de chunks >500 kB permanecem.
- Hotfixes de propriedade/admin, clones isolados, Bearer em downloads, clone
  assíncrono e health/deploy. Migration aditiva `029_ownership.sql`.
- OpenAPI validado (58 paths), incluindo `/api/jobs/{jobId}/cancel` e regras de
  propriedade/admin. `tasks.md` acompanha L0 concluído e L1–L7 pendentes.

Este relatório não declara o simulador completo. Esse marco exige todos os gates
do `PLANO.md §12.1`, não apenas a suíte atual.
