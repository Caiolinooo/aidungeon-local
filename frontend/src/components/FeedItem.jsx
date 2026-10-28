// Bloco individual do feed da história.
//
// Tipos (contrato Action.type):
//   start / continue / story → prosa corrida (story = narração injetada, itálico sutil)
//   do                     → rótulo "You" (ou o actor) + blockquote
//   say                    → "You say" / "{actor} says" + blockquote com aspas
//   see                    → card de mídia (ação "See"): action.image = {id, url, prompt, media_type?}
// Hover revela o botão Edit (qualquer bloco, inclusive respostas da IA).
import { useEffect, useRef, useState } from 'react'
import { api } from '../api.js'
import Modal from './Modal.jsx'
import { useToast } from './Toast.jsx'
import ClipActions from './ClipActions.jsx'
import { useSceneClip } from './useSceneClip.js'
import {
  clipVideoDomProps,
  isVideoMedia,
  mediaDownloadName,
  motionPrompt,
  prefersReducedMotion,
} from '../sceneClip.js'
import { playerDisplayText } from '../utils.js'
import { speakNarration, stopNarration } from './avatar/avatarBus.js'
import { createPcmPlayer, readPcm16Wav } from '../pcmStream.js'
import { emotionForLine, splitNarration } from '../ttsText.js'
import { loosenProse } from '../prose.js'
import { Edit2, Volume2, Square, Loader2, Eye, EyeOff } from 'lucide-react'

// A página e o dock reutilizam a voz já vista. api.voice invalida no PUT e
// no delete do narrador; não há evento de settings para escutar.
async function getNarratorVoice() {
  return api.voice.narratorVoice()
}

