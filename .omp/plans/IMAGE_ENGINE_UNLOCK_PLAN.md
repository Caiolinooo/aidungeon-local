## Context

The Comfy form locks FineView steps at 8 and hides the engine, CFG, sampler, and video knobs in `config/settings.json`. The L4 is 23034 MiB. The narrator holds about 14472 MiB, so a new model has to say, before it runs, whether it fits beside that process. Two opt-in engines are added. The defaults stay `flux2_klein` and `ltxv`.

Both new engines run while the narrator stays loaded. Do not stop `llama-server`, the voice unit, or embeddings. After the 29/10/2026 embeddings move, that process is 0 MiB of VRAM. The hole beside the narrator (14472 MiB) and the voice (~1426 MiB) on the 23034 MiB L4 is 7136 MiB. Comfy already starts with `--vram-headroom 2` (`deploy/vm/comfyui.service`), which spills whatever does not fit.

Z-Image uses the Unsloth Q4_K_M GGUF, not the INT8 safetensors. The INT8 DiT alone is 6234 MiB and does not leave room for a 1024² activation in that 7136 MiB hole. The GGUF is 3850130496 bytes (3672 MiB) plus the 320 MiB VAE, 3992 MiB of weights. At 1024² the activation allowance is 2048 MiB, total 6040, which is inside the hole. Its GGUF architecture string is `lumina2`, and the Wan pin of ComfyUI-GGUF (`6ea2651e7df66d7585f6ffee804b20e92fb38b8a`) already contains `lumina2` in `loader.py`. Do not move that pin. Do not use the BF16 or the NVFP4 DiT.

MiniMax H3's full DiT is 61.73 GiB. The reduced video preset stays the curve-form GGUF Q4_0 (11464827552 bytes, 10934 MiB) plus the video VAE (2681 MiB), 13615 MiB of weights, and the official NVFP4 text encoder on CPU (14961 MiB). The vendor states that encoder does not need a Blackwell GPU. 13615 is 6479 MiB over the hole, so DynamicVRAM spills that part into RAM. The Comfy cgroup is `MemoryMax=24G` and `MemorySwapMax=2G`. The encoder plus the spilled DiT is about 21 GiB, and `MemAvailable` on 29/10/2026 was 15 GB before Comfy started, so the 2G swap cap kills the process. Raise only those two cgroup numbers. The narrator unit is not changed. Saving a preset that exceeds live free memory is allowed. The form shows the spill and does not block.

## Approach

### 1. Presets and the budget

Add `backend/app/image_budget.py`. Nothing in the repo turns a preset plus live free memory into one response.

`h3_length(frames: int) -> int` snaps up onto the H3 grid `17k+5`: `n = max(5, int(frames))`; return `n + (5 - n % 17) % 17`. `121` returns `124`. `56` returns `56`. `48` returns `56`.

`preset_budget(kind: str, engine: str, *, width: int, height: int, frames: int = 1) -> dict` with keys `weights_vram_mib`, `activation_mib`, `vram_mib`, `ram_mib`. `kind` is `image` or `video`. Unknown engine raises `ValueError`. MiB is `(bytes + 1048575) // 1048576`.

| kind | engine | weights on GPU | RAM | activation |
|---|---|---|---|---|
| image | `qwen` | FineView UNET 7116801326 + VAE 675509688 = 7432 MiB | CLIP 9350798360 = 8918 MiB | `round(2048 * width * height / (1024 * 1024))` |
| image | `flux2_klein` | Klein UNET bytes from `config/runtime_models.json` `image.flux2_klein` if present, else 0 | 0 | `round(1536 * width * height / (1344 * 768))` |
| image | `z_image_turbo` | 3850130496 + 335304388 = 3992 MiB | 5631994051 = 5372 MiB | `round(2048 * width * height / (1024 * 1024))` |
| video | `ltxv` | 0 when the checkpoint `bytes` in `runtime_models.json` is null | 0 | `round(2048 * width * height / (768 * 448) * max(frames, 1) / 121)` |
| video | `wan` | 0 when those artifact `bytes` are null | 0 | same pixel term with base 1280×704 and base frames 121 |
| video | `h3` | 11464827552 + 2811065184 = 13615 MiB | 15687142551 = 14961 MiB | `round(3072 * width * height / (768 * 448) * h3_length(frames) / 56)` |

