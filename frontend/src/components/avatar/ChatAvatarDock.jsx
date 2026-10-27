// Dock de retratos 2D na lateral do feed. Cada NPC presente na cena usa o
// retrato principal do card (card.portrait.id), o nome e o visual_state.
// Carregado via React.lazy. A narração PCM sai pelo primeiro retrato visível.
//
// O humor do turno vem de sheet.visual_state (e dos hints do beat). Não há
// corpo separado nem miniatura por cima de outra malha.
//
// Narração: registra UM speaker no avatarBus — o PRIMEIRO retrato visível
// fala pela cena inteira (NarrateButton → speakNarration → speaker.speak).
//
// Props:
//   adventureId  — aventura atual (busca os cards na API)
//   storyText    — último texto narrativo da IA (define quais NPCs aparecem)
//   refreshKey   — muda → refetch dos cards (ex.: drawer fechou após edição)
import { useCallback, useEffect, useRef, useState } from 'react'
import { api } from '../../api.js'
import PortraitAvatar2D from './PortraitAvatar2D.jsx'
import { registerSpeaker } from './avatarBus.js'
import { mentionsNpc } from './emotionDetect.js'
import { mergedVisual, parseSheet, visualStateOf } from './avatarMotion.js'

function DockAvatar({ card, avatarRef, visual }) {
  const status = parseSheet(card)?.status || ''
  const marked = status === 'dead' || status === 'missing' || status === 'injured'
  const caption = [visual?.emotion, visual?.action].filter(Boolean).join(' · ')
  return (
    <div className="flex w-16 shrink-0 flex-col items-center gap-1 sm:w-24">
      <div className="relative h-20 w-16 overflow-hidden rounded-xl border border-subtle bg-surface/90 shadow-md sm:h-32 sm:w-24">
        <PortraitAvatar2D
          ref={avatarRef}
          imageId={card.portrait?.id}
          name={card.title || 'Sem título'}
          visual={visual}
          status={status}
          className="h-full w-full"
        />
        {card.voice && (
          <span className="absolute right-1 top-1 rounded bg-black/70 px-1 py-0.5 text-[9px] font-bold uppercase text-accent-pri">
            {card.voice}
          </span>
        )}
      </div>
      <span className="max-w-full truncate text-[10px] font-semibold text-primary" title={card.title}>
        {card.title || 'Sem título'}
      </span>
      {caption && (
        <span className="max-w-full truncate text-[9px] text-muted" title={caption}>
          {caption}
        </span>
      )}
      {marked && (
        <span className="text-[9px] uppercase tracking-wide text-red-400">{status}</span>
      )}
    </div>
  )
}

function dockStartsCollapsed() {
  return typeof window !== 'undefined' && window.matchMedia('(max-width: 639px)').matches
}

function seqMatches(visual, beatSeqs) {
  if (visual?.seq == null || !Array.isArray(beatSeqs)) return false
  return beatSeqs.some((seq) => seq != null && String(seq) === String(visual.seq))
}

export default function ChatAvatarDock({
  adventureId, storyText, refreshKey, spotlightIds = [], beatSeqs = [], visualHints = {},
}) {
  const [cards, setCards] = useState([])
  // No celular o retrato cobre o texto; começa fechado.
  const [collapsed, setCollapsed] = useState(dockStartsCollapsed)
  // Refs dos retratos por card.id — o speaker do bus usa o índice 0.
  const avatarRefs = useRef(new Map())

  const reload = useCallback(async () => {
    try {
      setCards(await api.listCards(adventureId))
    } catch { /* dock é acessório — falha silenciosa */ }
  }, [adventureId])

  useEffect(() => {
    reload()
  }, [reload, refreshKey, storyText])

  useEffect(() => {
    const onLive = (ev) => {
      const detail = ev.detail || {}
      if (detail.adventureId !== adventureId) return
      if (detail.type !== 'portrait_ready' && detail.type !== 'card_changed') return
      reload()
    }
    const onSync = (ev) => {
      if ((ev.detail || {}).adventureId !== adventureId) return
      reload()
    }
    window.addEventListener('aidungeon-live', onLive)
    window.addEventListener('aidungeon-cards-sync', onSync)
    return () => {
      window.removeEventListener('aidungeon-live', onLive)
      window.removeEventListener('aidungeon-cards-sync', onSync)
    }
  }, [adventureId, reload])

  const spotlight = new Set(spotlightIds.filter(Boolean))
  const present = cards.filter((card) => {
    if (card.type !== 'character') return false
    const visual = mergedVisual(card, visualHints) || visualStateOf(card)
    const player = Boolean(card.player_username)
    if (player) return Boolean(visual && (visual.emotion || visual.pose || visual.action))
    if (spotlight.has(card.id) || seqMatches(visual, beatSeqs)) return true
    return mentionsNpc(storyText, [card.title, ...(card.triggers || [])])
  })

  // Um speaker por dock: fala sempre pelo primeiro retrato visível (índice 0
  // da lista `present`). O registro é estável — lê o Map de refs na hora.
  useEffect(() => {
    if (present.length === 0) return undefined
    const firstId = present[0].id
    return registerSpeaker({
      speak: (args) => avatarRefs.current.get(firstId)?.speak(args),
      stop: () => avatarRefs.current.get(firstId)?.stop(),
    })
  }, [present.length > 0 ? present[0].id : null]) // eslint-disable-line react-hooks/exhaustive-deps

  if (present.length === 0) return null

  return (
    <div className={`absolute bottom-2 right-2 z-20 flex min-w-0 flex-row items-end gap-1 sm:bottom-2 sm:left-auto sm:right-2 sm:top-2 sm:flex-col sm:items-end ${collapsed ? '' : 'left-2'}`}>
      <button
        onClick={() => setCollapsed((c) => !c)}
        title={collapsed ? 'Mostrar retratos dos NPCs' : 'Esconder retratos dos NPCs'}
        className="shrink-0 rounded-md border border-zinc-700 bg-zinc-900/80 px-2 py-1 text-[10px] text-zinc-400 hover:text-zinc-200"
      >
        {collapsed ? `NPCs (${present.length})` : 'Ocultar'}
      </button>
      {!collapsed && (
        <div className="flex min-w-0 max-w-full flex-row gap-2 overflow-x-auto sm:min-h-0 sm:max-h-full sm:flex-col sm:overflow-x-hidden sm:overflow-y-auto sm:overscroll-y-contain">
          {present.map((c) => (
            <DockAvatar
              key={c.id}
              card={c}
              visual={mergedVisual(c, visualHints)}
              avatarRef={(el) => {
                if (el) avatarRefs.current.set(c.id, el)
                else avatarRefs.current.delete(c.id)
              }}
            />
          ))}
        </div>
      )}
    </div>
  )
}
