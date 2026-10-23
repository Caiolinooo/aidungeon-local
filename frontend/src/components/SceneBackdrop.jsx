// Faixa da cena, acima do texto — nunca atrás dele.
//
// POST /scene devolve o lugar (card type=location), o clima lido do texto
// e os NPCs mencionados, com o retrato de cada um. A pintura do fundo é um
// job: enquanto status=pending este componente consulta de novo. O retrato
// que ainda não existe entra na fila de portraits no mesmo pedido.
//
// A faixa reage à ação do jogador (flash + a frase, e o NPC citado dá um
// passo) e ao clima (noite, fogo, chuva…) via filtro sobre a pintura.
// O feed em si fica no fundo sólido da página: a imagem não atravessa a prosa.
import { useEffect, useRef, useState } from 'react'
import { api } from '../api.js'
import { playerDisplayText } from '../utils.js'

const STANCE_LABEL = {
  resist: 'recua',
  flirt: 'se aproxima',
  help: 'ajuda',
  flee: 'foge',
  attack: 'parte pra cima',
}

const MOOD_RING = {
  happy: 'border-warn-300',
  sad: 'border-info',
  angry: 'border-danger-500',
  fear: 'border-accent-pri',
  love: 'border-accent-gld',
}

const ATMOSPHERE_FILTER = {
  night: 'brightness-75',
  day: 'brightness-110',
  rain: 'saturate-150 hue-rotate-15',
  fire: 'sepia brightness-110',
  snow: 'brightness-125 saturate-50',
  blood: 'contrast-125 saturate-150',
}

function initials(title) {
  const words = (title || '').trim().split(/\s+/).filter(Boolean)
  if (words.length === 0) return '?'
  return words.slice(0, 2).map((word) => word[0].toUpperCase()).join('')
}

function namedIn(text, title) {
  if (!text || !title) return false
  return text.toLowerCase().includes(title.trim().toLowerCase())
}

