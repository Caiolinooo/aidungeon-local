# NPCs vivos — geração/regeneração por IA, life-sim e fotos de personagem

## Context

O AI Dungeon Local (FastAPI `backend/app/` + React/Vite `frontend/src/`) hoje gera cenário, cards e retratos, mas cada aspecto é gerado isoladamente: não existe "gerar tudo", não existe regeneração por campo, a ficha do NPC tem 5 campos (`appearance`, `body`, `clothing`, `personality`, `notes`) + `stats` + `status`, e **humor, lifestyle, sentiments, speech, goals, background, habits e likes não existem em nenhum lugar do código** (grep em `backend/app` não acha `humor|sentiment|lifestyle|habits|goals`). O backend já tem `routes/likeness.py` com upload de referência, mas **nenhuma tela do frontend o expõe** (`frontend/src/api.js` não tem função de upload; `NpcsTab.jsx` não tem `<input type="file">`), e o upload marca o NPC como "pessoa real", o que dispara o guardrail `real_person_explicit` que bloqueia cenas explícitas com o personagem.

Objetivo deste plano: (1) um caminho **"gerar tudo"** que produz projeto + personagens completos, com **regeneração** por campo e por NPC; (2) uma ficha de personagem **viva**, em que **toda** a informação (humor, sentiments, lifestyle, humor/lifestyle/…, memória, rotina, humor) **de fato chega** ao narrador, às escolhas, ao TTS e às imagens; (3) um **life-sim**: cada NPC tem mente, memórias, reflexão, rotina, humor e sentiments persistentes; (4) **fotos de personagem**: apagar o retrato, enviar uma foto local como referência para todas as gerações seguintes, e gerar um **novo** retrato a partir da foto enviada; (5) **remover o guardrail de "pessoa real"** que impede o usuário de usar a própria foto no personagem (mantendo o bloqueio de menores).

## Approach

Ordem de execução: Fases 1→2→3→4→5→6. Cada fase é um commit-verificável e passa `pytest` no fim.

---

### Fase 1 — Ficha viva: 8 campos novos, canônico único e persistência

A informação da ficha chega ao narrador **exclusivamente** por `npc_assets.card_canon_text` — tanto `context_builder._render_card` (`backend/app/context_builder.py:321`) quanto `backend/memory/triggers.render_card_text` chamam essa função. Portanto **estender `card_canon_text` é o único ponto de alavanca** para os campos novos (verificado em `backend/memory/triggers.py:render_card_text` e `backend/app/context_builder.py:321`).

**1.1 — `backend/app/npc_assets.py`: allow-list de persistência.**
`_normalize_sheet(data, previous=None)` (`backend/app/npc_assets.py:74`) hoje mantém apenas as chaves string ("appearance","body","clothing","personality","notes","status") + `stats` coercidos, e reaplica de `previous`: appearance/body (salvo `look_changed`), status, notes, stats, e preserva ("real_person","real_name","wiki_title","likeness","minor","age") + `bonds`. Chaves desconhecidas são **descartadas** aqui.
- Adicionar as 8 chaves novas ao conjunto de strings mantidas: `humor`, `speech`, `goals`, `background`, `lifestyle`, `habits`, `likes`, `sentiments`.
- Adicionar `"life"` e `"reference"` (novos sub-objetos da sheet) à lista de chaves preservadas de `previous`.
- Novas constantes no módulo:
  ```python
  SHEET_TEXT_FIELDS = ("appearance","body","clothing","personality","humor","speech",
                        "goals","background","lifestyle","habits","likes","sentiments","notes")
  LIFESIM_FIELDS = ("humor","speech","goals","background","lifestyle","habits","likes","sentiments")
  SHEET_FIELD_LIMITS = {"sentiments": 240}   # demais: 500
  ```
- Cada valor novo é truncado no limite da sua chave (500, exceto `sentiments` = 240) e só é gravado se for string não-vazia após `strip()`.

**1.2 — `backend/app/npc_assets.py`: prompt do LLM.**
`_ENRICH_SYSTEM` (`backend/app/npc_assets.py:29`) exige STRICT JSON com as chaves atuais. Acrescentar as 8 chaves, cada uma com definição de uma linha, e exigir **todas presentes** (string vazia quando desconhecido):
- `humor` — como brinca, o que a diverte, o que acha absurdo.
- `speech` — como fala: vocabulário, ritmo, vícios verbais, apelidos, bordões.
- `goals` — o que quer agora e o que teme perder.
- `background` — 2-3 frases de história que explicam a pessoa.
- `lifestyle` — rotina diária: onde acorda, trabalha, come, dorme, com quem mora.
- `habits` — tiques repetíveis: fuma, remexe, conta coisas, assobia.
- `likes` — gosta e não gosta, concreto.
- `sentiments` — como se sente agora com quem está em cena e com o jogador (começa pelo sentimento em relação ao jogador).
Manter a exigência de `status` ∈ {alive,dead,missing,injured} e `stats` 1-20.

