# v0.9.0 — Modo Turbo, Parágrafos, AI Story Studio, Web Search, UX + VM deploy

## Context

Finish and ship v0.9.0 of aidungeon-local (FastAPI backend in `backend/`, React/Vite SPA in `frontend/`) and deploy it to `vm.groupabz.com`. A large part of the backend is already drafted (uncommitted) in the working tree and all 333 backend tests pass on it; the frontend is mostly untouched. End state: llama.cpp slot pinning with a cache-friendly prompt layout ("Modo Turbo"), background post-turn work, GPU-aware SDXL job queue, sentence-safe paragraphs and clean See prompts/captions, a parameterized AI Story Studio with field/card refinement and full scenario CRUD, opt-in web grounding (scenario creation, cards, turns), a self-updating service worker, all live on the VM.

## Findings (verified this session)

- Already drafted in the working tree (uncommitted, suite green): `id_slot`/`cache_prompt` in `backend/app/llm_client.py`; `is_any_generation_active()`, `_spawn_post_turn()`, lock release before SSE `done`, and turn-level web search in `backend/app/game_engine.py`; job-worker gate in `backend/app/jobs.py::worker_loop`; entity-signal skip + reduced budget in `backend/app/npc_chronicle.py::update_from_turn`; `update_scenario`/`delete_scenario` and extended `update_adventure`/`update_adventure_memory` in `backend/app/db.py`; PATCH/PUT/DELETE `/api/scenarios/{id}` in `backend/app/routes/scenarios.py`; Story Studio params + `instruction` + `/api/assistant/card` + `/api/assistant/sync-plot` + `/api/assistant/web-search` in `backend/app/assistant.py` + `backend/app/routes/assistant.py` + `backend/app/schemas.py`; new `backend/app/web_search.py`; migration `backend/migrations/009_scenario_image_style.sql` (auto-applied on boot by `db.init_db` via `PRAGMA user_version`); `image_style` in `backend/schema.sql`; `steps: 20` in `config/settings.json`; `frontend/src/prose.js` rewritten (no `wrapLongChunk`, 240-char cut gone); caption cleanup regexes in `frontend/src/components/FeedItem.jsx`; `> You` strip in `backend/app/routes/images.py::_build_final_prompt`.
- Defects/gaps found in the draft and current code, all fixed by this plan:
  1. `llm_client.chat_completion` hardcodes `id_slot: 1`; llama.cpp **defers a request with a non-existent `id_slot` forever** (hangs to the 180 s timeout). Any single-slot server (local dev, fresh installs) would break. Fix: auto-detect slot count via `GET /props` and only pin when ≥ 2 slots exist.
  2. `context_builder.build_context` puts `[World info]`, `[Memories]`, `[Summary]`, `[Player state]` **inside the system message** and drops one action per turn once the recent window is full — both force re-processing of the whole story window every turn even with a pinned slot. Fix: cache-friendly layout (user-approved).
  3. `see` actions are fed verbatim into the narrator context (`format_action_text` returns their text unchanged) — today that text is the full English SDXL prompt polluting PT stories; a trailing `see` also becomes the "last action". Fix: exclude `see` from context.
  4. `_build_final_prompt` still leads the SDXL prompt with the raw beat (PT text for PT stories). Fix: summarized English sentence leads (WS2 as specified).
  5. `jobs.process_pending` drains the whole queue in one call — the worker gate only applies between batches. Fix: per-job pause check.
  6. `npc_chronicle.spawn_after_turn` has zero callers (dead code). Delete.
  7. VM DB has `llm.context_size = 262144`, which exceeds the per-slot context with 2 slots → requests would fail on long adventures. Fix: app-side clamp.