`vram_mib` is `weights_vram_mib + activation_mib`. Steps and CFG are not part of the sum. A null weight file sets `"weights_known": false`; the engines with byte sizes above set `"weights_known": true`. The response also includes `spill_mib`: 0 when `vram_mib` is less than or equal to the 7136 MiB hole, otherwise `vram_mib - 7136`. The hole constant is `HOLE_MIB = 7136`, named in `image_budget.py` with the comment `23034 - 14472 - 1426`. Live free VRAM still comes from `_memory()` and is what `fits_vram` uses. `spill_mib` is the planning number for the UI, not a second measurement.

`GET /api/settings/image/budget` query: `kind` (`image`|`video`), `engine`, `width`, `height`, `frames` (default 1). Missing or non-integer dimensions return 422 `validation_error`. Live memory comes from `routes.health._memory()`: `vram_free_mib = vram_total_mb - vram_used_mb` when both exist, else null; `ram_available_mib` from `ram_available_mb` or null. `fits_vram` / `fits_ram` are true when live is greater than or equal to the estimate, false when smaller, null when live is null. Always 200. This comparison does not unload the narrator and does not refuse a later save.

### 2. Z-Image-Turbo graph

`_comfy_image_engine` accepts `qwen`, `flux2_klein`, and `z_image_turbo`. Any other value still raises `ImageError` `config_error: image.engine desconhecido`.

`_generate_comfyui` calls a new `_comfy_zimage_graph` when the engine is `z_image_turbo`. The sampler, latent, and CLIP match the official INT8 template. The DiT is the GGUF that fits beside the narrator, and the text encoder stays on CPU:

- `UnetLoaderGGUF` `z-image-turbo-Q4_K_M.gguf`
- `CLIPLoader` `qwen_3_4b_fp8_mixed.safetensors`, `type` `lumina2`, `device` `cpu`
- `VAELoader` `ae.safetensors`
- `CLIPTextEncode` of the positive prompt
- `ConditioningZeroOut` when CFG is less than or equal to 1; `CLIPTextEncode` of the caller negative when CFG is greater than 1
- `EmptySD3LatentImage` at the requested width and height, each floored to a multiple of 16 with a floor of 64, batch 1
- `ModelSamplingAuraFlow` shift from the preset (default 3)
- `KSampler` steps from the preset (default 8), cfg default 1, `sampler_name` `res_multistep`, `scheduler` `simple`, denoise 1

Reference images are not connected. `TextEncodeZImageOmni` is experimental and is not in the official INT8 template. The stored prompt is the only identity input for this engine.

Do not download or select `z_image_turbo_bf16.safetensors`, `z_image_turbo_nvfp4.safetensors`, or `z_image_turbo_int8_convrot.safetensors`. Do not restart Comfy to add `--lowvram`. The existing `--vram-headroom 2` is the spill path.

### 3. MiniMax H3 graph

`_video_engine` accepts `ltxv`, `wan`, and `h3`. Clip generation calls a new `_h3_i2v_graph` only when the engine is `h3`. LTXV and Wan graphs stay as they are.

The graph copies the official i2v sampler chain and swaps only the DiT for the reduced GGUF:

- `UnetLoaderGGUF` `MiniMax-H3-fl2va-curve-Q4_0.gguf`
- `CLIPLoader` `qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors`, `type` `minimax`, `device` `cpu`
- `VAELoader` `minimax_h3_video_vae_int8_convrot.safetensors`
- `LoadImage` of the source frame
- `MiniMaxH3ImageToVideo` with that clip, that video VAE, the prompt, width, height, `length=h3_length(frames)`, and `first_frame` from `LoadImage`. No `last_frame`.
- `KSamplerSelect` `res_multistep`
- `BasicScheduler` `simple`, steps from the preset (default 8), denoise 1
- `RandomNoise` from the existing clip seed
- `BasicGuider` model, positive conditioning, cfg from the preset (default 1)
- `SamplerCustomAdvanced`, then `VAEDecode` with the video VAE, `CreateVideo` at the preset fps (default 24), `SaveVideo` H.264