**1.3 — `backend/app/npc_assets.py`: `card_canon_text(card)` (`backend/app/npc_assets.py:466`).**
Nova ordem exata de saída (cada linha **só** quando o valor for não-vazio; o bloco sem sheet continua sendo só `title: entry`):
```
Status: <status>
Mood: <sheet.life.mood>
Doing: <sheet.life.activity>
Appearance: ...
Body: ...
Clothing: ...
Personality: ...
Humor: ...
Speech: ...
Goals: ...
Background: ...
Lifestyle: ...
Habits: ...
Likes: ...
Sentiments: ...
Stats: str X, dex Y, con Z, int W, wis V, cha U
<title>: <entry>
Notes: ...
```
`Mood:` e `Doing:` vêm de `sheet["life"]` (chave `mood` / `activity`); se a sheet não tiver `life`, as duas linhas são omitidas. `Stats` mantém o formato atual (`str X, dex Y, ...`).

**1.4 — `backend/app/context_builder.py`: bloco de jogadores.**
`_players_block` (`backend/app/context_builder.py:366`) renderiza só `username, title, sheet.appearance[:120], sheet.status, sheet.stats`. Adicionar `personality[:120]` e `sentiments[:120]` ao mesmo formato — hoje a personalidade do jogador nunca chega ao narrador.

**1.5 — `backend/app/choices_service.py`: as escolhas conhecem os personagens.**
- `messages_for(actions, count, facts="")` → `messages_for(actions, count, facts="", cast="")`.
- `start(adventure_id, *, transport=None)` (`backend/app/choices_service.py:135`) já carrega `cards`; calcular o bloco `cast` com `npc_bonds.characters_in_text(cards, texto_recente)` (reaproveitado, sem reimplementar): para cada personagem presente, uma linha `Name — personality; humor; speech; sentiments; mood`, cada parte limitada a 120 chars, máximo 4 personagens, total ≤ 250 tokens.
- `_run` (`backend/app/choices_service.py:161`) passa `cast` para `messages_for`; o `SYSTEM` (`backend/app/choices_service.py:33`) recebe a instrução: "Use the character block to make each option specific to who is present."

**1.6 — `backend/app/npc_bonds.py`: o humor simulado vira a voz.**
`emotion_for_card(card, text, bonds)` (`backend/app/npc_bonds.py:212`) hoje deriva a emoção do texto (±180 chars em torno do nome) e cai para `emotion_from_bonds`. Nova ordem: se `sheet["life"]["mood"]` for não-vazio, usá-lo (mapeado por `voice_emotion.emotion_from_text`); senão o comportamento atual. `apply_turn` (`backend/app/npc_bonds.py:243`) **não muda** — a mente do NPC sobrescreve os bonds depois.

**1.7 — Frontend `frontend/src/components/drawer/NpcsTab.jsx`.**
- `SHEET_FIELDS` (`NpcsTab.jsx:80`) passa a ser `['appearance','body','clothing','personality','humor','speech','goals','background','lifestyle','habits','likes','sentiments','notes']`.
- Novo mapa `FIELD_LABELS = { appearance:'Aparência', body:'Corpo', clothing:'Roupa', personality:'Personalidade', humor:'Humor', speech:'Fala', goals:'Objetivos', background:'História', lifestyle:'Rotina', habits:'Manias', likes:'Gosta / não gosta', sentiments:'Sentimentos', notes:'Notas' }`.
- `SheetPanel` (`NpcsTab.jsx:105`): cada campo da lista ganha rótulo legível + botão `↻` **somente** para `personality`, `humor`, `speech`, `goals`, `background`, `lifestyle`, `habits`, `likes`, `sentiments` (chama `onRegenerateField(field)`).

**Fase 1 — verificação:** `backend/.venv/bin/python -m pytest tests/test_context_builder.py tests/test_npc_bonds.py -q` a partir da raiz do repo.

---

### Fase 2 — Regeneração por IA (campo, NPC, projeto)

**2.1 — `backend/app/npc_assets.py`: geração isolada de ficha.**
Nova função pública (não toca no banco):
```python
def build_sheet(llm_cfg, *, title: str, entry: str = "", sheet: dict | None = None,
                transport=None, role: str = "assistant") -> dict:
    """Gera a ficha completa de um NPC por LLM. Devolve o dict normalizado."""
```
Reaproveita `_ENRICH_SYSTEM` + `parse_sheet` (`npc_assets.py:63`) + `_normalize_sheet(data, previous=sheet or {})`; `max_tokens=600`, `temperature=0.4` (mesmos valores de `enrich_card`).
Refatorar `enrich_card(conn, adv, card, llm_cfg, transport, *, role="assistant")` (`backend/app/npc_assets.py:119`) para chamar `build_sheet(...)` e depois persistir como hoje (`db.update_card` com `sheet` + `portrait_prompt`) — **comportamento observável idêntico**, só o prompt passa a ser único.

