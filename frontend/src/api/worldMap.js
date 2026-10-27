import { request } from '../api.js'

export function getWorldMap(adventureId) {
  return request(`/adventures/${adventureId}/map`)
}

export function travelTo(adventureId, cardId) {
  return request(`/adventures/${adventureId}/map/travel/${cardId}`, { method: 'POST', body: {} })
}

export function revealLocation(adventureId, cardId) {
  return request(`/adventures/${adventureId}/map/reveal/${cardId}`, { method: 'POST', body: {} })
}
