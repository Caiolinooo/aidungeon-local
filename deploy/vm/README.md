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

- Auth obrigatória no app (Bearer HMAC em todas as rotas `/api`, com throttle de
  5 falhas → 60 s de lockout no login).
- Exponha só o nginx (8443). A porta 2122 e a 1234 do llama-server não devem
  ficar abertas para a internet (ver G8 em `tasks.md`).
- Backup: copiar `data/aidungeon.db` com o serviço parado ou via `.backup` do sqlite3.
