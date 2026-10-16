// Wrapper de fetch para a API (contrato: docs/openapi.yaml — fonte da verdade).
// Base: `${API_BASE}/api` — em dev o Vite faz proxy p/ http://127.0.0.1:8000 e
// em produção local o próprio FastAPI serve o build na mesma origem (API_BASE='').
// Deploy na Vercel: build com VITE_API_BASE_URL=https://<host-do-backend> faz
// as chamadas irem para esse host (CORS liberado via cors_origins no backend).
// Erros seguem o shape {"error": {"code", "message"}} do contrato.
//
// Auth: toda chamada leva "Authorization: Bearer <token>" (localStorage, ver
// auth.js). Em 401 (fora do próprio login): clearToken() + evento
// 'aidl:unauthorized', que o AuthGate escuta para voltar ao /login.

import { clearToken, getToken } from './auth.js'

// Prefixo absoluto opcional da API (deploy do frontend fora do backend).
// '' (default) = mesma origem — comportamento local inalterado.
export const API_BASE = (import.meta.env.VITE_API_BASE_URL || '').replace(/\/+$/, '')

export class ApiError extends Error {
  constructor(status, code, message) {
    super(message)
    this.name = 'ApiError'
    this.status = status // 0 = backend fora do ar / falha de rede
    this.code = code
  }
}

// 401 em qualquer rota (exceto o login): sessão inválida/expirada → derruba
// o token e avisa o AuthGate (que navega para /login).
export function notifyUnauthorized() {
  clearToken()
  try {
    window.dispatchEvent(new Event('aidl:unauthorized'))
  } catch { /* ignora */ }
}

function authHeaders(extra) {
  const headers = { ...(extra || {}) }
  const token = getToken()
  if (token) headers.Authorization = `Bearer ${token}`
  return headers
}

function qs(params) {
  const p = new URLSearchParams()
  for (const [k, v] of Object.entries(params)) {
    if (v != null && v !== '') p.set(k, v)
  }
  const s = p.toString()
  return s ? `?${s}` : ''
}

async function parseError(res) {
  let code = 'http_error'
  let message = `Request failed (${res.status})`
  try {
    const data = await res.json()
    if (data?.error?.code) code = data.error.code
    if (data?.error?.message) message = data.error.message
  } catch { /* resposta não-JSON: mantém mensagem padrão */ }
  return new ApiError(res.status, code, message)
}

async function request(path, { method = 'GET', body, signal } = {}) {
  let res
  try {
    res = await fetch(`${API_BASE}/api${path}`, {
      method,
      // FormData (upload multipart) define o próprio Content-Type com boundary
      headers: body instanceof FormData
        ? authHeaders()
        : authHeaders({ 'Content-Type': 'application/json' }),
      body: body instanceof FormData ? body : body != null ? JSON.stringify(body) : undefined,
      signal,
    })
  } catch (e) {
    if (e.name === 'AbortError') throw e
    throw new ApiError(0, 'network_error', 'Cannot reach the backend server.')
  }
  if (!res.ok) {
    if (res.status === 401 && !path.startsWith('/auth/login')) notifyUnauthorized()
    throw await parseError(res)
  }
  if (res.status === 204) return null
  // 200 com corpo não-JSON = backend antigo servindo o index.html do fallback SPA
  // (rota inexistente cai no StaticFiles). Sem este guarda o usuário via o erro
  // críptico "Unexpected token '<' ... is not valid JSON".
  const contentType = res.headers.get('content-type') || ''
  if (!contentType.includes('application/json')) {
    throw new ApiError(res.status, 'invalid_response',
      'Backend returned a non-JSON response — it is probably running an older version. Restart it (parar.bat → iniciar.bat).')
  }
  return res.json()
}

