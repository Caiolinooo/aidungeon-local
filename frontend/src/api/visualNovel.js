// Visual novel: replay do que já foi salvo e continuação no beat mais recente.
// O carregamento usa só GET. Continuar envia POST /api/adventures/:id/action,
// o mesmo contrato do Play (SSE token/done/error, ou 202 JSON de intenção).
// O texto ao vivo do stream não vira beat. Sem ação gravada, não há narração.
// Não chama POST /scene, /choices, /see nem /portraits.
//
// GET /api/adventures/:id
// GET /api/adventures/:id/cards
// GET /api/adventures/:id/jobs?status=done
// GET /api/adventures/:id/events/history?after&limit
//   (choices_ready navega; eventos action completam o replay se o array vier curto)
// GET /api/adventures/:id/map
// O fundo é o job scene_backdrop já pronto, mesmo pintado depois da última ação.
// POST /api/adventures/:id/action
// A página busca GET /api/images/:imageId com Bearer (api.fetchImageBlob).

import { getToken } from '../auth.js'
import { API_BASE, notifyUnauthorized, request } from '../api.js'
import { playerDisplayText } from '../utils.js'

export const VISUAL_NOVEL_ENDPOINTS = Object.freeze([
  'GET /api/adventures/:id',
  'GET /api/adventures/:id/cards',
  'GET /api/adventures/:id/jobs?status=done',
  'GET /api/adventures/:id/events/history',
  'GET /api/adventures/:id/map',
  'GET /api/images/:imageId',
])

export const VISUAL_NOVEL_ACTION_ENDPOINT = 'POST /api/adventures/:id/action'

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
  const eventList = listFrom(events, ['events', 'items', 'history'])
  const jobList = listFrom(jobs, ['jobs', 'items', 'results'])
  const storyCards = withPortraitJobs(
    preferList(cards, source.story_cards, ['cards', 'items', 'results']),
    jobList,
  )
  const people = peopleFrom(storyCards)
  const locations = locationsFrom(people.allCards, map)
  const suggestions = suggestionsBySeq(eventList)
  const actions = collectActions(source, eventList)
  const beats = actions.map((action) => makeBeat(action, people))
  attachChoices(beats, suggestions)
  attachOpenSuggestions(beats, suggestions)
  applyBackgrounds(beats, backdropImages(jobList), locations, storyCards)
  return {
    adventureId: String(source.id || ''),
    title: String(source.title || ''),
    beats: beats.map(publishBeat),
    play: {
      dmMode: !!source.dm_mode,
      owner: String(source.owner || ''),
      turnMode: !!source.turn_mode,
      turnUser: String(source.turn_user || ''),
    },
    source: {
      adventure: { ...source, actions },
      cards: storyCards,
      jobs: jobList,
      events: eventList,
      map: map && typeof map === 'object' ? map : null,
    },
  }
}

function listFrom(value, keys) {
  if (Array.isArray(value)) return value.filter((item) => item != null)
  if (typeof value === 'string') {
    const trimmed = value.trim()
    if (!trimmed.startsWith('[') && !trimmed.startsWith('{')) return []
    try {
      return listFrom(JSON.parse(trimmed), keys)
    } catch {
      return []
    }
  }
  if (!value || typeof value !== 'object') return []
  for (const key of keys) {
    if (value[key] == null) continue
    const nested = listFrom(value[key], keys)
    if (nested.length) return nested
  }
  return []
}

function preferList(primary, fallback, keys) {
  const first = listFrom(primary, keys)
  if (first.length) return first
  return listFrom(fallback, keys)
}

function parseJson(value) {
  if (typeof value !== 'string') return value && typeof value === 'object' ? value : null
  const trimmed = value.trim()
  if (!trimmed.startsWith('{') && !trimmed.startsWith('[')) return null
  try {
    const parsed = JSON.parse(trimmed)
    return parsed && typeof parsed === 'object' ? parsed : null
  } catch {
    return null
  }
}

function firstText(...values) {
  for (const value of values) {
    if (typeof value === 'string' && value.trim()) return value.trim()
  }
  return ''
}

function collectActions(adventure, events) {
  const source = adventure && typeof adventure === 'object' ? adventure : {}
  const raw = [
    ...actionRecords(source.actions),
    ...actionRecords(source.history),
  ]
  if (!raw.length && source.adventure && typeof source.adventure === 'object') {
    raw.push(...actionRecords(source.adventure.actions))
  }
  for (const event of events || []) raw.push(...actionsFromEvent(event))
  const normalized = raw.flatMap((item) => flattenAction(item)).map(normalizeAction).filter(Boolean)
  return dedupeActions(normalized)
    .sort((a, b) => seqOf(a) - seqOf(b) || String(a?.id ?? '').localeCompare(String(b?.id ?? '')))
}

