// Parser SSE sobre fetch + ReadableStream.
// EventSource não serve aqui: os endpoints de geração (/action, /continue,
// /retry) são POST — então consumimos o stream manualmente e parseamos os
// frames "event: <nome>\ndata: <json>\n\n" (contrato: docs/openapi.yaml).
//
// Eventos: "token" (trecho gerado), "done" (Action persistida), "error" (falha).
// Cancelamento: AbortController — interrompe o stream localmente (o backend
// pode concluir a geração e persistir a action; ela aparece ao recarregar).
//
// Auth: mesmo esquema do api.js — header Bearer em toda chamada; 401 →
// clearToken() + evento 'aidl:unauthorized' (AuthGate volta ao /login).

import { getToken } from './auth.js'
import { notifyUnauthorized } from './api.js'

export async function streamGeneration(path, { body, signal, onToken, onDone, onError } = {}) {
  let res
  try {
    const headers = body != null ? { 'Content-Type': 'application/json' } : {}
    const token = getToken()
    if (token) headers.Authorization = `Bearer ${token}`
    res = await fetch(`/api${path}`, {
      method: 'POST',
      headers,
      body: body != null ? JSON.stringify(body) : undefined,
      signal,
    })
  } catch (e) {
    if (e.name === 'AbortError') return { aborted: true }
    onError?.('Cannot reach the backend server.')
    return { aborted: false }
  }

  // Respostas de erro (401/404/409/422) vêm como JSON no shape Error do contrato
  if (!res.ok || !res.body) {
    if (res.status === 401) notifyUnauthorized()
    let msg = `Request failed (${res.status})`
    try {
      const d = await res.json()
      if (d?.error?.message) msg = d.error.message
    } catch { /* ignora */ }
    onError?.(msg)
    return { aborted: false }
  }

  const reader = res.body.getReader()
  const decoder = new TextDecoder()
  let buffer = ''
  const result = { aborted: false, done: null }

  try {
    for (;;) {
      const { value, done } = await reader.read()
      if (done) break
      // normaliza CRLF -> LF (SSE admite ambos); frames separam-se por linha em branco
      buffer += decoder.decode(value, { stream: true }).replace(/\r\n/g, '\n')
      let idx
      while ((idx = buffer.indexOf('\n\n')) >= 0) {
        const raw = buffer.slice(0, idx)
        buffer = buffer.slice(idx + 2)
        const ev = parseEvent(raw)
        if (!ev) continue
        if (ev.event === 'token') onToken?.(ev.data?.token ?? '')
        else if (ev.event === 'done') {
          result.done = ev.data
          onDone?.(ev.data)
        } else if (ev.event === 'error') {
          onError?.(ev.data?.message ?? 'Generation failed')
        }
      }
    }
  } catch (e) {
    if (e.name === 'AbortError') {
      result.aborted = true
      return result
    }
    onError?.('Stream interrupted.')
  }
  return result
}

function parseEvent(block) {
  let event = 'message'
  const dataLines = []
  for (const line of block.split('\n')) {
    if (line.startsWith('event:')) event = line.slice(6).trim()
    else if (line.startsWith('data:')) dataLines.push(line.slice(5).replace(/^ /, ''))
  }
  if (dataLines.length === 0) return null
  const text = dataLines.join('\n')
  let data = null
  try {
    data = JSON.parse(text)
  } catch {
    data = text // data não-JSON: repassa como string
  }
  return { event, data }
}