function playerLabel(type, actor) {
  const name = (actor || '').trim()
  if (!name) {
    if (type === 'say') return 'Você diz'
    if (type === 'story') return 'Narração'
    return 'Você age'
  }
  if (type === 'say') return `${name} diz`
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
      className={`group relative px-1 pb-7 pt-1 ${
        isLatest ? 'border-l-2 border-accent-pri/55 pl-4' : ''
      }`}
    >
      <button
        onClick={() => onEdit(action)}
        disabled={disabled}
        title="Editar ação"
        className="absolute right-2 top-1 flex items-center gap-1 rounded-lg border border-subtle bg-surface px-2 py-1 text-xs text-secondary hover:text-primary hover:bg-elevated transition disabled:opacity-40 max-sm:inline-flex sm:hidden sm:group-hover:inline-flex"
      >
        <Edit2 className="w-3 h-3" />
        <span>Editar</span>
      </button>

      {!isPlayer && !isSee && <NarrateButton text={display} actionId={action.id} adventureId={adventureId} />}


      {isSee ? (
        <SeeImageCard action={action} adventureId={adventureId} />
      ) : isPlayer ? (
        <blockquote className="border-l border-accent-pri/35 pl-4">
          <div className="mb-1.5 font-sans text-[10px] font-semibold uppercase tracking-[0.22em] text-accent-pri">
            {playerLabel(type, actor)}
          </div>
          <p className="whitespace-pre-wrap font-prose text-primary">
            {type === 'say' ? `“${display}”` : display}
          </p>
        </blockquote>
      ) : (
        <p
          className={`prose-turn whitespace-pre-wrap font-prose ${
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

function abortError() {
  const err = new Error('Aborted')
  err.name = 'AbortError'
  return err
}

// Botão 🔊 das entradas narrativas da IA. O texto vai em pedaços de 4096
// caracteres (teto da API): cada um é um WAV PCM. No player local, a síntese
// do pedaço seguinte começa quando o corpo WAV atual acabou e o áudio ainda
// toca — no máximo uma síntese OmniVoice. O avatar segura o Response até o
// fim da fala; começar o próximo antes disso abriria duas sínteses, então o
// dock continua em série. `playing` começa no primeiro bloco.
// Clique durante loading/play = stop.
function NarrateButton({ text, actionId, adventureId = '' }) {
  const toast = useToast()
  const [state, setState] = useState('idle') // idle | loading | playing
  const playerRef = useRef(null)
  // true enquanto a fala está a cargo do avatar (stop delegado ao bus).
  const viaAvatarRef = useRef(false)
  // Incrementar invalida o loop de pedaços em andamento (stop / unmount).
  const tokenRef = useRef(0)
  // Um AbortController por narração: para o fetch, o player e o avatar.
  const requestControllerRef = useRef(null)
  // Próximo WAV já pedido enquanto o atual ainda toca. stop cancela o corpo.
  const preloadRef = useRef(null)

  const cancelPreload = () => {
    const pending = preloadRef.current
    preloadRef.current = null
    if (pending && typeof pending.then === 'function') {
      pending.then((res) => {
        try { res?.body?.cancel() } catch { /* corpo já fechado */ }
      }).catch(() => {})
    }
  }

  const stop = () => {
    tokenRef.current += 1
    requestControllerRef.current?.abort()
    cancelPreload()
    playerRef.current?.stop()
    playerRef.current = null
    if (viaAvatarRef.current) {
      viaAvatarRef.current = false
      stopNarration()
    }
    setState('idle')
  }

  // Sem áudio/fala pendurados após o unmount (ex.: undo remove a entrada
  // tocando).
  useEffect(() => {
    return () => {
      tokenRef.current += 1
      requestControllerRef.current?.abort()
      cancelPreload()
      playerRef.current?.stop()
      if (viaAvatarRef.current) stopNarration()
    }
  }, [])

  // Devolve a espera do fim do áudio. O corpo WAV já terminou: o chamador
  // pode abrir a síntese seguinte sem uma segunda carga OmniVoice.
  const playPcm = async (response, signal, onFirstSample) => {
    let resolveEnded
    const ended = new Promise((resolve) => {
      resolveEnded = resolve
    })
    const player = createPcmPlayer({ onEnded: resolveEnded })
    playerRef.current = player
    let noted = false
    const release = async () => {
      try {
        await ended
      } finally {
        if (playerRef.current === player) {
          player.stop()
          playerRef.current = null
        }
      }
      if (signal.aborted) throw abortError()
    }
    try {
      await readPcm16Wav(response, {
        onFormat: (fmt) => player.start(fmt.sampleRate),
        onSamples: (pcm) => {
          if (!noted) {
            noted = true
            onFirstSample?.()
          }
          player.write(pcm)
        },
      })
      if (signal.aborted) throw abortError()
      player.finish()
      return release
    } catch (err) {
      player.stop()
      if (playerRef.current === player) playerRef.current = null
      throw err
    }
  }

  const play = async () => {
    tokenRef.current += 1
    cancelPreload()
    requestControllerRef.current?.abort()
    playerRef.current?.stop()
    if (viaAvatarRef.current) {
      viaAvatarRef.current = false
      stopNarration()
    }
    const controller = new AbortController()
    requestControllerRef.current = controller
    const token = tokenRef.current
    const alive = () => tokenRef.current === token
    setState('loading')
    try {
      const voice = await getNarratorVoice()
      if (!alive()) return
      const parts = splitNarration(text, 4096)
      if (parts.length === 0) {
        setState('idle')
        return
      }
      let useAvatar = null
      let prefetched = null
      for (let i = 0; i < parts.length; i++) {
        if (!alive()) return
        const response = prefetched
          ? await prefetched
          : await api.voice.ttsStream(parts[i], voice, {
              emotion: emotionForLine(parts[i]),
              adventureId,
              signal: controller.signal,
            })
        prefetched = null
        preloadRef.current = null
        if (!alive()) {
          try { await response.body?.cancel() } catch { /* corpo já fechado */ }
          return
        }
        const last = i === parts.length - 1
        const finish = last
          ? () => {
              if (alive()) stop()
            }
          : undefined
        const onFirstSample = () => {
          if (alive()) setState('playing')
        }
        if (useAvatar !== false) {
          const playback = speakNarration({
            response,
            signal: controller.signal,
            text: parts[i],
            interrupt: i === 0,
            onended: finish,
            onFirstSample,
          })
          if (playback) {
            useAvatar = true
            viaAvatarRef.current = true
            await playback
            if (!alive()) return
            continue
          }
          useAvatar = false
          viaAvatarRef.current = false
        }
        const release = await playPcm(response, controller.signal, onFirstSample)
        if (!last && alive() && useAvatar === false) {
          const next = api.voice.ttsStream(parts[i + 1], voice, {
            emotion: emotionForLine(parts[i + 1]),
            adventureId,
            signal: controller.signal,
          })
          prefetched = next
          preloadRef.current = next
        }
        await release()
        if (!alive()) return
      }
      if (alive()) setState('idle')
    } catch (e) {
      if (!alive()) return
      stop()
      if (e?.name !== 'AbortError') toast(e.message)
    }
  }

  useEffect(() => {
    const onAutoNarrate = (event) => {
      if (String(event.detail?.actionId) === String(actionId)) play()
    }
    window.addEventListener('aidl:auto-narrate', onAutoNarrate)
    return () => window.removeEventListener('aidl:auto-narrate', onAutoNarrate)
  }, [actionId, text])

  return (
    <button
      onClick={state === 'idle' ? play : stop}
      title={state === 'idle' ? 'Narrar em voz alta' : 'Parar narração'}
      aria-label={state === 'idle' ? 'Narrar em voz alta' : 'Parar narração'}
      className={`absolute right-16 top-2 flex items-center gap-1 rounded-lg border px-2 py-1 text-xs transition ${
        state === 'playing'
          ? 'inline-flex border-warn-500/50 bg-warn-500/15 text-warn-300'
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
          <Square className="w-3 h-3 fill-current text-warn-400" />
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

function SeeImageCard({ action, adventureId = '' }) {
  const toast = useToast()
  const image = action.image
  const [objUrl, setObjUrl] = useState(null)
  const [failed, setFailed] = useState(false)
  const [fullOpen, setFullOpen] = useState(false)
  const [copied, setCopiado] = useState(false)
  const [concealed, setConcealed] = useState(false)
  const holderRef = useRef(null)
  const sourceVideo = isVideoMedia(image?.media_type)
  const sceneClip = useSceneClip({
    adventureId,
    sourceImageId: !sourceVideo && image?.id ? image.id : '',
    prompt: motionPrompt({ caption: image?.prompt || action.text || '' }),
    toast,
  })

  useEffect(() => {
    setConcealed(false)
  }, [image?.id])

  useEffect(() => {
    if (!image?.id) return undefined
    let cancelled = false
    let url = null
    setFailed(false)
    setObjUrl(null)

    const load = async () => {
      try {
        const blob = await api.fetchImageBlob(image.id)
        if (cancelled) return
        url = URL.createObjectURL(blob)
        if (cancelled) {
          URL.revokeObjectURL(url)
          url = null
          return
        }
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

  const playingClip = !sourceVideo && sceneClip.showVideo && Boolean(sceneClip.clip?.url)
  const frameIsVideo = sourceVideo || playingClip
  const frameUrl = playingClip ? sceneClip.clip.url : objUrl
  const videoProps = clipVideoDomProps({ reducedMotion: prefersReducedMotion() })
  const stillName = mediaDownloadName(image?.id || action.id, image?.media_type)
  const failedCopy = failed || !image?.id
    ? (sourceVideo ? 'Não foi possível carregar o vídeo.' : 'Não foi possível carregar a imagem.')
    : (sourceVideo ? 'Carregando o vídeo…' : 'Carregando a ilustração…')

  const copyPrompt = async () => {
    if (!rawPrompt) return
    try {
      await navigator.clipboard.writeText(rawPrompt)
      setCopiado(true)
      toast('Prompt da imagem copiado.', 'ok')
      setTimeout(() => setCopiado(false), 2000)
    } catch {
      toast('Não foi possível copiar o prompt.')
    }
  }

  return (
    <figure ref={holderRef} className="my-2 overflow-hidden rounded-xl border border-subtle bg-surface/80 p-2">
      <div className="relative">
        {concealed ? (
          <HiddenImageNotice onShow={() => setConcealed(false)} />
        ) : frameIsVideo ? (
          <>
            <div className="play-media-frame overflow-hidden rounded-lg border border-subtle bg-black">
              {frameUrl ? (
                <video
                  key={playingClip ? sceneClip.clip.id : image?.id}
                  src={frameUrl}
                  {...videoProps}
                  onError={playingClip ? sceneClip.failPlayback : undefined}
                  className="play-media h-auto max-h-[min(34vh,18rem)] w-full bg-black object-contain"
                />
              ) : (
                <div className="flex h-48 items-center justify-center text-sm text-muted">
                  {failedCopy}
                </div>
              )}
            </div>
            {frameUrl && (
              <button
                type="button"
                onClick={() => setConcealed(true)}
                aria-label="Ocultar vídeo"
                title="Ocultar vídeo"
                className="absolute right-2 top-2 rounded-md border border-subtle bg-surface/90 p-1.5 text-secondary hover:text-primary"
              >
                <EyeOff className="h-4 w-4" aria-hidden="true" />
              </button>
            )}
          </>
        ) : (
          <>
            <button
              type="button"
              onClick={() => objUrl && setFullOpen(true)}
              title={objUrl ? 'Abrir em tamanho cheio' : undefined}
              className="block w-full cursor-pointer overflow-hidden rounded-lg border border-subtle bg-elevated/60"
            >
              {objUrl ? (
                <img src={objUrl} alt={displayCaption || 'Ilustração da cena'} className="play-media h-auto max-h-[min(34vh,18rem)] w-full bg-black object-contain" />
              ) : (
                <div className="flex h-48 items-center justify-center text-sm text-muted">
                  {failedCopy}
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
              {copied ? 'Copiado' : 'Copiar prompt'}
            </button>
          )}
          {objUrl && (
            <a
              href={objUrl}
              download={stillName}
              className="rounded-md border border-subtle bg-elevated px-2 py-1 text-[10px] font-medium text-secondary hover:text-primary transition"
            >
              {sourceVideo ? 'Baixar MP4' : 'Save PNG'}
            </a>
          )}
        </div>
      </div>
      {image?.id && adventureId && !sourceVideo && (
        <div className="mt-2 px-1">
          <ClipActions
            pending={sceneClip.pending}
            hasClip={Boolean(sceneClip.clip?.url)}
            showingVideo={sceneClip.showVideo}
            downloadHref={sceneClip.clip?.url || ''}
            downloadName={mediaDownloadName(sceneClip.clip?.id, sceneClip.clip?.mediaType || 'video/mp4', { prefix: 'clip' })}
            onStart={sceneClip.start}
            onCancel={sceneClip.cancel}
            onShowImage={sceneClip.showImage}
            onShowClip={sceneClip.showClip}
          />
        </div>
      )}

      {fullOpen && frameUrl && (
        <Modal title={frameIsVideo ? 'Ver — clipe da cena' : 'Ver — ilustração da cena'} onClose={() => setFullOpen(false)} wide>
          {concealed ? (
            <HiddenImageNotice onShow={() => setConcealed(false)} tall />
          ) : frameIsVideo ? (
            <video
              key={playingClip ? sceneClip.clip.id : image?.id}
              src={frameUrl}
              {...videoProps}
              onError={playingClip ? sceneClip.failPlayback : undefined}
              className="h-auto max-h-[75vh] w-full rounded-lg bg-black object-contain"
            />
          ) : (
            <div className="relative">
              <img src={objUrl} alt={displayCaption || 'Ilustração da cena'} className="h-auto max-h-[75vh] w-full rounded-lg bg-black object-contain" />
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
                {copied ? 'Copiado' : 'Copiar prompt'}
              </button>
            </div>
          )}
        </Modal>
      )}
    </figure>
  )
}