**2.2 — Nova rota `backend/app/routes/cards.py`:**
```
POST /api/adventures/{adventure_id}/cards/{card_id}/regenerate
body: {"scope": "all" | "field", "field": "<nome>", "overwrite": false}
```
- `scope="field"`: `field` precisa estar em
  ```python
  REGENERABLE_FIELDS = ("personality","humor","speech","goals","background",
                        "lifestyle","habits","likes","sentiments","portrait_prompt")
  ```
  caso contrário → `GameError(422, "invalid_field", "Campo não regenerável.")`. O LLM regenera **só** aquele valor, com o contexto do card (título, entry e o resto da sheet). O valor novo **sempre substitui** o antigo; se o LLM devolver vazio, o valor antigo é mantido.
- `scope="all"`: regenera os 9 campos de texto (8 novos + `personality`). Semântica de merge por padrão (`overwrite=false`): só campos **vazios** são preenchidos. Com `overwrite=true`: todos os 9 são substituídos.
- `portrait_prompt` **não** entra em `scope="all"` (só via `scope="field"`), para não invalidar um retrato já gerado.
- `entry`, `appearance`, `body`, `clothing`, `status`, `stats` e `notes` **nunca** são tocados por esta rota — são texto canônico que o usuário pode ter editado à mão.
- Resposta 200: `{"card": <card dict completo>, "regenerated": ["humor", ...], "skipped": ["goals", ...]}`.
- Erros: 404 card inexistente; 422 `invalid_field`; 503 `generation_failed` com a mensagem `"LLM returned an unparseable response; try again"` (mesma de `backend/app/assistant.py` `_generation_failed`).

**2.3 — `frontend/src/api.js`:**
```js
regenerateCard: (advId, cardId, body) =>
  request(`/adventures/${advId}/cards/${cardId}/regenerate`, { method: 'POST', body }),
```

**2.4 — `frontend/src/components/drawer/NpcsTab.jsx`:**
- Botão `🎲 Regenerate all` na linha do NPC → `window.confirm('Regenerate all character fields? This overwrites the current values.')` → `api.regenerateCard(adventure.id, card.id, { scope: 'all', overwrite: true })` → substitui o card local.
- Botão `↻` por campo → `api.regenerateCard(adventure.id, card.id, { scope: 'field', field })`.
- O botão `✨ Enrich` existente **mantém o papel de "preencher o que falta"** (merge) — não muda de comportamento.
- `↻` no campo `portrait_prompt` do formulário principal (o `portrait_prompt` já é editável em `NpcRow`).

**Fase 2 — verificação:** `backend/.venv/bin/python -m pytest tests/test_cards_bulk.py -q` + novo `tests/test_card_regenerate.py`.

---

### Fase 3 — Fotos do personagem (apagar, enviar, gerar a partir da foto) + remoção do guardrail de pessoa real

**3.1 — `backend/app/likeness.py`: referência visual separada da "pessoa real".**
- Nova constante `VISUAL_REFERENCE_DENOISE = 0.3`.
- Nova função `def visual_reference_bytes(card) -> bytes | None` — lê `sheet["reference"]["image_path"]` (arquivo sob `references_dir()`), reusando `absolute_reference` e `reference_bytes` como padrão de leitura/escrita.
- `save_upload(conn, card, content, *, filename="", mode="reference")` (hoje em `backend/app/likeness.py:646`):
  - `mode` ∈ {"reference","real_person"}; outro valor → `GameError(400, "invalid_payload", "mode deve ser 'reference' ou 'real_person'.")`.
  - Grava o arquivo com `_write_reference(card_id, content, ext)` → `data/references/{safe_card_id}{ext}`.
  - Grava `sheet["reference"] = {"image_path": "<caminho relativo>", "source": "upload", "mode": mode, "created_at": <utc iso>}`.
  - `mode == "real_person"`: **além** de `sheet["reference"]`, seta `sheet["real_person"] = True` e `sheet["real_name"]` (se vazio) e espelha o caminho em `sheet["likeness"]["reference_image_path"]` (para o guardrail de reais continuar funcionando).
  - `mode == "reference"` (padrão): **não** mexe em `real_person` — um NPC fictício com foto não é pessoa real.
  - **Limpa `portrait_anchor_id`** do card (a próxima geração parte da foto enviada) mas **mantém o retrato atual** até o usuário pedir um novo.
  - Erros mantidos: 400 `"Envie uma imagem de até 8 MB."`, 400 `"A referência precisa ser JPEG, PNG ou WebP."`, 422 `CODE_MINOR` para menores.
- `portrait_init(card, anchor, *, anchor_denoise)` — nova ordem de resolução:
  1. real + menor → `GameError(422, CODE_MINOR, "Não gero retrato de menor.")` (inalterado);
  2. referência enviada (`visual_reference_bytes`) → `(bytes, REFERENCE_DENOISE if is_real_person(card) else VISUAL_REFERENCE_DENOISE)`;
  3. referência de pessoa real (`reference_bytes`) → `(bytes, REFERENCE_DENOISE)`;
  4. âncora → `(anchor, anchor_denoise)`;
  5. senão `(None, None)`.
