# Executor de imagem e clipe curto

Leitura feita em 19 out 2026 na VM `caio@vm.groupabz.com`. Nada foi instalado. `llama-server`, `sd-webui` e o app não foram reiniciados.

> **26 out 2026.** O provider `comfyui` usa FineView Qwen Image 2.1 Turbo Fixed FP8, CLIP Qwen3-VL 8B INT8 em CPU e texture-fix VAE. O grafo tem oito passos, shift 1.31 e referências por `TextEncodeQwenImage21`; downloads e hashes estão em `deploy/vm/README.md`.
>
> **28 out 2026.** O provider ativo é `comfyui` com `engine=flux2_klein`: `flux-2-klein-4b-fp8.safetensors`, `qwen_3_4b.safetensors` (`CLIPLoader` type `flux2`) e `flux2-vae.safetensors`. Canvas único em `EmptyFlux2LatentImage` e `Flux2Scheduler`, 4 passos, CFG 1, euler. Referências: `LoadImage` → `ImageScaleToTotalPixels` (0,25 MP por âncora) → `VAEEncode` → `ReferenceLatent` no positivo e no negativo, teto 4. `engine=qwen` mantém o FineView; LTXV e Wan não mudam.

O app fala HTTP com o executor em `backend/app/image_client.py`: `POST {base}/sdapi/v1/txt2img` e, no retrato por foto, `POST /sdapi/v1/img2img`. A resposta usada é `images[0]` em base64. O corpo está fixo em `DPM++ 2M`, scheduler `Karras`, `cfg_scale` 5 — o que o epiCRealism XL pede.

## O que a L4 está segurando agora

`nvidia-smi` nesta leitura: **NVIDIA L4, 23034 MiB, 22547 MiB em uso, 19 MiB livres.**

| Processo | MiB |
| --- | ---: |
| `llama-server` (narrador) | 14472 |
| segundo `llama-server` | 698 |
| `python3` da voz | 1426 |
| `python` do sd-webui (A1111) | 5524 |
| desktop Cosmic | ~230 |

Checkpoints em `/home/caio/sd-webui/models/Stable-diffusion/`:

- `dreamshaper_8.safetensors` (2,0 GiB, SD 1.5)
- `epicrealismXL_vxviiCrystalclear.safetensors` (6,5 GiB)
- `juggernautXL_ragnarok.safetensors` (6,6 GiB)

VAE ao lado: `sdxl-vae-fp16-fix.safetensors`.

**Orçamento.** Tirar o A1111 (5524 MiB) devolve cerca de **5,4 GB**. O narrador e a voz ficam. Os 8–10 GB “livres ao lado do llama” só aparecem se a voz, o segundo `llama-server` e o desktop também saírem — aí sobra por volta de 8 GB, colado no processo de 14,5 GB. Pausar os dois `llama-server` e o A1111, com a voz no ar, devolve cerca de **20 GB**.

Nenhum modelo abaixo foi gerado nesta L4. O número de cada linha é o da fonte, não uma medição local.

## Still — 1024 e pele

