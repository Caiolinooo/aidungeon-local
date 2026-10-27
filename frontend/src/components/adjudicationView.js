// Projeção pública da adjudicação para o feed.
// O cue e qualquer campo de motivo oculto ficam no servidor.

const DECISIONS = ['allow', 'partial', 'refuse', 'own_beat']

export const DECISION_LABEL = {
  allow: 'Acontece',
  partial: 'Em parte',
  refuse: 'Não se concretiza',
  own_beat: 'Por conta própria',
}

const PLAYER_FIELDS = ['relationship', 'trust', 'distrust', 'fear', 'desire', 'boldness']
const NPC_FIELDS = ['trust', 'fear', 'affinity']

export const EMOTION_LABEL = {
  joy: 'alegria',
  happy: 'alegria',
  anger: 'raiva',
  angry: 'raiva',
  fear: 'medo',
  sadness: 'tristeza',
  sad: 'tristeza',
  tenderness: 'ternura',
  love: 'ternura',
  surprise: 'surpresa',
  disgust: 'nojo',
  neutral: 'neutro',
}

export function emotionLabel(value) {
  const key = String(value || '').trim().toLowerCase()
  return EMOTION_LABEL[key] || key
}

export const DELTA_LABEL = {
  relationship: 'relação',
  trust: 'confiança',
  distrust: 'desconfiança',
  fear: 'medo',
  desire: 'desejo',
  boldness: 'ousadia',
  affinity: 'afinidade',
}

