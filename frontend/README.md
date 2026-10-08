# AI Dungeon Local — Frontend

SPA React 18 + Vite 5 + Tailwind CSS 3 (tema escuro padrão), réplica funcional da
experiência do AI Dungeon (PLANO.md §4.5). Consome a API do contrato
`docs/openapi.yaml` (fonte da verdade).

## Telas

| Rota | Tela |
|---|---|
| `/` | Home — grid "Your Adventures", "New Adventure" (scenario ou em branco), "Import AI Dungeon backup" (.zip → ImportReport) |
| `/play/:id` | Jogo — feed com scroll automático, tabs Do/Say/Story, toolbar Retry/Undo/Continue/Stop, Edit por bloco (hover), drawer Story Cards / Memory / Config / Details, streaming SSE token a token |
| `/scenarios/new` | Editor de Scenario (criação, POST /api/scenarios) |
| `/scenarios/:id/edit` | Editor somente leitura — a v1 do contrato não tem PUT de scenario |

## Rodar em desenvolvimento

```bash
# terminal 1 — backend (a partir de backend/)
uvicorn app.main:app --port 8000

# terminal 2 — frontend (a partir de frontend/)
npm install
npm run dev        # http://localhost:5173 (proxy /api -> 127.0.0.1:8000)
```

## Build de produção

```bash
npm run build      # gera frontend/dist
```

O backend FastAPI serve `frontend/dist` estaticamente na raiz quando o diretório
existe (mount de StaticFiles com fallback SPA em `backend/app/main.py`) — basta
abrir http://127.0.0.1:8000 . Os routers `/api/*` têm precedência sobre o mount.

## Notas de implementação

- `src/api.js` — wrapper de fetch com baseURL `/api` e tratamento do shape
  `Error` do contrato (`{"error": {"code", "message"}}`).
- `src/sse.js` — parser SSE sobre `fetch` + `ReadableStream` (EventSource não
  serve: os endpoints de geração são POST). Eventos `token` / `done` / `error`;
  cancelamento via `AbortController` (Stop cancela o stream localmente — o
  backend pode concluir e persistir a action, que aparece ao recarregar).
- A aba Config expõe apenas os campos do contrato (`LlmSettingsUpdate`):
  `base_url`, `api_key` (write-only), `model_alias`, `context_size`,
  `max_tokens`, `streaming`, `disable_thinking`, `timeout_seconds`. O sampler
  (temperature/top_p/repetition_penalty) é fixo em `config/settings.json` na v1.
- UI em inglês (réplica do original); comentários do código em pt-BR.
