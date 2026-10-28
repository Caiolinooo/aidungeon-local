// Clipe image-to-video (Wan). Funções puras: prompt, casamento do SSE e rótulo.

export const SCENE_CLIP_LABEL = 'Animação de 5 s'

const ATMOSPHERE_PT = {
  night: 'noite',
  day: 'dia',
  rain: 'chuva',
  fire: 'fogo',
  snow: 'neve',
  blood: 'tensão',
}

const MOTION = 'câmera estável, gestos sutis e ambiente vivo por 5 segundos, preservando rostos, roupas e identidade, sem personagens novos'

export function jobTypeLabel(type) {
  if (type === 'scene_clip') return SCENE_CLIP_LABEL
  return ''
}

export function isVideoMedia(mediaType) {
  return typeof mediaType === 'string' && mediaType.toLowerCase().startsWith('video/')
}

export function mediaFileExtension(mediaType) {
  return isVideoMedia(mediaType) ? 'mp4' : 'png'
}

export function mediaDownloadName(id, mediaType, { prefix = 'scene' } = {}) {
  const safe = id == null || id === '' ? 'media' : String(id)
  return `${prefix}-${safe}.${mediaFileExtension(mediaType)}`
}

// Autoplay é opt-in por superfície; reduced motion sempre mantém o quadro parado.
export function prefersReducedMotion(matchMedia) {
  const query = matchMedia || (typeof window !== 'undefined' ? window.matchMedia : null)
  if (typeof query !== 'function') return true
  try {
    return Boolean(query('(prefers-reduced-motion: reduce)')?.matches)
  } catch {
    return true
  }
}

export function clipVideoProps({ reducedMotion = false, autoPlay = false } = {}) {
  const play = Boolean(autoPlay) && !reducedMotion
  return {
    controls: true,
    playsInline: true,
    muted: true,
    autoPlay: play,
    loop: play,
    reducedMotion: Boolean(reducedMotion),
  }
}

export function clipVideoDomProps(options) {
  const props = clipVideoProps(options)
  return {
    controls: props.controls,
    playsInline: props.playsInline,
    autoPlay: props.autoPlay,
    loop: props.loop,
    muted: props.muted,
  }
}

// Retrato: mp4 mudo em loop, sem controles. Reduced motion não monta <video>.
// Não é lipsync — a fala PCM continua por cima da foto ou do clipe.
export function portraitShowsClip(clip, { reducedMotion = false, imageId, adventureId } = {}) {
  if (reducedMotion) return false
  if (!clip || typeof clip !== 'object') return false
  if (typeof clip.url !== 'string' || clip.url === '') return false
  const type = clip.mediaType || clip.media_type || ''
  if (!isVideoMedia(type)) return false
  const source = clip.sourceImageId ?? clip.source_image_id
  if (source != null && source !== '') {
    if (imageId == null || imageId === '' || String(source) !== String(imageId)) return false
  }
  const clipAdventure = clip.adventureId ?? clip.adventure_id
  if (clipAdventure != null && clipAdventure !== '') {
    if (adventureId == null || adventureId === '' || String(clipAdventure) !== String(adventureId)) return false
  }
  return true
}

export function portraitClipVideoProps() {
  return {
    muted: true,
    loop: true,
    playsInline: true,
    autoPlay: true,
    controls: false,
  }
}

export function clipCaption(text) {
  return String(text || '')
    .split(/\s+BREAK\s+/i)[0]
    .replace(/^\s*>\s*(?:You(?:\s+say)?\s+)?/i, '')
    .replace(/\s*,\s*people present:.*$/i, '')
    .replace(/\s*,\s*exact likeness.*$/i, '')
    .replace(/\s*,\s*wide shot of that action.*$/i, '')
    .replace(/\s+/g, ' ')
    .trim()
}

function firstClause(story) {
  const clean = String(story || '').replace(/\s+/g, ' ').trim()
  if (!clean) return ''
  const cut = clean.split(/(?<=[.!?])\s/)[0]
  return cut.slice(0, 180)
}