function actionRecords(value) {
  const listed = listFrom(value, ['items', 'actions', 'results', 'data', 'history'])
  if (listed.length) return listed
  if (!value || typeof value !== 'object' || Array.isArray(value)) return []
  if (value.type && (value.text != null || value.value != null || value.seq != null || value.id != null)) {
    return [value]
  }
  const values = Object.values(value)
  if (
    values.length
    && values.every((item) => item && typeof item === 'object' && !Array.isArray(item)
      && (item.type || item.text != null || item.value != null || item.seq != null))
  ) {
    return values
  }
  return []
}

function actionsFromEvent(event) {
  if (!event || typeof event !== 'object') return []
  const payload = event.payload && typeof event.payload === 'object' ? event.payload : null
  if (!payload) return []
  if (event.type === 'action') {
    return [{
      ...payload,
      seq: payload.seq ?? event.turn_seq,
      created_at: payload.created_at || payload.createdAt || event.created_at || '',
    }]
  }
  const nested = []
  if (payload.player_action && typeof payload.player_action === 'object') nested.push(payload.player_action)
  if (payload.action && typeof payload.action === 'object' && payload.action.type) nested.push(payload.action)
  return nested
}

function flattenAction(raw) {
  if (!raw || typeof raw !== 'object' || Array.isArray(raw)) return []
  const nested = []
  for (const key of ['replies', 'children', 'actions']) {
    if (Array.isArray(raw[key])) nested.push(...raw[key])
  }
  return [raw, ...nested.flatMap((item) => flattenAction(item))]
}

function normalizeAction(raw) {
  if (!raw || typeof raw !== 'object' || Array.isArray(raw)) return null
  const type = String(raw.type || raw.action_type || '').trim().toLowerCase()
  let text = raw.text ?? raw.value ?? raw.content ?? ''
  if (text && typeof text === 'object') text = text.text ?? text.value ?? ''
  text = typeof text === 'string' ? text : String(text ?? '')
  const seq = raw.seq ?? raw.action_seq ?? raw.turn_seq
  const id = raw.id ?? raw.action_id ?? null
  if (!type && !text.trim() && seq == null && id == null) return null
  const created = firstText(raw.created_at, raw.createdAt, raw.finished_at)
  return {
    ...raw,
    id,
    seq,
    type: type || 'story',
    text,
    created_at: created,
    actor: String(raw.actor || raw.username || '').trim(),
    image: raw.image ?? null,
  }
}

function dedupeActions(list) {
  const seenId = new Set()
  const seenText = new Set()
  const out = []
  for (const action of list) {
    const id = action?.id == null || action.id === '' ? '' : String(action.id)
    const key = `${seqOf(action)}:${action.type}:${norm(action.text)}`
    if (id && seenId.has(id)) continue
    if (seenText.has(key)) continue
    if (id) seenId.add(id)
    seenText.add(key)
    out.push(action)
  }
  return out
}

function withPortraitJobs(cards, jobs) {
  const list = Array.isArray(cards) ? cards.map((card) => ({ ...card })) : []
  const byId = new Map(list.map((card) => [String(card.id || ''), card]))
  for (const job of jobs || []) {
    const type = String(job?.type || '').toLowerCase()
    if (type !== 'character_portrait' && type !== 'portrait') continue
    if (!['done', 'completed', 'success'].includes(String(job?.status || '').toLowerCase())) continue
    const result = parseJson(job?.result) || {}
    const image = imageFromJob(job)
    const cardId = String(job?.ref_id || result.card_id || '')
    if (!image || !cardId || !byId.has(cardId)) continue
    const card = byId.get(cardId)
    if (portraitFromCard(card)) continue
    card.portrait = image
  }
  return list
}

function imageFromJob(job) {
  const result = parseJson(job?.result) || {}
  const payload = parseJson(job?.payload) || {}
  return realImageRef(result.image)
    || realImageRef(result.image_id)
    || realImageRef(payload.image)
    || realImageRef(result)
}

function castIdsFrom(job, result) {
  const payload = parseJson(job?.payload) || {}
  const ids = payload.cast_card_ids || result?.cast_card_ids || payload.cast_ids || []
  return Array.isArray(ids) ? ids.map((id) => String(id)) : []
}