- VM state (read-only SSH inspection): llama-server (custom turboq-mtp build 10281) runs as **root** via `/etc/systemd/system/llama-server.service` (unreadable by `caio`, `sudo` needs a password — user must run the unit edit); cmdline starts `... -t 8 --threads-batch 16 -np 1 ...`; build supports `--ctx-checkpoints`, `--cache-ram`, `/slots`, and `--kv-unified` only when `-np` is auto (so with explicit `-np 2`, per-slot `n_ctx = -c / 2`). App at `/home/caio/aidungeon` (not a git repo): `backend/` (venv at `backend/venv`), `config/settings.json` (semantically identical to local HEAD), `data/aidungeon.db`, `frontend/dist`; `aidungeon.service` runs uvicorn on port 2122 as `User=caio` with `Restart=always RestartSec=3` (so `pkill -f 'uvicorn app.main:app'` restarts it without sudo). VM DB overrides: `llm.context_size = 262144`, `image.sdwebui_steps = 20` (already 20 — no DB change needed). VM `scenarios` table lacks `image_style` (migration 009 applies on next boot).
- `backend/app/context_builder.py::estimate_tokens` is chars/4; Portuguese tokenizes denser, so the app context cap must sit ~20% under the per-slot `n_ctx`.
- `third_person` is metadata only — `context_builder` never reads it; POV takes effect solely via `ai_instructions` text. First/third-person need an explicit directive appended.
- Frontend hardcodes `262144` in `frontend/src/pages/Settings.jsx` (lines ~125, 149, 166, 380-418) and `frontend/src/components/drawer/ConfigTab.jsx` (lines ~151-158). `GET /api/settings/llm` (`_settings_response` in `backend/app/routes/settings.py`) does not expose the configured max.
- `PUT /api/settings/llm` already calls `llm_client.invalidate_discovery_cache()` (per llm_client docstring); conftest's autouse `_clean_llm_state` fixture also calls it — so clearing the new slot cache inside that one function covers tests and settings changes with no conftest edit.
- Existing See tests encode the old "raw beat leads" contract and must be rewritten (not re-pinned): `tests/test_v2.py::test_see_flow_creates_image_and_action`, `tests/test_npc_chronicle.py::test_see_paints_the_latest_beat_not_the_old_story`, `::test_see_keeps_the_face_and_puts_the_beat_before_the_style`, `::test_see_paints_the_kiss_not_a_portrait`. `tests/test_context_builder.py` lines ~50-100 assert `[World info]`/`[Memories]`/`[Summary]` order inside `messages[0]` and must be updated to the new layout.
- llama-server is shared with another multimodal project (`--mmproj` preserved, per `docs/vm_runbooks/phase1_vram_liberation.md`); the other project's unpinned requests can claim any idle slot — inherent, not addressed here.
- Web-research block literal produced by `web_search.research_context` is `[Web Research & Canon Facts]` (already drafted; UI/tests must use this exact string).
- Mock transports in tests return 404 for unknown paths (incl. `/props`) → pinning auto-disables in the whole existing suite; no test breakage from step 1.

## Approach

### Phase A — backend hardening (suite stays green; test updates land with the step that breaks them)

**A1. Slot pinning with auto-detection (`backend/app/llm_client.py`)**
- Add `_slot_cache: dict[str, int]` and `server_slot_count(cfg, transport=None) -> int`: `GET` `{base}/props` where `base = cfg["base_url"].rstrip("/")` with a trailing `/v1` stripped; use `_make_client(cfg, transport, min(timeout, PROBE_TIMEOUT))` and `_headers(cfg)` (the VM server requires the API key on all endpoints); parse `int(data.get("total_slots") or 0)`; on any exception/non-200/non-JSON cache and return `0`. Key the cache by stripped base_url. Extend `invalidate_discovery_cache()` to also clear `_slot_cache`.
- Add `slot_for(cfg, role: str, transport=None) -> int | None`: returns `0` for `role == "narrative"`, `1` for `role == "aux"`, `None` when `server_slot_count(...) < 2`.
- `build_chat_payload(...)`: add `transport=None` param; keep `"cache_prompt": True`; replace `"id_slot": int(sampler.get("id_slot", 0))` with conditional `"id_slot": slot_for(cfg, "narrative", transport)` — only include the key when not None. Only caller is `stream_chat_completion` (verified by grep); pass its `transport` through.
- `chat_completion(...)`: remove the drafted `id_slot: int = 1` parameter entirely (clean cutover); include `"id_slot": slot_for(cfg, "aux", transport)` only when not None; keep `"cache_prompt": True`.
- Remove `id_slot=1` kwargs at the two drafted callsites: `backend/app/assistant.py::_chat` (line ~240) and `backend/app/npc_chronicle.py::update_from_turn` (line ~429).
- New behavior contract: payload sent to llama.cpp contains `cache_prompt: true` always; `id_slot` present only on servers with ≥2 slots (narrative stream → 0, every `chat_completion` aux caller → 1: choices, npc_chronicle, summarizer, assistant, see prompt-builder, ui_profile, jobs, web_search — all go through `chat_completion`, verified by grep).