- `clear_reference(conn, card)` — apaga o arquivo, zera `sheet["reference"]` **e** `sheet["likeness"]["reference_image_path"]`/`source_url`/`verified_by_user`.

**3.2 — `backend/app/routes/likeness.py`:**
- `PUT /api/adventures/{aid}/cards/{cid}/reference` passa a aceitar o campo de formulário `mode: str = Form("reference")` junto de `file: UploadFile = File(...)` e repassa `mode` a `save_upload`. Resposta 200: o card atualizado.
- `DELETE /api/adventures/{aid}/cards/{cid}/reference` — mesma rota, novo comportamento (limpa `sheet["reference"]`).

**3.3 — Apagar a foto do personagem.**
- Nova rota em `backend/app/routes/cards.py`:
  ```
  DELETE /api/adventures/{adventure_id}/cards/{card_id}/portrait
  ```
  Apaga a imagem do retrato **e** a da âncora (quando forem linhas diferentes de `images`), incluindo os PNGs em disco; zera `portrait_image_id` e `portrait_anchor_id`. Responde **204 sempre** (idempotente); 404 se o card não existir.
- Novos helpers em `backend/app/db.py`:
  ```python
  def delete_image(conn, image_id) -> bool          # apaga a linha e o arquivo
  def clear_card_portrait(conn, card_id) -> bool    # zera portrait_image_id/portrait_anchor_id
  ```
  `delete_image` resolve `file_path` contra `REPO_ROOT` e **só** desliga o arquivo se o caminho resolvido continuar dentro de `REPO_ROOT`; caso contrário apaga só a linha do banco.

**3.4 — "Gerar novo retrato a partir da foto enviada".**
- `POST /api/adventures/{adventure_id}/cards/{card_id}/portrait` (rota existente em `backend/app/routes/jobs.py:38`) passa a aceitar um **corpo JSON opcional**:
  ```json
  {"source": "anchor" | "reference"}
  ```
  `source="reference"` sem `sheet["reference"]` → `GameError(422, "reference_required", "Este NPC não tem foto de referência.")`. O job recebe `payload["force_reference"] = True`.
- `backend/app/jobs.py` `_execute_portrait` (jobs.py:338) passa `force_reference` a `likeness.portrait_init`; com `force_reference=True` a âncora é **ignorada** e só a foto enviada alimenta o init image.
  Nova assinatura: `likeness.portrait_init(card, anchor, *, anchor_denoise, force_reference=False)`.

**3.5 — Remover o guardrail de "pessoa real" (pedido explícito do usuário).**
- `backend/app/likeness.py`:
  - **Remover** `CODE_REAL = "real_person_explicit"` e `MSG_REAL = "Não gero nudez nem sexo com pessoa real."`.
  - `decide_frame(cast, *, explicit)` (hoje `likeness.py:137-149`) passa a:
    ```python
    minors = [c for c in cast if is_minor(c)]
    if explicit and minors:
        return FrameDecision([], False, CODE_MINOR, MSG_MINOR, list(minors))
    return FrameDecision(list(cast), bool(explicit), None, "", [])
    ```
  - **Remover** o campo `dropped` de `FrameDecision` e a função `likeness.omit_names`, junto com **todos** os call sites: `backend/app/scene.py:164`, `backend/app/routes/images.py:356`, `backend/app/routes/images.py:450`, `backend/app/routes/images.py:455`. (grep `omit_names` para confirmar a lista completa — não há outros.)
  - Atualizar o docstring do módulo: o bloqueio passa a ser **apenas para menores**.
  - `is_minor` / `CODE_MINOR` / `MSG_MINOR` **permanecem** — é um piso de segurança para geração de retrato de card marcado como menor, independente do LLM.
- `backend/app/scene.py:158` e `backend/app/routes/images.py:348` e `:530` mantêm o `raise GameError(422, decision.refusal, decision.message)` — agora ele só pode ser a recusa de menor.

**3.6 — Frontend `NpcsTab.jsx` (área do retrato):**
- `Upload photo` → `<input type="file" accept="image/jpeg,image/png,image/webp">` + select de modo: `Reference (fictional NPC)` (default) | `Real person`.
- `Remove photo` → apaga a referência (DELETE reference).
- `Delete portrait` → `window.confirm` → DELETE portrait.
- `New image from photo` → POST portrait com `{source:'reference'}`; botão desabilitado quando não há `card.sheet.reference`.
- `frontend/src/api.js`:
  ```js
  uploadReference: (advId, cardId, file, mode) => { /* FormData: file + mode */ },
  deleteReference: (advId, cardId) => request(`/adventures/${advId}/cards/${cardId}/reference`, { method: 'DELETE' }),
  deletePortrait:  (advId, cardId) => request(`/adventures/${advId}/cards/${cardId}/portrait`,  { method: 'DELETE' }),
  ```
  O `request()` do `api.js` já detecta `FormData` e não seta `Content-Type` — reusar, não reescrever.

