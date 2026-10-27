// visual_state da ficha para o retrato 2D.
// tint / actionMotion / poseMotion são tokens fechados de CSS no retrato
// inteiro. Texto livre continua só como rótulo e nunca vira URL, classe ou malha.

export function parseSheet(card) {
  const raw = card?.sheet
  if (!raw) return {}
  if (typeof raw === 'object') return raw
  try {
    const parsed = JSON.parse(raw)
    return parsed && typeof parsed === 'object' ? parsed : {}
  } catch {
    return {}
  }
}

export function mergedVisual(card, hints) {
  const stored = visualStateOf(card)
  const hint = hints?.[card?.id]
  if (!hint) return stored
  return {
    action: hint.action || stored?.action || '',
    emotion: hint.emotion || stored?.emotion || '',
    pose: hint.pose || stored?.pose || '',
    seq: stored?.seq ?? null,
  }
}

export function visualStateOf(card) {
  const visual = parseSheet(card).visual_state
  if (!visual || typeof visual !== 'object') return null
  return {
    action: String(visual.action ?? '').trim(),
    emotion: String(visual.emotion ?? '').trim(),
    pose: String(visual.pose ?? '').trim(),
    seq: visual.seq ?? null,
  }
}

function token(value) {
  return String(value ?? '').trim().toLowerCase().replace(/\s+/g, '')
}

const TINTS = {
  neutral: 'neutral',
  happy: 'joy',
  joy: 'joy',
  alegria: 'joy',
  sad: 'sad',
  sadness: 'sad',
  triste: 'sad',
  tristeza: 'sad',
  angry: 'anger',
  anger: 'anger',
  raiva: 'anger',
  fear: 'fear',
  medo: 'fear',
  disgust: 'sour',
  nojo: 'sour',
  love: 'warm',
  tenderness: 'warm',
  carinho: 'warm',
  sleep: 'sleep',
  sono: 'sleep',
}

const ACTION_MOTION = {
  wave: 'sway',
  handup: 'sway',
  acena: 'sway',
  shrug: 'tilt',
  encolhe: 'tilt',
  point: 'lean',
  index: 'lean',
  aponta: 'lean',
  recua: 'pull',
}

const POSE_MOTION = {
  sitting: 'settle',
  sit: 'settle',
  senta: 'settle',
  sentar: 'settle',
  kneel: 'low',
  kneeling: 'low',
  ajoelha: 'low',
  ajoelhar: 'low',
  back: 'back',
}

export function portraitCue(visual) {
  const emotion = String(visual?.emotion ?? '').trim()
  const action = String(visual?.action ?? '').trim()
  const pose = String(visual?.pose ?? '').trim()
  return {
    emotion,
    action,
    pose,
    tint: TINTS[token(emotion)] || '',
    actionMotion: ACTION_MOTION[token(action)] || '',
    poseMotion: POSE_MOTION[token(pose)] || '',
  }
}

export function prefersReducedMotion() {
  return typeof window !== 'undefined'
    && typeof window.matchMedia === 'function'
    && window.matchMedia('(prefers-reduced-motion: reduce)').matches
}