**A2. Cache-friendly context layout (`backend/app/context_builder.py::build_context`, lines 318-502)**
- At function top, drop see actions from narration: `actions = [a for a in actions if a.get("type") != "see"]`. This single filter fixes window, `recent_text` card/memory scan, and last-action selection (a trailing See no longer becomes the narrative "last action").
- System message (position 0) keeps ONLY: `BASE_SYSTEM_PROMPT` + `ai_instructions` (block 1) and `[Plot]` (block 2). These change only on explicit edits (chronicle writes plot once, only when empty — `apply_chronicle` line ~369).
- Move the volatile blocks — `[World info]` (cards), `[Memories]`, `[Summary]`, `[Player state]` — out of `system_parts` and into the **final user message**, inserted at the top of `final_parts`, before `[Author's note]` / Active Lore / Continuity Anchor / STYLE_CUE / CANON_CUE / last action. Keep budgets, dynamic scaling, `meta["cards_included"]`, `meta["memories_included"]`, and `extra_canon_tokens` accounting exactly as-is (only placement changes).
- Chunked window drop so the window's left edge moves rarely: after the existing loop that fills `chosen` within `window_budget`, snap the dropped count UP to a multiple of `WINDOW_DROP_CHUNK = 8` (new module constant):
  ```python
  dropped = len(window_actions) - len(chosen)
  if dropped:
      snapped = ((dropped + WINDOW_DROP_CHUNK - 1) // WINDOW_DROP_CHUNK) * WINDOW_DROP_CHUNK
      keep = len(window_actions) - snapped
      chosen = chosen[-keep:] if keep > 0 else []
      used = sum(estimate_tokens(p) for p in chosen)
  ```
  Recompute `used` before the continuity-anchor threshold check that reads it. Update `meta["window_action_count"]` / `meta["window_tokens_est"]` from the snapped values.
- Update `tests/test_context_builder.py` (lines ~50-100, 160-170, 260-270): system message now contains only base prompt + `[Plot]`; assert `[World info]`/`[Memories]`/`[Summary]` appear in `messages[-1]["content"]` and before `[Author's note]`; assert a `see` action's text appears in NO message. Add a chunked-window test: small `context_size` cfg, 25 fixed-size actions where 10 would fit — expect the window to hold the last 9 (dropped 16 = 15 snapped up to chunk 8) and the 10th-from-end absent.

