## Context

See only follows the picture when the player writes English. The image LLM is already told to write one English sentence (`_PROMPT_BUILDER_SYSTEM` / `_EXPLICIT_BUILDER_SYSTEM` in `backend/app/routes/images.py`, and `_PORTRAIT_SYSTEM` in `backend/app/jobs.py`). That sentence is not the string the diffusion model receives. After it returns, `_build_final_prompt` appends the raw player order (`extra_prompt`, lines 1022 and 1080), the location card title (`Place:`, line 1024), the location `entry` (line 1111 and the explicit place line around 1084), and `adventures.image_style` (line 1112). `scene._prompt` never calls the LLM: backdrop text is `title`, `entry`, and the narration beat joined in code. `_execute_portrait` appends raw `sheet_context` (appearance, body, clothing) onto `portrait_prompt`. Clips copy those strings (`_scene_clip_prompt`, `_portrait_clip_prompt`, `_turnaround_prompts`). `likeness.english_bits` and `npc_assets.english_appearance` do not translate; they drop accented Portuguese. Leave those filters in place. They are why a Portuguese narration beat stays out of See (`tests/test_v9_see.py`) and why a Portuguese sheet look is not pasted over a reference photo.

Every positive prompt that reaches ComfyUI or SDWebUI must be English: See, backdrop, portrait, location image, turnaround master/sheet/clip, and scene/portrait clips. The story caption (`action.text` for See) stays the builder sentence. No internal phrase dictionary and no `_PT_WORDS` list. The existing `image_prompt` role rewrites the assembled string.

Do not change the image or video checkpoint, steps, CFG, sampler, resolution, frames, or fps. On the L4, FLUX.2 Klein 4B (the active `engine=flux2_klein`, 4 steps) finished in 24.802 s with 2264 MiB VRAM free, and LTXV 2B distilled (8 steps, 768×448, 121 frames) finished in 35.528 s with 1248 MiB free (`deploy/vm/README.md`, 29/10/2026). Wan 2.2 at 20 steps, CFG 5, 1280×704 took about 13 minutes. FineView’s text encoder is `qwen3vl_8b_int8_convrot.safetensors` (9350798360 bytes) and `config/runtime_models.json` requires `min_ram_mib` 8918 with `clip_device=cpu`. 8 GB free RAM is below that, and free RAM is not VRAM. Z-Image-Turbo and Wan 2.1 are already recorded as not fitting beside the narrator. Switching models would either swap or multiply the measured time. The quality change that fits is English text into the current encoders.

## Approach

### 1. Rewriter, no dictionary

Add `backend/app/image_english.py`. It imports `llm_client`, `settings_store`, and `db` only. It does not import `image_client`, `jobs`, `scene`, or `routes.images` at import time. Nothing in the repo rewrites a finished provider prompt; do not extend `english_appearance` or `english_bits`.

`render_english(text: str, *, transport=None, llm_cfg: dict | None = None) -> str`

- `None` becomes `""`. Empty or whitespace-only returns the same string and does not load settings or call the LLM.
- Otherwise mask names, call the LLM, accept or retry once, unmask, return the string. Never return the Portuguese source after a failed rewrite.
- Memoize only accepted results in a process-local `OrderedDict` of 128 entries, keyed by the exact input string, under a `threading.Lock`. A second call with the same string does not call the LLM. Do not memoize failures.

Name mask, applied left to right before the call and restored after. Do not mask the token `BREAK`. This is a placeholder, not a translation dictionary:

```python
_NAME_SPAN = re.compile(
    r"[A-ZÁÀÂÃÉÊÍÓÔÕÚÇ][\w'’.-]*"
    r"(?:[ \t]+(?:d[aeo]s?|e|y|of|the|and)[ \t]+[A-ZÁÀÂÃÉÊÍÓÔÕÚÇ][\w'’.-]*"
    r"|[ \t]+[A-ZÁÀÂÃÉÊÍÓÔÕÚÇ][\w'’.-]*)+"
    r"|[A-ZÁÀÂÃÉÊÍÓÔÕÚÇ][\w'’.-]*[àáâãäéêíóôõúüç][\w'’.-]*"
)
```

A span is two or more capitalized words with those lowercase particles allowed between them (`A Donzela da Noite`, `Castelo do Vampiro`, `Princess Alusair`), or one capitalized word that itself contains a diacritic (`João`). Replace with `[[0]]`, `[[1]]`, … in order.

One `llm_client.chat_completion`, `role="image_prompt"`, `temperature=0`, `max_tokens=800`, `transport=transport`. Load `llm_cfg` only when it was not passed: `settings_store.effective_llm_settings` inside `db.session()`. Do not add a scheduler role and do not set `llm.scheduler.role_overrides`. The 3B aux stays unused; the README blocks overrides until `tools/eval_turn_aux.py` passes.

System text, exact, no trailing newline beyond the last line:

```text
You rewrite an image-model prompt into English.
Return only the rewritten prompt. No JSON, no markdown, no quotes, no explanation.
Rules:
- Translate every non-English word into English.
- Keep each placeholder [[0]], [[1]], [[2]] and so on exactly as written.
- Keep the token BREAK exactly where it is, with the same count.
- Keep every English word and every number.
- Do not add people, clothing, nudity, style, camera directions, or actions.
- If the prompt is already English, return it unchanged.
```

User content is the masked string and nothing else.

Accept the reply only when all of these hold:

- Strip one optional ``` or ```json fence and one pair of surrounding `"` or `'` . Result is non-empty.
- The token `BREAK` occurs the same number of times as in the masked source.
- Every `[[n]]` from the masked source is still present.
- Every ASCII word of 4 or more letters in the masked source that matches `^[A-Za-z]{4,}$` is present in the reply as a whole word, case-insensitive, except words that contain any of `àáâãäéêíóôõúüç` after casefold (those are the words the model must be allowed to replace). Do not keep a Portuguese word list. A reply that drops `knight`, `BREAK`, or `people` is rejected. A reply that replaces `vampiro` is accepted.

On a rejected reply, empty reply, parse miss, or `LlmError`: call once more with the same user string and the same system text plus a final line `The previous answer was rejected. Return only the English prompt.` At most two calls. The second failure raises `image_client.ImageError` (lazy import) with message exactly `generation_failed: image prompt could not be translated to English`.

Unmask accepted text and return it.

Also append this sentence to `_PROMPT_BUILDER_SYSTEM` and `_EXPLICIT_BUILDER_SYSTEM`, immediately before the existing `Output ONLY` line: `Never copy a Portuguese word. Translate the player order and the place into English.` Do not change the rest of those prompts, the JSON shape, or the code that appends order, place, entry, and style. The rewriter sees that assembled string. The See caption stays `sentence` from the builder, taken before `render_english`.

### 2. Call the rewriter on the provider string

Do not call it inside `image_client.generate_image` or `generate_clip`. Those functions are what `tests/test_image_comfyui.py` drives with a Comfy-only transport, and the posted graph text must stay the caller’s string (`a knight in the rain`).

Call `render_english` on the positive prompt only. Do not pass negatives, the Wan/LTXV negative, or `style_suffix` through a separate call; they ride inside the positive string when the caller already concatenated them.

- `routes/images.py` `_build_final_prompt`: after `prompt` is assembled and before the `return`, `prompt = image_english.render_english(prompt, transport=transport, llm_cfg=llm_cfg)`. `caption` is unchanged. `_plan_see` is the only caller. Catch `image_client.ImageError` there the same way it catches `LlmError`: `raise GameError(503, "generation_failed", str(exc))`.
- `jobs.py` `_execute_see`: rewrite `prompt` before `request_key`. The payload from `_plan_see` is already rewritten; the memoize hit makes the second call free. A job whose payload was queued earlier still gets English before the hash.
- `jobs.py` `_execute_backdrop`: rewrite `prompt` before `backdrop_cache_key` and before `generate_image`. Store and return that English string.
- `jobs.py` `_execute_portrait`: rewrite `final` after the lock suffixes are appended and before the cache lookup’s `generate_image`. Pass `llm_cfg` loaded with `settings_store.effective_llm_settings` in the existing settings session. `insert_image(..., prompt=final)` stores the English string.
- `jobs.py` `_execute_location_image`: rewrite `prompt` first. Ignore `payload["cache_key"]`. Recompute with `image_client.request_key(img_cfg, prompt, width=1344, height=768)` and use that key for the insert.
- `jobs.py` clip executor (the function that calls `generate_clip` around line 1990): rewrite `prompt` before `clip_request_key`.
- `jobs.py` `_execute_turnaround`: rewrite `prompts["master"]`, `prompts["design_sheet"]`, and `prompts["clip"]` once, before the generate loop. Do not rewrite the deterministic split sentence `Deterministic split: canonical anchor face and full-body master`.

Each executor that does not already have `llm_cfg` loads it once inside the `db.session()` it already opens for image settings. Pass that dict and the job `transport` into `render_english`. Do not start a second session just for settings if one is already open.

### 3. Cache stamp

Old rows were rendered from Portuguese. In `image_client.request_key`, add `"prompt_locale": "en"` to the hashed payload dict next to the existing fields, for every provider, immediately before `json.dumps`. In `clip_request_key`, add the same field to its payload. In `npc_assets.portrait_state_key`, add `"prompt_locale": "en"` on the payload dict before it is hashed. Always, not only for Portuguese. Equality tests compare two calls, so they stay equal. The first live request after deploy misses the old row and stores the English render. Do not add a second translation anywhere else.

### 4. Models stay