**Fase 3 — verificação:** `backend/.venv/bin/python -m pytest tests/test_likeness.py tests/test_v9_jobs.py -q`.

---

### Fase 4 — Life-sim: mente, memórias, rotina, humor, sentiments

**4.1 — Migração `backend/migrations/024_npc_life.sql`** (a última existente é `023_multiplayer_live.sql`):
```sql
CREATE TABLE IF NOT EXISTS npc_memories (
  id TEXT PRIMARY KEY,
  adventure_id TEXT NOT NULL,
  card_id TEXT NOT NULL,
  kind TEXT NOT NULL DEFAULT 'event',
  text TEXT NOT NULL,
  mood TEXT DEFAULT '',
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_npc_memories_card ON npc_memories(adventure_id, card_id, created_at);
ALTER TABLE adventures ADD COLUMN life_day INTEGER NOT NULL DEFAULT 0;
ALTER TABLE adventures ADD COLUMN life_tick_seq INTEGER NOT NULL DEFAULT 0;
```
- **Os mesmos objetos vão também para `backend/schema.sql`**: `db.init_db` (`backend/app/db.py:125`) aplica `schema.sql` num banco novo e marca todas as migrations como aplicadas, então banco novo e banco antigo precisam terminar com o mesmo schema.
- `db._apply_migration` (`backend/app/db.py:80`) tolera `ALTER TABLE ... ADD COLUMN` já existente — a migração é segura para reexecução.
- Helpers em `backend/app/db.py`:
  ```python
  def insert_npc_memory(conn, *, adventure_id, card_id, kind, text, mood="", memory_id=None) -> dict
  def list_npc_memories(conn, adventure_id, card_id=None, limit=50) -> list[dict]
  def update_adventure_life(conn, adventure_id, *, day=None, tick_seq=None) -> bool
  ```
- `DELETE /cards/{card_id}` (backend/app/routes/cards.py) passa a apagar também as memórias do card.

**4.2 — Novo módulo `backend/app/npc_life.py`:**
```python
MEMORY_KINDS = ("event", "reflection")

def life_state(sheet) -> dict            # {"mood","activity","day","last_tick"}
def write_life(sheet, state) -> dict     # escreve sheet["life"]
def add_memory(conn, adventure_id, card_id, kind, text, mood="") -> dict
def list_memories(conn, adventure_id, card_id, limit=20) -> list[dict]
def recent_for_cards(conn, adventure_id, card_ids, per_card=3) -> list[dict]

def mind_prompt(cards, memories, *, reason) -> list[dict]   # messages pro LLM
def run_mind(adventure_id, transport=None) -> dict            # 1 chamada de LLM + persistência
def tick_day(adventure_id, transport=None) -> dict            # o tick do dia
```
- `run_mind`: uma chamada `llm_client.chat_completion(..., role="chronicle", temperature=0.4, max_tokens=700)`; o JSON esperado (parseado com `npc_assets._extract_json`):
  ```json
  {"characters": [
    {"name": "<nome canônico>",
     "memory": "<o que aconteceu com ele, 1-2 frases, a partir da passagem>",
     "mood": "<uma palavra ou frase curta>",
     "activity": "<o que está fazendo agora>",
     "sentiments": "<sentimentos com quem está em cena, ≤ 240 chars>",
     "bonds": {"relationship": 50, "trust": 50, "fear": 10, "desire": 20, "boldness": 40}}]}
  ```
  O LLM vê, por personagem: `npc_assets.card_canon_text(card)`, os bonds atuais e as 3 memórias mais recentes.
  Persistência por personagem: `add_memory(kind="event")`; `sheet["life"] = {"mood","activity","day","last_tick"}`; `sheet["sentiments"] = <texto do LLM>`; `sheet["bonds"]` recebe **apenas as chaves que o LLM devolveu**, com valores absolutos clipados em 0-100 (as ausentes mantêm o valor atual). O sheet é gravado com `db.update_card(conn, adventure_id, card_id, {"sheet": json.dumps(sheet)})` — mesmo caminho do `npc_chronicle.apply_character`, que **preserva** chaves desconhecidas da sheet.
  `run_mind` é best-effort: erro de LLM/JSON é logado e engolido (mesmo contrato do chronicle).
- `tick_day`: avança `adventures.life_day` (+1) e grava `life_tick_seq` = maior `seq` de `db.list_actions`. Para cada card de personagem: uma chamada de LLM (`role="chronicle"`, `temperature=0.6`, `max_tokens=500`) que devolve `{"event": "...", "mood": "...", "activity": "...", "reflection": "<≤400 chars, só se houver ≥ 6 memórias>"}`. O prompt **exige**: "invent only off-screen daily events; never change who the character is, their status, their appearance or their relationships". A reflection é gravada como `kind="reflection"`; o evento como `kind="event"`.

