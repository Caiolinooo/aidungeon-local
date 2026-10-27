import { request } from '../api.js'

export function getAdventureScripts(adventureId) {
  return request(`/adventures/${adventureId}/scripts`)
}

export function putAdventureScripts(adventureId, body) {
  return request(`/adventures/${adventureId}/scripts`, { method: 'PUT', body })
}
