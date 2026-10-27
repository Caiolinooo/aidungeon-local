import { request } from '../api.js'

export function listMods(q = '') {
  const params = new URLSearchParams()
  if (q) params.set('q', q)
  const suffix = params.toString()
  return request(`/mods${suffix ? `?${suffix}` : ''}`)
}

export function getMod(modId) {
  return request(`/mods/${modId}`)
}

export function getModVersion(modId, versionId) {
  return request(`/mods/${modId}/versions/${versionId}`)
}

export function publishMod(body) {
  return request('/mods', { method: 'POST', body })
}

export function publishModVersion(modId, body) {
  return request(`/mods/${modId}/versions`, { method: 'POST', body })
}

export function listAdventureMods(adventureId) {
  return request(`/adventures/${adventureId}/mods`)
}

export function applyMod(adventureId, modVersionId) {
  return request(`/adventures/${adventureId}/mods`, {
    method: 'POST',
    body: { mod_version_id: modVersionId },
  })
}

export function enableAdventureMod(adventureId, versionId) {
  return request(`/adventures/${adventureId}/mods/${versionId}/enable`, { method: 'POST', body: {} })
}

export function disableAdventureMod(adventureId, versionId) {
  return request(`/adventures/${adventureId}/mods/${versionId}/disable`, { method: 'POST', body: {} })
}

export function removeAdventureMod(adventureId, versionId) {
  return request(`/adventures/${adventureId}/mods/${versionId}`, { method: 'DELETE' })
}
