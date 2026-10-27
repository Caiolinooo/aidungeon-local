// Retrato 2D do card. A imagem é o portrait real; emoção, ação e pose
// só mudam cor e o enquadramento da foto. Fala acende o estado "falando"
// e toca o PCM. Não há boca, visema nem malha.
import { forwardRef, useEffect, useImperativeHandle, useRef, useState } from 'react'
import { api } from '../../api.js'
import { createPcmPlayer, readPcm16Wav } from '../../pcmStream.js'
import { portraitCue, prefersReducedMotion } from './avatarMotion.js'

const ACTION_CLASS = {
  sway: 'portrait-motion-sway',
  tilt: 'portrait-motion-tilt',
  lean: 'portrait-motion-lean',
  pull: 'portrait-motion-pull',
}

const POSE_CLASS = {
  settle: 'portrait-pose-settle',
  low: 'portrait-pose-low',
  back: 'portrait-pose-back',
}

const TINT_CLASS = {
  neutral: 'portrait-tint-neutral',
  joy: 'portrait-tint-joy',
  sad: 'portrait-tint-sad',
  anger: 'portrait-tint-anger',
  fear: 'portrait-tint-fear',
  warm: 'portrait-tint-warm',
  sour: 'portrait-tint-sour',
  sleep: 'portrait-tint-sleep',
}

function abortError() {
  const err = new Error('Aborted')
  err.name = 'AbortError'
  return err
}

async function discard(response) {
  try { await response?.body?.cancel() } catch { /* corpo já fechado */ }
}