Do not load the audio VAE, the turbo LoRA, `ref2va`, a ControlNet, or any embedding. Do not attach `minimax_h3_fl2v_turbo_4step_v1.0_768p_comfyui_bf16.safetensors` to the GGUF. If Comfy rejects the GGUF with `Unexpected architecture type` or says `MiniMaxH3ImageToVideo` cannot take that model, raise `ImageError` `config_error: h3 gguf was rejected; ComfyUI-H3-Multishot v2.7.2 is required`. Do not retry on the 19.52 GiB FP8 or the 61.73 GiB BF16. Do not stop the narrator to make the weights resident. The spill is the way it runs beside the narrator.

Default H3 frame size when the video engine changes to `h3` and width/height/frames were still the LTXV defaults (768×448 and 121): store width 768, height 448, frames 56, steps 8, cfg 1, fps 24. If the saved video block already has engine `h3`, do not overwrite those numbers on read.

### 4. Settings the form can change

`ImageSettingsUpdate` and `effective_image_settings` gain these public fields. Omitted fields stay. `comfyui_steps` remains the FineView schedule: a value other than `len(sigmas) - 1` is still 422, and `tests/test_settings.py::test_comfyui_steps_must_match_sigmas` stays.

- `comfyui_engine`: `qwen` | `flux2_klein` | `z_image_turbo`
- `flux2_steps` int 1–8, `flux2_cfg` float 1–2, `flux2_sampler` `euler` | `res_multistep`, `flux2_megapixels` exactly 0.25 or 1. Stored inside the existing `image.comfyui_flux2` override. Add `steps`, `cfg`, `sampler`, `megapixels` to the flux merge allow-list. `with_flux2_cfg` still forces 2.0 for one explicit See and does not persist it.
- `zimage_steps` int 1–20, `zimage_cfg` float 0–20, `zimage_shift` float 0.01–100. Default block in `settings.json` under `image.providers.comfyui.zimage` with the filenames and defaults in section 2. Override key `image.comfyui_zimage`.
- `video_engine`: `ltxv` | `wan` | `h3`
- `video_steps` int 1–40, `video_cfg` float 0–20, `video_width` and `video_height` int 64–1920, `video_frames` int 5–241, `video_fps` float 8–30. These write the existing `image.comfyui_video` override. Selecting `wan` still applies that preset's checkpoint, clip, 20 steps, CFG 5, and 1280×704 when the request omits those video fields. Selecting `h3` applies section 3's defaults the same way. Selecting `ltxv` restores the file's LTXV block for any video field the request omitted.

`frontend/src/pages/Settings.jsx` and `frontend/src/components/drawer/ConfigTab.jsx` both render one new component `frontend/src/components/settings/ImageEngineFields.jsx`. The read-only FineView steps input remains only while `comfyui_engine` is `qwen`, labeled `Passos do FineView (fixos no schedule)`. For Klein and Z-Image the steps, CFG, and sampler controls above are editable. Width and height stay the existing shared Comfy fields. The video engine select shows the video fields. No filename text inputs.

The component polls `GET /api/health/llm` every 5 seconds while mounted and `GET /api/settings/image/budget` when engine, kind, width, height, or frames change. It shows `VRAM livre {live} MiB · este preset pede {vram_mib} MiB` and the same sentence for RAM. When `fits_vram` or `fits_ram` is false, the sentence is a warning and the save button stays enabled. Under the Z-Image engine, one line: `Z-Image-Turbo não recebe as fotos de referência; a identidade fica no prompt.` When `spill_mib` is 0, the budget line stays the live-versus-estimate sentence. When `spill_mib` is greater than 0, add `Não cabe inteiro ao lado do narrador. O Comfy troca {spill_mib} MiB com a RAM; o narrador continua. Fica mais lento.` The save button stays enabled in both cases.

`docs/openapi.yaml` `ImageSettings` gains the same fields. `config/runtime_models.json` gains `image.z_image_turbo` and `video.h3` with the filenames, byte sizes, URLs, and `sha256` equal to these LFS oids:

