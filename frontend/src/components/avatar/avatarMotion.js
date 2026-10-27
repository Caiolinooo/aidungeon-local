// Liga sheet.visual_state ao TalkingHead já instalado.
// O corpo 3D continua sendo o GLB de avatarConfig (brunette/avaturn/custom).
// Não há malha derivada do retrato.

import { MOODS } from './avatarConfig.js'

export const TALKING_HEAD_POSES = [
  'side', 'hip', 'turn', 'bend', 'back', 'straight', 'wide', 'oneknee', 'kneel', 'sitting',
]

export const TALKING_HEAD_GESTURES = [
  'handup', 'index', 'ok', 'thumbup', 'thumbdown', 'side', 'shrug', 'namaste',
]

const EMOTION_TO_MOOD = {
  neutral: 'neutral',
  happy: 'happy',
  joy: 'happy',
  alegria: 'happy',
  sad: 'sad',
  sadness: 'sad',
  triste: 'sad',
  tristeza: 'sad',
  angry: 'angry',
  anger: 'angry',
  raiva: 'angry',
  fear: 'fear',
  medo: 'fear',
  disgust: 'disgust',
  nojo: 'disgust',
  love: 'love',
  tenderness: 'love',
  carinho: 'love',
  sleep: 'sleep',
  sono: 'sleep',
}

const POSE_ALIAS = {
  side: 'side',
  hip: 'hip',
  turn: 'turn',
  bend: 'bend',
  bow: 'bend',
  back: 'back',
  straight: 'straight',
  stand: 'straight',
  standing: 'straight',
  wide: 'wide',
  oneknee: 'oneknee',
  kneel: 'kneel',
  kneeling: 'kneel',
  sitting: 'sitting',
  sit: 'sitting',
  senta: 'sitting',
  sentar: 'sitting',
  ajoelha: 'kneel',
  ajoelhar: 'kneel',
  curva: 'bend',
  inclina: 'bend',
}

const GESTURE_ALIAS = {
  handup: 'handup',
  wave: 'handup',
  index: 'index',
  point: 'index',
  ok: 'ok',
  thumbup: 'thumbup',
  thumbdown: 'thumbdown',
  side: 'side',
  shrug: 'shrug',
  namaste: 'namaste',
  acena: 'handup',
  aponta: 'index',
  encolhe: 'shrug',
}

function token(value) {
  return String(value ?? '').trim().toLowerCase().replace(/\s+/g, '')
}

function fromAlias(value, alias, allowed) {
  const key = token(value)
  if (!key) return null
  const mapped = alias[key] || (allowed.includes(key) ? key : null)
  return mapped && allowed.includes(mapped) ? mapped : null
}

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

export function moodFromEmotion(emotion) {
  const mapped = EMOTION_TO_MOOD[token(emotion)]
  return mapped && MOODS.includes(mapped) ? mapped : null
}

export function poseFromVisual(pose) {
  return fromAlias(pose, POSE_ALIAS, TALKING_HEAD_POSES)
}

export function gestureFromVisual(action) {
  return fromAlias(action, GESTURE_ALIAS, TALKING_HEAD_GESTURES)
}

// mood null = não chamar setMood (TalkingHead lança "Unknown mood").
// pose/gesture null = não chamar playPose/playGesture.
// playPose com nome fora do template tenta baixar um FBX; por isso o filtro.
export function resolveAvatarMotion(visual, fallbackMood) {
  const mood = moodFromEmotion(visual?.emotion)
    || (MOODS.includes(fallbackMood) ? fallbackMood : null)
  return {
    mood,
    pose: poseFromVisual(visual?.pose),
    gesture: gestureFromVisual(visual?.action),
  }
}

export function prefersReducedMotion() {
  return typeof window !== 'undefined'
    && typeof window.matchMedia === 'function'
    && window.matchMedia('(prefers-reduced-motion: reduce)').matches
}
