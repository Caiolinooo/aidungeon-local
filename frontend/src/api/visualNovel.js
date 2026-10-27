// Replay read-only da visual novel.
// Lê ações, retratos, cenários e sugestões já persistidos. Não chama
// POST /scene, /choices, /see nem /portraits — esses endpoints geram mídia ou texto.
//
// GET /api/adventures/:id
// GET /api/adventures/:id/cards
// GET /api/adventures/:id/jobs?status=done
// GET /api/adventures/:id/events/history?after&limit
// GET /api/adventures/:id/map
// A página busca GET /api/images/:imageId com Bearer (api.fetchImageBlob).

import { request } from '../api.js'
import { playerDisplayText } from '../utils.js'

export const VISUAL_NOVEL_ENDPOINTS = Object.freeze([
  'GET /api/adventures/:id',
  'GET /api/adventures/:id/cards',
  'GET /api/adventures/:id/jobs?status=done',
  'GET /api/adventures/:id/events/history',
  'GET /api/adventures/:id/map',
  'GET /api/images/:imageId',
])

const PLAYER_TYPES = new Set(['do', 'say', 'story'])
const NARRATIVE_TYPES = new Set(['start', 'continue'])
const HISTORY_PAGE = 500
const HISTORY_PAGES = 40
const CAST_LIMIT = 4

export function initialCursor() {
  return { index: 0, rejected: '' }
}

export function isEnd(cursor, count) {
  return count > 0 && Number(cursor?.index) >= count
}

export function positionLabel(index, count) {
  const total = Math.max(0, Number(count) || 0)
  if (total === 0) return '0 / 0'
  const at = Number(index) || 0
  if (at >= total) return `fim · ${total}`
  return `${at + 1} / ${total}`
}

export function viewIndex(cursor, count) {
  const total = Math.max(0, Number(count) || 0)
  if (total === 0) return -1
  const index = Number(cursor?.index) || 0
  if (index >= total) return total - 1
  if (index < 0) return 0
  return index
}

export function reduceNovel(state, action, beatCount) {
  const count = Math.max(0, Number(beatCount) || 0)
  const current = normalizeCursor(state, count)
  const type = action?.type
  if (type === 'next') {
    if (count === 0 || current.index >= count) return clearRejected(current)
    return { index: current.index + 1, rejected: '' }
  }
  if (type === 'prev') {
    if (current.index <= 0) return { index: 0, rejected: '' }
    return { index: current.index - 1, rejected: '' }
  }
  if (type === 'restart') return { index: 0, rejected: '' }
  if (type === 'end') return { index: count, rejected: '' }
  if (type === 'jump') {
    const raw = Number(action.index)
    const index = Number.isFinite(raw) ? Math.min(count, Math.max(0, raw)) : current.index
    return { index, rejected: '' }
  }
  if (type === 'reject') {
    return { index: current.index, rejected: String(action.text || '') }
  }
  return state && typeof state === 'object' ? state : current
}

export function selectChoice(state, choice, beats) {
  const list = Array.isArray(beats) ? beats : []
  if (!choice?.taken) {
    return reduceNovel(state, { type: 'reject', text: choice?.text || '' }, list.length)
  }
  const index = list.findIndex((beat) => beat.seq === choice.actionSeq && beat.type === choice.type)
  if (index < 0) return reduceNovel(state, { type: 'next' }, list.length)
  return reduceNovel(state, { type: 'jump', index }, list.length)
}

