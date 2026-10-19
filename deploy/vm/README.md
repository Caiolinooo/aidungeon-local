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
| HTTPS | nginx `aidungeon-https.conf`: `https://vm.groupabz.com:8443` → `127.0.0.1:2122` (Let's Encrypt) |
| LLM | `llama-server.service` na porta 1234 (ver `docs/vm_runbooks/`) |
| Embeddings | `llama-embeddings.service` (CPU) na porta 8091 |
| Imagem | `sd-webui.service` (A1111) na porta 7860 |

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

```bash
# na máquina local, a partir da raiz do repositório
tar czf /tmp/aidungeon-src.tgz backend/app backend/memory backend/migrations \
    backend/schema.sql backend/requirements.txt config
scp /tmp/aidungeon-src.tgz caio@vm.groupabz.com:/tmp/

# na VM
cd /home/caio/aidungeon
cp data/aidungeon.db data/aidungeon.db.bak-$(date +%Y%m%d-%H%M)   # backup
tar xzf /tmp/aidungeon-src.tgz
backend/venv/bin/pip install -r backend/requirements.txt
sudo systemctl restart aidungeon
curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:2122/api/health/llm   # 401 = no ar
```

As migrations em `backend/migrations/` rodam sozinhas no boot (`db.init_db`).
Frontend: `cd frontend && npm ci && npm run build` na máquina local e copie
`frontend/dist/` para `/home/caio/aidungeon/frontend/dist/`.

Rollback: pare o serviço, restaure o tarball anterior e o backup do banco,
`sudo systemctl start aidungeon`.

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
