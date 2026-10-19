# Imagem mais realista e vídeo curto na L4

Leitura única de `nvidia-smi` em 2026-10-18 23:12 (-03), em `caio@vm.groupabz.com`. Nenhum peso foi baixado. A1111, llama-server e o app principal ficaram como estavam.

## Veredito

**Go:** continuar no `epiCRealism XL CrystalClear`. É o checkpoint fotorrealista mais capaz que já está no disco e que cabe ao lado do narrador. O slot livre para imagem, com o narrador no ar, é 5 542 MiB.

**Later, só com o llama-server fora da placa** (cerca de 19,5 GiB se o A1111 também sair; voz e embeddings ficam):

- Imagem: [FLUX 3 Image Klein](https://the-decoder.com/black-forest-labs-open-weights-image-model-flux-3-image-klein-runs-on-consumer-gpus-with-just-8-gb-of-vram) (15/10/2026, 5,6B, INT8 a partir de 8 GB).
- Vídeo: [Wan 2.1 T2V-1.3B](https://github.com/Wan-Video/Wan2.1), 8,19 GB, clipe de 5 s em 480p.

**No ao lado do narrador:** Flux, SD3, HiDream, Pony, SVD, LTX, CogVideoX, Wan e Hunyuan. Nenhum publica um pico que caiba em 5,4 GiB com folga, e a placa está com 26 MiB livres.

O ArcFace 0,04–0,26 de pessoa real mede retrato saído de txt2img, sem a foto da pessoa. epiCRealism XL e Juggernaut XL são SDXL fotorrealista. Trocar o checkpoint não faz a Selene continuar a mesma pessoa. O retrato pede a referência (img2img / likeness), que é outro trabalho e não entra neste arquivo.

## A placa nesta leitura

L4, 23 034 MiB no total, 22 540 MiB em uso, 26 MiB livres, GPU a 0%.

| Processo | Desde (-03) | VRAM |
|---|---|---|
| llama-server, Qwen3.6-35B IQ2_M, porta 1234 | 18/10 16:17:06 | 14 472 MiB |
| llama-server de embeddings, Qwen3-Embedding-0.6B, `--n-gpu-layers 0`, porta 8091 | 18/10 16:54:26 | 698 MiB |
| voz, `audio_server.py` | 09/10 15:32 | 1 426 MiB |
| A1111, `launch.py` | 18/10 20:31:50 | 5 516 MiB |
| desktop Cosmic | — | ~269 MiB |

O A1111 está com `--api --medvram --no-half-vae --opt-sdp-attention` (`webui-user.sh`). Os 5 516 MiB são o checkpoint residente, com a GPU ociosa. Um txt2img 1344×768 que deixou ~21 MiB livres é o mesmo teto: não há um segundo orçamento escondido.

Orçamento:

- Imagem no lugar do A1111, narrador no ar: 5 516 + 26 = **5 542 MiB (5,4 GiB)**.
- Só o narrador fora, A1111 ainda residente: **14 498 MiB (14,2 GiB)** livres.
- Narrador e A1111 fora, voz e embeddings ficam: 26 + 14 472 + 5 516 = **20 014 MiB (19,5 GiB)**.

Os 698 MiB dos embeddings são contexto CUDA. O processo subiu com `--n-gpu-layers 0` e a VRAM continua ocupada.

## Checkpoints já em `/home/caio/sd-webui/models`

| Arquivo | Bytes | Tamanho |
|---|---:|---:|
| `Stable-diffusion/epicrealismXL_vxviiCrystalclear.safetensors` | 6 938 062 130 | 6,46 GiB |
| `Stable-diffusion/juggernautXL_ragnarok.safetensors` | 7 105 350 162 | 6,62 GiB |
| `Stable-diffusion/dreamshaper_8.safetensors` | 2 132 625 894 | 1,99 GiB |
| `VAE/sdxl-vae-fp16-fix.safetensors` | 334 641 162 | 319 MiB |

Juggernaut XL está no disco. O arquivo é `juggernautXL_ragnarok.safetensors`. O runbook da fase 3 cita `juggernautXL_v9Rundiffusion.safetensors`; esse nome não está na pasta.

## Imagem

| Opção | Veredito | Por quê |
|---|---|---|
| epiCRealism XL CrystalClear | **go** | Já carregado. SDXL fotorrealista. É o melhor peso que esta pasta tem para foto, e o pico atual já usa o cartão inteiro. |
| Juggernaut XL Ragnarok | **later** | Mesma classe SDXL, já no disco, mesmo slot do A1111 (`--medvram`). Serve para um teste cego de gosto. Não é uma arquitetura nova. |
| DreamShaper 8 | **no** | 1,99 GiB, tamanho de checkpoint SD 1.5. Os dois XL passam de 6 GiB. Fica abaixo do que já roda. |
| Pony XL | **no** | Finetune de SDXL para anime, cartoon e furry ([guia](https://stable-diffusion-art.com/pony-diffusion-v6-xl/), [VRAM igual à do SDXL](https://willitrunai.com/blog/pony-diffusion-v6-xl-vram-requirements)). O slot aguentaria. O look fica menos fotográfico que o epiCRealism. |
| Flux.1 dev / schnell, FP16 | **no** | 12B. FP16 fica em 24–33 GB, acima da L4 inteira ([tabela 2026](https://localaimaster.com/blog/flux-vram-requirements-by-gpu)). |
| Flux.1 FP8 ou GGUF Q5–Q8 | **later** | FP8 e Q8 em 12–16 GB: cabe nos 19,5 GiB, com o narrador fora. Q4 é citado em 6–8 GB, e uma tabela marca Q4_K_S em 6,8 GB ([willitrun](https://willitrunai.com/blog/how-to-run-flux-locally)). Isso passa dos 5,4 GiB. O dev é licença não comercial; o schnell é Apache-2.0, com a mesma conta de VRAM. |
| FLUX 3 Image Klein (5,6B) | **later** | 15/10/2026. INT8 a partir de 8 GB; o anúncio fala em 8 GB ([The Decoder](https://the-decoder.com/black-forest-labs-open-weights-image-model-flux-3-image-klein-runs-on-consumer-gpus-with-just-8-gb-of-vram), [GlobeNewswire via Yahoo](https://finance.yahoo.com/technology/ai/articles/black-forest-labs-expands-access-150500027.html)). Cabe com o narrador fora. O 8 GB é o mínimo publicado, não um 4K medido nesta L4. Licença Kommunity, comercial abaixo de US$ 5 milhões/ano. Precisa de outro runner. O A1111 atual não carrega esse peso. |
| SD3 Medium | **no** | Mínimo publicado de 5 GB, recomendado 16 GB ([VentureBeat](https://venturebeat.com/technology/stability-ai-brings-new-size-to-image-generation-with-stable-diffusion-medium)). O mínimo empata com o slot inteiro. Não há medida de que supere o epiCRealism em retrato. |
| SD3.5 Large | **no** | 8B. O card manda bf16 na GPU e não publica o pico ([card](https://huggingface.co/stabilityai/stable-diffusion-3.5-large)). Com o T5-XXL, o bf16 encosta ou passa dos 19,5 GiB. |
| HiDream-I1 Full (17B) | **no** | O card baixa o Llama-3.1-8B como encoder de texto ([card](https://huggingface.co/HiDream-ai/HiDream-I1-Full)). bf16/fp16 não rodou em GPU de 24 GB num benchmark ([InstaSD](https://www.instasd.com/post/hidream-performance-benchmarks-in-comfyui)). O NF4 pede 16 GB ([card NF4](https://huggingface.co/azaneko/HiDream-I1-Fast-nf4)): ainda acima dos 5,4 GiB, e apertado nos 19,5 GiB. |

## Vídeo curto

Nenhum cabe ao lado do narrador. O que cabe, cabe com o llama-server descarregado.

| Opção | Veredito | Clipe | VRAM publicada |
|---|---|---|---|
| Stable Video Diffusion XT | **no** | 25 frames, 576×1024, até 4 s, sem texto | O card não publica VRAM. O próprio card diz que rostos em geral podem sair mal ([card](https://huggingface.co/stabilityai/stable-video-diffusion-img2vid-xt)). |
| LTX-Video 2B 0.9.8 | **later** | melhor abaixo de 720×1280 e de 257 frames ([card](https://huggingface.co/Lightricks/LTX-Video)) | O card não dá GB. Uma tabela de terceiro põe o FP8 em 12 GB para 512×512 e 25 frames ([willitrun](https://willitrunai.com/video-models/ltx-video-2-3)). Acima dos 5,4 GiB; dentro dos 19,5 GiB se o número se confirmar. |
| LTX-2.5 | **later** | material de lançamento: 10 s, 720p | Mínimo de 16 GB. Os 6,8 s citados são em duas GB200, não nesta L4 ([VentureBeat, 11/08/2026](https://venturebeat.com/technology/ltx-2-5-can-generate-a-10-second-ai-video-from-an-image-in-just-6-8-seconds-on-nvidia-superchips-and-its-open-weights)). |
| CogVideoX-2B | **later** | 6 s, 720×480, 49 frames, 8 fps | SAT em GPU única: 18 GB. O "4 GB minimum" do diffusers é offload ([README](https://github.com/zai-org/CogVideo)). 18 GB dentro de 19,5 GiB deixa pouca folga. |
| CogVideoX-5B e 1.5-5B | **no** | 6 s em 720×480; a 1.5 vai a 5–10 s em 1360×768 | SAT 26 GB e 76 GB. O mínimo baixo do diffusers é offload. |
| Wan 2.1 T2V-1.3B | **later** | 5 s, 480p | **8,19 GB**. Cerca de 4 min numa RTX 4090, sem quantização ([README](https://github.com/Wan-Video/Wan2.1)). É o vídeo que cabe com folga nos 19,5 GiB. O tempo nesta L4 não foi medido. |
| Wan 2.1 14B | **no** | 480p e 720p | Na GPU única o README manda `--offload_model`. Pesos bf16 de 14B ficam perto de 28 GB, acima dos 19,5 GiB. |
| HunyuanVideo (13B) | **no** | 129 frames; 544×960 ou 720p | Pico de 45 GB e 60 GB ([README](https://github.com/Tencent-Hunyuan/HunyuanVideo)). |
| HunyuanVideo 1.5 (8,3B) | **later** | 720p, 121 frames, com offload | Mínimo de 14 GB com offload ([README](https://github.com/Tencent-Hunyuan/HunyuanVideo-1.5)). Pico de 13,6 GB no relato, com offload e VAE tiling ([arXiv](https://arxiv.org/html/2511.18870v1)). Cabe com o narrador fora, com menos folga que o Wan 1,3B. |

Matrix-Game 4.0 continua fora desta escolha. O julgamento dele está em `gameplay_unificado_e_matrix_game.md`: re-renderiza três jogos low-poly e não gera a cena da história.

## Próximo passo

Deixar o epiCRealism XL no A1111. O rosto de pessoa real segue pela foto de referência, no trabalho de likeness, sem baixar modelo e sem reiniciar serviço. Juggernaut XL Ragnarok pode entrar depois num teste cego, no mesmo slot, porque o arquivo já está no disco.

Vídeo e Flux ficam para uma janela em que o llama-server não esteja na placa. Nessa janela, o primeiro vídeo com folga é o Wan 2.1 de 1,3B. O primeiro salto de imagem é o FLUX 3 Image Klein, noutro runner, no lugar do A1111.

## Fontes

- Leitura de `nvidia-smi` e da pasta `models` na VM, 2026-10-18 23:12 (-03)
- [FLUX.1 VRAM por precisão](https://localaimaster.com/blog/flux-vram-requirements-by-gpu) (2026)
- [Flux local, tabela FP16 até Q4](https://willitrunai.com/blog/how-to-run-flux-locally)
- [FLUX 3 Image Klein, 8 GB](https://the-decoder.com/black-forest-labs-open-weights-image-model-flux-3-image-klein-runs-on-consumer-gpus-with-just-8-gb-of-vram) (15/10/2026)
- [Anúncio FLUX 3 Image Klein](https://finance.yahoo.com/technology/ai/articles/black-forest-labs-expands-access-150500027.html) (15/10/2026)
- [SD3 Medium, 5 GB](https://venturebeat.com/technology/stability-ai-brings-new-size-to-image-generation-with-stable-diffusion-medium)
- [SD3.5 Large](https://huggingface.co/stabilityai/stable-diffusion-3.5-large)
- [HiDream-I1-Full](https://huggingface.co/HiDream-ai/HiDream-I1-Full)
- [HiDream-I1 NF4, 16 GB](https://huggingface.co/azaneko/HiDream-I1-Fast-nf4)
- [HiDream em 24 GB](https://www.instasd.com/post/hidream-performance-benchmarks-in-comfyui)
- [Pony Diffusion v6 XL](https://stable-diffusion-art.com/pony-diffusion-v6-xl/)
- [Pony, mesma VRAM do SDXL](https://willitrunai.com/blog/pony-diffusion-v6-xl-vram-requirements)
- [SVD XT](https://huggingface.co/stabilityai/stable-video-diffusion-img2vid-xt)
- [LTX-Video](https://huggingface.co/Lightricks/LTX-Video)
- [LTX-Video 2B, tabela de terceiro](https://willitrunai.com/video-models/ltx-video-2-3)
- [LTX-2.5, mínimo 16 GB](https://venturebeat.com/technology/ltx-2-5-can-generate-a-10-second-ai-video-from-an-image-in-just-6-8-seconds-on-nvidia-superchips-and-its-open-weights) (11/08/2026)
- [CogVideoX](https://github.com/zai-org/CogVideo)
- [Wan 2.1](https://github.com/Wan-Video/Wan2.1)
- [HunyuanVideo](https://github.com/Tencent-Hunyuan/HunyuanVideo)
- [HunyuanVideo 1.5](https://github.com/Tencent-Hunyuan/HunyuanVideo-1.5)
- [HunyuanVideo 1.5, pico de 13,6 GB](https://arxiv.org/html/2511.18870v1)