**4.3 — Gatilhos.**
- `backend/app/db.py`: `JOB_TYPES` (`db.py:1103`) → adicionar `"npc_day_tick"` (senão `db.insert_job` levanta `ValueError`).
- `backend/app/jobs.py`: `PRIORITY_LIFE_TICK = 4` (depois de `PRIORITY_BULK_PORTRAIT = 3`); `enqueue_day_tick(conn, adventure_id, *, reason="auto")`; `_execute_day_tick(job, transport)` → `npc_life.tick_day(adventure_id, transport)`; registrar em `_EXECUTORS` (jobs.py:506).
- `backend/app/game_engine.py` — dentro de `_spawn_post_turn._bg` (`game_engine.py:200-216`), logo após `npc_chronicle.update_from_turn(adventure_id, transport)` (e dentro do mesmo `try/except Exception: pass`):
  ```python
  npc_life.run_mind(adventure_id, transport)
  ```
  e, em seguida, o auto-tick: se `settings_store.load_file_settings().get("life", {}).get("auto_tick", True)` e `seq_atual - adventures.life_tick_seq >= life.tick_every_turns`, enfileirar `jobs.enqueue_day_tick(conn, adventure_id, reason="auto")`.
- Nova rota `backend/app/routes/adventures.py`:
  ```
  POST /api/adventures/{adventure_id}/life/tick   → 202 {"job": {...}}
  ```
  enfileira o job (não roda LLM no request).

**4.4 — Contexto: bloco `[NPC minds]`.**
- `backend/app/context_builder.py`: nova função `_minds_block(conn, adventure_id, cards, budget_tokens)` → uma linha por personagem presente (o mesmo conjunto devolvido por `_select_cards`):
  `Name — mood; activity` + até 3 memórias recentes (a **reflection** primeiro, depois os events mais novos), cada uma ≤ 240 chars. Budget: `context.budgets.npc_minds` (default 300).
- Inserir o bloco em `build_context` (`context_builder.py:487`) **logo depois** do bloco `[World info]`, dentro do `<lore>` volátil.
- `FIXED_BUDGET_KEYS` (`context_builder.py:220`) → adicionar `"npc_minds"`.
- `CONTEXT_STOPS` (`context_builder.py:164`) → adicionar `"[NPC minds]"`.

**4.5 — Configuração `config/settings.json`** (novo bloco `life`, lido por `settings_store.load_file_settings()`, sem override no banco e sem rota de settings — só arquivo):
```json
"life": {"enabled": true, "auto_tick": true, "tick_every_turns": 5,
         "memories_per_card": 20, "reflection_after": 6}
```
- `config/settings.json` → `context.budgets` ganha `"npc_minds": 300`.

**4.6 — Frontend:**
- `NpcsTab.jsx`: bloco "State" por NPC — `mood`, `activity`, `day`, e as 3 últimas memórias (`api.listNpcMemories`); botão `⏩ Simulate a day` no cabeçalho do drawer (`api.simulateDay`).
- `frontend/src/api.js`:
  ```js
  listNpcMemories: (advId, cardId) => request(`/adventures/${advId}/cards/${cardId}/memories`),
  simulateDay:     (advId) => request(`/adventures/${advId}/life/tick`, { method: 'POST' }),
  ```
- Nova rota de leitura `GET /api/adventures/{aid}/cards/{cid}/memories` → `{"memories": [...]}` (limite 20, mais recentes primeiro).

**Fase 4 — verificação:** `backend/.venv/bin/python -m pytest tests/test_npc_life.py tests/test_context_builder.py -q`.

---

### Fase 5 — "Gerar tudo": o orquestrador

**5.1 — Nova rota `backend/app/routes/assistant.py`:**
```
POST /api/assistant/generate
body: AssistantGenerateRequest (novo modelo em backend/app/schemas.py)
  brief: str (min 3)
  current: dict | None = None
  language: str = "en"
  genre: str = ""
  tone: str = ""
  pov: Literal["second","third","first"] | None = None
  protagonist_name: str = ""
  protagonist_role: str = ""
  protagonist_appearance: str = ""
  image_style: str = ""
  card_count: int = 5           # 3..8
  web_search_mode: Literal["auto","always","off"] = "auto"
  deep_characters: bool = True
```
- Comportamento: chama `generate_scenario_draft(...)` (`backend/app/assistant.py:452`) com os mesmos argumentos de `/assistant/scenario`; quando `deep_characters` é true, para **cada** card de personagem do draft (`draft["story_cards"]`, `type == "character"`) chama `npc_assets.build_sheet(llm_cfg, title=card["title"], entry=card.get("entry",""), transport=transport)` e mescla o resultado em `card["sheet"]` (o que o draft já tiver prevalece; `build_sheet` preenche o resto).
- `current` (o rascunho atual) entra no prompt como "the current draft to regenerate" — sem ele, o modelo cria do zero a partir do `brief`.
- Resposta: o mesmo dicionário de draft de `/assistant/scenario`.
- Erros: 503 `generation_failed` com a mensagem `"LLM returned an unparseable response; try again"` (mesma de `assistant._generation_failed`).

