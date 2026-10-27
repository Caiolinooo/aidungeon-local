/** Inícios nomeados de um scenario. O primeiro é o padrão e espelha opening_text. */

export function newStoryStartId() {
  const cryptoObj = globalThis.crypto
  if (cryptoObj?.randomUUID) return cryptoObj.randomUUID()
  return `start-${Date.now().toString(16)}-${Math.random().toString(16).slice(2)}`
}

export function storyStartsFromScenario(scenario) {
  const raw = Array.isArray(scenario?.story_starts) ? scenario.story_starts : []
  const starts = raw
    .filter((item) => item && String(item.title || '').trim() && String(item.text || '').trim())
    .map((item) => ({
      id: String(item.id || newStoryStartId()),
      title: String(item.title || ''),
      text: String(item.text || ''),
    }))
  if (starts.length) return starts
  const opening = String(scenario?.opening_text || '')
  if (!opening.trim()) return []
  return [{ id: newStoryStartId(), title: 'Opening', text: opening }]
}

export function openingFromStarts(starts) {
  return String(starts?.[0]?.text || '')
}

/** Atualiza o texto do início padrão, ou cria um se o texto novo não for vazio. */
export function applyOpeningText(starts, opening) {
  const next = (starts || []).map((item) => ({ ...item }))
  const text = String(opening ?? '')
  if (!text.trim()) return next
  if (next.length) next[0] = { ...next[0], text }
  else next.push({ id: newStoryStartId(), title: 'Opening', text })
  return next
}

export function replaceWithOpening(opening) {
  const text = String(opening ?? '')
  if (!text.trim()) return []
  return [{ id: newStoryStartId(), title: 'Opening', text }]
}

export function storyStartsPayload(starts) {
  return (starts || []).map((start) => ({
    id: start.id,
    title: String(start.title || '').trim(),
    text: String(start.text || '').trim(),
  }))
}

export function storyStartsAreValid(starts) {
  return (starts || []).every(
    (start) => String(start.title || '').trim() && String(start.text || '').trim(),
  )
}