export default function SceneBackdrop({
  adventureId,
  storyText,
  generating,
  playerAction,
}) {
  const [scene, setScene] = useState(null)
  const [urls, setUrls] = useState({})
  const urlsRef = useRef([])
  const [reacting, setReacting] = useState(false)
  const [line, setLine] = useState('')

  useEffect(() => {
    if (!adventureId || generating) return undefined
    let dead = false
    let timer = 0

    const started = Date.now()
    const tick = async () => {
      try {
        const next = await api.syncScene(adventureId)
        if (dead) return
        setScene(next)
        const waiting = next.backdrop?.status === 'pending'
          || (next.cast || []).some((npc) => !npc.portrait)
        // O passe dos NPCs grava o card uns segundos depois da resposta.
        const settling = Date.now() - started < 20000
        if (waiting || settling) timer = window.setTimeout(tick, 4000)
      } catch {
        // cena é acessória — o feed continua sem o fundo
      }
    }
    tick()
    return () => {
      dead = true
      window.clearTimeout(timer)
    }
  }, [adventureId, storyText, generating])

  // Fundo e retrato prontos: um sync, sem esperar o poll de 4s. O poll segue como fallback.
  useEffect(() => {
    if (!adventureId) return undefined
    const onLive = (ev) => {
      const detail = ev.detail || {}
      if (detail.adventureId !== adventureId) return
      if (detail.type !== 'backdrop_ready' && detail.type !== 'portrait_ready') return
      api.syncScene(adventureId).then((next) => setScene(next)).catch(() => {})
    }
    window.addEventListener('aidungeon-live', onLive)
    return () => window.removeEventListener('aidungeon-live', onLive)
  }, [adventureId])

  // Ação do jogador: a faixa pisca, a frase aparece, o NPC citado reage.
  useEffect(() => {
    if (!playerAction) return undefined
    const shown = playerDisplayText(playerAction.type, playerAction.text)
    setLine(playerAction.type === 'say' ? `“${shown}”` : shown)
    setReacting(true)
    const timer = window.setTimeout(() => {
      setReacting(false)
      setLine('')
    }, 2800)
    return () => window.clearTimeout(timer)
  }, [playerAction?.id]) // eslint-disable-line react-hooks/exhaustive-deps

  // Retratos e fundo precisam de fetch autenticado (<img> não manda o Bearer).
  useEffect(() => {
    const ids = []
    if (scene?.backdrop?.image?.id) ids.push(scene.backdrop.image.id)
    for (const npc of scene?.cast || []) {
      if (npc.portrait?.id) ids.push(npc.portrait.id)
    }
    let dead = false
    const created = []
    for (const id of ids) {
      if (urls[id]) continue
      api.fetchImageBlob(id).then((blob) => {
        if (dead) return
        const url = URL.createObjectURL(blob)
        created.push(url)
        urlsRef.current.push(url)
        setUrls((prev) => (prev[id] ? prev : { ...prev, [id]: url }))
      }).catch(() => {})
    }
    return () => {
      dead = true
    }
    // urls de propósito fora das deps: cada id é buscado uma vez.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [scene])

  useEffect(() => () => {
    for (const url of urlsRef.current) URL.revokeObjectURL(url)
  }, [])

  if (!scene) return null

  const backdropUrl = urls[scene.backdrop?.image?.id]
  const filters = (scene.atmosphere || '')
    .split('+')
    .map((name) => ATMOSPHERE_FILTER[name])
    .filter(Boolean)
    .join(' ')
  const playerText = playerAction
    ? playerDisplayText(playerAction.type, playerAction.text)
    : ''

  const retry = async () => {
    try {
      setScene(await api.syncScene(adventureId, { retry: true }))
    } catch {
      /* o próximo ciclo do efeito tenta de novo */
    }
  }

  const place = scene.location && scene.location !== 'the current scene'
    ? scene.location
    : ''

  return (
    <section className={`relative shrink-0 overflow-hidden border-b border-subtle bg-elevated ${
      scene.notice ? 'min-h-48' : 'h-40 sm:h-52'
    }`}>
      {backdropUrl && (
        <div
          className={`absolute inset-0 bg-cover bg-center transition-all duration-700 ${filters} ${
            reacting ? 'scene-react' : ''
          }`}
          style={{ backgroundImage: `url("${backdropUrl}")` }}
        />
      )}
      <div className="absolute inset-0 bg-gradient-to-t from-canvas via-canvas/60 to-canvas/10" />

      <div className="relative flex h-full flex-col justify-between px-4 py-3">
        <div className="flex items-start justify-between gap-2">
          {place || scene.notice ? (
            <div className="max-w-[75%]">
              {place ? (
                <h2 className="border-b border-primary/15 pb-1 font-prose text-[13px] font-semibold uppercase tracking-[0.22em] text-primary drop-shadow-sm">
                  {place}
                  {scene.backdrop?.status === 'pending' ? (
                    <span className="ml-2 text-[10px] font-medium normal-case tracking-normal text-muted">· pintando…</span>
                  ) : ''}
                </h2>
              ) : null}
              {scene.notice ? (
                <div className="mt-1.5 inline-block rounded-md border border-warn-800 bg-canvas/90 px-2 py-1 text-[10px] font-medium text-warn-200">{scene.notice}</div>
              ) : null}
            </div>
          ) : <span />}
          {scene.backdrop?.status === 'error' && (
            <button
              onClick={retry}
              className="rounded-full border border-warn-800 bg-canvas/90 px-2.5 py-1 text-[10px] text-warn-200 hover:bg-warn-950/60 transition"
            >
              Cenário falhou — repetir
            </button>
          )}
        </div>

        {scene.cast?.length > 0 && (
          <div className="flex items-end justify-start gap-3">
            {scene.cast.map((npc) => {
              const src = urls[npc.portrait?.id]
              const stepped = reacting && namedIn(playerText, npc.title)
              return (
                <figure
                  key={npc.id}
                  className={`flex w-14 shrink-0 flex-col items-center sm:w-16 ${stepped ? 'npc-react' : ''}`}
                >
                  {src ? (
                    <img
                      src={src}
                      alt=""
                      className={`h-12 w-12 rounded-full border-2 object-cover shadow-lg shadow-canvas/60 sm:h-14 sm:w-14 ${
                        MOOD_RING[npc.mood] || 'border-primary/70'
                      }`}
                    />
                  ) : (
                    <div className="grid h-12 w-12 place-items-center rounded-full border-2 border-subtle bg-elevated/80 text-sm font-semibold text-primary sm:h-14 sm:w-14">
                      {initials(npc.title)}
                    </div>
                  )}
                  <figcaption className="mt-1 max-w-full text-center text-[10px] font-medium leading-tight text-primary drop-shadow">
                    <span className="block truncate">{npc.title}</span>
                    {STANCE_LABEL[npc.stance] ? (
                      <span className="block text-[9px] font-normal uppercase tracking-wider text-accent-pri">
                        {STANCE_LABEL[npc.stance]}
                      </span>
                    ) : null}
                  </figcaption>
                </figure>
              )
            })}
          </div>
        )}

        <p className={`truncate text-center text-[11px] leading-tight text-secondary ${line ? '' : 'invisible'}`}>
          {line || '·'}
        </p>
      </div>
    </section>
  )
}