**A3. See prompt lead + clean caption (`backend/app/routes/images.py`, `frontend/src/components/FeedItem.jsx`)**
- In `_build_final_prompt` (lines ~278-320): keep the drafted `moment_clean`/`beat` computation, but use `beat` only as fallback. New lead order: `summarized` (LLM English sentence, after `contradicting_looks`) → explicit-nudity bit → `where` → `people present:` → canon `clause[:260]` → interaction "wide shot" bit. If `summarized` is empty string, append `beat` as the lead's first item instead. Remove `summarized` from the `tail` parts (it moved to the lead); tail keeps location `entry[:160]`, `image_style`, `style_suffix`, `extra_prompt`.
- Change signature to `-> tuple[str, str, str]` returning `(prompt, negative, caption)` where `caption = summarized or beat`.
- In `post_see` (line ~365): `db.insert_action(conn, adventure_id, seq, "see", caption)` (was `prompt`). `db.insert_image(..., prompt=prompt)` unchanged — the full technical prompt stays on the image row.
- `FeedItem.jsx` `SeeImageCard`: caption source becomes the action text first — `const captionSource = action.text || image?.prompt || ''` run through the existing drafted cleanup chain (split `BREAK`, strip `> You`, strip `people present:`/`exact likeness`/`wide shot` suffixes) with `|| captionSource` fallback; Copy button copies `image?.prompt || action.text` raw. (The regex chain is a no-op on new clean captions and fixes legacy rows whose action text is the full prompt.)
- Rewrite the four old-contract tests to the new contract (mock transport's fixed English `chat_content` IS the summarized sentence):
  - `test_v2.py::test_see_flow_creates_image_and_action`: assert mock summary `"a dark tower under a blood-red sky"` is in the prompt and before `"ink sketch"`; `"Thunder cracks overhead"` (raw beat) NOT in prompt; `data["action"]["text"] == "a dark tower under a blood-red sky"`; `data["prompt"]` still contains `"dramatic angle"`.
  - `test_npc_chronicle.py::test_see_paints_the_latest_beat_not_the_old_story`: keep `"ancient dragon" not in` prompt and capture assertions (window selection unchanged); replace raw-beat assertions with: prompt head contains the mock summary, and the captured prompt-builder messages contain the two latest action texts (that input is unchanged).
  - `::test_see_keeps_the_face_and_puts_the_beat_before_the_style`: head = summarized + canon clause — assert `"plate armor"` and `"short black hair"` in head, mock summary in head before `"plate armor"`, `"ink sketch"` in tail.
  - `::test_see_paints_the_kiss_not_a_portrait`: assert `"kiss"` in head (mock summary for this test must say it — change the mock `chat_content` to `"Caio kisses Princess Alusair in the hall."`), `"caio"`/`"alusair"` in head via names/clause, portrait negatives unchanged.
  - Add new: PT beat exclusion — actions in Portuguese (`"Princesa Alusair entra na chuva."`), mock summary English → `"entra na chuva" not in prompt`, summary in prompt head.

**A4. Job-queue gating per job + post-turn delay + dead code (`backend/app/jobs.py`, `backend/app/game_engine.py`, `backend/app/npc_chronicle.py`)**
- `jobs.process_pending(transport=None, *, should_pause=None) -> int`: inside the drain loop, before each `db.claim_next_job`, `if should_pause is not None and should_pause(): return processed`. `worker_loop` calls `await asyncio.to_thread(process_pending, should_pause=game_engine.is_any_generation_active)` unconditionally (drop the drafted outer `if`). Existing test calls `jobs.process_pending(transport)` keep working (keyword-only addition).
- `game_engine.py`: add `import time` and module constant `_POST_TURN_DELAY_S = 1.5`; first line of `_spawn_post_turn._bg()` is `time.sleep(_POST_TURN_DELAY_S)` with a comment: the client's `onDone` fires `POST /choices` immediately (`Play.jsx::fetchChoices`) and both choices and post-turn work pin slot 1 — the delay lets the interactive call claim the slot first. Test mode (`AIDUNGEON_CHRONICLE=0`) is unchanged (synchronous `_maybe_update_memory`, no thread, no delay).
- Delete `npc_chronicle.spawn_after_turn` (no callers; grep to confirm: `spawn_after_turn` appears only in its own def). Remove `threading`/`os` imports from `npc_chronicle.py` only if nothing else in the file uses them (grep `threading\.` / `os\.` in that file after deletion).
- New tests: jobs gating — enqueue a portrait job, `game_engine.acquire_generation_lock(adv_id)`, assert `process_pending(transport, should_pause=game_engine.is_any_generation_active) == 0` and job still pending; release lock, assert drains. Scenario CRUD — PATCH one field preserves others; DELETE → GET 404; DELETE with a linked adventure leaves the adventure with `scenario_id=None`.

**A5. Story Studio contract fixes (`backend/app/schemas.py`, `backend/app/assistant.py`)**
- `AssistantScenarioRequest.card_count`: `Field(default=None, ge=3, le=8)` (spec says 3–8; currently ge=1).
- POV guarantee in `generate_scenario_draft`: when `pov` is provided, append a deterministic directive to `draft["ai_instructions"]` unless its exact text is already present:
  ```python
  _POV_DIRECTIVES = {
      "second": "Narrate in second person: address the player as 'you' ('você' in Portuguese).",
      "third": "Narrate in third person: refer to the protagonist by name or he/she/they; never 'you'.",
      "first": "Narrate in first person: the protagonist narrates as 'I' ('eu' in Portuguese); never address the player as 'you'.",
  }
  ```
  (Needed because `context_builder` never reads `third_person` — POV only reaches the narrator through `ai_instructions`.)
- New tests (mock LLM transport): `/api/assistant/card` returns a normalized card (title/triggers/entry) and `web_sources` empty without web; with a custom handler that serves Wikipedia API JSON for `*.wikipedia.org` hosts (pattern: custom `httpx.MockTransport` handlers already used in `tests/test_npc_chronicle.py`), `web_search=True` yields non-empty `web_sources`. `/api/assistant/sync-plot` returns `plot_essentials`/`authors_note` from the mock JSON; 404 for unknown adventure. `/api/assistant/scenario` with `pov="first"` → `ai_instructions` contains the first-person directive. Turn web search: `POST /action {"type":"do","text":"visit Rome","web_search":true}` with the Wikipedia-serving transport → SSE `done` data has non-empty `web_sources` and the captured stream payload's last user message contains `[Web Research & Canon Facts]` (action ≤7 words → deterministic short-query path, no LLM query-extraction call).

**A6. Context-size cap plumbing (per-slot safety)**
- `config/settings.json`: `"context_size_limits": { "min": 4096, "max": 106496 }` (= per-slot 131072 ÷ 1.2, rounded down to a 4096 multiple; protects against the chars/4 estimator undercounting PT).
- `backend/app/settings_store.py::effective_llm_settings`: after assembling cfg, `cfg["context_size"] = min(cfg["context_size"], int(llm.get("context_size_limits", {}).get("max", 106496)))` — silently clamps the VM's stored 262144 on every read; no DB write needed. No schema change (`LlmSettingsUpdate` `le=262144` stays; clamp on read makes oversized writes harmless).
- `backend/app/routes/settings.py::_settings_response`: add `"context_size_max": int(settings_store.load_file_settings().get("llm", {}).get("context_size_limits", {}).get("max", 106496))`.
- `frontend/src/pages/Settings.jsx` and `frontend/src/components/drawer/ConfigTab.jsx`: replace every hardcoded `262144` with `contextSizeMax = llm.context_size_max ?? 106496` (from `getLlmSettings`); preset list becomes the entries of `[{32k,32768},{64k,65536},{128k,131072}]` with `val <= contextSizeMax`, plus `{label: `${Math.round(contextSizeMax/1024)}k Max`, val: contextSizeMax}`; label `"262,144 max (256k)"` → `` `${contextSizeMax.toLocaleString()} max (per slot)` ``; the "64k–256k campaigns" copy → "64k–104k campaigns".
- Test: `db.set_setting(conn, "llm.context_size", json.dumps(262144))` → `GET /api/settings/llm` returns `context_size == 106496` and `context_size_max == 106496`.

### Phase B — frontend

**B1. `frontend/src/api.js` additions** (follow existing `request()` patterns):
```js
updateScenario: (id, payload) => request(`/scenarios/${id}`, { method: 'PATCH', body: payload }),
deleteScenario: (id) => request(`/scenarios/${id}`, { method: 'DELETE' }),
assistantCard: ({ concept, card_type = 'character', adventure_id, draft, current_card, instruction, web_search, language } = {}) =>
  request('/assistant/card', { method: 'POST', body: { concept, card_type, adventure_id, draft, current_card, instruction, web_search, language } }),
assistantSyncPlot: ({ adventure_id, instruction, language } = {}) =>
  request('/assistant/sync-plot', { method: 'POST', body: { adventure_id, instruction, language } }),
```
Extend `assistantScenario` to pass through `genre, tone, pov, protagonist_name, protagonist_role, protagonist_appearance, image_style, card_count, web_search_mode` (undefined keys are dropped by `JSON.stringify`), and `assistantField` to pass `instruction` and `web_search`.

**B2. Turn web search UI (`frontend/src/components/InputBar.jsx`, `frontend/src/pages/Play.jsx`)**
- InputBar: new `webOn` state (default false, session-only); a sticky toggle button `🌐` between the mic and Send buttons, copying the existing `panelOpen` button style pattern (`aria-pressed`, title `"Search the web for grounding (Wikipedia/DuckDuckGo)"`); `submit()` calls `onSend(tab, t, { webSearch: webOn })`.
- Play.jsx: `sendAction = (type, text, opts)` → body `{ type, text, ...(opts?.webSearch ? { web_search: true } : {}) }`; other `sendAction` callers (GamePanel widgets, `pickChoice`) pass no opts — unchanged. New `webSources` state: set from `data.web_sources` in `onDone` when non-empty; cleared at the top of `runStream`. Render a dismissible strip between the choices chips and InputBar (mirror the choices-strip markup): `🌐 Sources:` + each source as `<a href={s.url} target="_blank" rel="noreferrer">{s.title}</a>` + `✕` dismiss.

**B3. AI Story Studio UI (`frontend/src/components/GenerateAdventureModal.jsx`)**
- Step 1, below the brief textarea, always-visible `grid sm:grid-cols-2 gap-3`: Genre/Setting (text), Tone & Pacing (text), Narrative POV (select: `second` default / `third` / `first`), Story cards (select 3–8, default 5), Language (select: Auto=browser default / `pt` / `en` / `es`), Visual style for images (text), Protagonist name / Role / Appearance (three text inputs, name spans 2), Web search (select: `auto` default / `always` / `off`). `generate()` sends them all via the extended `api.assistantScenario`; `language` = chosen ?? `draftLanguage()`; `card_count` always sent (default 5).
- Progress feedback while `generating`: 1 s interval updates a stage label — `t<4s && webMode!=='off'` → `"🌐 Searching the web…"`, `t<25s` → `"✍️ Writing scenario…"`, else `"🃏 Building story cards…"`; replaces the static hint line (keeps the "about a minute" note). Clear the interval in `finally`.
- Step 2: when `draft.web_sources?.length`, a "🌐 Sources consulted" block listing title→URL links. New editable field block "Visual Style (image generation)" wired to `fieldHeader('image_style', …)`. The per-field ↻ Regenerate button now runs `window.prompt('Refinement instruction (optional):')` and passes non-empty text as `instruction`; a small `🌐 web` checkbox next to the Story Cards header (state `webOnRefine`, default false) is passed as `web_search` on field/card AI calls.
- Story cards list: per-card `✨ AI` button → `window.prompt('Instruction for this card (optional)')` → `api.assistantCard({ concept: card.title, card_type: card.type || 'character', draft, current_card: card, instruction, web_search: webOnRefine, language: draftLanguage() })` → replace card at index with `r.card`; toast `"Enriched with N web sources."` when `r.web_sources?.length`. Header button `+ Add story card with AI` → `window.prompt('Card name or concept')` → `api.assistantCard({ concept, card_type: 'character', draft, web_search: webOnRefine, language })` → append `r.card`.
- `create()`: include `image_style: draft.image_style ?? ''` and `third_person: !!draft.third_person` in `createScenario` body (backend sets `draft.third_person` when `pov` was sent); `createAdventure({ scenario_id })` unchanged (route already inherits `image_style`/POV).

**B4. Shared card list editor + full ScenarioEditor (`frontend/src/components/StoryCardsEditor.jsx` new, `frontend/src/pages/ScenarioEditor.jsx` rewrite)**
- New `StoryCardsEditor.jsx` extracting the card list UI (title/type inputs, triggers TagInput, entry textarea, remove button, add button) from GenerateAdventureModal, props `{ cards, onChange, busy, onAiCard, webOn, onToggleWeb }`; used by both the modal (B3) and ScenarioEditor. (New shared component justified: two consumers would otherwise duplicate ~80 lines.)
- ScenarioEditor: remove `readOnly` and the amber warning banner; load scenario in edit mode (existing `load()`); add `image_style` input and `story_cards` via StoryCardsEditor; per-field AI refine buttons reusing the same `window.prompt` + `api.assistantField({ field, current, draft: form, instruction, web_search, language })` flow. Save: create mode → `api.createScenario` (existing); edit mode → `api.updateScenario(id, form)`. Edit-mode footer buttons: **Save changes**, **Duplicate** (`createScenario({ ...form, title: `${form.title} (copy)` })` → toast + navigate to the new `/scenarios/:id/edit`), **Delete** (`window.confirm('Delete scenario "…"? Adventures that started from it keep running.')` → `api.deleteScenario` → `navigate('/')`). Routes in `App.jsx` already exist — no change.

**B5. Home scenario management (`frontend/src/pages/Home.jsx::NewAdventureModal`)**
- Each scenario row gains two right-side icon buttons (on click: `e.preventDefault(); e.stopPropagation()`): `✎` → `navigate(`/scenarios/${s.id}/edit`)` + `onClose()`; `🗑` → `window.confirm` → `api.deleteScenario(s.id)` → `setScenarios(list => list.filter(x => x.id !== s.id))`, reset `selected` to `'blank'` if it was selected, toast. Style: copy the `border border-zinc-700 text-zinc-400 hover:bg-zinc-800` button pattern used in the modal.

**B6. Inspector AI buttons (`frontend/src/components/drawer/MemoryTab.jsx`, `CardsTab.jsx`)**
- MemoryTab, Plot Essentials header row: button `✨ Sync Plot with AI` → `api.assistantSyncPlot({ adventure_id: adventure.id, language: (navigator.language || 'pt').slice(0, 2) })` → fill `plot`/`note` textarea state with the response (NOT auto-saved — user reviews and clicks Save), toast `summary_delta` when present. Disabled with spinner while in flight.
- CardsTab editor modal footer (left side): `✨ Enrich with AI` + `🌐 web` checkbox → `window.prompt('Instruction (optional)')` → `api.assistantCard({ concept: form.title || 'card', card_type: form.type, adventure_id: adventure.id, current_card: form, instruction, web_search: checked, language })` → merge `r.card` fields into `form` (title/triggers/entry, plus `sheet`/`portrait_prompt` when present — add both to form state initialized from the editing card so AI-provided sheets persist through save; `StoryCardUpsert` already accepts them). Tab header next to `New Card`: `🌐 AI Card` → `window.prompt('Card name or concept')` → `api.assistantCard({ concept, card_type: 'character', adventure_id: adventure.id, web_search: true, language })` → `api.createCard(adventure.id, r.card)` → reload + toast.

**B7. Cache invalidation (`frontend/public/sw.js`, `frontend/src/main.jsx`, `backend/app/main.py`)**
- `sw.js`: `CACHE_NAME = 'aidungeon-local-v3'` (activate handler already deletes old caches). Add before the generic static handler: same-origin `.js`/`.css` requests → network-first with cache fill, falling back to cache on network failure (mirrors the existing navigation handler shape). API and navigation handlers unchanged.
- `main.jsx`: after SW registration add a guarded `controllerchange` listener (`if (refreshing) return; refreshing = true; window.location.reload()`) — with `skipWaiting` + `clients.claim` already in sw.js, a deploy now self-updates open tabs.
- `main.py::_SpaStaticFiles.get_response`: wrap the result — `path in ("index.html", "sw.js", "manifest.webmanifest")` (including the 404 fallback to index.html) → `Cache-Control: no-cache`; `path.startswith("assets/")` → `Cache-Control: public, max-age=31536000, immutable` (Vite hashes asset filenames).

### Phase C — build, deploy, VM verification (in order)

**C1. Local gates:** `backend/.venv/bin/python -m pytest -q tests` all green; `cd frontend && npm run build` succeeds.

**C2. Deploy files** (ssh as `caio@vm.groupabz.com`; rsync exists both ends):
```
rsync -az --delete --exclude='__pycache__' backend/app/      caio@vm.groupabz.com:/home/caio/aidungeon/backend/app/
rsync -az --delete --exclude='__pycache__' backend/memory/   caio@vm.groupabz.com:/home/caio/aidungeon/backend/memory/
rsync -az               backend/migrations/                  caio@vm.groupabz.com:/home/caio/aidungeon/backend/migrations/
rsync -az backend/schema.sql backend/exporter.py backend/importer.py backend/requirements.txt caio@vm.groupabz.com:/home/caio/aidungeon/backend/
rsync -az config/settings.json                               caio@vm.groupabz.com:/home/caio/aidungeon/config/settings.json
rsync -az --delete        frontend/dist/                     caio@vm.groupabz.com:/home/caio/aidungeon/frontend/dist/
```

**C3. DB backup (online, safe while running):** `cd /home/caio/aidungeon && backend/venv/bin/python -c "import sqlite3; s=sqlite3.connect('data/aidungeon.db'); d=sqlite3.connect('data/aidungeon.db.bak-v090'); s.backup(d); d.close()"`.

**C4. llama-server 2 slots — USER-ASSISTED (unit file is root-owned, `sudo` needs a password).** First capture ground truth without sudo: `ps -o args= -p "$(pgrep -f 'llama-server.*port 1234' | head -1)"` (world-readable) and confirm it contains `-np 1`, `-c 262144`, `--mmproj`; record the full line. Then hand the user exactly:
```
sudo cp /etc/systemd/system/llama-server.service /etc/systemd/system/llama-server.service.v090.bak
sudo sed -i -E 's/(^|\s)-np 1(\s|$)/\1-np 2\2/; s/--parallel 1\b/--parallel 2/' /etc/systemd/system/llama-server.service
sudo systemctl daemon-reload && sudo systemctl restart llama-server
```
Never remove `--mmproj`, `-ctk q8_0`, `-ctv q8_0`. Verify: `set -a; . /home/caio/aidungeon/.env; set +a; curl -s -H "Authorization: Bearer $LLM_API_KEY" http://127.0.0.1:1234/props` → `"total_slots": 2`; `curl ... /slots` → both slots `"n_ctx": 131072`; `nvidia-smi` shows llama-server VRAM ≈ pre-change (~10 GB — same `-c`, same KV total).
If the user cannot run sudo now: skip to C5 anyway — slot auto-detection (A1) simply disables pinning and the app works exactly as before; mark Modo Turbo pending and report.

**C5. Restart backend without sudo:** `ssh caio@vm.groupabz.com 'pkill -f "uvicorn app.main:app"'` — `aidungeon.service` has `Restart=always RestartSec=3`. Verify up: `curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:2122/api/health/llm` → `401`. Verify migration: `PRAGMA table_info(scenarios)` on `data/aidungeon.db` (read-only `mode=ro`) now includes `image_style`.

**C6. VM smoke (create one adventure, then delete it):**
- Slot pin + cache probe, direct: two identical `POST /v1/chat/completions` (non-stream, ~500-token repeated prompt, `"id_slot": 0, "cache_prompt": true`, max_tokens 8) → second response's `timings.prompt_n` ≈ 0 with `timings.cache_n` ≈ prompt size. (This llama.cpp build returns a `timings` object; if absent, fall back to comparing wall time, expect ≥5× faster prompt phase.)
- App TTFT: login (`POST /api/auth/login`), `POST /api/adventures` from a scenario, two `POST /action` SSE turns measuring time-to-first-token with `curl -N -w` → turn 2 TTFT materially below turn 1 (cold prefix vs warm slot 0); `GET /api/settings/llm` → `context_size == 106496` (VM DB 262144 clamped); one turn with `"web_search": true` → `done` event contains `web_sources` (VM has internet); `POST /api/assistant/card` → returns a card. Then `DELETE /api/adventures/{id}` cleanup.
- Static headers: `curl -sI http://127.0.0.1:2122/sw.js` → `Cache-Control: no-cache`; pick the hashed `assets/index-*.js` from `index.html` → `immutable` header.
- SDXL on-demand: do NOT trigger a See during smoke (GPU churn); steps=20 already confirmed in the VM DB.

## Critical files & anchors

- `backend/app/context_builder.py` — `build_context` lines 318-502: volatile-block move, `WINDOW_DROP_CHUNK` snap, top-level `see` filter. Highest-risk edit; read fully before touching.
- `backend/app/llm_client.py` — lines 40-60 (caches) + 150-280 (payloads): slot auto-detect mirrors the existing `_discovery_cache` pattern.
- `backend/app/routes/images.py` — `_build_final_prompt` lines 225-325 and `post_see` line ~365: lead reorder + caption return; four tests in `tests/test_v2.py`/`tests/test_npc_chronicle.py` encode the old contract.
- `frontend/src/components/GenerateAdventureModal.jsx` — the whole file (330 lines): Studio params, prompt()-based refine, AI cards, stage label; its card-list JSX is the extraction source for `StoryCardsEditor.jsx`.
- `backend/app/jobs.py` — `process_pending` line ~262: keyword-only `should_pause`; `worker_loop` line ~278 passes `game_engine.is_any_generation_active`.

## Verification

Prereqs: repo at `/mnt/DEV/Projeto/aidungeon-local`; local venv `backend/.venv`; frontend `node_modules` present (175 entries); ssh `caio@vm.groupabz.com` key auth working; VM `.env` at `/home/caio/aidungeon/.env` supplies `LLM_API_KEY`.

1. `backend/.venv/bin/python -m pytest -q tests` → all pass, including: new `tests/test_v9.py` (slot pinning on/off via `/props` mock, chunked window, see-exclusion, scenario PATCH/DELETE + orphan nulling, jobs `should_pause`, assistant card/sync-plot, POV directive, turn web search, settings clamp) and the four rewritten See tests.
2. `cd frontend && npm run build` → success (proves JSX compiles).
3. Local API smoke (temp DB, real VM LLM): `cd backend && AIDUNGEON_DB_PATH=/tmp/aidl-v090.db AIDUNGEON_JOBS_WORKER=0 ../backend/.venv/bin/python -m uvicorn app.main:app --port 8123`; login → token; `POST /api/scenarios` → `PATCH` title → `GET` reflects it → `DELETE` → `GET` 404; `POST /api/assistant/card {"concept":"Princess Alusair"}` → card JSON; `POST /api/adventures` → `POST /api/adventures/{id}/action {"type":"do","text":"visit the Colosseum in Rome","web_search":true}` → SSE `done` JSON has `web_sources` array non-empty; `curl -sI localhost:8123/sw.js` → `no-cache` (after B7 + build). Kill the uvicorn.
4. Browser pass (Play page): toggle 🌐 on, send an action → sources strip appears after done; open Inspector → Memory → `✨ Sync Plot with AI` fills textareas without saving; Cards → `🌐 AI Card` creates a card; Home → New Adventure modal shows ✎/🗑 per scenario; Generate with AI shows the new parameter grid and stage labels. If browser automation is unavailable, substitute DOM-level checks in the built bundle plus the API smoke above and note it.
5. VM per Phase C: `total_slots == 2`; per-slot `n_ctx == 131072`; pin probe `timings.prompt_n` ≈ 0 on repeat; app turn-2 TTFT < turn-1; `/api/settings/llm` → 106496; web-search turn returns sources; `sw.js` `no-cache`; smoke adventure deleted; `nvidia-smi` llama-server VRAM ≈ pre-change.

## Assumptions & contingencies (user-decided)

- **Cache-friendly layout approved**: volatile blocks (`[World info]`, `[Memories]`, `[Summary]`, `[Player state]`) move from the system message to the final user message; the recent window drops actions in chunks of 8. Narrative prompt layout visibly changes; order tests updated accordingly.
- **`-np 2 -c 262144`** (not 196608): per-slot 131,072, app cap 106,496 (~20% estimator headroom), KV VRAM unchanged vs today. If VM `/slots` reports a different per-slot `n_ctx` (build quirk/unified KV), recompute `cap = floor(per_slot / 1.2 / 4096) * 4096` and set `config/settings.json → llm.context_size_limits.max` to that value before deploying; everything else is unchanged.
- **See lead**: the LLM-summarized English sentence always leads the SDXL prompt; the raw beat is kept only as fallback when the summary comes back empty; `action.text` of a See stores the caption sentence. Old See-feed rows (text = full prompt) are cleaned client-side by the existing FeedItem regexes.
- **Sudo on the VM needs a password**: the llama-server unit edit (C4) is executed by the user with the exact commands above. If not done, deploy proceeds — slot auto-detection disables pinning cleanly and the app behaves as today; report Modo Turbo as pending the unit change.
- llama-server is shared with another project that sends unpinned requests; those can claim an idle slot. Accepted — not addressed in v0.9.0.