Do not edit `config/settings.json`, `config/runtime_models.json`, or `deploy/vm/*`. Leave `engine` `flux2_klein`, video `engine` `ltxv`, Klein `steps` 4, `cfg` 1, `megapixels` 0.25, FineView sigmas, LTXV `steps` 8, `frames` 121, `fps` 24, `768`×`448`. Do not set `engine` to `qwen` or `wan`. Do not raise CFG, steps, or resolution. 8 GB free RAM cannot hold the FineView CLIP (8918 MiB minimum, 9.35 GB file) without swap, and the L4 had at most 2264 MiB VRAM free beside the narrator.

## Critical files & anchors

- `backend/app/routes/images.py` `_build_final_prompt` — assembled `prompt` (the `BREAK` join around lines 1107 and 1115) is what `render_english` rewrites; `caption` is not.
- `backend/app/scene.py` `_prompt` — leave the joiner. Backdrop translation happens in `_execute_backdrop`, which is what sends the string.
- `backend/app/jobs.py` `_execute_portrait` (`final` near line 1186), `_execute_see` (line 1837), `_execute_backdrop` (line 1422), `_execute_location_image` (line 1903), `_execute_turnaround` (line 2057).
- `backend/app/image_client.py` `request_key` (payload build near line 838) and `clip_request_key` — add the constant stamp only.
- `backend/app/npc_assets.py` `portrait_state_key` near line 1137 — same stamp. Do not change `english_appearance`.

## Verification

New file `tests/test_image_english.py`. Monkeypatch `llm_client.chat_completion`. No network.

- `render_english("   ")` returns `"   "` and the stub is not called.
- `render_english("wide cinematic shot, ink sketch")` calls the stub once. Stub returns that same string. Result equals it.
- Source `wide cinematic shot, o vampiro morde o pescoço BREAK ink sketch`. The user message contains `morde` and `BREAK` and does not contain a Portuguese word list. Stub returns `wide cinematic shot, the vampire bites the neck BREAK ink sketch`. Result equals that return.
- Source `A Donzela da Noite morde o pescoço`. The user message contains `[[0]]` and `morde` and does not contain `Donzela`. Stub returns `[[0]] bites the neck`. Result is `A Donzela da Noite bites the neck`.
- Stub returns `nope` with no `BREAK` while the source contains `BREAK`, then returns `nope` again. `render_english` raises `ImageError` whose message is `generation_failed: image prompt could not be translated to English`.
- Two `render_english` calls with the same accepted source call the stub once.

In `tests/conftest.py` add:

```python
IMAGE_ENGLISH_SYSTEM_PREFIX = "You rewrite an image-model prompt into English."

def image_english_echo(payload: dict) -> str | None:
    messages = payload.get("messages") or []
    system = next((m.get("content") or "" for m in messages if m.get("role") == "system"), "")
    if not str(system).startswith(IMAGE_ENGLISH_SYSTEM_PREFIX):
        return None
    return next((m.get("content") or "" for m in messages if m.get("role") == "user"), "")
```

In every `tests/**/*.py` handler that answers `/chat/completions`, before the existing response and before any scripted pop, if `image_english_echo(payload)` is not `None`, return that string as the non-stream `choices[0].message.content` and do not consume a scripted step. `tests/test_v2.py::make_v2_transport` is the See path: without this, the fixed `chat_content` would replace `dramatic angle`. Handlers that only serve SSE still need the branch on the non-stream path.

`tests/test_image_comfyui.py` must stay on `generate_image` with the Comfy transport and must not import `render_english`. The encoder prompt assertion `a knight in the rain` stays.

Then run from `/mnt/DEV/Projeto/aidungeon-local`:

```bash
python -m pytest tests/test_image_english.py tests/test_v9_see.py tests/test_v2.py::test_see_flow_creates_image_and_action tests/test_scene.py tests/test_image_comfyui.py tests/test_image_cache.py tests/test_portrait_sync.py tests/test_scene_clip.py tests/test_v3.py -q
```

`test_v9_see.py` still asserts `entra na chuva` is absent and the caption is the builder sentence. `test_see_flow_creates_image_and_action` still asserts `dramatic angle` and `a dark tower under a blood-red sky` are in `data["prompt"]` and `Thunder cracks overhead` is absent. A failure that the stored See prompt lost `dramatic angle` means the echo branch did not run and the fixed stub replaced the rewriter user message.

## Assumptions & contingencies

- The rewriter is the existing 35B `image_prompt` slot (20 s timeout). See pays that call on top of the builder call; the executor’s second call is a memoize hit. Backdrop, portrait, location, clip, and turnaround pay one call inside the job, before Comfy, and do not change the 4-step or 8-step graphs. If that aux call fails twice, the job fails with the `ImageError` above. Do not send the Portuguese string.
- If a chat mock in `tests/` starts returning the builder sentence as the provider prompt, the echo branch is missing on that handler. Add it. Do not weaken `render_english` to skip English or to skip when the transport is a mock.
- If `MemAvailable` on the VM is later shown above 12000 MiB and VRAM free beside the narrator is above 8000 MiB, still do not switch to FineView or Wan in this change. The files above stay on Klein and LTXV.
