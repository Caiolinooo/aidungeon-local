// Wrapper de fetch para a API (contrato: docs/openapi.yaml — fonte da verdade).
// baseURL '/api': em dev o Vite faz proxy p/ http://127.0.0.1:8000; em produção
// o próprio FastAPI serve o build na mesma origem.
// Erros seguem o shape {"error": {"code", "message"}} do contrato.

export class ApiError extends Error {
  constructor(status, code, message) {
    super(message)
    this.name = 'ApiError'
    this.status = status // 0 = backend fora do ar / falha de rede
    this.code = code
  }
}

function qs(params) {
  const p = new URLSearchParams()
  for (const [k, v] of Object.entries(params)) {
    if (v != null && v !== '') p.set(k, v)
  }
  const s = p.toString()
  return s ? `?${s}` : ''
}

async function request(path, { method = 'GET', body, signal } = {}) {
  let res
  try {
    res = await fetch(`/api${path}`, {
      method,
      // FormData (upload multipart) define o próprio Content-Type com boundary
      headers: body instanceof FormData ? undefined : { 'Content-Type': 'application/json' },
      body: body instanceof FormData ? body : body != null ? JSON.stringify(body) : undefined,
      signal,
    })
  } catch (e) {
    if (e.name === 'AbortError') throw e
    throw new ApiError(0, 'network_error', 'Cannot reach the backend server.')
  }
  if (!res.ok) {
    let code = 'http_error'
    let message = `Request failed (${res.status})`
    try {
      const data = await res.json()
      if (data?.error?.code) code = data.error.code
      if (data?.error?.message) message = data.error.message
    } catch { /* resposta não-JSON: mantém mensagem padrão */ }
    throw new ApiError(res.status, code, message)
  }
  if (res.status === 204) return null
  return res.json()
}

export const api = {
  // ------------------------------------------------------------- scenarios
  listScenarios: (params = {}) => request(`/scenarios${qs(params)}`),
  getScenario: (id) => request(`/scenarios/${id}`),
  createScenario: (payload) => request('/scenarios', { method: 'POST', body: payload }),

  // ------------------------------------------------------------ adventures
  listAdventures: () => request('/adventures'),
  getAdventure: (id) => request(`/adventures/${id}`),
  createAdventure: (payload) => request('/adventures', { method: 'POST', body: payload }),
  undo: (id) => request(`/adventures/${id}/undo`, { method: 'POST' }),
  exportUrl: (id) => `/api/adventures/${id}/export`, // download direto (zip)

  // --------------------------------------------------------------- actions
  patchAction: (id, text) => request(`/actions/${id}`, { method: 'PATCH', body: { text } }),

  // ----------------------------------------------------------------- cards
  listCards: (advId) => request(`/adventures/${advId}/cards`),
  createCard: (advId, payload) => request(`/adventures/${advId}/cards`, { method: 'POST', body: payload }),
  updateCard: (advId, cardId, payload) =>
    request(`/adventures/${advId}/cards/${cardId}`, { method: 'PATCH', body: payload }),
  deleteCard: (advId, cardId) =>
    request(`/adventures/${advId}/cards/${cardId}`, { method: 'DELETE' }),

  // ---------------------------------------------------------------- memory
  getMemory: (advId) => request(`/adventures/${advId}/memory`),
  putMemory: (advId, payload) => request(`/adventures/${advId}/memory`, { method: 'PUT', body: payload }),

  // ---------------------------------------------------------------- import
  importBackup: (file) => {
    const fd = new FormData()
    fd.append('file', file)
    return request('/import/aidungeon', { method: 'POST', body: fd })
  },

  // ----------------------------------------------------- settings / health
  getLlmSettings: () => request('/settings/llm'),
  putLlmSettings: (payload) => request('/settings/llm', { method: 'PUT', body: payload }),
  healthLlm: () => request('/health/llm'),
}