| Modelo | VRAM publicada para uma imagem | Ao lado do narrador (5,4 GB) |
| --- | --- | --- |
| SDXL (epiCRealism / Juggernaut, já no disco) | o A1111 parado já usa 5,5 GB; em 1024 a placa enche | é o que está no ar |
| SD 1.5 `dreamshaper_8` | cabe no buraco | já está no disco; é um passo abaixo do XL em pele |
| FLUX.1 schnell / dev, FP16 | ~24 GB | não cabe na L4 |
| FLUX.1 FP8 | ~12 GB ([Thunder Compute](https://www.thundercompute.com/blog/flux-comfyui-ai-image-generation)) | só com o narrador pausado |
| FLUX.1 GGUF Q4 / NF4 | 6–8 GB no mesmo guia | não cabe nos 5,4 GB medidos |
| FLUX.2 dev | 32B, ~90 GB mesmo em lowVRAM ([NVIDIA](https://blogs.nvidia.com/blog/rtx-ai-garage-flux-2-comfyui/)) | não cabe na L4 |
| FLUX.2 Klein 4B destilado | FP8 oficial, 4 passos, encoder Qwen3 4B e VAE próprios ([BFL](https://huggingface.co/black-forest-labs/FLUX.2-klein-4B)) | **medido na L4 em 1344×768:** 27,99 s frio e 6,32 s aquecido sem referência; com um retrato real, 32,61 s de render a 1 MP e **25,49 s a 0,25 MP** (fila excluída); narrador/voz ativos e Forge descarregado |
| FLUX.2 Klein 9B | 29 GB no mesmo guia | não cabe na L4 |
| FLUX 3 Image Klein INT8 | 5,6B; 8 GB; 1 MP em menos de 9 s numa RTX 4070 ([BFL](https://bfl.ai/blog/flux-3-image-klein)) | planejado: UNET 5,8 GB + VAE 0,3 GB na GPU, CLIP 4,9 GB na CPU. Não medido |
| SD 3.5 Large | acima de 18 GB em BF16; 11 GB em FP8 TensorRT ([NVIDIA](https://blogs.nvidia.com/blog/rtx-ai-garage-gtc-paris-tensorrt-rtx-nim-microservices/)) | só com o narrador pausado, e o FP8 ainda passa dos 5,4 GB |
| SD 3.5 Medium | o cartão não publica GB ([Hugging Face](https://huggingface.co/stabilityai/stable-diffusion-3.5-medium)); um guia cita ~9,9 GB | não trato como cabe |
| HiDream-I1-Full | estimativa de terceiros: FP8 ~19 GB, INT4 ~9,6 GB ([Aquanode](https://www.aquanode.io/models/hidream-ai/hidream-i1-full)) | só com o narrador pausado, e olhe lá |
| Chroma1-HD | BF16 acima de 19 GB mais T5 ([relato no lançamento do Comfy](https://www.reddit.com/r/StableDiffusion/comments/1kc7jwq/chroma_is_now_officially_implemented_in_comfyui/)) | só com o narrador pausado |
| Qwen-Image (20B) | FP8 ~24 GB em 1024 ([WillItRunAI](https://willitrunai.com/image-models/qwen-image)) | não cabe na L4 |
| Qwen-Image-2.1 | artigo: 7B, 15 GB ([Medium](https://medium.com/data-science-collective/qwen-image-2-1-is-the-best-local-image-model-in-2026-the-download-is-33-gb-6e8a23001e3a)) | só com o narrador pausado |
| Z-Image-Turbo | 6B, “cabe folgado em 16 GB”, fotorrealismo ([Hugging Face](https://huggingface.co/Tongyi-MAI/Z-Image-Turbo)) | só com o narrador pausado |

Não achei um teste publicado de rosto/pele contra o epiCRealism XL. O que o cartão do Z-Image-Turbo afirma é fotorrealismo e que o Turbo empata ou passa “concorrentes de ponta”, em 8 avaliações. O que a BFL afirma do FLUX 3 Image Klein (15 out 2026) é o maior Elo interno entre modelos abertos, acima do Ideogram V4.5 e do FLUX.2 pro, com build INT8 em 8 GB, via ComfyUI ou Diffusers.

## Clipe de 2–4 s

Matrix-Game 4.0 continua fora: só repinta três jogos low-poly.

| Modelo | O que a fonte publica | Ao lado do narrador |
| --- | --- | --- |
| Wan 2.1 T2V-1.3B | **8,19 GB**, 5 s, 480p, ~4 min numa RTX 4090 sem quantizar ([GitHub](https://github.com/Wan-Video/Wan2.1)) | não cabe. 8,19 > 5,4. Cabe com o narrador pausado |
| Wan 2.1 14B | dezenas de GB | não cabe nem com o narrador pausado |
| CogVideoX-2B | SAT FP16 **18 GB**; diffusers “a partir de 4 GB”, no mesmo cartão que cita `enable_model_cpu_offload()`. Clipe de 6 s ([Hugging Face](https://huggingface.co/zai-org/CogVideoX-2b)) | os 18 GB não cabem. Os 4 GB são offload, não uma folga medida aqui |
| SVD | um apanhado cita 16 GB mínimos ([aifreeforever](https://aifreeforever.com/blog/open-source-ai-video-models-free-tools-to-make-videos)) | não cabe |
| HunyuanVideo 1.0 | 60–80 GB ([VRLA](https://vrlatech.com/best-workstation-for-ai-video-generation-in-2026/)) | não cabe na L4 |
| HunyuanVideo 1.5 | ~24 GB, ou ~14 GB com offload ([InsiderLLM](https://insiderllm.com/guides/local-ai-video-generation/)) | só com o narrador pausado, no modo com offload |
| LTX-2.5 | 10 s medidos em dois GB200 ([VentureBeat](https://venturebeat.com/technology/ltx-2-5-can-generate-a-10-second-ai-video-from-an-image-in-just-6-8-seconds-on-nvidia-superchips-and-its-open-weights)) | não é carga de L4 |

Um guia atribui 4–6 GB ao Wan 2.1 1.3B em GGUF 480p ([WillItRunAI](https://willitrunai.com/blog/video-generation-gpu-guide-2026)). É estimativa de terceiros, não o README, e não foi medida aqui. Não uso esse número para dizer que o clipe cabe ao lado do narrador.

## O que trocar

**Stills.** O provider principal usa **FLUX.2 Klein 4B distilled** (`engine=flux2_klein`),
com FP8, encoder `qwen_3_4b`, VAE `flux2-vae`, 4 passos, CFG 1 e até 4 referências.
O A/B real em 1344×768 mediu 27,99 s frio e 6,32 s aquecido, contra 245,26 s e
80,03 s do epiCRealism em 20 passos. FineView Qwen Image 2.1 (`engine=qwen`) e
SDWebUI/epiCRealism continuam instalados para rollback. Retratos usam a âncora
canônica e fotos como pixels; similaridade de embeddings não define identidade.

O cliente monta o grafo (`POST /prompt`) porque o ComfyUI não devolve
`images[0]` em `/sdapi/v1/txt2img`; polling e download ficam em `/history` e
`/view`. InvokeAI e SwarmUI falam outra API e não têm cliente no app.

O prompt upsampler oficial fica de fora: o LLM do jogo já entrega o prompt
detalhado e outro modelo de texto ocuparia RAM/VRAM sem necessidade.

**Clipe.** O default é **LTXV 2B distilled**, 121 frames a 24 fps, 768×448 ou 448×768, 8 passos e CFG 1. Um `scene_clip` recente renderizou em 14,34 s, mas ficou 895,42 s na fila atrás de jobs menos urgentes; por isso `scene_clip` agora tem prioridade 3, antes de lotes, `card_assets` e retratos animados. Wan 2.2 TI2V continua opt-in. Narrador e voz não são pausados.

## Fontes

- [BFL — FLUX 3 Image Klein](https://bfl.ai/blog/flux-3-image-klein) (15 out 2026)
- [The Decoder — resumo do mesmo lançamento](https://the-decoder.com/black-forest-labs-open-weights-image-model-flux-3-image-klein-runs-on-consumer-gpus-with-just-8-gb-of-vram) (15 out 2026)
- [Thunder Compute — Flux no ComfyUI](https://www.thundercompute.com/blog/flux-comfyui-ai-image-generation) (out 2026)
- [NVIDIA — FLUX.2](https://blogs.nvidia.com/blog/rtx-ai-garage-flux-2-comfyui/)
- [ComfyUI — FLUX.2 Klein 4B](https://docs.comfy.org/tutorials/flux/flux-2-klein)
- [BFL — FLUX.2 Klein 4B oficial](https://huggingface.co/black-forest-labs/FLUX.2-klein-4B)
- [NVIDIA — SD 3.5 e TensorRT](https://blogs.nvidia.com/blog/rtx-ai-garage-gtc-paris-tensorrt-rtx-nim-microservices/) (12 jun 2025)
- [Hugging Face — SD 3.5 Medium](https://huggingface.co/stabilityai/stable-diffusion-3.5-medium)
- [Hugging Face — Z-Image-Turbo](https://huggingface.co/Tongyi-MAI/Z-Image-Turbo)
- [Hugging Face — CogVideoX-2B](https://huggingface.co/zai-org/CogVideoX-2b)
- [GitHub — Wan2.1](https://github.com/Wan-Video/Wan2.1)
- [GitHub — Forge](https://github.com/lllyasviel/stable-diffusion-webui-forge)
- [GitHub — Forge Neo](https://github.com/Haoming02/sd-webui-forge-classic)
- [GitHub — panel-foundry, cliente `/sdapi/v1/txt2img`](https://github.com/cainezen/panel-foundry)
- [VentureBeat — LTX-2.5](https://venturebeat.com/technology/ltx-2-5-can-generate-a-10-second-ai-video-from-an-image-in-just-6-8-seconds-on-nvidia-superchips-and-its-open-weights)
- [InsiderLLM — vídeo local, HunyuanVideo 1.5](https://insiderllm.com/guides/local-ai-video-generation/)
- [VRLA — VRAM de vídeo em 2026](https://vrlatech.com/best-workstation-for-ai-video-generation-in-2026/)
- [WillItRunAI — Qwen-Image](https://willitrunai.com/image-models/qwen-image) e [guia de vídeo](https://willitrunai.com/blog/video-generation-gpu-guide-2026)
- [Medium — Qwen-Image-2.1](https://medium.com/data-science-collective/qwen-image-2-1-is-the-best-local-image-model-in-2026-the-download-is-33-gb-6e8a23001e3a)
- [Aquanode — HiDream-I1-Full](https://www.aquanode.io/models/hidream-ai/hidream-i1-full) (estimativa)
- [Reddit — Chroma no ComfyUI](https://www.reddit.com/r/StableDiffusion/comments/1kc7jwq/chroma_is_now_officially_implemented_in_comfyui/)
- [aifreeforever — apanhado de modelos de vídeo](https://aifreeforever.com/blog/open-source-ai-video-models-free-tools-to-make-videos)
