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
| Imagem | `sd-webui-forge.service` (Forge Neo) na porta 7861, epiCRealism XL. O A1111 continua em `/home/caio/sd-webui` (unit parada) |
| Voz | `abz-voice-audio.service`, Supertonic 3 em `https://vm.groupabz.com/voice/v1`. Drama-3 não cabe nesta L4: `drama3.md` |
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
O corpo segue DPM++ 2M, scheduler Karras, CFG 5, 20 passos.

O Neo tirou o `--medvram`. No lugar: `--cuda-malloc --expandable-segments
--cuda-stream --tiled-conv2d 128` e `setting_allocated_vram` 1.0. Com 0,25 o
Neo reservava ~17 GB e via 0 MB úteis (o modelo inteiro ia para a RAM). Com
1.0 ele ocupa o buraco livre: pico medido ~4,5 GB no 512 e o 800×720 em 20
passos voltou HTTP 200 em 24 s, com o `llama-server` ainda em ~14,5 GB e
`GET /health` 200.

Não instalados, porque não cabem ao lado do narrador: Z-Image-Turbo (o README
do Neo não publica quantização que caiba em 5,5 GB; o cartão pede 16 GB),
FLUX 3 Klein INT8 (~8 GB, outro cliente) e Wan 2.1.

Rollback: parar o Forge, subir o A1111 e devolver a URL.

```bash
sudo systemctl disable --now sd-webui-forge.service
sudo systemctl enable --now sd-webui.service
# image.sdwebui_base_url nos dois bancos: "http://127.0.0.1:7860"
```

Backup desta troca: `/home/caio/aidungeon-backups/forge-20261019T025223Z`.

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

- Auth obrigatória no app (Bearer HMAC em todas as rotas `/api`, com throttle de
  5 falhas → 60 s de lockout no login).
- Exponha só o nginx (8443). A porta 2122 e a 1234 do llama-server não devem
  ficar abertas para a internet (ver G8 em `tasks.md`).
- Backup: copiar `data/aidungeon.db` com o serviço parado ou via `.backup` do sqlite3.
