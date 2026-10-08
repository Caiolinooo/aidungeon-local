# Deploy do frontend na Vercel

A SPA (React + Vite) vira um site estático na Vercel; o **backend FastAPI continua na VM**
(`vm.groupabz.com`) — serverless tem filesystem efêmero e perderia o SQLite
(decisão registrada no PLANO.md §11). A ponte entre os dois é a variável de build
`VITE_API_BASE_URL`.

Pré-requisitos: repo no GitHub (privado) e Vercel CLI (`npm i -g vercel`, já instalado).

---

## 1. Login na Vercel (uma vez)

```powershell
vercel login
```

Abre o navegador para autenticar a CLI na sua conta.

## 2. Configurar o projeto

O `frontend\vercel.json` já define tudo: framework Vite, `npm ci`, `npm run build`,
output `dist`, rewrites de SPA (todas as rotas caem em `index.html`) e `index.html`
sem cache (os assets em `/assets/*` têm hash no nome e ficam com cache imutável).

Importe o projeto com a raiz em `frontend/`:

```powershell
cd frontend
vercel          # primeira vez: link/criação do projeto (Root Directory = frontend)
```

Responda o wizard: scope pessoal, nome do projeto (ex.: `aidungeon-local`),
"modify settings?" → **No** (o `vercel.json` já cobre build/output).

## 3. Variável de ambiente `VITE_API_BASE_URL`

Sem ela a SPA chamaria `/api` na própria Vercel (não há backend lá). Aponte para o
backend da VM:

```powershell
# dentro de frontend\
vercel env add VITE_API_BASE_URL production
# cole o valor quando pedir, ex.: https://api.groupabz.com  (ou http://vm.groupabz.com:8000)
```

Ou pelo painel: **Project → Settings → Environment Variables → `VITE_API_BASE_URL`**
(scope Production; se quiser preview também, repita para Preview).

> É variável de **build** (Vite embute no bundle): depois de criar/alterar, é
> preciso redeploy (`vercel --prod`).

> HTTPS recomendado: se a URL for `http://`, navegadores bloqueiam "mixed content"
> quando a página é `https://`. O `deploy\vm\Caddyfile` sobe TLS na VM em minutos —
> ver `deploy\vm\README.md` §6.

## 4. Deploy de produção

```powershell
cd frontend
vercel --prod
```

Ao final a CLI imprime a URL de produção (ex.: `https://aidungeon-local.vercel.app`).

## 5. OBRIGATÓRIO: liberar o domínio no CORS do backend

O middleware Bearer já protege todas as rotas, mas o navegador bloqueia chamadas
cross-origin sem CORS. Na VM, edite `config\settings.json` e adicione o domínio da
Vercel em `cors_origins` (mecanismo já existente — `backend/app/main.py` lê essa
lista no startup):

```json
"cors_origins": [
  "http://127.0.0.1:8000",
  "http://localhost:8000",
  "http://localhost:5173",
  "https://aidungeon-local.vercel.app"
]
```

Reinicie o backend na VM (`sudo systemctl restart aidungeon-backend`) para aplicar.
Sem este passo a tela de login nem carrega a sessão (as chamadas `/api` falham no
navegador com erro de CORS).

## 6. Verificação

1. Abra a URL de produção → deve aparecer a página de **login**.
2. Entre com o usuário/senha do `.env` da VM → Home com as aventuras.
3. Aba **Config** da UI mostra o status do LLM (`GET /api/health/llm`).

## Solução de problemas

| Sintoma | Causa provável | Correção |
|---|---|---|
| Login não sai da tela / erro de rede | `VITE_API_BASE_URL` ausente ou errada | §3 + `vercel --prod` |
| Erro de CORS no console do navegador | domínio Vercel não está em `cors_origins` | §5 (editar settings.json na VM + restart) |
| 401 em tudo logo após login | token não está sendo enviado | confira se está na URL `https` correta; limpe o localStorage do domínio |
| Página em branco em rota direta (ex.: /play/123) | rewrite SPA não aplicado | confirme que `frontend\vercel.json` subiu junto (ele está no repo) |
| Mixed content blocked | backend em `http://` | habilite TLS com o Caddyfile (`deploy\vm\README.md` §6) |
