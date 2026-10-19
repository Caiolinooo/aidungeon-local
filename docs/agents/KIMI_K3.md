# Briefing do Kimi K3: Narrativa, Imagens (conteúdo) e Frontend

Você cuida do que o jogador lê e vê: narração limpa, cards e memória
confiáveis, conteúdo das imagens, likeness de pessoas reais (só SFW) e o
frontend. O Grok cuida da plataforma. Os contratos entre vocês estão em
[CONTRATOS.md](CONTRATOS.md) e são lei.

## Ambiente

- Backend FastAPI + SQLite em `backend/`, frontend React + Vite + Tailwind (PWA)
  em `frontend/`.
- Ambiente principal: `https://vm.groupabz.com:8443` (nginx → uvicorn 2122). Staging
  (G9): porta 2123. Você **não** mexe na VM principal.
- LLM: Qwen3.6-35B-A3B (híbrido MoE, `enable_thinking=false`) com visão
  (`--mmproj`) no llama-server. SD: SDXL (A1111 hoje, Forge depois da G6).
- Testes: `python -m pytest -q --ignore=tests/e2e_opaque`; front:
  `cd frontend && npm ci && npm run build`.

## Pode e deve mexer

- Backend existente: `npc_chronicle.py`, `npc_assets.py`, `assistant.py`,
  `routes/assistant.py`, `routes/cards.py`, `context_builder.py`,
  `memory/summarizer.py`, `routes/images.py`, `scene.py`, `routes/scene.py`,
  `web_search.py`.
- Backend novo: `meta_commands.py`, `output_sanitizer.py`, `likeness.py`,
  `image_plan.py`, `routes/likeness.py`, `tools/backfill_chronicle.py`.
- Todo o `frontend/`.
- `config/settings.json`, só as chaves `sampler.*`, `stop_sequences`,
  `memory_bank.*`, `story_cards.*`, `assistant.*`, `likeness.*`,
  `image.prompt_builder.*`.

## Não pode mexer

VM principal, flags do llama-server e do SD, `llm_client.py`, `jobs.py`,
`image_client.py`, `game_engine.py` (ganchos são pedidos via contrato C3).

## Tarefas em ordem

### K0 Frontend v0.9.0 (só se o fonte não vier do PC do usuário)
- Reconstruir em `frontend/src` as features que só existem no bundle da VM
  (`index-SorzepRf.js`): toggle `web_search` com `web_sources`; `image_style` no
  editor de cenário; Story Studio com protagonista, `card_count` e
  `web_search_mode`; `POST /assistant/card` e `POST /assistant/sync-plot`.
- Aceite: `npm run build` sem erro e paridade verificada com Playwright.

### K1 Saída limpa do narrador
- `clean_narration` remove `[World info]`, `[Memories]`, `[Summary]`,
  `[Player state]`, `[Plot]`, `[Author's note`, `[Continuity Anchor`,
  `[Active Lore`, falas `> You` e a frase incompleta quando `truncated`.
- Blocos voláteis do contexto delimitados, com instrução de nunca repeti-los, e
  nas `stop_sequences`.
- Invariante de cache: system estável, janela que só cresce, voláteis no fim
  (`test_prompt_prefix_stability`).
- Aceite: replay do seq 19 da Costazul sem vazamento; 0 frases cortadas em 50 turnos.

### K2 Meta-comandos
- `/card <nome>`, `/foto <nome>` (alias `/retrato`), `/cena`, e linguagem
  natural pt/en ("cria o card da Clara", "mostra a foto da Clara").
- Gera o card com `generate_single_card` usando o histórico (com web search se
  for pessoa real), enfileira o retrato, publica eventos, não grava prosa.
- Aceite: card em ≤ 15 s e retrato logo depois; testes dos padrões.

### K3 Chronicle confiável
- `role=chronicle`, reparo de JSON e 1 retry com prompt curto.
- Detecção determinística de nome novo ("— Sou Clara —") garante a tentativa.
- Card novo passa por `enrich_card` e sempre ganha retrato
  (`likeness.auto_portrait_new_npcs=true`), sem depender de `has_visual`.
- `present`/`left` gravados em `adventures.scene_state` (migration 010).
- Aceite: replay da Costazul cria a Clara em ≤ 30 s depois do seq 6; nenhum
  card para o protagonista.

### K4 Memória e backfill
- Summarizer em `role=summary`, adiado com breaker aberto.
- `tools/backfill_chronicle.py` para Costazul, "Invincible Universe" e "Faerûn: Caio".
- Aceite: Costazul com Clara (card e retrato), ≥ 3 memórias e summary não vazio.

### K5 See com conteúdo correto (planejador C4)
- Elenco de `scene_state.present` (máx. 3); quem saiu não entra.
- Presente sem card ganha card rápido antes da pintura; sem ele, visual da prosa
  traduzido, nunca negado.
- Fim do banimento cruzado do `contradicting_looks`; negative só com qualidade +
  "extra people".
- Prompt 100% em inglês; quem tem retrato vai primeiro (limite de 77 tokens do CLIP).
- Aceite: fixtures da Costazul (Clara de vestido vermelho; sem Alok e Villar
  depois que saem; zero português no prompt e no negative).

### K6 Likeness de pessoa real (C8)
- Detecção (cenário, assistente, chronicle) com verificação na Wikipedia +
  toggle manual.
- Referência: imagem principal da Wikipedia (REST `page/summary`) em
  `data/references/` com URL e licença; upload manual tem prioridade.
- Descritor: texto da Wikipedia + visão do llama-server descrevendo a foto.
- Prompt: nome real + descritor no início, mais `face_refs`.
- **Guardrail obrigatório**: pessoa real força `allow_explicit=false`; cena
  explícita com pessoa real tira a pessoa ou recusa; menores nunca.
- Aceite: ArcFace ≥ 0,45 em 5 pessoas de teste; testes do guardrail verdes.

### K7 Qualidade do pt-BR
- A/B do sampler com `tools/eval_conversation.py`: DRY 0,8 → 0,4; frequency
  0,1 → 0; presence 0,15 → 0,1; `max_tokens` 300–380 com corte limpo.
- Aceite: queda medida de erros de grafia por 1000 palavras.

### K8 Frontend fluido
- SSE do C2 (polling vira fallback com backoff); input liberado logo após
  `done`; ação nova aborta choices; See com placeholder.
- Drawer de NPCs: badge "pessoa real", referência (ver/trocar/remover), botão
  "resolver likeness". Toasts dos meta-comandos. Não reenviar `api_key` mascarada.

### K9 e2e com Playwright contra o staging
1. Ação, `done` e nova ação imediata sem 409.
2. Choices em pt-BR em ≤ 10 s.
3. "Clara" em diálogo → card e retrato em ≤ 50 s.
4. `/card Clara` e "cria o card da Clara".
5. See não bloqueia o input e só mostra quem está presente.
6. Pessoa real mostra a referência; cena explícita com pessoa real é recusada.

## Regras

- Branch por tarefa a partir da branch da Fase 0; PR pequeno com `pytest` verde.
- Proibido: force-push; commitar `.env`, DB, `data/images`, `data/references`,
  chaves ou senhas; desligar o guardrail de pessoa real; gerar nudez ou sexo com
  pessoa real ou menor de idade.
- Atualize `tasks.md` no mesmo PR.
