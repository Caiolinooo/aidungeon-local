// Bloco individual do feed da história.
//
// Tipos (contrato Action.type):
//   start / continue / story → prosa corrida (story = narração injetada, itálico sutil)
//   do                     → rótulo "You" (ou o actor) + blockquote
//   say                    → "You say" / "{actor} says" + blockquote com aspas
//   see                    → card de imagem (ação "See", v2): action.image = {id, url, prompt}
// Hover revela o botão Edit (qualquer bloco, inclusive respostas da IA).
import { useEffect, useRef, useState } from 'react'
import { api } from '../api.js'
import Modal from './Modal.jsx'
import { useToast } from './Toast.jsx'
import { playerDisplayText } from '../utils.js'
import { speakNarration, stopNarration } from './avatar/avatarBus.js'
import { emotionForLine, splitNarration } from '../ttsText.js'
import { loosenProse } from '../prose.js'
import { Edit2, Volume2, Square, Loader2, Eye, EyeOff } from 'lucide-react'

// Cache em módulo da voz do narrador (v0.4.0): narrator_voice é estável
// durante a sessão — evita um GET /api/voices a cada clique no 🔊.
let narratorVoiceCache = null

async function getNarratorVoice() {
  if (narratorVoiceCache === null) {
    const { narrator_voice } = await api.voice.list()
    narratorVoiceCache = narrator_voice
  }
  return narratorVoiceCache
}

function playerLabel(type, actor) {
  const name = (actor || '').trim()
  if (!name) return `You ${type}`
  if (type === 'say') {
    const pt = typeof navigator !== 'undefined' && /^pt\b/i.test(navigator.language || '')
    return pt ? `${name} diz` : `${name} says`
  }
  return name
}

// playerDisplayText tira o wrapper "\n> You ...\n". Se o texto ainda veio
// como "\n> {actor} ...\n", tira o prefixo aqui — o corpo continua sem o wrapper.
function playerBody(type, text, actor) {
  const base = playerDisplayText(type, text)
  const name = (actor || '').trim()
  if (!name) return base
  let t = base.trim()
  if (!t.startsWith('>')) return base
  t = t.replace(/^>\s*/, '')
  const who = `(?:You|${name.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')})`
  if (type === 'do') {
    const m = t.match(new RegExp(`^${who}\\s+([\\s\\S]*)$`, 'i'))
    if (m) return m[1].trim()
  } else if (type === 'say') {
    const quoted = t.match(new RegExp(`^${who}\\s+(?:say|says|diz)\\s+["“]([\\s\\S]*)["”]\\s*$`, 'i'))
    if (quoted) return quoted[1].trim()
    const plain = t.match(new RegExp(`^${who}\\s+(?:say|says|diz)\\s+([\\s\\S]*)$`, 'i'))
    if (plain) return plain[1].trim().replace(/^["“]|["”]$/g, '')
  }
  return base
}

export default function FeedItem({ action, onEdit, disabled, isLatest = false, adventureId = '' }) {
  const { type, text, edited_at } = action
  const actor = (action.actor || '').trim()
  const isPlayer = type === 'do' || type === 'say'
  const isSee = type === 'see'
  // do/say vêm formatados do backend ("\n> You ...\n") — exibe sem o wrapper
  const display = isPlayer ? playerBody(type, text, actor) : text
  const shown = isPlayer || isSee ? display : loosenProse(text)

  return (
    <div
      id={`action-${action.id}`}
      className={`group relative rounded-xl px-4 pb-3 pt-9 transition-colors hover:bg-elevated/40 sm:pt-4 ${
        isLatest ? 'border-l-2 border-accent-pri bg-accent-pri/10' : ''
      }`}
    >
      {isLatest && (
        <div className="mb-1.5 flex items-center gap-2">
          <span className="inline-flex items-center rounded bg-accent-pri px-1.5 py-0.5 text-[9px] font-bold uppercase tracking-wider text-white shadow-sm">
            New
          </span>
        </div>
      )}
      <button
        onClick={() => onEdit(action)}
        disabled={disabled}
        title="Edit action"
        className="absolute right-2 top-2 flex items-center gap-1 rounded-lg border border-subtle bg-surface px-2 py-1 text-xs text-secondary hover:text-primary hover:bg-elevated transition disabled:opacity-40 max-sm:inline-flex sm:hidden sm:group-hover:inline-flex"
      >
        <Edit2 className="w-3 h-3" />
        <span>Edit</span>
      </button>

      {!isPlayer && !isSee && <NarrateButton text={display} adventureId={adventureId} />}


      {isSee ? (
        <SeeImageCard action={action} />
      ) : isPlayer ? (
        <blockquote className="border-l-4 border-emerald-700/70 pl-4">
          <div className="mb-0.5 text-xs font-bold uppercase tracking-wide text-emerald-400">
            {playerLabel(type, actor)}
          </div>
          <p className="whitespace-pre-wrap font-prose text-primary">
            {type === 'say' ? `“${display}”` : display}
          </p>
        </blockquote>
      ) : (
        <p
          className={`whitespace-pre-wrap font-prose text-[1.05rem] leading-8 ${
            type === 'story' ? 'italic text-secondary' : 'text-primary'
          }`}
        >
          {shown}
        </p>
      )}

      {edited_at && (
        <div className="mt-1 text-[10px] uppercase tracking-wide text-muted">edited</div>
      )}
    </div>
  )
}

