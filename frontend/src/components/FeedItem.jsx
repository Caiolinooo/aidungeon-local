// Bloco individual do feed da história.
//
// Tipos (contrato Action.type):
//   start / continue / story → prosa corrida (story = narração injetada, itálico sutil)
//   do                     → "**You do**" + blockquote (destaque do jogador)
//   say                    → "**You say**" + blockquote com aspas
//   see                    → card de imagem (ação "See", v2): action.image = {id, url, prompt}
// Hover revela o botão Edit (qualquer bloco, inclusive respostas da IA).
import { useEffect, useRef, useState } from 'react'
import { api } from '../api.js'
import Modal from './Modal.jsx'
import { useToast } from './Toast.jsx'
import { playerDisplayText } from '../utils.js'
import { speakNarration, stopNarration } from './avatar/avatarBus.js'
import { splitNarration } from '../ttsText.js'
import { loosenProse } from '../prose.js'

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

export default function FeedItem({ action, onEdit, disabled, isLatest = false }) {
  const { type, text, edited_at } = action
  const isPlayer = type === 'do' || type === 'say'
  const isSee = type === 'see'
  // do/say vêm formatados do backend ("\n> You ...\n") — exibe sem o wrapper
  const display = isPlayer ? playerDisplayText(type, text) : text
  const shown = isPlayer || isSee ? display : loosenProse(text)

  return (
    <div
      id={`action-${action.id}`}
      className={`group relative rounded-lg px-3 py-2 transition-colors hover:bg-zinc-900/70 ${
        isLatest ? 'border-l-2 border-emerald-500/70 bg-emerald-950/20' : ''
      }`}
    >
      {isLatest && (
        <span className="absolute -left-2 top-1 rounded bg-emerald-600 px-1.5 py-0.5 text-[9px] font-bold uppercase tracking-wide text-white">
          New
        </span>
      )}
      <button
        onClick={() => onEdit(action)}
        disabled={disabled}
        title="Edit"
        className="absolute right-2 top-2 hidden rounded-md border border-zinc-700 bg-zinc-800 px-2 py-1 text-xs text-zinc-300 hover:bg-zinc-700 disabled:opacity-40 group-hover:block"
      >
        ✏ Edit
      </button>

      {!isPlayer && !isSee && <NarrateButton text={display} />}

      {isSee ? (
        <SeeImageCard action={action} />
      ) : isPlayer ? (
        <blockquote className="border-l-4 border-emerald-700/70 pl-4">
          <div className="mb-0.5 text-xs font-bold uppercase tracking-wide text-emerald-400">
            You {type}
          </div>
          <p className="whitespace-pre-wrap font-prose text-zinc-100">
            {type === 'say' ? `“${display}”` : display}
          </p>
        </blockquote>
      ) : (
        <p
          className={`whitespace-pre-wrap font-prose text-[1.05rem] leading-8 ${
            type === 'story' ? 'italic text-zinc-200' : 'text-zinc-100'
          }`}
        >
          {shown}
        </p>
      )}

      {edited_at && (
        <div className="mt-1 text-[10px] uppercase tracking-wide text-zinc-600">edited</div>
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
function NarrateButton({ text }) {
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
      let upcoming = api.voice.tts(parts[0], voice)
      let useAvatar = null
      for (let i = 0; i < parts.length; i++) {
        const blob = await upcoming
        if (!alive()) return
        if (i + 1 < parts.length) upcoming = api.voice.tts(parts[i + 1], voice)
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
      className="absolute right-16 top-2 hidden rounded-md border border-zinc-700 bg-zinc-800 px-2 py-1 text-xs text-zinc-300 hover:bg-zinc-700 group-hover:block"
    >
      {state === 'loading' ? '⏳' : state === 'playing' ? '⏹' : '🔊'}
    </button>
  )
}

// Card de imagem da ação "See": lazy-load via IntersectionObserver (fetch
// autenticado → blob → objectURL, pois <img> não envia o header Bearer).
// objectURL é revogado no unmount. Clique abre a imagem em tamanho full (Modal).
function SeeImageCard({ action }) {
  const image = action.image
  const [objUrl, setObjUrl] = useState(null)
  const [failed, setFailed] = useState(false)
  const [fullOpen, setFullOpen] = useState(false)
  const holderRef = useRef(null)

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

  const caption = image?.prompt || action.text

  return (
    <figure ref={holderRef} className="my-1">
      <button
        type="button"
        onClick={() => objUrl && setFullOpen(true)}
        title={objUrl ? 'Open full size' : undefined}
        className="block w-full cursor-pointer overflow-hidden rounded-lg border border-zinc-800 bg-zinc-900/60"
      >
        {objUrl ? (
          <img src={objUrl} alt={caption} className="max-h-96 w-full object-contain" />
        ) : (
          <div className="flex h-48 items-center justify-center text-sm text-zinc-600">
            {failed || !image?.id ? 'Could not load the image.' : 'Loading image…'}
          </div>
        )}
      </button>
      {caption && (
        <figcaption className="mt-1 line-clamp-2 text-xs italic text-zinc-500">{caption}</figcaption>
      )}

      {fullOpen && objUrl && (
        <Modal title="See — scene image" onClose={() => setFullOpen(false)} wide>
          <img src={objUrl} alt={caption} className="w-full rounded-lg object-contain" />
          {caption && <p className="mt-2 text-xs italic text-zinc-500">{caption}</p>}
        </Modal>
      )}
    </figure>
  )
}