const SECRET_TEXT = /\[Adjudicação|motivação oculta|hidden\s*:|motive\s*:|\bcue\b/i

export function adjudicationMode(type) {
  if (type === 'story' || type === 'retry') return 'ignore'
  return 'apply'
}

function cleanText(value, limit = 180) {
  const text = String(value ?? '').replace(/\s+/g, ' ').trim()
  if (!text || SECRET_TEXT.test(text)) return ''
  return text.slice(0, limit)
}

function isPlayerTarget(target) {
  return String(target ?? '').trim().toLowerCase() === 'player'
}

function integerDelta(value) {
  if (typeof value === 'boolean' || value == null || value === '') return null
  const number = Number(value)
  if (!Number.isFinite(number) || !Number.isInteger(number) || number === 0) return null
  return number
}

export function publicDelta(raw) {
  if (!raw || typeof raw !== 'object') return null
  const target = cleanText(raw.target, 80)
  if (!target) return null
  const fields = isPlayerTarget(target) ? PLAYER_FIELDS : NPC_FIELDS
  const delta = {}
  const source = raw.delta && typeof raw.delta === 'object' ? raw.delta : {}
  for (const key of fields) {
    const value = integerDelta(source[key])
    if (value != null) delta[key] = value
  }
  if (Object.keys(delta).length === 0) return null
  const reason = cleanText(raw.reason)
  const evidence = cleanText(raw.evidence)
  return {
    card_id: cleanText(raw.card_id, 80),
    name: cleanText(raw.name, 80) || 'Alguém',
    target,
    target_name: cleanText(raw.target_name, 80),
    delta,
    reason,
    evidence: evidence && evidence !== reason ? evidence : '',
  }
}

export function deltaKey(change) {
  const delta = Object.keys(change?.delta || {}).sort().map((key) => `${key}:${change.delta[key]}`).join(',')
  return [change?.card_id || '', change?.target || '', delta, change?.reason || '', change?.evidence || ''].join('|')
}

function publicReaction(raw) {
  if (!raw || typeof raw !== 'object') return null
  const reaction = cleanText(raw.reaction, 240)
  const line = cleanText(raw.line, 180)
  const emotion = cleanText(raw.emotion, 40)
  const pose = cleanText(raw.pose, 40)
  if (!reaction && !line && !emotion && !pose) return null
  return {
    npc: cleanText(raw.npc, 80) || 'Alguém',
    card_id: cleanText(raw.card_id, 80),
    reaction,
    line,
    emotion,
    pose,
  }
}

function publicInitiative(raw) {
  if (!raw || typeof raw !== 'object') return null
  const line = cleanText(raw.line, 180)
  const npc = cleanText(raw.npc, 80)
  if (!npc && !line) return null
  return {
    npc: npc || 'Alguém',
    card_id: cleanText(raw.card_id, 80),
    line,
  }
}

export function presentAdjudication(raw, { includeDeltas = true } = {}) {
  if (!raw || typeof raw !== 'object' || Array.isArray(raw)) return null
  const decision = DECISIONS.includes(raw.decision) ? raw.decision : ''
  const outcome = cleanText(raw.outcome, 280)
  const reactions = (Array.isArray(raw.reactions) ? raw.reactions : []).map(publicReaction).filter(Boolean).slice(0, 8)
  const initiative = publicInitiative(raw.initiative)
  const rawChanges = Array.isArray(raw.relation_changes)
    ? raw.relation_changes
    : (Array.isArray(raw.relation_updates) ? raw.relation_updates : [])
  const relationChanges = includeDeltas
    ? rawChanges.map(publicDelta).filter(Boolean).slice(0, 12)
    : []
  if (!decision && !outcome && reactions.length === 0 && !initiative && relationChanges.length === 0) return null
  return {
    decision,
    outcome,
    reactions,
    initiative,
    relation_changes: dedupeDeltas(relationChanges),
  }
}

function dedupeDeltas(changes) {
  const seen = new Set()
  const out = []
  for (const change of changes) {
    const key = deltaKey(change)
    if (seen.has(key)) continue
    seen.add(key)
    out.push(change)
  }
  return out
}

export function mergeBeat(base, extra) {
  if (!extra) return base || null
  if (!base) return extra
  return {
    decision: extra.decision || base.decision,
    outcome: extra.outcome || base.outcome,
    reactions: extra.reactions.length ? extra.reactions : base.reactions,
    initiative: extra.initiative || base.initiative,
    relation_changes: dedupeDeltas([...(base.relation_changes || []), ...(extra.relation_changes || [])]),
  }
}

export function storeBeat(bySeq, seq, incoming) {
  if (seq == null || !incoming) return bySeq
  const key = String(seq)
  const merged = mergeBeat(bySeq[key], incoming)
  if (!merged) return bySeq
  if (bySeq[key] && JSON.stringify(bySeq[key]) === JSON.stringify(merged)) return bySeq
  return { ...bySeq, [key]: merged }
}

export function dropBeats(bySeq, seqs) {
  const next = { ...bySeq }
  let changed = false
  for (const seq of seqs) {
    if (seq == null) continue
    const key = String(seq)
    if (key in next) {
      delete next[key]
      changed = true
    }
  }
  return changed ? next : bySeq
}

export function visualHints(beat) {
  const hints = {}
  for (const reaction of beat?.reactions || []) {
    if (!reaction.card_id) continue
    hints[reaction.card_id] = {
      emotion: reaction.emotion || '',
      pose: reaction.pose || '',
      action: reaction.reaction || '',
    }
  }
  if (beat?.initiative?.card_id) {
    const current = hints[beat.initiative.card_id] || { emotion: '', pose: '', action: '' }
    if (beat.initiative.line) current.action = beat.initiative.line
    hints[beat.initiative.card_id] = current
  }
  return hints
}

export function spotlightIds(beat) {
  if (!beat) return []
  const ids = []
  for (const reaction of beat.reactions || []) {
    if (reaction.card_id) ids.push(reaction.card_id)
  }
  if (beat.initiative?.card_id) ids.push(beat.initiative.card_id)
  for (const change of beat.relation_changes || []) {
    if (change.card_id) ids.push(change.card_id)
    if (change.target && !isPlayerTarget(change.target)) ids.push(change.target)
  }
  return [...new Set(ids)]
}

export function targetLabel(change) {
  if (isPlayerTarget(change?.target)) return 'você'
  return change?.target_name || 'outro'
}

export function formatDelta(delta) {
  return Object.keys(delta || {}).map((key) => {
    const value = delta[key]
    const sign = value > 0 ? `+${value}` : String(value)
    return `${DELTA_LABEL[key] || key} ${sign}`
  }).join(', ')
}

export function beatHasBody(beat) {
  return Boolean(beat && (
    beat.outcome
    || beat.decision
    || (beat.reactions || []).length
    || beat.initiative
    || (beat.relation_changes || []).length
  ))
}