function atmosphereWords(atmosphere) {
  return String(atmosphere || '')
    .split('+')
    .map((name) => name.trim())
    .filter(Boolean)
    .map((name) => ATMOSPHERE_PT[name] || name)
}

export function motionPrompt({ place = '', atmosphere = '', caption = '', story = '' } = {}) {
  const cap = clipCaption(caption).slice(0, 280)
  const beat = firstClause(story)
  const loc = String(place || '').trim()
  const moods = atmosphereWords(atmosphere)
  const parts = []
  if (cap) parts.push(cap)
  if (beat && beat !== cap) parts.push(beat)
  if (loc && loc.toLowerCase() !== 'the current scene') parts.push(loc)
  if (moods.length) parts.push(moods.join(', '))
  const context = parts.join('. ')
  const text = context ? `${MOTION}. ${context}` : MOTION
  return text.replace(/\s+/g, ' ').trim().slice(0, 500)
}

function adventureMatches(detail, data, adventureId) {
  if (adventureId == null || adventureId === '') return false
  const fromData = data.adventure_id
  const fromDetail = detail.adventureId
  if (fromData == null && fromDetail == null) return false
  if (fromData != null && String(fromData) !== String(adventureId)) return false
  if (fromDetail != null && String(fromDetail) !== String(adventureId)) return false
  return true
}

export function clipEventMatches(detail, { adventureId, sourceImageId } = {}) {
  if (!detail || detail.type !== 'clip_ready') return false
  if (sourceImageId == null || sourceImageId === '') return false
  const data = detail.data && typeof detail.data === 'object' ? detail.data : {}
  if (!adventureMatches(detail, data, adventureId)) return false
  if (data.source_image_id == null || String(data.source_image_id) !== String(sourceImageId)) return false
  if (!data.image || data.image.id == null || data.image.id === '') return false
  return true
}

export function clipJobTerminal(detail, { adventureId, jobId } = {}) {
  if (!detail || jobId == null || jobId === '') return ''
  if (detail.type !== 'job_progress' && detail.type !== 'job_changed') return ''
  const data = detail.data && typeof detail.data === 'object' ? detail.data : {}
  if (data.job_id == null || String(data.job_id) !== String(jobId)) return ''
  if (!adventureMatches(detail, data, adventureId)) return ''
  if (data.status === 'error') return 'error'
  if (data.status === 'canceled' || data.status === 'cancelled') return 'canceled'
  if (data.status === 'done') return 'done'
  return ''
}

export function clipImageFromJob(job, sourceImageId) {
  if (!job || typeof job !== 'object') return null
  const result = job.result
  if (!result || typeof result !== 'object') return null
  const image = result.image
  if (!image || image.id == null || image.id === '') return null
  if (String(image.id) === String(sourceImageId)) return null
  const source = result.source_image_id ?? job.payload?.image_id
  if (source == null || source === '') return null
  if (String(source) !== String(sourceImageId)) return null
  return image
}

export function restoreSceneClip(jobs, sourceImageId) {
  if (!Array.isArray(jobs) || sourceImageId == null || sourceImageId === '') return null
  const matches = jobs.filter((job) => {
    if (!job || job.type !== 'scene_clip') return false
    const source = job.payload?.source_image_id ?? job.payload?.image_id ?? job.ref_id
    return source != null && String(source) === String(sourceImageId)
  })
  const latest = matches[matches.length - 1]
  if (!latest) return null
  if (['preparing', 'pending', 'running'].includes(latest.status)) {
    return { pending: true, jobId: String(latest.id || ''), image: null }
  }
  for (let index = matches.length - 1; index >= 0; index -= 1) {
    const job = matches[index]
    const image = job.status === 'done' ? clipImageFromJob(job, sourceImageId) : null
    if (image) return { pending: false, jobId: '', image }
  }
  return null
}