// Botão 🔊 das entradas narrativas da IA. O texto vai em pedaços curtos
// (splitNarration): o primeiro mp3 começa a tocar enquanto o próximo ainda
// está sendo sintetizado, e textos acima de 2000 caracteres não batem no
// teto da rota. Avatar no dock (avatarBus) fala com lip-sync, enfileirando
// os pedaços; senão, cada mp3 toca via Audio() em sequência.
// Clique durante loading/play = stop.
function NarrateButton({ text, adventureId = '' }) {
  const toast = useToast()
  const [state, setState] = useState('idle') // idle | loading | playing
  const audioRef = useRef(null)
  const urlRef = useRef(null)
  const audioDoneRef = useRef(null)
  // true enquanto a fala está a cargo do avatar (stop delegado ao bus).
  const viaAvatarRef = useRef(false)
  // Incrementar invalida o loop de pedaços em andamento (stop / unmount).
  const tokenRef = useRef(0)

  const releaseAudio = () => {
    audioDoneRef.current?.()
    audioDoneRef.current = null
    if (audioRef.current) {
      audioRef.current.onended = null
      audioRef.current.onerror = null
      audioRef.current.pause()
      audioRef.current = null
    }
    if (urlRef.current) {
      URL.revokeObjectURL(urlRef.current)
      urlRef.current = null
    }
  }

  const stop = () => {
    tokenRef.current += 1
    if (viaAvatarRef.current) {
      viaAvatarRef.current = false
      stopNarration()
    }
    releaseAudio()
    setState('idle')
  }

  // Sem áudio/fala pendurados após o unmount (ex.: undo remove a entrada
  // tocando).
  useEffect(() => {
    return () => {
      tokenRef.current += 1
      if (viaAvatarRef.current) stopNarration()
      releaseAudio()
    }
  }, [])

  const playBlob = (blob) =>
    new Promise((resolve, reject) => {
      releaseAudio()
      const url = URL.createObjectURL(blob)
      urlRef.current = url
      const audio = new Audio(url)
      audioRef.current = audio
      audioDoneRef.current = () => {
        audioDoneRef.current = null
        resolve()
      }
      audio.onended = () => audioDoneRef.current?.()
      audio.onerror = () => {
        audioDoneRef.current = null
        reject(new Error('Could not play narration audio.'))
      }
      audio.play().catch((err) => {
        audioDoneRef.current = null
        reject(err)
      })
    })

  const play = async () => {
    const token = ++tokenRef.current
    const alive = () => tokenRef.current === token
    setState('loading')
    try {
      const voice = await getNarratorVoice()
      if (!alive()) return
      const parts = splitNarration(text)
      if (parts.length === 0) {
        setState('idle')
        return
      }
      const speakPart = (part) => api.voice.tts(part, voice, {
        emotion: emotionForLine(part),
        adventureId,
      })
      let upcoming = speakPart(parts[0])
      let useAvatar = null
      for (let i = 0; i < parts.length; i++) {
        const blob = await upcoming
        if (!alive()) return
        if (i + 1 < parts.length) upcoming = speakPart(parts[i + 1])
        const last = i === parts.length - 1
        const finish = last
          ? () => {
              if (alive()) stop()
            }
          : undefined
        if (useAvatar !== false) {
          const took = await speakNarration({
            blob,
            text: parts[i],
            interrupt: i === 0,
            onended: finish,
          })
          if (!alive()) return
          if (took) {
            useAvatar = true
            viaAvatarRef.current = true
            setState('playing')
            continue
          }
          useAvatar = false
        }
        setState('playing')
        await playBlob(blob)
        if (!alive()) return
      }
      if (useAvatar === false && alive()) {
        releaseAudio()
        setState('idle')
      }
    } catch (e) {
      if (!alive()) return
      stop()
      toast(e.message)
    }
  }

  return (
    <button
      onClick={state === 'idle' ? play : stop}
      title={state === 'idle' ? 'Narrate (text-to-speech)' : 'Stop narration'}
      aria-label={state === 'idle' ? 'Narrate text-to-speech' : 'Stop narration'}
      className={`absolute right-16 top-2 flex items-center gap-1 rounded-lg border px-2 py-1 text-xs transition ${
        state === 'playing'
          ? 'inline-flex border-amber-500/50 bg-amber-500/15 text-amber-300'
          : state === 'loading'
            ? 'inline-flex border-accent-pri/50 bg-accent-pri/15 text-accent-pri'
            : 'border-subtle bg-surface text-secondary hover:text-primary hover:bg-elevated max-sm:inline-flex sm:hidden sm:group-hover:inline-flex'
      }`}
    >
      {state === 'loading' ? (
        <>
          <Loader2 className="w-3.5 h-3.5 animate-spin text-accent-pri" />
          <span className="text-[10px] font-medium">TTS…</span>
        </>
      ) : state === 'playing' ? (
        <>
          <Square className="w-3 h-3 fill-current text-amber-400" />
          <span className="text-[10px] font-medium">Speaking</span>
        </>
      ) : (
        <Volume2 className="w-3.5 h-3.5" />
      )}
    </button>
  )
}