function mergeCastIds(beat, castIds, cards) {
  if (!beat || !Array.isArray(castIds) || castIds.length === 0) return
  const cast = Array.isArray(beat.cast) ? beat.cast.slice() : []
  const seen = new Set(cast.map((person) => norm(person?.title)))
  for (const id of castIds) {
    const card = (cards || []).find((item) => String(item?.id || '') === String(id))
    if (!card) continue
    const title = String(card.title || '').trim()
    const key = norm(title)
    if (!key || seen.has(key)) continue
    seen.add(key)
    cast.push({ title, portrait: portraitFromCard(card), speaking: false })
    if (cast.length >= CAST_LIMIT) break
  }
  beat.cast = cast
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
    const batch = listFrom(data, ['events', 'items', 'history'])
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
  if (typeof image === 'string' || typeof image === 'number') {
    return realImageRef({ id: image, url: '' })
  }
  if (!image || typeof image !== 'object') return null
  const rawId = image.id ?? image.image_id ?? image.portrait_image_id ?? ''
  const id = String(rawId ?? '').trim()
  const url = typeof image.url === 'string' ? image.url.trim() : ''
  const prompt = typeof image.prompt === 'string' ? image.prompt : ''
  const safeId = /^[A-Za-z0-9_-]+$/.test(id)
  if (url && isBackendImageUrl(url)) {
    return { id: safeId ? id : idFromUrl(url), url, prompt }
  }
  if (!url && id && isBackendImageUrl(id)) {
    return { id: idFromUrl(id), url: id.startsWith('/') ? id : `/${id.replace(/^\/+/, '')}`, prompt }
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
  const id = String(card.portrait_image_id ?? card.portrait_id ?? '').trim()
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
    const image = realImageRef(entry?.image) || realImageRef(entry?.image_id)
    const previous = byKey.get(key)
    const current = !!(entry?.current || previous?.current)
    if (!previous || (!previous.image && image) || (current && !previous.current)) {
      byKey.set(key, { title, image: image || previous?.image || null, current: current || !!previous?.current })
    }
  }
  for (const card of cards || []) {
    if (String(card.type || '').toLowerCase() !== 'location') continue
    put({ title: card.title, image: card.image || card.image_id || null })
  }
  const source = map && typeof map === 'object' ? map : {}
  const nodes = listFrom(source.locations, ['locations', 'items', 'nodes'])
  const currentId = String(source.current?.id || source.current_id || '')
  for (const node of nodes) {
    put({ ...node, current: !!(node?.current || (currentId && node?.id === currentId)) })
  }
  if (source.current && typeof source.current === 'object') {
    put({ ...source.current, current: true, image: source.current.image || null })
  }
  return [...byKey.values()]
}

function suggestionsBySeq(events) {
  const map = new Map()
  const list = listFrom(events, ['events', 'items', 'history'])
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
  const ownImage = realImageRef(action?.image) || realImageRef(action?.image_id)
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
  const list = listFrom(jobs, ['jobs', 'items', 'results'])
  return list
    .filter((job) => {
      const type = String(job?.type || '').toLowerCase()
      const status = String(job?.status || '').toLowerCase()
      return (type === 'scene_backdrop' || type === 'backdrop')
        && (status === 'done' || status === 'completed' || status === 'success')
    })
    .map((job) => {
      const result = parseJson(job?.result)
      const at = firstText(job?.finished_at, job?.created_at, job?.createdAt, job?.started_at)
      return {
        at,
        image: imageFromJob(job),
        castIds: castIdsFrom(job, result),
      }
    })
    .filter((item) => item.image)
    .sort((a, b) => {
      if (a.at && b.at) return a.at < b.at ? -1 : a.at > b.at ? 1 : 0
      if (a.at) return -1
      if (b.at) return 1
      return 0
    })
}