- `z-image-turbo-Q4_K_M.gguf` 3850130496 `4ab6bceb1f0b82c9e725f123848018eac662a6fc1d467c019e13e701afb7b55c` from `https://huggingface.co/unsloth/Z-Image-Turbo-GGUF/resolve/main/z-image-turbo-Q4_K_M.gguf`
- `qwen_3_4b_fp8_mixed.safetensors` 5631994051 `72450b19758172c5a7273cf7de729d1c17e7f434a104a00167624cba94f68f15`
- `ae.safetensors` 335304388 `afc8e28272cd15db3919bacdb6918ce9c1ed22e96cb12c4d5ed0fba823529e38`
- `MiniMax-H3-fl2va-curve-Q4_0.gguf` 11464827552 `226c83f6b971077f61f4ccf4ffaaed5d7ccf914642b7c25359da1da2f13dcd1a` from `https://huggingface.co/joeygambino/MiniMax-H3-curve-GGUF/resolve/main/fl2va/MiniMax-H3-fl2va-curve-Q4_0.gguf`
- `qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors` 15687142551 `35a88d51044231fe332301d7a62aa81e3f2cba62febeb446e2c1e3e0ef76f2c6` from `https://huggingface.co/Comfy-Org/MiniMax-H3/resolve/main/text_encoders/qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors`
- `minimax_h3_video_vae_int8_convrot.safetensors` 2811065184 `52a2c8c73583c86e4f41cdcce3a6ad0ea562987bc0bf3d60a0cef5f5c8e60c0e`

`min_vram_mib` for Z-Image is 6040 (3992 + 2048 at 1024²). `min_ram_mib` is 5372. For H3, `min_vram_mib` is 16687 (13615 + 3072 at the default frame) and `min_ram_mib` is 14961. `spill_mib` at those defaults is 0 for Z-Image and 9551 for H3 (`16687 - 7136`).

### 5. Installers

`deploy/vm/install_z_image_turbo.sh` and `deploy/vm/install_h3_video.sh` follow `deploy/vm/install_flux2_klein.sh`: `COMFY_HOME` default `/home/caio/ComfyUI`, `curl -fL`, `sha256sum -c` against the oids above, `.part` then rename. They do not start a download until the node check passes.

Z-Image check: `rg -n "lumina2" "$COMFY/custom_nodes/ComfyUI-GGUF/loader.py"` exits 0, and `rg -n "class ZImage" "$COMFY/comfy/supported_models.py"` exits 0. Otherwise exit 1 with `ComfyUI-GGUF or ZImage support is missing; do not download`. Do not change the GGUF checkout.

In `deploy/vm/comfyui.service`, change `MemoryMax=24G` to `MemoryMax=36G` and `MemorySwapMax=2G` to `MemorySwapMax=8G`. Leave `MemoryHigh=20G`, `--vram-headroom 2`, the user, and the port. Do not edit the llama or voice units. The install scripts do not restart `comfyui.service`; the last line of `install_h3_video.sh` says to restart it so the cgroup change is picked up.

H3 check: clone `https://github.com/jlucasmcrell/ComfyUI-H3-Multishot.git` into `$COMFY/custom_nodes/ComfyUI-H3-Multishot` at commit `d7d197709fc93c1afb125e3db1dfd4ac0ae28c60` (tag `v2.7.2`). If that checkout does not contain the string `minimax_h3`, exit 1 and download nothing. Do not move the Wan pin of `ComfyUI-GGUF` (`6ea2651e7df66d7585f6ffee804b20e92fb38b8a`). Do not restart `llama-server`. The script's last line tells the operator to restart `comfyui.service` only.

Do not change `image.provider`, the default `engine`, or the default `video.engine` in `config/settings.json`. Add the `zimage` object and leave `engine` as `flux2_klein`. Add the H3 filenames only under `runtime_models.json` `video.h3`, not as the active video block.

## Critical files & anchors

- `backend/app/image_client.py` `_comfy_image_engine` (line 446) and `_video_engine` (line 1020) — the allow-lists. `_wan_ti2v_graph` (line 1117) is the GGUF-loader pattern `_h3_i2v_graph` copies.
- `backend/app/settings_store.py` `_FLUX2_IDENTITY` (line 77) and `apply_image_update` — sampling keys are not in the flux allow-list today.
- `backend/app/routes/health.py` `_memory` (line 86) — live VRAM and RAM. Do not duplicate `nvidia-smi`.
- `frontend/src/pages/Settings.jsx` Comfy branch around line 775 — the read-only steps field.
- `tests/test_settings.py` `test_comfyui_steps_must_match_sigmas` — must stay red on `comfyui_steps: 12`.