export function commandForKey(key, meta = {}) {
  if (meta.altKey || meta.metaKey || meta.ctrlKey) return null
  if (meta.editable) return null
  const tag = String(meta.tag || '').toUpperCase()
  if (tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT') return null
  if (key === ' ' || key === 'Spacebar') {
    if (tag === 'BUTTON' || tag === 'A') return null
    return { type: 'next' }
  }
  if (key === 'ArrowRight') return { type: 'next' }
  if (key === 'ArrowLeft') return { type: 'prev' }
  if (key === 'Home') return { type: 'restart' }
  if (key === 'End') return { type: 'end' }
  return null
}

export async function loadVisualNovel(adventureId, { signal } = {}) {
  const id = String(adventureId || '').trim()
  if (!id) {
    const error = new Error('adventure id is required')
    error.code = 'adventure_required'
    throw error
  }
  const pathId = encodeURIComponent(id)
  const adventure = await request(`/adventures/${pathId}`, { signal })
  const [cards, jobs, events, map] = await Promise.all([
    optionalRequest(`/adventures/${pathId}/cards`, signal),
    optionalRequest(`/adventures/${pathId}/jobs?status=done`, signal),
    loadHistory(pathId, signal),
    optionalRequest(`/adventures/${pathId}/map`, signal),
  ])
  return mapNovel({
    adventure,
    cards: Array.isArray(cards) ? cards : [],
    jobs: Array.isArray(jobs) ? jobs : (Array.isArray(jobs?.jobs) ? jobs.jobs : []),
    events: Array.isArray(events) ? events : [],
    map: map && typeof map === 'object' ? map : null,
  })
}

export function mapNovel({ adventure, cards, jobs, events, map } = {}) {
  const source = adventure && typeof adventure === 'object' ? adventure : {}
  const people = peopleFrom(Array.isArray(cards) && cards.length ? cards : source.story_cards)
  const locations = locationsFrom(people.allCards, map)
  const suggestions = suggestionsBySeq(events)
  const actions = (Array.isArray(source.actions) ? source.actions : [])
    .slice()
    .sort((a, b) => seqOf(a) - seqOf(b) || String(a?.id ?? '').localeCompare(String(b?.id ?? '')))

  const beats = actions.map((action) => makeBeat(action, people))
  attachChoices(beats, suggestions)
  attachOpenSuggestions(beats, suggestions)
  applyBackgrounds(beats, backdropImages(jobs), locations)

  return {
    adventureId: String(source.id || ''),
    title: String(source.title || ''),
    beats: beats.map(publishBeat),
  }
}

function normalizeCursor(state, count) {
  let index = Number(state?.index)
  if (!Number.isFinite(index)) index = 0
  if (index < 0) index = 0
  if (index > count) index = count
  const rejected = typeof state?.rejected === 'string' ? state.rejected : ''
  return { index, rejected }
}

function clearRejected(cursor) {
  return cursor.rejected ? { index: cursor.index, rejected: '' } : cursor
}

function isAbort(error) {
  return error?.name === 'AbortError'
}

async function optionalRequest(path, signal) {
  try {
    return await request(path, { signal })
  } catch (error) {
    if (isAbort(error)) throw error
    return null
  }
}

async function loadHistory(pathId, signal) {
  const events = []
  let after = 0
  for (let page = 0; page < HISTORY_PAGES; page += 1) {
    let data
    try {
      data = await request(`/adventures/${pathId}/events/history?after=${after}&limit=${HISTORY_PAGE}`, { signal })
    } catch (error) {
      if (isAbort(error)) throw error
      return events
    }
    const batch = Array.isArray(data?.events) ? data.events : []
    events.push(...batch)
    if (!data?.has_more || batch.length === 0) break
    const next = Number(data?.next_cursor)
    const fallback = Number(batch[batch.length - 1]?.event_id)
    const cursor = Number.isFinite(next) ? next : fallback
    if (!Number.isFinite(cursor) || cursor <= after) break
    after = cursor
  }
  return events
}

function seqOf(action) {
  const value = Number(action?.seq)
  return Number.isFinite(value) ? value : 0
}

function norm(text) {
  return String(text || '').replace(/\s+/g, ' ').trim().toLowerCase()
}

function escapeRegExp(text) {
  return String(text).replace(/[.*+?^${}()|[\]\\]/g, '\\$&')
}

function mentioned(text, title) {
  const name = String(title || '').trim()
  if (name.length < 2 || !text) return false
  const pattern = new RegExp(`(^|[^\\p{L}\\p{N}])${escapeRegExp(name)}([^\\p{L}\\p{N}]|$)`, 'iu')
  return pattern.test(String(text))
}

export function realImageRef(image) {
  if (!image || typeof image !== 'object') return null
  const id = typeof image.id === 'string' ? image.id.trim() : ''
  const url = typeof image.url === 'string' ? image.url.trim() : ''
  const prompt = typeof image.prompt === 'string' ? image.prompt : ''
  const safeId = /^[A-Za-z0-9_-]+$/.test(id)
  if (url && isBackendImageUrl(url)) {
    return { id: safeId ? id : idFromUrl(url), url, prompt }
  }
  if (safeId) return { id, url: `/api/images/${id}`, prompt }
  return null
}

function isBackendImageUrl(url) {
  try {
    const parsed = new URL(url, 'http://local')
    return /^\/api\/images\/[^/]+$/.test(parsed.pathname)
  } catch {
    return false
  }
}

function idFromUrl(url) {
  try {
    const parsed = new URL(url, 'http://local')
    return decodeURIComponent(parsed.pathname.split('/').pop() || '')
  } catch {
    return ''
  }
}

function portraitFromCard(card) {
  if (!card || typeof card !== 'object') return null
  const embedded = realImageRef(card.portrait)
  if (embedded) return embedded
  const id = typeof card.portrait_image_id === 'string' ? card.portrait_image_id.trim() : ''
  if (!id) return null
  return realImageRef({ id, url: '', prompt: card.portrait_prompt || '' })
}

function peopleFrom(cards) {
  const allCards = Array.isArray(cards) ? cards.filter((card) => card && typeof card === 'object') : []
  const people = allCards.filter((card) => {
    const type = String(card.type || '').toLowerCase()
    if (type === 'location') return false
    if (type === 'character') return true
    if (String(card.player_username || '').trim()) return true
    return false
  })
  return { allCards, people }
}

function locationsFrom(cards, map) {
  const byKey = new Map()
  const put = (entry) => {
    const title = String(entry?.title || '').trim()
    if (title.length < 2) return
    const key = norm(title)
    const image = realImageRef(entry?.image)
    const previous = byKey.get(key)
    if (!previous || (!previous.image && image)) {
      byKey.set(key, { title, image: image || previous?.image || null })
    }
  }
  for (const card of cards || []) {
    if (String(card.type || '').toLowerCase() !== 'location') continue
    put({ title: card.title, image: null })
  }
  const nodes = Array.isArray(map?.locations) ? map.locations : []
  for (const node of nodes) put(node)
  return [...byKey.values()]
}

function suggestionsBySeq(events) {
  const map = new Map()
  const list = Array.isArray(events) ? events : []
  for (const event of list) {
    if (event?.type !== 'choices_ready') continue
    const payload = event.payload && typeof event.payload === 'object' ? event.payload : {}
    const seq = Number(payload.for_seq)
    if (!Number.isFinite(seq) || !Array.isArray(payload.choices)) continue
    map.set(seq, payload.choices)
  }
  return map
}

function makeBeat(action, people) {
  const type = String(action?.type || '')
  const text = displayText(type, action?.text)
  const actor = String(action?.actor || '').trim()
  const speaker = speakerOf(type, actor, people.people)
  const ownImage = type === 'see' ? realImageRef(action?.image) : null
  return {
    key: `${seqOf(action)}:${action?.id ?? ''}`,
    seq: seqOf(action),
    actionId: action?.id ?? null,
    type,
    kind: kindOf(type),
    text,
    createdAt: typeof action?.created_at === 'string' ? action.created_at : '',
    speaker: speaker?.name || '',
    speakerLabel: labelOf(type, speaker?.name || ''),
    portrait: speaker?.portrait || null,
    cast: castOf(text, speaker, people.people),
    ownImage,
    choices: [],
    locationTitle: '',
    locationImage: null,
    background: null,
  }
}

function displayText(type, text) {
  if (type === 'do' || type === 'say' || type === 'story') return playerDisplayText(type, text)
  return String(text || '').trim()
}

function kindOf(type) {
  if (type === 'say') return 'dialogue'
  if (type === 'do') return 'action'
  if (type === 'see') return 'scene'
  return 'narration'
}

function labelOf(type, speaker) {
  if (type === 'say') return speaker || 'Você'
  if (type === 'do') return speaker || 'Você'
  if (type === 'story') return speaker || 'Você'
  if (type === 'see') return 'Cena'
  return 'Narrador'
}

function speakerOf(type, actor, people) {
  if (!PLAYER_TYPES.has(type)) return null
  if (actor) {
    const named = findPerson(people, actor)
    return { name: named?.title || actor, portrait: named?.portrait || null }
  }
  const players = people.filter((card) => String(card.player_username || '').trim())
  if (players.length === 1) {
    const card = players[0]
    return { name: 'Você', portrait: portraitFromCard(card) }
  }
  return { name: 'Você', portrait: null }
}

function findPerson(people, name) {
  const wanted = norm(name)
  if (!wanted) return null
  const exact = people.find((card) => norm(card.title) === wanted || norm(card.player_username) === wanted)
  if (exact) return { title: String(exact.title || exact.player_username || name), portrait: portraitFromCard(exact) }
  return null
}

function castOf(text, speaker, people) {
  const found = []
  const seen = new Set()
  const push = (title, portrait, speaking) => {
    const key = norm(title)
    if (!key || seen.has(key)) return
    seen.add(key)
    found.push({ title, portrait: portrait || null, speaking: !!speaking })
  }
  if (speaker?.name) push(speaker.name, speaker.portrait, true)
  const ranked = people
    .map((card) => ({ card, title: String(card.title || '').trim() }))
    .filter((item) => item.title.length >= 2 && mentioned(text, item.title))
    .sort((a, b) => b.title.length - a.title.length)
  for (const item of ranked) {
    if (found.length >= CAST_LIMIT) break
    push(item.title, portraitFromCard(item.card), norm(item.title) === norm(speaker?.name))
  }
  return found
}

function attachChoices(beats, suggestions) {
  for (let index = 0; index < beats.length - 1; index += 1) {
    const next = beats[index + 1]
    if (!PLAYER_TYPES.has(next.type)) continue
    const recorded = {
      type: next.type,
      text: next.text,
      taken: true,
      actionSeq: next.seq,
    }
    const offered = suggestionsFor(beats, index + 1, suggestions)
    const choices = []
    let matched = false
    for (const suggestion of offered) {
      const text = String(suggestion?.text || '').trim()
      if (!text) continue
      const type = PLAYER_TYPES.has(suggestion?.type) ? suggestion.type : next.type
      const taken = !matched && type === next.type && norm(text) === norm(next.text)
      if (taken) matched = true
      choices.push(taken
        ? recorded
        : { type, text, taken: false, actionSeq: null })
    }
    if (!matched) choices.unshift(recorded)
    else choices.sort((a, b) => Number(b.taken) - Number(a.taken))
    beats[index].choices = choices
  }
}

function attachOpenSuggestions(beats, suggestions) {
  for (let index = beats.length - 1; index >= 0; index -= 1) {
    const type = beats[index].type
    if (PLAYER_TYPES.has(type)) return
    if (!NARRATIVE_TYPES.has(type)) continue
    if (beats[index].choices.length > 0) return
    beats[index].choices = untakenChoices(suggestions.get(beats[index].seq) || [])
    return
  }
}

function untakenChoices(offered) {
  const choices = []
  for (const suggestion of offered) {
    const text = String(suggestion?.text || '').trim()
    if (!text) continue
    const type = PLAYER_TYPES.has(suggestion?.type) ? suggestion.type : 'do'
    choices.push({ type, text, taken: false, actionSeq: null })
  }
  return choices
}

function suggestionsFor(beats, playerIndex, suggestions) {
  for (let index = playerIndex - 1; index >= 0; index -= 1) {
    const type = beats[index].type
    if (PLAYER_TYPES.has(type)) return []
    if (NARRATIVE_TYPES.has(type)) return suggestions.get(beats[index].seq) || []
  }
  return []
}

function backdropImages(jobs) {
  const list = Array.isArray(jobs) ? jobs : []
  return list
    .filter((job) => job?.type === 'scene_backdrop' && job?.status === 'done')
    .map((job) => ({
      at: typeof job.finished_at === 'string' && job.finished_at
        ? job.finished_at
        : '',
      image: realImageRef(job.result?.image),
    }))
    .filter((item) => item.image && item.at)
    .sort((a, b) => (a.at < b.at ? -1 : a.at > b.at ? 1 : 0))
}

function applyBackgrounds(beats, backdrops, locations) {
  let cursor = 0
  let scene = null
  for (const beat of beats) {
    const place = matchNamed(beat.text, locations)
    beat.locationTitle = place?.title || ''
    beat.locationImage = place?.image || null
    const at = beat.createdAt
    while (cursor < backdrops.length && at && backdrops[cursor].at <= at) {
      scene = { image: backdrops[cursor].image, at: backdrops[cursor].at }
      cursor += 1
    }
    if (beat.ownImage) scene = { image: beat.ownImage, at: at || scene?.at || '' }
    beat.background = scene?.image || (!scene ? beat.locationImage : null)
  }
}

function matchNamed(text, entries) {
  let best = null
  for (const entry of entries || []) {
    if (!mentioned(text, entry.title)) continue
    if (!best || entry.title.length > best.title.length) best = entry
  }
  return best
}

function publishBeat(beat) {
  return {
    key: beat.key,
    seq: beat.seq,
    actionId: beat.actionId,
    type: beat.type,
    kind: beat.kind,
    text: beat.text,
    createdAt: beat.createdAt,
    speaker: beat.speaker,
    speakerLabel: beat.speakerLabel,
    portrait: beat.portrait,
    cast: beat.cast,
    choices: beat.choices,
    locationTitle: beat.locationTitle,
    background: beat.background,
  }
}