function applyBackgrounds(beats, backdrops, locations, cards) {
  const current = (locations || []).find((entry) => entry.current && (entry.image || entry.title))
  const timed = []
  const untimed = []
  for (const item of backdrops || []) {
    if (item?.at) timed.push(item)
    else untimed.push(item)
  }
  let cursor = 0
  let scene = null
  for (const beat of beats) {
    const place = matchNamed(beat.text, locations)
    beat.locationTitle = place?.title || ''
    beat.locationImage = place?.image || null
    const at = beat.createdAt
    while (cursor < timed.length && at && timed[cursor].at <= at) {
      scene = timed[cursor]
      cursor += 1
    }
    if (beat.ownImage) scene = { image: beat.ownImage, at: at || scene?.at || '', castIds: [] }
    const painted = beat.ownImage || scene?.image || null
    beat.background = painted || beat.locationImage || current?.image || null
    if (!beat.locationTitle && current?.title && beat.background === current.image) {
      beat.locationTitle = current.title
    }
    mergeCastIds(beat, scene?.castIds, cards)
  }
  const pending = timed.slice(cursor)
  const latest = pending.length
    ? pending[pending.length - 1]
    : (timed.length ? timed[timed.length - 1] : (untimed[untimed.length - 1] || null))
  if (latest?.image) {
    for (const beat of beats) {
      if (beat.background || beat.ownImage) continue
      beat.background = latest.image
      if (!beat.locationTitle && current?.title) beat.locationTitle = current.title
      mergeCastIds(beat, latest.castIds, cards)
    }
  }
  if (pending.length && beats.length && !beats[beats.length - 1].ownImage) {
    const last = beats[beats.length - 1]
    last.background = pending[pending.length - 1].image
    if (!last.locationTitle && current?.title) last.locationTitle = current.title
    mergeCastIds(last, pending[pending.length - 1].castIds, cards)
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

export function canWriteReply(cursor, count) {
  const total = Math.max(0, Number(count) || 0)
  if (total === 0) return true
  const index = Number(cursor?.index)
  if (!Number.isFinite(index)) return false
  return index === total - 1
}

export function presentChoices(choices) {
  const items = (Array.isArray(choices) ? choices : []).map((choice) => {
    const type = PLAYER_TYPES.has(choice?.type) ? choice.type : 'do'
    const text = String(choice?.text || '').trim()
    const taken = !!choice?.taken && choice?.actionSeq != null
    return {
      type,
      text,
      taken,
      actionSeq: taken ? choice.actionSeq : null,
      effect: taken ? 'jump' : 'none',
    }
  })
  return { navigationOnly: true, items }
}

export function latestTurnBeats(beats, index) {
  const list = Array.isArray(beats) ? beats : []
  const at = Number(index)
  if (!Number.isFinite(at) || at < 0 || at >= list.length) return []
  const current = list[at]
  const previous = list[at - 1]
  if (
    at === list.length - 1
    && previous
    && PLAYER_TYPES.has(previous.type)
    && NARRATIVE_TYPES.has(current?.type)
  ) {
    return [previous, current]
  }
  return current ? [current] : []
}

export function beginReply({ busy = false, text = '', type = '' } = {}) {
  if (busy) return null
  if (!PLAYER_TYPES.has(type)) return null
  const trimmed = String(text || '').trim()
  if (!trimmed) return null
  return { type, text: trimmed }
}

// Enter e Ctrl/Cmd+Enter enviam. Shift+Enter quebra a linha. Composição IME não envia.
export function replyKeyAction(event) {
  if (!event || event.key !== 'Enter') return null
  if (event.isComposing || event.keyCode === 229) return null
  if (event.shiftKey) return 'line'
  return 'send'
}

export function imageMediaKind(type) {
  const value = String(type || '').toLowerCase().split(';', 1)[0].trim()
  if (value.startsWith('video/')) return 'video'
  if (!value || value === 'application/octet-stream' || value.startsWith('image/')) return 'image'
  return 'invalid'
}

export function appendStreamResult(model, done) {
  const adventure = model?.source?.adventure
  if (!adventure || typeof adventure !== 'object') return model
  const incoming = []
  if (done?.player_action && typeof done.player_action === 'object') incoming.push(done.player_action)
  if (done?.action && typeof done.action === 'object') incoming.push(done.action)
  if (!incoming.length) return model
  const existing = Array.isArray(adventure.actions) ? adventure.actions : []
  const seen = new Set(existing.map((action) => (action?.id == null ? '' : String(action.id))).filter(Boolean))
  const actions = existing.slice()
  let changed = false
  for (const action of incoming) {
    const id = action?.id == null ? '' : String(action.id)
    if (id && seen.has(id)) continue
    if (id) seen.add(id)
    actions.push(action)
    changed = true
  }
  if (!changed) return model
  return mapNovel({
    adventure: { ...adventure, actions },
    cards: model.source.cards,
    jobs: model.source.jobs,
    events: model.source.events,
    map: model.source.map,
  })
}

export function novelAfterSubmit(model, result) {
  if (result?.intent) {
    return { model, committed: false, intent: result.intent, missing: false }
  }
  const next = appendStreamResult(model, result?.done)
  const committed = !!next && next !== model && (next.beats?.length || 0) > (model?.beats?.length || 0)
  if (!committed) return { model, committed: false, intent: null, missing: true }
  return { model: next, committed: true, intent: null, missing: false }
}

export async function submitNovelAction(adventureId, {
  type,
  text,
  signal,
  onToken,
  onEvent,
} = {}) {
  const id = String(adventureId || '').trim()
  if (!id) {
    const error = new Error('adventure id is required')
    error.code = 'adventure_required'
    throw error
  }
  if (!PLAYER_TYPES.has(type)) {
    const error = new Error('tipo de ação inválido')
    error.code = 'type_required'
    throw error
  }
  const bodyText = String(text || '').trim()
  if (!bodyText) {
    const error = new Error('resposta vazia')
    error.code = 'text_required'
    throw error
  }
  const path = `/adventures/${encodeURIComponent(id)}/action`
  let res
  try {
    const headers = { 'Content-Type': 'application/json' }
    const token = getToken()
    if (token) headers.Authorization = `Bearer ${token}`
    res = await fetch(`${API_BASE}/api${path}`, {
      method: 'POST',
      headers,
      body: JSON.stringify({ type, text: bodyText }),
      signal,
    })
  } catch (error) {
    if (error?.name === 'AbortError') throw error
    const failure = new Error('Sem conexão com o servidor.')
    failure.status = 0
    failure.code = 'network_error'
    throw failure
  }
  if (signal?.aborted) {
    const error = new Error('aborted')
    error.name = 'AbortError'
    throw error
  }
  const contentType = res.headers.get('content-type') || ''
  if (!res.ok || contentType.includes('application/json')) return readActionJson(res)
  return readActionStream(res, { onToken, onEvent, signal })
}

async function readActionJson(res) {
  let data = null
  try {
    data = await res.json()
  } catch {
    data = null
  }
  if (!res.ok) {
    if (res.status === 401) notifyUnauthorized()
    const error = new Error(data?.error?.message || `Request failed (${res.status})`)
    error.status = res.status
    error.code = typeof data?.error?.code === 'string' ? data.error.code : ''
    throw error
  }
  if (data?.intent) return { intent: data.intent, done: null }
  const error = new Error('Resposta inesperada do servidor.')
  error.status = res.status
  error.code = 'invalid_response'
  throw error
}

async function readActionStream(res, { onToken, onEvent, signal }) {
  if (!res.body) {
    const error = new Error('A narração não chegou.')
    error.code = 'stream_incomplete'
    throw error
  }
  const reader = res.body.getReader()
  const decoder = new TextDecoder()
  let buffer = ''
  let donePayload = null
  let streamError = null
  let terminal = false
  const dispatch = (raw) => {
    const ev = parseSse(raw)
    if (!ev || terminal) return
    if (ev.event === 'token') onToken?.(ev.data?.token ?? '')
    else if (ev.event === 'done') {
      terminal = true
      donePayload = ev.data && typeof ev.data === 'object' ? ev.data : {}
    } else if (ev.event === 'error') {
      terminal = true
      streamError = new Error(ev.data?.message || 'Generation failed')
      streamError.status = ev.data?.status
      streamError.code = typeof ev.data?.code === 'string' ? ev.data.code : ''
    } else onEvent?.(ev.event, ev.data)
  }
  try {
    for (;;) {
      const { value, done } = await reader.read()
      if (done) break
      buffer += decoder.decode(value, { stream: true }).replace(/\r\n/g, '\n')
      let idx
      while ((idx = buffer.indexOf('\n\n')) >= 0) {
        const raw = buffer.slice(0, idx)
        buffer = buffer.slice(idx + 2)
        dispatch(raw)
      }
    }
    buffer += decoder.decode().replace(/\r\n/g, '\n')
    if (buffer.trim()) dispatch(buffer)
  } catch (error) {
    if (error?.name === 'AbortError') throw error
    if (!terminal) {
      const failure = new Error('Stream interrupted.')
      failure.code = 'stream_interrupted'
      throw failure
    }
  }
  if (signal?.aborted) {
    const error = new Error('aborted')
    error.name = 'AbortError'
    throw error
  }
  if (streamError) throw streamError
  if (!terminal || !donePayload) {
    const error = new Error('A narração não chegou.')
    error.code = 'stream_incomplete'
    throw error
  }
  return { intent: null, done: donePayload }
}

function parseSse(block) {
  let event = 'message'
  const dataLines = []
  for (const line of String(block || '').split('\n')) {
    if (line.startsWith('event:')) event = line.slice(6).trim()
    else if (line.startsWith('data:')) dataLines.push(line.slice(5).replace(/^ /, ''))
  }
  if (dataLines.length === 0) return null
  const text = dataLines.join('\n')
  let data = null
  try {
    data = JSON.parse(text)
  } catch {
    data = text
  }
  return { event, data }
}