## Verification

`tests/test_image_budget.py`, no network:

- `h3_length(121) == 124`, `h3_length(56) == 56`, `h3_length(48) == 56`.
- `preset_budget("image", "z_image_turbo", width=1024, height=1024)` returns `weights_vram_mib == 3992`, `activation_mib == 2048`, `ram_mib == 5372`, `spill_mib == 0`.
- `preset_budget("video", "h3", width=768, height=448, frames=121)` uses length 124, so activation is `round(3072 * 124 / 56)`.

`tests/test_image_comfyui.py`: a cfg with `comfyui_engine="z_image_turbo"` and the zimage block posts a graph whose loader is `UnetLoaderGGUF` and whose filename is `z-image-turbo-Q4_K_M.gguf`, whose CLIP `device` is `cpu`, and which contains no `ReferenceLatent` node. Engine `foo` still raises. `preset_budget("image", "z_image_turbo", width=1024, height=1024)["spill_mib"]` is 0. `preset_budget("video", "h3", width=768, height=448, frames=56)["spill_mib"]` is 9551.

A clip cfg with `engine="h3"` posts `UnetLoaderGGUF` named `MiniMax-H3-fl2va-curve-Q4_0.gguf`, CLIP `device` `cpu`, and `MiniMaxH3ImageToVideo` length `56` when frames is 56. No audio VAE filename appears.

`PUT /api/settings/image` with `{"comfyui_engine": "z_image_turbo", "zimage_steps": 8, "flux2_steps": 4}` returns those values. `{"comfyui_steps": 12}` is still 422 and the stored FineView steps stay 8. `{"flux2_megapixels": 0.5}` is 422.

From `/mnt/DEV/Projeto/aidungeon-local`:

```bash
python -m pytest tests/test_image_budget.py tests/test_image_comfyui.py tests/test_settings.py tests/test_scene_clip.py tests/test_runtime_settings.py -q
```

## Assumptions & contingencies

- The operator runs the two install scripts on the VM. This change does not download weights during pytest and does not restart the narrator.
- If `class ZImage` is missing, the Z-Image installer exits 1 before `curl`. If the H3 pack checkout does not contain `minimax_h3`, that installer exits 1 before `curl`. Neither installer substitutes another quant.
- `with_flux2_cfg(..., 2.0)` on an explicit See still wins over a saved `flux2_cfg` for that one generation.
- Klein and LTXV remain the engines used when the new fields are absent.
- The narrator, the voice unit, and embeddings stay loaded. Z-Image Q4_K_M is the resident fit in the 7136 MiB hole. H3 spills 9551 MiB at the default frame and uses the raised Comfy swap cap. Neither path unloads llama-server.

## Ship

Do not push branch `portal`. This repo has no such branch. Remote `origin` is `https://github.com/Caiolinooo/aidungeon-local.git`. `origin/HEAD` is `origin/main`. The work tree at planning time was `feat/unified` (`b787dd3`).

After the pytest command in Verification is green:

- README only: add a `## Funcionalidades (v0.14.0)` section above `v0.13.0` describing the unlocked Comfy/video controls, Z-Image Q4_K_M beside the narrator, and H3 Q4 spilling to RAM. There is no CHANGELOG file; do not create one.
- Commit on the current branch. Message: `feat: run Z-Image and H3 beside the narrator`. Do not stage `.omp/plans/` or secrets.
- Check out `main`, merge that commit into `main` (merge commit if `main` is not a direct ancestor; do not rebase and do not force-push). `git push origin main`.
- VM, as `deploy/vm/README.md` already documents: `git push ssh://caio@vm.groupabz.com/home/caio/aidungeon.git main`, then on the VM `cd /home/caio/aidungeon-git` and `tools/deploy.sh --target staging --ref origin/main`. If that smoke passes, `tools/deploy.sh --target main --ref <that commit>`. Do not pass `--force`. If the app refuses because the player acted in the last 10 minutes, stop and report that; leave staging deployed.
- Do not run the weight installers during deploy. They stay operator scripts. A missing file fails the Comfy job; it does not switch quant.
