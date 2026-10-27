import { request } from '../api.js'

export function listQuests(id) {
  return request(`/adventures/${id}/sim/quests`)
}

export function createQuest(id, body) {
  return request(`/adventures/${id}/sim/quests`, { method: 'POST', body })
}

export function updateQuest(id, questId, body) {
  return request(`/adventures/${id}/sim/quests/${questId}`, { method: 'PATCH', body })
}

export function transitionQuest(id, questId, body) {
  return request(`/adventures/${id}/sim/quests/${questId}/transition`, { method: 'POST', body })
}

export function deleteQuest(id, questId) {
  return request(`/adventures/${id}/sim/quests/${questId}`, { method: 'DELETE' })
}

export function createTrigger(id, body) {
  return request(`/adventures/${id}/sim/triggers`, { method: 'POST', body })
}

export function deleteTrigger(id, triggerId) {
  return request(`/adventures/${id}/sim/triggers/${triggerId}`, { method: 'DELETE' })
}