export const api = {
  // ------------------------------------------------------------------ auth
  login: (username, password) =>
    request('/auth/login', { method: 'POST', body: { username, password } }),
  checkAuth: () => request('/auth/check'),

  // ------------------------------------------------------------- scenarios
  listScenarios: (params = {}) => request(`/scenarios${qs(params)}`),
  getScenario: (id) => request(`/scenarios/${id}`),
  createScenario: (payload) => request('/scenarios', { method: 'POST', body: payload }),

  // ------------------------------------------------------------ adventures
  listAdventures: () => request('/adventures'),
  getAdventure: (id) => request(`/adventures/${id}`),
  createAdventure: (payload) => request('/adventures', { method: 'POST', body: payload }),
  updateAdventure: (id, payload) => request(`/adventures/${id}`, { method: 'PATCH', body: payload }),
  deleteAdventure: (id) => request(`/adventures/${id}`, { method: 'DELETE' }),
  undo: (id) => request(`/adventures/${id}/undo`, { method: 'POST' }),
  // Download autenticado do .zip (Âncoras <a href> não levam o header Bearer).
  downloadExport: async (id) => {
    let res
    try {
      res = await fetch(`${API_BASE}/api/adventures/${id}/export`, { headers: authHeaders() })
    } catch {
      throw new ApiError(0, 'network_error', 'Cannot reach the backend server.')
    }
    if (!res.ok) {
      if (res.status === 401) notifyUnauthorized()
      throw await parseError(res)
    }
    return res.blob()
  },
  // Backup completo (todas as aventuras) num único .zip — mesmo padrão do downloadExport.
  downloadBackupAll: async () => {
    let res
    try {
      res = await fetch(`${API_BASE}/api/adventures/export`, { headers: authHeaders() })
    } catch {
      throw new ApiError(0, 'network_error', 'Cannot reach the backend server.')
    }
    if (!res.ok) {
      if (res.status === 401) notifyUnauthorized()
      throw await parseError(res)
    }
    return res.blob()
  },

  // --------------------------------------------------------------- actions
  patchAction: (id, text) => request(`/actions/${id}`, { method: 'PATCH', body: { text } }),

  // ----------------------------------------------------------------- cards
  listCards: (advId, params = {}) => request(`/adventures/${advId}/cards${qs(params)}`),
  createCard: (advId, payload) => request(`/adventures/${advId}/cards`, { method: 'POST', body: payload }),
  updateCard: (advId, cardId, payload) =>
    request(`/adventures/${advId}/cards/${cardId}`, { method: 'PATCH', body: payload }),
  deleteCard: (advId, cardId) =>
    request(`/adventures/${advId}/cards/${cardId}`, { method: 'DELETE' }),
  bulkCards: (advId, payload) =>
    request(`/adventures/${advId}/cards/bulk`, { method: 'POST', body: payload }),

  // ----------------------------------------------------- assets / jobs (v0.3.0)
  // Fila assíncrona de assets (retratos de NPC etc.) — ver docs do pipeline.
  listJobs: (advId, status) => request(`/adventures/${advId}/jobs${qs({ status })}`),
  cancelJob: (advId, jobId) =>
    request(`/adventures/${advId}/jobs/${jobId}`, { method: 'DELETE' }),
  regeneratePortrait: (advId, cardId) =>
    request(`/adventures/${advId}/cards/${cardId}/portrait`, { method: 'POST' }),
  generateAllPortraits: (advId) =>
    request(`/adventures/${advId}/portraits`, { method: 'POST' }),
  // Fundo do lugar atual + elenco com retrato. Enfileira a pintura se o
  // lugar ou o clima mudou; polling no mesmo endpoint enquanto pending.
  syncScene: (advId, { retry = false } = {}) =>
    request(`/adventures/${advId}/scene`, { method: 'POST', body: retry ? { retry: true } : {} }),
  // v0.5.0 — ficha estruturada do NPC (síncronos, ver docs/openapi.yaml).
  enrichCard: (advId, cardId) =>
    request(`/adventures/${advId}/cards/${cardId}/enrich`, { method: 'POST', body: {} }),

  // ---------------------------------------------------------------- memory
  getMemory: (advId) => request(`/adventures/${advId}/memory`),
  putMemory: (advId, payload) => request(`/adventures/${advId}/memory`, { method: 'PUT', body: payload }),
  listMemories: (advId) => request(`/adventures/${advId}/memories`),
  createMemory: (advId, payload) => request(`/adventures/${advId}/memories`, { method: 'POST', body: payload }),
  patchMemory: (advId, memoryId, payload) =>
    request(`/adventures/${advId}/memories/${memoryId}`, { method: 'PATCH', body: payload }),
  deleteMemory: (advId, memoryId) =>
    request(`/adventures/${advId}/memories/${memoryId}`, { method: 'DELETE' }),
  regenerateMemories: (advId) =>
    request(`/adventures/${advId}/memories/regenerate`, { method: 'POST' }),


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
  getVoiceSettings: () => request('/settings/voice'),
  putVoiceSettings: (payload) => request('/settings/voice', { method: 'PUT', body: payload }),
  getEmbeddingsSettings: () => request('/settings/embeddings'),
  putEmbeddingsSettings: (payload) => request('/settings/embeddings', { method: 'PUT', body: payload }),
  getScriptingSettings: () => request('/settings/scripting'),
  putScriptingSettings: (payload) => request('/settings/scripting', { method: 'PUT', body: payload }),


  // --------------------------------------------------------- assistant (v2)
  // Gera rascunho estruturado de cenário a partir de uma ideia (não persiste).
  assistantScenario: ({ brief, language } = {}) =>
    request('/assistant/scenario', {
      method: 'POST',
      body: language ? { brief, language } : { brief },
    }),
  // Regenera um campo do rascunho (resposta: {field, value} ou {field, story_cards}).
  assistantField: ({ field, brief, current, draft, language } = {}) =>
    request('/assistant/field', {
      method: 'POST',
      body: language ? { field, brief, current, draft, language } : { field, brief, current, draft },
    }),

  // ------------------------------------------------------------ images (v2)
  // Ação "See": gera imagem da cena (JSON, não-SSE — pode levar minutos).
  see: (advId, { action_seq, extra_prompt } = {}) =>
    request(`/adventures/${advId}/see`, {
      method: 'POST',
      body: extra_prompt ? { action_seq, extra_prompt } : { action_seq },
    }),
  // Sugestões de próxima ação ("choices") — {"choices": [{type, text}]}.
  getChoices: (advId) => request(`/adventures/${advId}/choices`, { method: 'POST' }),

  // ------------------------------------------- controles dinâmicos (v0.7.0)
  // Manifesto gerado pela IA: {tabs, widgets} + estado dos widgets (ui_state).
  getUiProfile: (advId) => request(`/adventures/${advId}/ui-profile`),
  generateUiProfile: (advId) =>
    request(`/adventures/${advId}/ui-profile`, { method: 'POST' }),
  putUiState: (advId, state) =>
    request(`/adventures/${advId}/ui-state`, { method: 'PUT', body: { state } }),
  getImageSettings: () => request('/settings/image'),
  putImageSettings: (payload) => request('/settings/image', { method: 'PUT', body: payload }),
  // Fetch autenticado da imagem (<img> não envia o header Bearer — ver downloadExport).
  fetchImageBlob: async (imageId) => {
    let res
    try {
      res = await fetch(`${API_BASE}/api/images/${imageId}`, { headers: authHeaders() })
    } catch {
      throw new ApiError(0, 'network_error', 'Cannot reach the backend server.')
    }
    if (!res.ok) {
      if (res.status === 401) notifyUnauthorized()
      throw await parseError(res)
    }
    return res.blob()
  },

  // ------------------------------------------------------------- voice (v0.4.0)
  // TTS (Supertonic) + STT (Whisper) via stack de voz da VM — contrato v0.4.0.
  voice: {
    // GET /voices → {voices: [...], narrator_voice: "M1"} (config efetiva).
    list: () => request('/voices'),
    // POST /tts: sucesso = bytes audio/mpeg (não-JSON!) → blob; erro = envelope
    // JSON {"error": {...}} (voice inválida, texto longo, voice_disabled etc.).
    tts: async (text, voice) => {
      let res
      try {
        res = await fetch(`${API_BASE}/api/tts`, {
          method: 'POST',
          headers: authHeaders({ 'Content-Type': 'application/json' }),
          // voice omitida → backend usa narrator_voice (contrato)
          body: JSON.stringify(voice ? { text, voice } : { text }),
        })
      } catch {
        throw new ApiError(0, 'network_error', 'Cannot reach the backend server.')
      }
      const contentType = res.headers.get('content-type') || ''
      if (!res.ok || !contentType.includes('audio/mpeg')) {
        if (res.status === 401) notifyUnauthorized()
        throw await parseError(res)
      }
      return res.blob()
    },
    // POST /stt: multipart (campo 'file') → {"text": "..."}.
    // O nome acompanha o mime real (webm/mp4/ogg) — mandar tudo como
    // audio.webm faz o Whisper recusar um mp4 do Safari.
    // language: subtag do browser (pt, en…). Sem isso o servidor de voz
    // força português e uma fala em inglês volta vazia.
    stt: (blob) => {
      const fd = new FormData()
      const mime = blob.type || 'audio/webm'
      const ext = mime.includes('mp4') || mime.includes('m4a')
        ? 'm4a'
        : mime.includes('ogg')
          ? 'ogg'
          : mime.includes('wav')
            ? 'wav'
            : 'webm'
      fd.append('file', blob, `audio.${ext}`)
      const lang = (navigator.language || 'pt').slice(0, 2).toLowerCase()
      if (/^[a-z]{2}$/.test(lang)) fd.append('language', lang)
      return request('/stt', { method: 'POST', body: fd })
    },
    getSettings: () => request('/settings/voice'),
    putSettings: (patch) => request('/settings/voice', { method: 'PUT', body: patch }),
  },
}
