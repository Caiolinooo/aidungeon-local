# AI Dungeon Local — Frontend

SPA React 18 + Vite 5 + Tailwind CSS 3 (tema escuro padrão), réplica funcional da
experiência do AI Dungeon (PLANO.md §4.5). Consome a API do contrato
`docs/openapi.yaml`. PATCH/DELETE de cenário, Story Studio e `web_search` da
v0.9.0 estão no backend e na UI; o YAML ainda não lista esses contratos.

## Telas

| Rota | Tela |
|---|---|
| `/` | Home — grid "Your Adventures", "New Adventure" (scenario ou em branco; cada scenario tem editar e apagar), "Import AI Dungeon backup" (.zip → ImportReport) |
| `/play/:id` | Jogo — feed com scroll automático, tabs Do/Say/Story/See, toolbar Retry/Undo/Continue/Stop, Edit por bloco (hover), drawer Story Cards / Memory / Config / Details, streaming SSE token a token. Toggle de pesquisa web na barra; fontes do turno numa faixa dispensável. Caption do See é o texto da action; o prompt técnico fica na imagem |
| `/scenarios/new` | Editor de Scenario (criação, POST /api/scenarios), inclusive `image_style` e story cards |
| `/scenarios/:id/edit` | Editor completo — PATCH/PUT `/api/scenarios/{id}`, duplicar (POST com título "(copy)") e DELETE (aventuras já criadas perdem o vínculo) |

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
