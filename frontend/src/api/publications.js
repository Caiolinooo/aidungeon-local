/** Catálogo de publicações. Usa `request` de api.js — não editar api.js.
 *
 * Jogar (owner integra um dos dois, não os dois ao mesmo tempo):
 * - prop `onPlay({ publicationId, version, title })` em Catalog
 * - ou `window.addEventListener(PLAY_PUBLICATION_EVENT, (event) => event.detail)`
 *   detail: `{ publicationId, version, title }`
 * Se `onPlay` existe, o evento não dispara.
 *
 * Remix:
 * - prop `onRemix({ publicationId, version, scenarioId, title, scenario })`
 * - ou `REMIX_PUBLICATION_EVENT` (`aidl:publication-remix`)
 *   detail: `{ publicationId, version, scenarioId, title, scenario }`
 */

import { request } from '../api.js'

export const PLAY_PUBLICATION_EVENT = 'aidl:play-publication'
export const REMIX_PUBLICATION_EVENT = 'aidl:publication-remix'

function query(params) {
  const search = new URLSearchParams()
  for (const [key, value] of Object.entries(params)) {
    if (value != null && value !== '') search.set(key, value)
  }
  const text = search.toString()
  return text ? `?${text}` : ''
}

export function listPublications({ q, tag, sort, limit } = {}) {
  return request(`/publications${query({ q, tag, sort, limit })}`)
}

export function getPublication(id) {
  return request(`/publications/${encodeURIComponent(id)}`)
}

export function getPublicationVersion(id, version) {
  return request(`/publications/${encodeURIComponent(id)}/versions/${version}`)
}

export function publishPublication({ kind, source_id, cover_image }) {
  const body = { kind, source_id }
  if (cover_image) body.cover_image = cover_image
  return request('/publications', { method: 'POST', body })
}

export function deletePublication(id) {
  return request(`/publications/${encodeURIComponent(id)}`, { method: 'DELETE' })
}

export function likePublication(id) {
  return request(`/publications/${encodeURIComponent(id)}/like`, { method: 'POST' })
}

export function unlikePublication(id) {
  return request(`/publications/${encodeURIComponent(id)}/like`, { method: 'DELETE' })
}

export function remixPublication(id, version) {
  const body = {}
  if (version != null) body.version = version
  return request(`/publications/${encodeURIComponent(id)}/remix`, { method: 'POST', body })
}

export function emitPlayPublication(detail) {
  const payload = {
    publicationId: String(detail.publicationId || ''),
    version: Number(detail.version),
    title: String(detail.title || ''),
  }
  window.dispatchEvent(new CustomEvent(PLAY_PUBLICATION_EVENT, { detail: payload }))
  return payload
}

export function emitRemixPublication(detail) {
  const payload = {
    publicationId: String(detail.publicationId || ''),
    version: Number(detail.version),
    scenarioId: String(detail.scenarioId || ''),
    title: String(detail.title || ''),
    scenario: detail.scenario || null,
  }
  window.dispatchEvent(new CustomEvent(REMIX_PUBLICATION_EVENT, { detail: payload }))
  return payload
}

export function onPlayPublication(handler) {
  const wrapped = (event) => handler(event.detail)
  window.addEventListener(PLAY_PUBLICATION_EVENT, wrapped)
  return () => window.removeEventListener(PLAY_PUBLICATION_EVENT, wrapped)
}

export function onRemixPublication(handler) {
  const wrapped = (event) => handler(event.detail)
  window.addEventListener(REMIX_PUBLICATION_EVENT, wrapped)
  return () => window.removeEventListener(REMIX_PUBLICATION_EVENT, wrapped)
}