const PortraitAvatar2D = forwardRef(function PortraitAvatar2D(
  { imageId, name, visual = null, className = '', status = '' },
  ref,
) {
  const [src, setSrc] = useState('')
  const [speaking, setSpeaking] = useState(false)
  const [reduced, setReduced] = useState(prefersReducedMotion)
  const mountedRef = useRef(true)
  const playerRef = useRef(null)
  const speakGenRef = useRef(0)
  const cancelledRef = useRef(0)
  const tailRef = useRef(Promise.resolve())
  const stopRef = useRef(() => {})

  useEffect(() => {
    mountedRef.current = true
    return () => {
      mountedRef.current = false
      stopRef.current()
    }
  }, [])

  useEffect(() => {
    if (typeof window.matchMedia !== 'function') return undefined
    const query = window.matchMedia('(prefers-reduced-motion: reduce)')
    const onChange = () => setReduced(query.matches)
    onChange()
    query.addEventListener?.('change', onChange)
    return () => query.removeEventListener?.('change', onChange)
  }, [])

  useEffect(() => {
    if (!imageId) {
      setSrc('')
      return undefined
    }
    let cancelled = false
    let objectUrl = ''
    api.fetchImageBlob(imageId).then((blob) => {
      if (cancelled) return
      objectUrl = URL.createObjectURL(blob)
      setSrc(objectUrl)
    }).catch(() => {
      if (!cancelled) setSrc('')
    })
    return () => {
      cancelled = true
      if (objectUrl) {
        URL.revokeObjectURL(objectUrl)
        setSrc('')
      }
    }
  }, [imageId])

  stopRef.current = () => {
    cancelledRef.current += 1
    speakGenRef.current += 1
    const prev = playerRef.current
    playerRef.current = null
    if (mountedRef.current) setSpeaking(false)
    prev?.stop()
  }

  useImperativeHandle(ref, () => ({
    speak(args = {}) {
      const interrupt = args.interrupt !== false
      if (interrupt) {
        cancelledRef.current += 1
        speakGenRef.current += 1
        const prev = playerRef.current
        playerRef.current = null
        if (mountedRef.current) setSpeaking(false)
        prev?.stop()
      }
      const epoch = cancelledRef.current
      const run = () => {
        if (cancelledRef.current !== epoch) {
          return discard(args.response).then(() => { throw abortError() })
        }
        return playback(args, epoch)
      }
      const task = interrupt ? run() : tailRef.current.then(run, run)
      tailRef.current = task.then(() => {}, () => {})
      return task
    },
    stop() {
      stopRef.current()
    },
  }))

  async function playback({ response, signal, onended, onFirstSample }, epoch) {
    if (cancelledRef.current !== epoch || signal?.aborted) {
      await discard(response)
      throw abortError()
    }
    const gen = ++speakGenRef.current
    const mine = () => speakGenRef.current === gen && cancelledRef.current === epoch
    let resolveWait = () => {}
    const wait = new Promise((resolve) => { resolveWait = resolve })
    let waitSettled = false
    const finishWait = () => {
      if (waitSettled) return
      waitSettled = true
      resolveWait()
    }
    let halted = false
    let completed = false
    let started = false
    let noted = false

    const player = createPcmPlayer({
      onEnded: () => {
        const natural = !halted && mine() && !signal?.aborted
        if (playerRef.current === player) playerRef.current = null
        if (mountedRef.current && mine()) setSpeaking(false)
        if (natural) {
          completed = true
          try { onended?.() } catch { /* o botão trata o fim */ }
        }
        finishWait()
      },
    })
    playerRef.current = player

    const halt = () => {
      halted = true
      try { player.stop() } catch { /* player já parado */ }
      finishWait()
    }
    const onAbort = () => {
      if (!mine()) return
      halt()
    }
    signal?.addEventListener('abort', onAbort, { once: true })

    try {
      await readPcm16Wav(response, {
        onFormat: async (fmt) => {
          if (!mine() || signal?.aborted) return
          await player.start(fmt.sampleRate)
          if (!mine() || signal?.aborted) {
            halt()
            return
          }
          started = true
        },
        onSamples: (pcm) => {
          if (!mine() || signal?.aborted || halted) return
          if (!noted) {
            noted = true
            if (mountedRef.current) setSpeaking(true)
            try { onFirstSample?.() } catch { /* estado do botão */ }
          }
          player.write(pcm)
        },
      })
      if (!mine() || signal?.aborted || halted) throw abortError()
      if (!started) {
        halt()
        return
      }
      player.finish()
      await wait
      if (!completed) throw abortError()
    } catch (err) {
      halt()
      if (playerRef.current === player) playerRef.current = null
      if (mountedRef.current && mine()) setSpeaking(false)
      if (err?.name === 'AbortError' || signal?.aborted || !mine()) throw abortError()
      throw err
    } finally {
      signal?.removeEventListener('abort', onAbort)
    }
  }

  const cue = portraitCue(visual)
  const actionClass = reduced ? '' : (ACTION_CLASS[cue.actionMotion] || '')
  const poseClass = POSE_CLASS[cue.poseMotion] || ''
  const tintClass = TINT_CLASS[cue.tint] || ''
  const statusClass = status === 'dead'
    ? 'grayscale'
    : status === 'missing'
      ? 'opacity-50'
      : status === 'injured'
        ? 'saturate-50'
        : ''
  const caption = [cue.emotion, cue.action, cue.pose].filter(Boolean).join(' · ')
  const label = name || 'Retrato'

  return (
    <div
      className={`relative overflow-hidden ${className}`.trim()}
      data-speaking={speaking ? 'true' : 'false'}
      data-emotion={cue.tint || undefined}
      data-action={cue.actionMotion || undefined}
      data-pose={cue.poseMotion || undefined}
      title={caption || label}
    >
      {src ? (
        <img
          src={src}
          alt={label}
          className={`h-full w-full object-cover ${poseClass} ${actionClass} ${statusClass}`.trim()}
        />
      ) : (
        <div className={`flex h-full w-full items-center justify-center bg-elevated text-xs font-semibold text-muted ${statusClass}`.trim()}>
          {(name || '?').slice(0, 1).toUpperCase()}
        </div>
      )}
      {tintClass && <div className={`pointer-events-none absolute inset-0 ${tintClass}`} aria-hidden="true" />}
      {speaking && (
        <>
          <span
            className={`pointer-events-none absolute inset-0 portrait-speak ${reduced ? '' : 'portrait-speaking'}`}
            aria-hidden="true"
          />
          <span className="absolute bottom-1 left-1 rounded bg-black/70 px-1 py-0.5 text-[8px] font-medium text-zinc-100">
            falando
          </span>
        </>
      )}
    </div>
  )
})

export default PortraitAvatar2D