**5.2 — `backend/app/assistant.py`: prompts de card com ficha completa.**
`_SCENARIO_SYSTEM` (assistant.py:40), `_FIELD_INSTRUCTIONS["story_cards"]` (assistant.py:106), `_CARDS_SYSTEM` (assistant.py:73) e `_SINGLE_CARD_SYSTEM` (assistant.py:109) passam a exigir, para cards de personagem, a sheet completa: `appearance, body, clothing, personality, humor, speech, goals, background, lifestyle, habits, likes, sentiments, status, stats, notes`. `_normalize_cards` (assistant.py:188) já preserva o `sheet` verbatim para personagens — nenhuma mudança lá.

**5.3 — Frontend:**
- `frontend/src/components/GenerateAdventureModal.jsx`: checkbox `Deep characters (full sheets)` — **ligado por padrão** → quando ligado, o passo 1 chama `api.assistantGenerate` no lugar de `api.assistantScenario`.
- `frontend/src/pages/ScenarioEditor.jsx`: botão `🎲 Regenerate everything` → `api.assistantGenerate({ brief: <descrição atual>, current: <rascunho atual>, ... })`.
- `frontend/src/api.js`:
  ```js
  assistantGenerate: (body) => request('/assistant/generate', { method: 'POST', body }),
  ```

**Fase 5 — verificação:** `backend/.venv/bin/python -m pytest tests/test_v9_assistant.py -q`.

---

### Fase 6 — Documentação da API

`docs/openapi.yaml` — documentar os endpoints novos e as sheet fields novas:
- `POST /api/assistant/generate`
- `POST /api/adventures/{adventureId}/cards/{cardId}/regenerate`
- `PUT /api/adventures/{adventureId}/cards/{cardId}/reference` (campo de formulário `mode`) e `DELETE` da mesma rota
- `DELETE /api/adventures/{adventureId}/cards/{cardId}/portrait`
- `POST /api/adventures/{adventureId}/cards/{cardId}/portrait` (corpo `{"source": "anchor"|"reference"}`)
- `GET /api/adventures/{adventureId}/cards/{cardId}/memories`
- `POST /api/adventures/{adventureId}/life/tick`
- Campos novos no schema do Card: `humor`, `speech`, `goals`, `background`, `lifestyle`, `habits`, `likes`, `sentiments`, `life`, `reference`.

---

## Critical files & anchors

1. `backend/app/npc_assets.py` — `_normalize_sheet` (:74), `_ENRICH_SYSTEM` (:29), `card_canon_text` (:466), `enrich_card` (:119), `parse_sheet` (:63): a allow-list da sheet, o prompt, o canônico que chega ao narrador e o único ponto de alavanca para os campos novos.
2. `backend/app/likeness.py` — `decide_frame` (:137), `save_upload` (:646), `portrait_init`, `clear_reference`: o guardrail a remover e o upload a estender (modo `reference` vs `real_person`).
3. `backend/app/game_engine.py` — `_spawn_post_turn._bg` (:200-216): o gancho pós-turno onde entram a mente do NPC e o auto-tick do dia.
4. `backend/app/context_builder.py` — `build_context` (:487), `_fit_budget` (:233), `CONTEXT_STOPS` (:164), `FIXED_BUDGET_KEYS` (:220), `_players_block` (:366): onde os campos novos e o bloco `[NPC minds]` entram no prompt.
5. `frontend/src/components/drawer/NpcsTab.jsx` — `SHEET_FIELDS` (:80), `SheetPanel` (:105), `NpcRow` (:188), `NpcsTab` (:355): toda a UI de personagem (ficha, foto, estado).

## Verification

Ambiente: repo `/mnt/DEV/Projeto/aidungeon-local`; Python em `backend/.venv/bin/python` (3.12.15, com `pytest`); frontend em `frontend/` (`npm run dev`, porta 5173, proxy `/api` → `http://127.0.0.1:8000`); backend em `backend/` (`uvicorn app.main:app --port 8000`).

