// Sessão da SPA: token Bearer em localStorage ("aidl_token").
// O token é emitido por POST /api/auth/login (HMAC-SHA256, TTL de
// settings.auth.token_ttl_hours) e vai no header Authorization de toda
// chamada /api (ver api.js e sse.js).

const KEY = 'aidl_token'

export function getToken() {
  try {
    return localStorage.getItem(KEY)
  } catch {
    return null // storage indisponível (janela privada etc.)
  }
}

export function setToken(token) {
  try {
    localStorage.setItem(KEY, token)
  } catch { /* ignora */ }
}

export function clearToken() {
  try {
    localStorage.removeItem(KEY)
  } catch { /* ignora */ }
}
