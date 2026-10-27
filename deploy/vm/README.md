# Deploy na VM (vm.groupabz.com)

O backend é FastAPI + SQLite **persistente** e serve também o build do frontend
(SPA). Este documento descreve a instalação que roda hoje na VM.

## Como está hoje

| Item | Valor |
|---|---|
| Diretório do app | `/home/caio/aidungeon` (código copiado do checkout `/home/caio/aidungeon-git` pelo `tools/deploy.sh`) |
| Serviço | `aidungeon.service` (usuário `caio`), uvicorn na porta **2122** |
| Python | venv em `/home/caio/aidungeon/backend/venv` |
| Segredos | `/home/caio/aidungeon/.env` (lido via `AIDUNGEON_ENV_PATH`) |
| Banco | `/home/caio/aidungeon/data/aidungeon.db` |
| Imagens | `/home/caio/aidungeon/data/images/` |
| Frontend | `/home/caio/aidungeon/frontend/dist` (servido pelo FastAPI) |
| HTTPS | nginx `aidungeon-https.conf`: `https://vm.groupabz.com:8443` → `127.0.0.1:2122` (Let's Encrypt); access log sem query string, porque o EventSource do SSE leva o token em `?token=` |
| LLM | `llama-server.service` na porta 1234, 5 slots (seção abaixo) |
| Watchdog | `slot-watchdog.timer` (a cada 10 s) reinicia o llama-server com slot preso |
| Embeddings | `llama-embeddings.service` (`--n-gpu-layers 0`, ~700 MiB de VRAM do CUDA) na porta 8091 |
| Imagem | `sd-webui-forge.service` na porta 7861, epiCRealism XL CrystalClear; ComfyUI/FineView permanece instalado como provider opcional |
| Voz | `abz-voice-audio.service` em `https://vm.groupabz.com/voice/v1`. O jogo pede `omnivoice` (OmniVoice Q8 na GPU). Supertonic permanece para `tts-1` e `supertonic-3`. Drama-3 não cabe nesta L4: `drama3.md` |
| Staging | `aidungeon-staging.service`, `127.0.0.1:2123`, código em `/home/caio/aidungeon-staging` |
| Backups | `/home/caio/aidungeon-backups/` (banco e código de antes de cada deploy) |

Unit atual do app (`/etc/systemd/system/aidungeon.service`):

```ini
[Unit]
Description=AIDungeon Local (FastAPI + SPA)
After=network.target

[Service]
Type=simple
User=caio
WorkingDirectory=/home/caio/aidungeon/backend
Environment=AIDUNGEON_ENV_PATH=/home/caio/aidungeon/.env
ExecStart=/home/caio/aidungeon/backend/venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 2122
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
```
## Desempenho

- O app usa contexto de 32768 tokens, escala dinâmica 1.0 e DRY nos últimos 512 tokens. O NPC gate é ignorado quando não existe NPC vivo e, quando necessário, tem teto de 400 tokens de saída.
- O prompt textual de retratos é preparado antes de entrar na fila do worker de GPU. O polling do ComfyUI usa 0,25 s; o piso de configuração é 0,2 s.
- O cliente de voz mantém conexões HTTP por endpoint e faz upload do RVQ de uma voz clonada uma vez por processo, validando o hash local antes do PUT.
- Em `/home/caio/Desktop/abz-voice-local/audio_server.py`, o streaming OmniVoice precisa usar `proc.stdout.read1(65536)`. `read(65536)` segura PCM até encher o buffer ou encerrar o processo. No benchmark M1 com 169 caracteres, `read1` antecipou o primeiro PCM de 10,709 s para 8,814 s e o WAV completo de 10,974 s para 9,059 s, sem mudar modelo, texto ou bytes PCM.


Esta versão também aplica as migrations `035_story_starts.sql` (inícios),
`036_dm_mode.sql` (fila do DM), `037_publications.sql` (catálogo/remix) e
`038_mods.sql` (mods versionados). O smoke deve terminar em `user_version=38`
e `integrity=ok`. A cópia jogável de uma publicação não contém actions,
jogadores ou memórias da origem. Não usar `--with-db` no deploy normal: as
migrations preservam os saves e o backup automático já é feito antes do boot.

## Atualizar o código (G9)

Sempre passa pelo staging antes (seção abaixo).

O código chega por git. O repositório é privado e a VM não tem credencial do
GitHub, então o checkout `/home/caio/aidungeon-git` tem como `origin` um
repositório bare na própria VM (`/home/caio/aidungeon.git`), que recebe
`git push` por SSH de quem faz o deploy. Com uma deploy key do GitHub na VM,
basta `git -C /home/caio/aidungeon-git remote set-url origin
git@github.com:Caiolinooo/aidungeon-local.git`.

Os diretórios dos apps continuam onde estão: `.env`, `data/` e o venv ficam
neles, porque staging, watchdog e ferramentas usam esses caminhos fixos. O
`tools/deploy.sh` copia só o código do checkout (`backend/app`, `memory`,
`migrations`, `schema.sql`, `requirements.txt`, `exporter.py`, `importer.py`
e `config/`).

```bash
# instalação (uma vez)
ssh caio@vm.groupabz.com git init --bare /home/caio/aidungeon.git
git push ssh://caio@vm.groupabz.com/home/caio/aidungeon.git <branch>     # na máquina local
ssh caio@vm.groupabz.com git clone /home/caio/aidungeon.git /home/caio/aidungeon-git

# cada deploy
git push ssh://caio@vm.groupabz.com/home/caio/aidungeon.git <branch>     # na máquina local
cd /home/caio/aidungeon-git                                               # na VM
tools/deploy.sh --target staging --ref origin/<branch>
tools/deploy.sh --target main --ref <commit testado no staging>
```

O `deploy.sh`:

1. com `--ref`, faz `fetch` e `checkout --detach` e reexecuta a versão nova
   de si mesmo (recusa checkout com mudança local);
2. no app principal, recusa se o jogador mexeu nos últimos 10 min
   (`--idle-min`, `--force` ignora);
3. faz backup do banco (API de backup do sqlite) e do código, com o `DEPLOYED`
   anterior, em `/home/caio/aidungeon-backups/deploy-<alvo>-<data>/`;
4. copia o código, roda `pip install` se o `requirements.txt` mudou (o venv é o
   do app principal, o staging usa o mesmo) e, só com `--frontend`, faz
   `npm ci && npm run build` no checkout e copia o `dist`. Sem a flag o
   `frontend/dist` do alvo fica como está;
5. grava `DEPLOYED` (commit, data, backup) no diretório do app, reinicia o
   serviço (as migrations rodam no boot, em `db.init_db`) e roda
   `tools/smoke_check.py`: 401 sem token, `GET /api/adventures` 200,
   `GET /api/health/llm` 200 e as migrations registradas no banco. LLM fora do
   ar só gera aviso;
6. smoke falhou: volta o código do backup, reinicia e sai com 1.

Rollback manual:

```bash
tools/deploy.sh --target main --rollback /home/caio/aidungeon-backups/deploy-main-<data>
tools/deploy.sh --target main --rollback /home/caio/aidungeon-backups/deploy-main-<data> --with-db
```

O rollback também salva o estado atual antes. As migrations só acrescentam
(colunas, tabelas e índices) e o código anterior roda no banco novo, então o
`--with-db` só é preciso se uma migration estragou dados; ele perde o que foi
gravado depois do backup. Deploy, rollback e o rollback automático foram
testados com um uvicorn local (`tools/README.md`, seção 2.4).

## Staging

Mesmo venv e mesmo `.env` do app, código e banco próprios, só em `127.0.0.1`.
Usa o mesmo llama-server e o mesmo Forge do app principal: teste fora do horário
de jogo. Unit em [`aidungeon-staging.service`](aidungeon-staging.service).

```bash
# na VM: renova o banco do staging com uma cópia do app principal (opcional)
S=/home/caio/aidungeon-staging; mkdir -p $S/data
python3 -c "import sqlite3,sys; s=sqlite3.connect('/home/caio/aidungeon/data/aidungeon.db'); d=sqlite3.connect(sys.argv[1]); s.backup(d)" $S/data/aidungeon.db
rsync -a /home/caio/aidungeon/data/images/ $S/data/images/
rsync -a /home/caio/aidungeon/frontend/dist/ $S/frontend/dist/
# e sobe o código novo
cd /home/caio/aidungeon-git && tools/deploy.sh --target staging --ref origin/<branch>
```

Aceite com `tools/replay_turn.py` (ver `tools/README.md`), rodando na própria VM:

```bash
python3 tools/replay_turn.py --env-file /home/caio/aidungeon/.env \
    --adventure dd5a6fa4c0394dd3832a1261778ef5a2 \
    --turn "do:saiam todos, quero apenas a clara aqui" --turn "say:clara? tudo bem?" \
    --see-during-turn
```

No app principal (2122), só chamadas que não gravam (estado, `/choices`,
`/api/health/llm`): o replay escreve turnos na história.

## Imagem: Forge Neo

Desde 19 out 2026 o app principal e o staging falam com o Forge Neo
(`Haoming02/sd-webui-forge-classic`, branch `neo`) em
`http://127.0.0.1:7861`. Código em `/home/caio/sd-webui-forge-neo`. Unit
[`sd-webui-forge.service`](sd-webui-forge.service). Checkpoint
`epicrealismXL_vxviiCrystalclear.safetensors` e VAE `sdxl-vae-fp16-fix.safetensors`,
apontados para a pasta do A1111 (`--forge-ref-a1111-home /home/caio/sd-webui`).
O corpo segue DPM++ 2M, scheduler Karras, CFG 5, 20 passos. No
`config.json` do Neo, `res_step` é 8: o retrato no ar é 800×720, e o
padrão 64 recusaria esses dois lados.

O Neo tirou o `--medvram`. No lugar: `--cuda-malloc --expandable-segments
--cuda-stream --tiled-conv2d 128` e `setting_allocated_vram` 1.0. Com 0,25 o
Neo reservava ~17 GB e via 0 MB úteis (o modelo inteiro ia para a RAM). Com
1.0 ele ocupa o buraco livre: pico medido ~4,5 GB no 512 e o 800×720 em 20
passos voltou HTTP 200 em 24 s, com o `llama-server` ainda em ~14,5 GB e
`GET /health` 200.

Não instalados, porque não cabem ao lado do narrador: Z-Image-Turbo (o README
do Neo não publica quantização que caiba em 5,5 GB; o cartão pede 16 GB) e
Wan 2.1. O Forge/epiCRealism voltou a ser o provider ativo em 27 out 2026; o FineView no ComfyUI é opcional.

Rollback: parar o Forge, subir o A1111 e devolver a URL.

```bash
sudo systemctl disable --now sd-webui-forge.service
sudo systemctl enable --now sd-webui.service
# image.sdwebui_base_url nos dois bancos: "http://127.0.0.1:7860"
```

Backup desta troca: `/home/caio/aidungeon-backups/forge-20261019T025223Z`.

## Provider opcional: FineView Qwen Image 2.1 no ComfyUI

O cliente `image.provider = "comfyui"` em `backend/app/image_client.py` fala a
API do ComfyUI: `POST /prompt`, polling em `GET /history/{id}` e download por
`GET /view`. O grafo usa `UNETLoader`, CLIP `qwen_image` e
`TextEncodeQwenImage21` nas identidades, `ModelSamplingAuraFlow` shift 1.31,
`ManualSigmas` oficial com oito passos e `SamplerCustom` euler. O CLIP fica em CPU.

Arquivos e hashes obrigatórios:

| Arquivo em `/home/caio/ComfyUI/models/` | Bytes | SHA256 |
|---|---:|---|
| `diffusion_models/fineviewQWEN_qw21TurboFixed_3299367.safetensors` | 7116801326 | `7ba6c0774c233540ef6ea0ea3971acf3cbf403f70bd3672af57eaca00c33578e` |
| `text_encoders/qwen3vl_8b_int8_convrot.safetensors` | 9350798360 | `8bfd0f6e12abf2d2d697ecc888e5e90b0d6741d6708f05799f53afa560452e8f` |
| `vae/texture_fix_vae_for_qwen_image_2.1_bf16.safetensors` | 675509688 | `36f56608b02077a50a08ea3dbcdb88351298ceba49948f41f9f87186378e6908` |

Downloads (o FineView na Civitai exige `CIVITAI_API_KEY`):

```bash
cd /home/caio/ComfyUI
curl -fL -H "Authorization: Bearer $CIVITAI_API_KEY" \
  -o models/diffusion_models/fineviewQWEN_qw21TurboFixed_3299367.safetensors.part \
  'https://civitai.com/api/download/models/3407540?fileId=3299367'
curl -fL \
  -o models/vae/texture_fix_vae_for_qwen_image_2.1_bf16.safetensors.part \
  https://huggingface.co/madebyollin/texture-fix-vae-for-qwen-image-2.1/resolve/main/texture_fix_vae_for_qwen_image_2.1_bf16.safetensors
curl -fL \
  -o models/text_encoders/qwen3vl_8b_int8_convrot.safetensors.part \
  https://huggingface.co/Comfy-Org/Qwen-Image-2.1/resolve/main/text_encoders/qwen3vl_8b_int8_convrot.safetensors
# Conferir os três SHA256 da tabela antes de remover o sufixo .part.
```

A unit é `deploy/vm/comfyui.service`, instalada como
`/etc/systemd/system/comfyui.service`. O ComfyUI escuta somente em
`127.0.0.1:8188`. Antes do primeiro retrato, `GET /system_stats` deve responder
e os três arquivos precisam ter os hashes acima. `clip_device = "cpu"` mantém o
Qwen3-VL 8B INT8 fora da VRAM; UNET FP8 e VAE dividem a L4 com narrador e voz.

Antes de `systemctl start comfyui`, verifique `ss -ltnp 'sport = :8188'`. Se um
ComfyUI manual já estiver escutando, encerre esse processo primeiro; a unit não
deve ficar reiniciando por `Port 8188 is already in use` enquanto a API responde
na outra instância. Depois do start, confirme `systemctl is-active comfyui` e
`/system_stats` novamente.

O provider efetivo pode ser trocado via `PUT /api/settings/image` ou nos dois
bancos, somente depois dos hashes e do smoke do grafo:

```bash
for DB in /home/caio/aidungeon/data/aidungeon.db \
          /home/caio/aidungeon-staging/data/aidungeon.db; do
  sqlite3 "$DB" "INSERT INTO settings(key,value) VALUES('image.provider','\"comfyui\"') \
    ON CONFLICT(key) DO UPDATE SET value=excluded.value;"
done
```

Para ativar o FineView, trocar `image.provider = "comfyui"`, parar o Forge e
subir `comfyui.service` em `127.0.0.1:8188`. No rollback, devolver `sdwebui`,
parar o ComfyUI e subir o Forge. Não reiniciar llama-swap nem
`abz-voice-staging`. Forge e FineView não devem medir VRAM ao mesmo tempo.

## Vídeo real: Wan 2.2 TI2V 5B Q8

O job `scene_clip` anima uma imagem já salva e devolve MP4 de 121 frames a
24 fps (aproximadamente 5 s), em 1280×704 ou 704×1280. O Wan é um modelo
temporal separado: os checkpoints Moody, DesireX e Stable Yogi citados nas
opções do Civitai são de imagem; MP4 na galeria não muda isso.

Instalação reproduzível, com hashes e revisões fixadas:

```bash
bash /home/caio/aidungeon/deploy/vm/install_wan_video.sh
sudo systemctl restart comfyui
```

O instalador usa `Wan2.2-TI2V-5B-Q8_0.gguf` (5,40 GB), UMT5 FP8 (6,74 GB),
VAE Wan 2.2 (1,41 GB) e ComfyUI-GGUF. Total: 13,55 GB, contra 18,14 GB do
workflow fp16. Não instalar os três checkpoints Civitai junto com ele sem
antes liberar disco: os três somam outros 15,47 GiB e ainda exigem encoders.

Antes do clipe, o worker descarrega o checkpoint do Forge local. O ComfyUI
recebe `/free` antes e depois do job. Assim narrador e voz continuam ativos,
mas imagem e vídeo não executam ao mesmo tempo. Não enviar prompts de outro
cliente ao mesmo ComfyUI durante o clipe: `/interrupt` e `/free` são globais.
O próximo job SDXL recarrega o Forge sob demanda.

Smoke do executor, sem gerar uma imagem:

```bash
curl -fsS http://127.0.0.1:8188/system_stats
```

## A1111: memória

Sem tcmalloc o A1111 crescia até ~20 GB de RSS e empurrava 11 GB para o swap.
O `webui.sh` faz `LD_PRELOAD` do tcmalloc sozinho quando a lib existe:

```bash
sudo apt-get install -y libtcmalloc-minimal4t64
journalctl -u sd-webui | grep -i tcmalloc     # "execute LD_PRELOAD=...libtcmalloc_minimal.so.4"
```

Com ele, o anon fica em ~9 GB e o drop-in
[`sd-webui-memory.conf`](sd-webui-memory.conf) (em
`/etc/systemd/system/sd-webui.service.d/memory.conf`) segura o resto:
`MemoryHigh=12G`, `MemoryMax=14G`, `MemorySwapMax=2G`, `Restart=always`.
Medido na G1: 1024² em 20 passos leva 37,7 s a frio e 15,2 s quente, sem swap e
sem evento `max`/`oom` em `memory.events`. Sem tcmalloc o render trava sob o
`MemoryHigh`; se o pacote sumir, tire o drop-in até reinstalar.

## llama-server (G2)

Unit `/etc/systemd/system/llama-server.service` (root, modo 600 enquanto a
chave estiver na linha de comando; a G8 troca por `--api-key-file`). Build
`b10281-c8064719f` do fork `Indras-Mirror/llama.cpp-turboq-mtp`:

```ini
ExecStart=/home/caio/llama.cpp-turboq-mtp/build/bin/llama-server --host 0.0.0.0 --port 1234 \
  -t 8 --threads-batch 16 -np 5 --kv-unified -cram 0 --ctx-checkpoints 8 --metrics \
  -c 262144 -ctk q8_0 -ctv q8_0 -b 2048 -ub 1024 --api-key <chave> -rea auto -fa on \
  --tools all --jinja -m /home/caio/Qwen3.6-35B-A3B-Uncensored-HauhauCS-Aggressive-IQ2_M.gguf \
  --mmproj /home/caio/mmproj-Qwen3.6-35B-A3B-Uncensored-HauhauCS-Aggressive-f16.gguf \
  --n-gpu-layers 99 -ncmoe 24 --no-mmap --image-min-tokens 1024
```

| Flag | Por quê |
|---|---|
| `-np 5 --kv-unified` | slots 0–3 do app (contrato C1) e o 4 para clientes sem `id_slot`; o KV unificado divide os 262144 tokens entre eles |
| `-cram 0` | sem prompt cache no servidor: era ele que disparava o livelock #28280; também libera ~7 GB de RAM |
| `--ctx-checkpoints 8` | o padrão é 32 checkpoints de ~63 MiB por slot |
| `-ub 1024` | prompt 1290 tok/s contra 913 com 512; o 2048 (1596 tok/s) estoura o pico de VRAM com SDXL até a G6 |
| `--metrics` | `/metrics` para a G10 |

Medições e decisões em `docs/vm_runbooks/phase5_slots_record.md`. Backups da
unit: `llama-server.service.v090.bak` (original) e `.g2-np2.bak` (`-np 2`).
Rollback: copiar o backup por cima, `sudo systemctl daemon-reload` e
`sudo systemctl restart llama-server` (~30 s para carregar o modelo). Reinicie
só com os slots ociosos (`GET /slots`), fora do horário de jogo.

## Embeddings e VRAM

`llama-embeddings.service` roda com `--n-gpu-layers 0`, mas o CUDA reserva
~700 MiB (646 a 698 MiB medidos) e faz os lotes na GPU. Com as duas linhas
abaixo fica em 0 MiB:

```ini
Environment=CUDA_VISIBLE_DEVICES=
ExecStart=... --n-gpu-layers 0 --device none ...
```

Essa variante ficou no ar de 16:16 a 16:54 de 18/10, durante benchmarks e o
primeiro soak, e gastou 15,6 min de CPU em 38 min; a original tinha gastado
6,6 min em 2 dias de uso leve do jogador. A carga não foi a mesma, mas é CPU
que os experts do llama-server (`-ncmoe 24`) também usam. A unit voltou para
a original (igual a `llama-embeddings.service.g7.bak`); a variante está em
`llama-embeddings.service.devnone` e a G7 compara as duas com a mesma carga,
junto com o `-t` do llama-server.

## Watchdog de slots

Reinicia o llama-server quando um slot fica preso (#28280). Instalação:

```bash
# na máquina local
scp tools/slot_watchdog.py deploy/vm/slot-watchdog.service deploy/vm/slot-watchdog.timer \
    caio@vm.groupabz.com:/tmp/
# na VM
sudo install -o root -g root -m 755 /tmp/slot_watchdog.py /usr/local/sbin/slot_watchdog.py
sudo install -o root -g root -m 644 /tmp/slot-watchdog.service /tmp/slot-watchdog.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo /usr/bin/python3 /usr/local/sbin/slot_watchdog.py --env-file /home/caio/aidungeon/.env \
    --state-dir /tmp/wd-dry --dry-run && sudo cat /tmp/wd-dry/status.json   # lê os 5 slots?
sudo systemctl enable --now slot-watchdog.timer
```

Estado em `/var/lib/slot-watchdog/status.json`, que o app mostra em
`GET /api/health/llm` (`slots.watchdog`, caminho em
`scheduler.watchdog_status_path`). Os `--hint-file` da unit são as dicas de
narrativa parada do app principal e do staging. Versão anterior (sem dica) em
`slot_watchdog.py.g2.bak` e `slot-watchdog.service.g2.bak`. O journal da unit
só mostra reinícios (`warning`) e esperas (`notice`):
`journalctl -u slot-watchdog -p notice`. Desligar:
`sudo systemctl disable --now slot-watchdog.timer`.

## Segredos (`.env`)

| Variável | Como gerar |
|---|---|
| `LLM_API_KEY` | a chave do llama-server (diferente da senha SSH; ver G8) |
| `AUTH_USER` | usuário do login (ex.: `Caio`) |
| `AUTH_PASSWORD_HASH` | `python3 -c "import hashlib; print(hashlib.sha256('SUA_SENHA'.encode()).hexdigest())"` |
| `AUTH_SECRET` | `python3 -c "import secrets; print(secrets.token_hex(32))"` |

Sem as 3 variáveis de auth o app **fail closed**: todas as rotas `/api` respondem
401 e o login responde 503. Permissões: `chmod 600 /home/caio/aidungeon/.env`.

## Instalação do zero (outra máquina)

O `package_backend.ps1` gera `deploy\vm\aidungeon-backend.zip` (gitignored) com
`backend\`, `config\`, `.env.example`, a unit de exemplo
`aidungeon-backend.service` e o `Caddyfile`. Ajuste caminhos e porta da unit de
exemplo para o diretório escolhido; a referência é a unit acima.

## Hardening

## 5. Serviço systemd

```bash
sudo useradd -r -s /usr/sbin/nologin aidungeon        # usuário de serviço
sudo chown -R aidungeon:aidungeon /opt/aidungeon
sudo cp /opt/aidungeon/aidungeon-backend.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now aidungeon-backend
sudo systemctl status aidungeon-backend
journalctl -u aidungeon-backend -f                    # logs
```

O unit de exemplo (`aidungeon-backend.service`) roda como `aidungeon`, lê o
`/opt/aidungeon/.env` via `EnvironmentFile`, reinicia em falha e aplica
hardening básico (`NoNewPrivileges`, `ProtectSystem`, `ProtectHome`, `PrivateTmp`).
Ajuste `User`/caminhos se instalar em outro diretório.

## 6. Hardening e TLS

- **Auth já é obrigatória** no app (Bearer HMAC em todas as rotas `/api`, com
  throttle de 5 falhas → 60s de lockout no login).
- **VM atual (produção):** exponha só o nginx (8443). A porta 2122 (app) e a
  1234 (llama-server) não devem ficar abertas para a internet.
- **Firewall:** libere só o necessário.
  - Modo direto (HTTP na 8000): `sudo ufw allow 8000/tcp`.
  - Modo Caddy (recomendado): feche a 8000 para fora e libere 80/443
    (`sudo ufw allow 80,443/tcp`); o uvicorn pode então subir com
    `--host 127.0.0.1` (edite o `ExecStart` do unit).
- **TLS com Caddy** (opcional mas recomendado — frontend na Vercel é HTTPS e
  navegadores bloqueiam mixed content): instale o Caddy, copie o `Caddyfile`
  deste pacote, troque o placeholder `api.groupabz.com` pelo seu domínio
  (DNS apontando para a VM) e `sudo systemctl reload caddy`. O Caddy emite e
  renova o certificado sozinho.

## 7. Depois do deploy

1. Adicione o domínio da Vercel em `config/settings.json → cors_origins` na VM e
   reinicie o serviço (ver `docs/DEPLOY_VERCEL.md` §5).
2. Aponte `VITE_API_BASE_URL` do frontend para este backend (idem §3).
3. Backup: copiar `/opt/aidungeon/data/aidungeon.db` (com o serviço parado ou via
   `.backup` do sqlite3).

## 8. v0.9.0 — Modo Turbo

A VM já em uso não segue o layout `/opt` das seções 2–6: app em
`/home/caio/aidungeon`, venv em `backend/venv`, unit `aidungeon.service`
(`User=caio`, `Restart=always`). O llama-server é outro unit, do root:
`/etc/systemd/system/llama-server.service`.

Alvo do llama-server: `-np 2 -c 262144`. Com `-np` explícito o contexto por
slot é 131072 (`-c / 2`). O app corta `context_size` na leitura em
`llm.context_size_limits.max` (**106496**); valor maior no banco (ex.: 262144)
não é reescrito. Não há chave de slot em `settings.json`.

Pinning é automático. `llm_client` faz `GET {base}/props` (`base` = `llm.base_url`
sem o sufixo `/v1`, com a mesma `Authorization` do chat) e lê `total_slots`.
`cache_prompt: true` vai sempre. `id_slot` só entra se `total_slots >= 2`:
narrativa (stream) → 0, auxiliar (`chat_completion`) → 1. Menos de 2 slots,
HTTP ≠ 200, JSON inválido ou exceção → a chave é omitida (um `id_slot`
inexistente trava o llama.cpp até o timeout).

Ordem do deploy: **backup do SQLite, restart do llama-server, depois o
backend**. A migration `009_scenario_image_style.sql` (`scenarios.image_style`)
aplica no boot do backend; o backup tem de vir antes desse restart. O backend
só sobe depois do llama-server, para o primeiro `GET /props` já ver 2 slots.

Backup online (o serviço pode continuar no ar; API `sqlite3.backup`):

```bash
cd /home/caio/aidungeon && backend/venv/bin/python -c "import sqlite3; s=sqlite3.connect('data/aidungeon.db'); d=sqlite3.connect('data/aidungeon.db.bak-v090'); s.backup(d); d.close()"
```

llama-server (sudo — o unit é do root). O `sed` só troca `-np 1` /
`--parallel 1`. Confira que a linha já tem `-c 262144` e não apague
`--mmproj`, `-ctk q8_0` nem `-ctv q8_0`:

```bash
sudo cp /etc/systemd/system/llama-server.service /etc/systemd/system/llama-server.service.v090.bak
sudo sed -i -E 's/(^|\s)-np 1(\s|$)/\1-np 2\2/; s/--parallel 1\b/--parallel 2/' /etc/systemd/system/llama-server.service
sudo systemctl daemon-reload && sudo systemctl restart llama-server
```

Backend, sem sudo (`Restart=always` sobe de novo sozinho):

```bash
pkill -f 'uvicorn app.main:app'
```
## 9. A1111 na VM (sd-webui, porta 7860)

O WebUI não entra no zip. Na VM ele é o `sd-webui.service`, código em
`/home/caio/sd-webui`, flags `--medvram --no-half-vae --opt-sdp-attention`.
Checkpoint `epicrealismXL_vxviiCrystalclear.safetensors`, VAE
`sdxl-vae-fp16-fix.safetensors`.

Em 18/10/2026 o `txt2img` passou a responder HTTP 500 depois de um CUDA OOM.
A linha completa:

```text
RuntimeError: Expected all tensors to be on the same device, but found at least two devices, cpu and cuda:0! (when checking argument for argument index in method wrapper_CUDA__index_select)
```

O `send_me_to_gpu` (`modules/lowvram.py`) estourou no meio do `module.to(cuda)`
durante o `sample_dpmpp_2m` (pediu 20 MiB, sobravam 33,75 MiB; o PyTorch tinha
210,84 MiB reservados e fragmentados). O embedding de token do CLIP ficou na
CPU e o `input_ids` em `cuda:0`. Os pedidos seguintes morriam no
`index_select`, antes do sampler. `--medvram` ficou: no pico de 1344×768
sobraram 21 MiB, abaixo dos 2 GB exigidos para `--medvram-sdxl`.

Dois arquivos, já aplicados na unit. Não reiniciar o `llama-server`.

```bash
sudo cp deploy/vm/sd-webui-alloc.conf /etc/systemd/system/sd-webui.service.d/alloc.conf
sudo systemctl daemon-reload
# patch já está em /home/caio/sd-webui; para reaplicar:
patch -p1 -d /home/caio/sd-webui < deploy/vm/sd-webui-lowvram-device.patch
sudo systemctl restart sd-webui
```

Medido com o llama-server carregado (~14424 MiB) e o alocador
`expandable_segments:True`: 512² em 1 passo, HTTP 200, pico do A1111 5286 MiB
(304 MiB livres na placa); 1344×768 em 4 passos, HTTP 200, pico 5568 MiB
(21 MiB livres). Em idle o A1111 fica em ~300 MiB e a placa tem ~5286 MiB livres.