1. **Suíte completa** (raiz do repo): `backend/.venv/bin/python -m pytest tests/ -q` → precisa passar inteira, incluindo `tests/test_likeness.py`, `tests/test_context_builder.py`, `tests/test_npc_bonds.py`, `tests/test_v9_jobs.py`, `tests/test_v9_assistant.py`, `tests/test_cards_bulk.py`, `tests/test_db_migrations.py` (a migração 024 tem que ser aplicada num DB antigo e num DB novo).
2. **Comportamento novo, prova observável** (novo `tests/test_npc_life.py`):
   - `card_canon_text` de um card com `humor`/`lifestyle`/`sentiments`/`life.mood` contém as linhas `Humor:`, `Lifestyle:`, `Sentiments:`, `Mood:` (entrada concreta → saída esperada, strings exatas).
   - `run_mind` com LLM mockeado: grava 1 linha em `npc_memories` com `kind="event"`, atualiza `sheet["life"]["mood"]` e `sheet["bonds"]["trust"]` para o valor devolvido pelo mock, e **preserva** `sheet["humor"]` (campo manual não é sobrescrito).
   - `tick_day` com LLM mockeado: `adventures.life_day` vai de 0 → 1 e, com ≥ 6 memórias pré-existentes, cria uma `kind="reflection"`.
   - `build_context` inclui o bloco `[NPC minds]` com a linha do personagem presente.
3. **Comportamento novo, prova observável** (novo `tests/test_card_regenerate.py`): `POST /cards/{cid}/regenerate` com `{"scope":"all","overwrite":false}` **não** sobrescreve um `humor` já preenchido e **preenche** um `lifestyle` vazio; com `{"scope":"field","field":"entry"}` → 422 `invalid_field`; com `field="humor"` → o valor do mock substitui o antigo.
4. **Fotos** (estender `tests/test_likeness.py`): upload com `mode="reference"` **não** deixa `sheet["real_person"]` True e grava `sheet["reference"]["image_path"]`; upload com `mode="real_person"` deixa `real_person` True; `portrait_init` com referência enviada devolve os bytes da foto; `decide_frame` com uma pessoa real em cena explícita **não** remove o personagem (o mock de `cast` com `real_person=True` continua no `FrameDecision.cast`) e com `minor=True` continua recusando.
5. **Smoke end-to-end (manual, com o app rodando)**: `cd frontend && npm run dev` + `backend/.venv/bin/uvicorn app.main:app --port 8000` (em `backend/`). No app: (a) criar aventura pelo modal "Generate with AI" com "Deep characters" ligado → os NPCs do drawer já vêm com `humor`/`speech`/`goals`/`lifestyle` preenchidos; (b) em um NPC, clicar `↻` em `humor` → o valor muda para o texto do mock/novo texto; (c) "Upload photo" com um JPEG local → o card mostra a referência e o botão "New image from photo" fica habilitado; (d) "New image from photo" → fila um job `character_portrait` (o job list mostra `pending`/`running` → `done`) e o retrato novo sai da foto; (e) "Delete portrait" → o retrato some da lista e o PNG some de `data/images/{adventure_id}/`; (f) "Simulate a day" → o bloco "State" mostra `day` incrementado e uma memória nova; (g) mandar um turno no chat → a narração respeita a ficha nova.
   - **Pré-requisito**: o provedor de imagem (`sdwebui` em `http://127.0.0.1:7861`, conforme `config/settings.json`) precisa estar rodando para (d) e (e); se não estiver, os passos (d)/(e) são verificados com o `httpx.MockTransport` do `tests/conftest.py` (item 4) e o smoke fica limitado a (a)(b)(c)(f)(g).
6. **Lint/format**: o repo não tem formatter/linter configurado — não inventar um. Só `pytest`.

## Assumptions & contingencies

- **A folha de estilo da UI segue o padrão existente** do `NpcsTab.jsx` (Tailwind com zinc/emerald). Se o implementador precisar de classes novas, copiar as já usadas no arquivo (`inputCls` em `NpcRow`, `inputCls` em `SheetPanel`).
- **Se o `sdwebui` local não estiver disponível** no ambiente de execução, a Fase 3 (parte de imagem) é verificada via mock, e a verificação manual 5(d)/(e) fica explicitamente pendente — não trocar o provider nem mexer em `config/settings.json` para contornar.
- **Se o LLM mockado devolver sheet sem alguma das 8 chaves** (modelo local nem sempre cumpre JSON strict), `_normalize_sheet` preenche com string vazia e `card_canon_text` **omite** a linha — comportamento correto (nada inventado, nada quebrado).
- **Se `npc_chronicle` e `npc_life` escreverem a sheet no mesmo turno**, o último a escrever ganha; o chronicle escreve `plot_essentials`/appearance/clothing/personality/status/notes/stats e o mind escreve `life`/`sentiments`/`bonds` — campos disjuntos, sem conflito. Se um LLM devolver `sentiments` no chronicle, o valor do chronicle é aceito (a sheet preserva chaves desconhecidas) e o próximo tick sobrescreve.
- **Se o banco em uso já tiver `npc_memories`** (rodada anterior do plano), a migração 024 usa `CREATE TABLE IF NOT EXISTS` e `db._apply_migration` tolera `ADD COLUMN` repetido — reaplicar é seguro.
- **Se o usuário quiser o tick do dia só manual**, basta `auto_tick: false` em `config/settings.json` — nenhuma mudança de código.
