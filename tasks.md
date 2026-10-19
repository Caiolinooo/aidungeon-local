# Quadro de tarefas: AI Dungeon fluido

Plano mestre: sincronizar VM, local e GitHub; destravar o jogo (4 slots + imagem
em paralelo); cards, retratos e memória confiáveis; See correto; likeness de
pessoas reais (só SFW). Briefings: [docs/agents/GROK.md](docs/agents/GROK.md),
[docs/agents/KIMI_K3.md](docs/agents/KIMI_K3.md). Contratos:
[docs/agents/CONTRATOS.md](docs/agents/CONTRATOS.md).

Status: `pendente` · `em andamento` · `feito` · `bloqueado`.

## Fase 0 (Orquestrador)

| ID | Tarefa | Status |
|---|---|---|
| F0.1 | Backend da VM no repositório (20 arquivos, `web_search.py`, migration 009, `config/settings.json`) | feito |
| F0.2 | Cherry-pick `6a4bdd4` (PR #3), mantendo o filtro de `see` da VM | feito |
| F0.3 | Suite alinhada ao contexto da VM (339 verdes) | feito |
| F0.4 | `tasks.md`, `docs/agents/{CONTRATOS,GROK,KIMI_K3}.md` | feito |
| F0.5 | `deploy/vm/README.md` com o deploy real; `data/references/` no `.gitignore` | feito |
| F0.6 | Fonte do frontend v0.9.0 (usuário) | bloqueado → K0 |
| F0.7 | Trocar senha SSH, API key e cadastrar chaves por agente (usuário) | pendente |

## Grok: Plataforma e Performance

| ID | Tarefa | Depende de | Status |
|---|---|---|---|
| G1 | Hotfix: auxiliares sem cache e fora do slot 0, choices/See sem lock, timeouts curtos, stubs C3, A1111 com `MemoryMax` | F0 | pendente |
| G2 | llama-server `-np 4`, repro do #28280, watchdog de slots | G1 | pendente |
| G3 | Scheduler de LLM (prioridade, cancelamento, breaker, pós-turno) | G1 | pendente |
| G4 | Event bus + SSE | G3 | pendente |
| G5 | Jobs com prioridade + See assíncrono | G3 | pendente |
| G6 | Forge + DMD2 + FaceID/InstantID, presets SDXL | G1 | pendente |
| G7 | Orçamento de RAM/VRAM, `-ub`/`-ncmoe`/`-t` | G2 | pendente |
| G8 | Segurança: `--api-key-file`, SSH por chave, bind local, ufw | F0 | pendente |
| G9 | Deploy git + staging (2123) | F0 | pendente |
| G10 | Observabilidade (`/api/health/llm`, log por turno) | G3 | pendente |

## Kimi K3: Narrativa, Imagens e Frontend

| ID | Tarefa | Depende de | Status |
|---|---|---|---|
| K0 | Frontend v0.9.0 reconstruído a partir do bundle da VM | F0 | pendente |
| K1 | `output_sanitizer.clean_narration` + blocos voláteis delimitados | F0 | pendente |
| K2 | Meta-comandos `/card`, `/foto`, `/cena` + linguagem natural | K1 | pendente |
| K3 | Chronicle confiável, retrato automático, `scene_state` (migration 010) | G3 | pendente |
| K4 | Summarizer em `role=summary` + `tools/backfill_chronicle.py` | K3 | pendente |
| K5 | See com elenco presente e prompt em inglês (planejador C4) | K3, G5 | pendente |
| K6 | Likeness de pessoa real + guardrail SFW (C8) | G6 | pendente |
| K7 | Sampler pt-BR (A/B) | — | pendente |
| K8 | Frontend via SSE, input livre após `done`, drawer de likeness | G4 | pendente |
| K9 | e2e Playwright no staging | K5, K6, K8 | pendente |

## QA final

| ID | Tarefa | Status |
|---|---|---|
| QA | pytest, Playwright, soak 200 turnos, replay Costazul, benchmarks, deploy com rollback | pendente |
