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
| G1 | Hotfix: auxiliares sem cache e fora do slot 0, choices/See sem lock, timeouts curtos, stubs C3, A1111 com `MemoryMax` | F0 | feito (no app principal em 2026-10-18) |
| G2 | llama-server `-np 5 --kv-unified -cram 0`, repro do #28280, watchdog de slots | G1 | em andamento (soak de 200 turnos no staging) |
| G3 | Scheduler de LLM (prioridade, cancelamento, breaker, pós-turno) | G1 | feito no código (testes locais); falta medir no staging |
| G4 | Event bus + SSE | G3 | feito no código (teste de integração com uvicorn); falta o staging |
| G5 | Jobs com prioridade + See assíncrono | G3 | feito no código (testes locais: 202 < 300 ms, jogador age durante o render, fundo velho cancelado); falta o staging |
| G6 | Forge + DMD2 + FaceID/InstantID, presets SDXL | G1 | pendente |
| G7 | Orçamento de RAM/VRAM, `-ub`/`-ncmoe`/`-t` | G2 | pendente |
| G8 | Segurança: `--api-key-file`, SSH por chave, bind local, ufw | F0 | em andamento: PUT de settings ignora chave mascarada (e180253); nginx sem query no access log e `.env` 600 aplicados na VM em 2026-10-18; `--api-key-file`, SSH por chave, bind 127.0.0.1 e ufw esperam a troca de senha e de chave pelo usuário |
| G9 | Deploy git + staging (2123) | F0 | feito: `tools/deploy.sh` + `smoke_check.py` (backup, smoke, rollback automático), repositório git na VM (`/home/caio/aidungeon.git` + checkout `/home/caio/aidungeon-git`); o `git fetch` direto do GitHub precisa de deploy key (usuário) |
| G10 | Observabilidade (`/api/health/llm`, log por turno) | G3 | feito no código (7896e8b); falta conferir no staging |

### G1: aceite e pendências

Replay da Costazul no staging (`tools/replay_turn.py`, turno das 23:18 e o
seguinte logo após o `done`, com See no meio de cada turno):

- 0 respostas 409; o segundo turno saiu 0,0 s depois do `done` e streamou.
- Choices: 200 em 9,7 s e um 503 limpo em 8,2 s (resposta sem array JSON).
- Backdrop: 28 s na fila (antes, 180 s ou mais) e 20,5 s de render.
- See no meio do turno: 200 em 20,9 s e um 503 `llm_timeout` em 20 s, porque
  com `-np 2` todos os auxiliares dividem o slot 1. Resolve na G2 (`-np 4`).
- Narração: TTFT 4–6 s, 10–14 tok/s.
- A1111 com tcmalloc dentro de 12G/14G: 1024² em 15,2 s quente, sem swap.

Ficou para outras tarefas:

- K3/K7: chronicle e choices às vezes devolvem JSON quebrado. Gerar com
  `response_format` (JSON schema do llama-server) em vez de só parsear melhor.
- K7: choices saem em inglês numa história em pt-BR.
- See que termina durante o turno seguinte grava entre a ação e a resposta. O
  undo já pula esse See; o planejador da K5 deve decidir onde ele aparece.

## Kimi K3: Narrativa, Imagens e Frontend

| ID | Tarefa | Depende de | Status |
|---|---|---|---|
| K0 | Frontend v0.9.0 reconstruído a partir do bundle da VM | F0 | pendente |
| K1 | `output_sanitizer.clean_narration` + blocos voláteis delimitados | F0 | feito no código (`<lore>`, stops, sanitizer, `test_prompt_prefix_stability`); falta o replay no staging |
| K2 | Meta-comandos `/card`, `/foto`, `/cena` + linguagem natural | K1 | pendente |
| K3 | Chronicle confiável, retrato automático, `scene_state` (migration 010) | G3 | pendente |
| K4 | Summarizer em `role=summary` + `tools/backfill_chronicle.py` | K3 | parcial: história que cabe na janela de 106k e no horizonte de 4096 tk agora arquiva pela cena viva de 8 turnos (`archive_horizon_turns`); falta `tools/backfill_chronicle.py` e o aceite de ≥3 memórias nas outras aventuras |
| K5 | See com elenco presente e prompt em inglês (planejador C4) | K3, G5 | pendente |
| K6 | Likeness de pessoa real + guardrail SFW (C8) | G6 | feito no código: pessoa real força SFW, cena explícita tira a pessoa ou recusa, menor nunca; Wikipedia e upload em `data/references`. ArcFace/FaceID esperam a G6 |
| K7 | Sampler pt-BR (A/B) | — | pendente |
| K8 | Frontend via SSE, input livre após `done`, drawer de likeness | G4 | pendente |
| K9 | e2e Playwright no staging | K5, K6, K8 | pendente |

## QA final

| ID | Tarefa | Status |
|---|---|---|
| QA | pytest, Playwright, soak 200 turnos, replay Costazul, benchmarks, deploy com rollback | pendente |

---

## Fatos do mundo (merge remote-code-world-facts-3ee3)


O pedido do jogador é tentativa. Lugar, item, pessoa e consentimento só entram se a abertura, a narração, uma ação Story, o Plot, um card ou uma memória já os estabeleceu.

1. Julgar destino, item e ordem contra o cânone (`backend/app/world_facts.py`).
2. Injetar o bloco `[Facts]` no narrador e nas sugestões.
3. Descartar narração que cumpre um pedido bloqueado (quarto inventado, NPC que obedece sem motivo).
4. Pintar o See e o fundo com o lugar real e a recusa, não com o desejo.
5. Mostrar o aviso na faixa da cena (`notice`).
6. Impedir o registro de lugar/item/obediência que só existiu no pedido.
7. Travar o sexo na imagem: mulher fica mulher, homem fica homem, a frase vai no começo do prompt.

## NPCs vivos (ficha, regeneração, foto, life-sim, gerar tudo)

| ID | Tarefa | Status |
|---|---|---|
| N1 | Ficha viva: humor, speech, goals, background, lifestyle, habits, likes, sentiments no canônico, nas escolhas e no painel | feito |
| N2 | Regenerar um campo ou os nove textos; Enrich continua preenchendo o que falta | feito |
| N3 | Foto de referência sem marcar pessoa real; apagar retrato; retrato novo a partir da foto; bloqueio só de menor | feito |
| N4 | Life-sim: memórias, mente pós-turno, tick do dia, bloco `[NPC minds]` | feito |
| N5 | POST /api/assistant/generate com fichas profundas | feito |
| N6 | OpenAPI dos endpoints e dos campos novos | feito |
