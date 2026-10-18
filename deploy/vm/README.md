# Deploy na VM (vm.groupabz.com)

O backend é FastAPI + SQLite **persistente** e serve também o build do frontend
(SPA). Este documento descreve a instalação que roda hoje na VM.

## Como está hoje

| Item | Valor |
|---|---|
| Diretório do app | `/home/caio/aidungeon` (cópia, ainda não é checkout git; ver G9 em `tasks.md`) |
| Serviço | `aidungeon.service` (usuário `caio`), uvicorn na porta **2122** |
| Python | venv em `/home/caio/aidungeon/backend/venv` |
| Segredos | `/home/caio/aidungeon/.env` (lido via `AIDUNGEON_ENV_PATH`) |
| Banco | `/home/caio/aidungeon/data/aidungeon.db` |
| Imagens | `/home/caio/aidungeon/data/images/` |
| Frontend | `/home/caio/aidungeon/frontend/dist` (servido pelo FastAPI) |
| HTTPS | nginx `aidungeon-https.conf`: `https://vm.groupabz.com:8443` → `127.0.0.1:2122` (Let's Encrypt); access log sem query string, porque o EventSource do SSE leva o token em `?token=` |
| LLM | `llama-server.service` na porta 1234, 5 slots (seção abaixo) |
| Watchdog | `slot-watchdog.timer` (a cada 10 s) reinicia o llama-server com slot preso |
| Embeddings | `llama-embeddings.service` (CPU, 0 MiB de VRAM) na porta 8091 |
| Imagem | `sd-webui.service` (A1111) na porta 7860, com o drop-in de memória abaixo |
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

## Atualizar o código (até a G9 existir)

Sempre passa pelo staging antes (seção abaixo).

```bash
# na máquina local, a partir da raiz do repositório
tar czf /tmp/aidungeon-src.tgz --exclude=__pycache__ backend/app backend/memory \
    backend/migrations backend/schema.sql backend/requirements.txt config
scp /tmp/aidungeon-src.tgz caio@vm.groupabz.com:/tmp/

# na VM: backup do banco (online, API de backup do sqlite) e do código atual
TS=$(date +%Y%m%d-%H%M); B=/home/caio/aidungeon-backups; mkdir -p $B
cd /home/caio/aidungeon
python3 -c "import sqlite3,sys; s=sqlite3.connect('data/aidungeon.db'); d=sqlite3.connect(sys.argv[1]); s.backup(d)" $B/aidungeon.db.pre-$TS
tar czf $B/code.pre-$TS.tgz --exclude=__pycache__ backend/app backend/memory \
    backend/migrations backend/schema.sql backend/requirements.txt config
tar xzf /tmp/aidungeon-src.tgz
backend/venv/bin/pip install -r backend/requirements.txt
sudo systemctl restart aidungeon
curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:2122/api/adventures   # 401 = no ar
```

As migrations em `backend/migrations/` rodam sozinhas no boot (`db.init_db`).
Frontend: `cd frontend && npm ci && npm run build` na máquina local e copie
`frontend/dist/` para `/home/caio/aidungeon/frontend/dist/`.

Rollback: `sudo systemctl stop aidungeon`, `tar xzf $B/code.pre-$TS.tgz` na raiz
do app, copie `$B/aidungeon.db.pre-$TS` por cima de `data/aidungeon.db` (só se
uma migration mexeu no banco) e `sudo systemctl start aidungeon`.

## Staging

Mesmo venv e mesmo `.env` do app, código e banco próprios, só em `127.0.0.1`.
Usa o mesmo llama-server e o mesmo A1111 do app principal: teste fora do horário
de jogo. Unit em [`aidungeon-staging.service`](aidungeon-staging.service).

```bash
# na VM: copia o banco do app principal e sobe o código novo no staging
S=/home/caio/aidungeon-staging; mkdir -p $S/data
python3 -c "import sqlite3,sys; s=sqlite3.connect('/home/caio/aidungeon/data/aidungeon.db'); d=sqlite3.connect(sys.argv[1]); s.backup(d)" $S/data/aidungeon.db
rsync -a /home/caio/aidungeon/data/images/ $S/data/images/
rsync -a /home/caio/aidungeon/frontend/dist/ $S/frontend/dist/
tar xzf /tmp/aidungeon-src.tgz -C $S
sudo systemctl restart aidungeon-staging
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

## Embeddings fora da GPU

`llama-embeddings.service` roda em CPU, mas o CUDA ainda reservava 646 MiB
(186 MiB só com `--device none`). Com as duas linhas abaixo, 0 MiB (backup
`llama-embeddings.service.g7.bak`):

```ini
Environment=CUDA_VISIBLE_DEVICES=
ExecStart=... --n-gpu-layers 0 --device none ...
```

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

Estado em `/var/lib/slot-watchdog/status.json`. O journal da unit só mostra
reinícios (`warning`) e esperas (`notice`):
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