// Card de imagem da ação "See": lazy-load via IntersectionObserver (fetch
// autenticado → blob → objectURL, pois <img> não envia o header Bearer).
// objectURL é revogado no unmount. Clique abre a imagem em tamanho full (Modal).
function HiddenImageNotice({ onShow, tall = false }) {
  return (
    <button
      type="button"
      onClick={onShow}
      className={`flex w-full flex-col items-center justify-center gap-2 rounded-lg border border-subtle bg-elevated/60 px-4 text-center text-sm text-muted ${
        tall ? 'min-h-[50vh]' : 'h-48'
      }`}
    >
      <Eye className="h-5 w-5" aria-hidden="true" />
      <span>Imagem oculta — clique para mostrar</span>
    </button>
  )
}

function SeeImageCard({ action }) {
  const toast = useToast()
  const image = action.image
  const [objUrl, setObjUrl] = useState(null)
  const [failed, setFailed] = useState(false)
  const [fullOpen, setFullOpen] = useState(false)
  const [copied, setCopied] = useState(false)
  const [concealed, setConcealed] = useState(false)
  const holderRef = useRef(null)

  useEffect(() => {
    setConcealed(false)
  }, [image?.id])

  useEffect(() => {
    if (!image?.id) return undefined
    let cancelled = false
    let url = null

    const load = async () => {
      try {
        const blob = await api.fetchImageBlob(image.id)
        if (cancelled) return
        url = URL.createObjectURL(blob)
        setObjUrl(url)
      } catch {
        if (!cancelled) setFailed(true)
      }
    }

    // Lazy: só baixa quando o card se aproxima da viewport.
    const el = holderRef.current
    if (!el || !('IntersectionObserver' in window)) {
      load()
    } else {
      const obs = new IntersectionObserver(
        (entries) => {
          if (entries.some((e) => e.isIntersecting)) {
            obs.disconnect()
            load()
          }
        },
        { rootMargin: '300px' },
      )
      obs.observe(el)
      return () => {
        cancelled = true
        obs.disconnect()
        if (url) URL.revokeObjectURL(url)
      }
    }
    return () => {
      cancelled = true
      if (url) URL.revokeObjectURL(url)
    }
  }, [image?.id])

  const captionSource = action.text || image?.prompt || ''
  const rawPrompt = image?.prompt || action.text || ''
  const displayCaption = captionSource
    .split(/\s+BREAK\s+/i)[0]
    .replace(/^\s*>\s*(?:You(?:\s+say)?\s+)?/i, '')
    .replace(/\s*,\s*people present:.*$/i, '')
    .replace(/\s*,\s*exact likeness.*$/i, '')
    .replace(/\s*,\s*wide shot of that action.*$/i, '')
    .trim() || captionSource

  const copyPrompt = async () => {
    if (!rawPrompt) return
    try {
      await navigator.clipboard.writeText(rawPrompt)
      setCopied(true)
      toast('Image prompt copied to clipboard.', 'ok')
      setTimeout(() => setCopied(false), 2000)
    } catch {
      toast('Could not copy prompt.')
    }
  }

  return (
    <figure ref={holderRef} className="my-2 overflow-hidden rounded-xl border border-subtle bg-surface/80 p-2">
      <div className="relative">
        {concealed ? (
          <HiddenImageNotice onShow={() => setConcealed(false)} />
        ) : (
          <>
            <button
              type="button"
              onClick={() => objUrl && setFullOpen(true)}
              title={objUrl ? 'Open full size' : undefined}
              className="block w-full cursor-pointer overflow-hidden rounded-lg border border-subtle bg-elevated/60"
            >
              {objUrl ? (
                <img src={objUrl} alt={displayCaption || 'Scene illustration'} className="max-h-96 w-full object-contain transition duration-300 hover:scale-[1.01]" />
              ) : (
                <div className="flex h-48 items-center justify-center text-sm text-muted">
                  {failed || !image?.id ? 'Could not load the image.' : 'Loading scene illustration…'}
                </div>
              )}
            </button>
            {objUrl && (
              <button
                type="button"
                onClick={() => setConcealed(true)}
                aria-label="Ocultar imagem"
                title="Ocultar imagem"
                className="absolute right-2 top-2 rounded-md border border-subtle bg-surface/90 p-1.5 text-secondary hover:text-primary"
              >
                <EyeOff className="h-4 w-4" aria-hidden="true" />
              </button>
            )}
          </>
        )}
      </div>
      <div className="mt-2 flex items-center justify-between gap-2 px-1">
        {displayCaption ? (
          <figcaption className="line-clamp-2 flex-1 text-xs italic text-secondary">{displayCaption}</figcaption>
        ) : <span />}
        <div className="flex shrink-0 items-center gap-1.5">
          {rawPrompt && (
            <button
              type="button"
              onClick={copyPrompt}
              className="rounded-md border border-subtle bg-elevated px-2 py-1 text-[10px] font-medium text-secondary hover:text-primary transition"
            >
              {copied ? 'Copied' : 'Copy prompt'}
            </button>
          )}
          {objUrl && (
            <a
              href={objUrl}
              download={`scene-${image?.id || action.id}.png`}
              className="rounded-md border border-subtle bg-elevated px-2 py-1 text-[10px] font-medium text-secondary hover:text-primary transition"
            >
              Save PNG
            </a>
          )}
        </div>
      </div>

      {fullOpen && objUrl && (
        <Modal title="See — Scene Illustration" onClose={() => setFullOpen(false)} wide>
          {concealed ? (
            <HiddenImageNotice onShow={() => setConcealed(false)} tall />
          ) : (
            <div className="relative">
              <img src={objUrl} alt={displayCaption || 'Scene illustration'} className="max-h-[75vh] w-full rounded-lg object-contain bg-black/40" />
              <button
                type="button"
                onClick={() => setConcealed(true)}
                aria-label="Ocultar imagem"
                title="Ocultar imagem"
                className="absolute right-2 top-2 rounded-md border border-subtle bg-surface/90 p-1.5 text-secondary hover:text-primary"
              >
                <EyeOff className="h-4 w-4" aria-hidden="true" />
              </button>
            </div>
          )}
          {rawPrompt && (
            <div className="mt-3 flex items-start justify-between gap-3 rounded-lg border border-subtle bg-elevated/50 p-3">
              <p className="text-xs italic text-secondary">{displayCaption}</p>
              <button
                type="button"
                onClick={copyPrompt}
                className="shrink-0 rounded-lg border border-subtle bg-surface px-2.5 py-1 text-xs font-medium text-primary hover:bg-elevated"
              >
                {copied ? 'Copied' : 'Copy prompt'}
              </button>
            </div>
          )}
        </Modal>
      )}
    </figure>
  )
}
