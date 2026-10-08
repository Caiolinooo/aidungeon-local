# Deploy do backend na VM (vm.groupabz.com)

O backend é um FastAPI + SQLite **persistente** (por isso não vai para a Vercel —
serverless tem filesystem efêmero, ver PLANO.md §11). Este diretório gera um zip
auto-contido e documenta a instalação na VM.

## 1. Gerar o pacote (na máquina local)

```powershell
powershell -ExecutionPolicy Bypass -File deploy\vm\package_backend.ps1
```

Produz `deploy\vm\aidungeon-backend.zip` (gitignored) com:

```
backend\                  código + requirements.txt + schema.sql (SEM .venv)
config\                   settings.json + models.json
logs\                     vazio (logs de runtime)
data\                     vazio (o SQLite nasce aqui no 1º boot)
.env.example              modelo dos 4 segredos
aidungeon-backend.service unit systemd de exemplo
Caddyfile                 TLS opcional (§6)
README-DEPLOY.md          este arquivo
```

O pacote **nunca** inclui `.env`, `data\*.db` ou qualquer segredo real.

## 2. Instalar na VM

```bash
# copiar o zip p/ a VM (ex.: scp) e então:
sudo mkdir -p /opt/aidungeon
sudo unzip aidungeon-backend.zip -d /opt/aidungeon
cd /opt/aidungeon

# Python 3.12+ (confira: python3 --version)
python3.12 -m venv .venv
./.venv/bin/pip install -r backend/requirements.txt
```

## 3. Segredos (.env) — obrigatório

```bash
cp .env.example /opt/aidungeon/.env
nano /opt/aidungeon/.env     # preencha os 4 valores
```

| Variável | Como gerar |
|---|---|
| `LLM_API_KEY` | a mesma chave do llama.cpp que já roda na VM (`:1234`) |
| `AUTH_USER` | usuário do login (ex.: `Caio`) |
| `AUTH_PASSWORD_HASH` | `python3 -c "import hashlib; print(hashlib.sha256('SUA_SENHA'.encode()).hexdigest())"` |
| `AUTH_SECRET` | `python3 -c "import secrets; print(secrets.token_hex(32))"` |

Sem as 3 variáveis de auth o app **fail closed**: todas as rotas `/api` respondem
401 e o login responde 503. Permissões: `chmod 600 /opt/aidungeon/.env`.

## 4. Teste manual (antes de virar serviço)

```bash
cd /opt/aidungeon/backend
../.venv/bin/python -m uvicorn app.main:app --host 0.0.0.0 --port 8000
# em outro shell:
curl -i http://127.0.0.1:8000/api/health/llm     # 401 sem token = auth ok, no ar
```

O SQLite é criado sozinho em `/opt/aidungeon/data/aidungeon.db` no primeiro boot.
**Para levar os dados locais (aventura importada) para a VM:** pare o backend e
copie o `data\aidungeon.db` local por cima antes do primeiro start.

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
