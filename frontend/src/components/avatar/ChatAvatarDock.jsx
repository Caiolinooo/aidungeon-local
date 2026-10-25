// Dock de avatares do chat (v0.6.0): mostra, na lateral do
// feed, os avatares 3D (TalkingHead) dos NPCs mencionados na narrativa mais
// recente. Carregado via React.lazy (three.js fica fora do bundle inicial,
// mesmo padrão do AvatarsTab).
//
// Diferenças da era VRM: sem tints por cor (avatar é um GLB completo, ver
// avatarConfig.js) e sem o botão 🎨 de sync do retrato; o mood exibido é o
// persistido em card.vrm_id (o auto-emotion "follow the story" mora na
// AvatarsTab, não no dock).
//
// Narração: registra UM speaker no avatarBus — o PRIMEIRO avatar visível
// fala pela cena inteira (NarrateButton → speakNarration → speaker.speak).
//
// Props:
//   adventureId  — aventura atual (busca os cards na API)
//   storyText    — último texto narrativo da IA (define quais NPCs aparecem)
//   refreshKey   — muda → refetch dos cards (ex.: drawer fechou após edição)
import { useCallback, useEffect, useRef, useState } from 'react'
import { api } from '../../api.js'
import TalkingAvatar from './TalkingAvatar.jsx'
import { parseAvatarConfig } from './avatarConfig.js'
import { registerSpeaker } from './avatarBus.js'
import { mentionsNpc } from './emotionDetect.js'

function PortraitCover({ imageId }) {
  const [src, setSrc] = useState(null)
  useEffect(() => {
    if (!imageId) return undefined
    let url = null
    let dead = false
    api.fetchImageBlob(imageId).then((blob) => {
      if (dead) return
      url = URL.createObjectURL(blob)
      setSrc(url)
    }).catch(() => {})
    return () => {
      dead = true
      if (url) URL.revokeObjectURL(url)
    }
  }, [imageId])
  if (!src) return null
  return <img src={src} alt="" className="absolute inset-0 h-full w-full object-cover" />
}

function readSheet(card) {
  const raw = card?.sheet
  if (!raw) return null
  if (typeof raw === 'object') return raw
  try {
    const parsed = JSON.parse(raw)
    return parsed && typeof parsed === 'object' ? parsed : null
  } catch {
    return null
  }
}

function DockAvatar({ card, avatarRef }) {
  const cfg = parseAvatarConfig(card.vrm_id)
  const status = readSheet(card)?.status || ''
  const marked = status === 'dead' || status === 'missing' || status === 'injured'
  return (
    <div className="flex w-16 shrink-0 flex-col items-center gap-1 sm:w-24">
      <div className="relative h-20 w-16 overflow-hidden rounded-xl border border-subtle bg-surface/90 shadow-md sm:h-32 sm:w-24">
        <TalkingAvatar ref={avatarRef} cfg={cfg} view="upper" />
        <PortraitCover imageId={card.portrait?.id} />
        {card.voice && (
          <span className="absolute right-1 top-1 rounded bg-black/70 px-1 py-0.5 text-[9px] font-bold uppercase text-accent-pri">
            {card.voice}
          </span>
        )}
        {cfg?.mood && (
          <span className="absolute bottom-1 left-1 rounded bg-black/70 px-1 py-0.5 text-[8px] font-medium capitalize text-zinc-200">
            {cfg.mood}
          </span>
        )}
      </div>
      <span className="max-w-full truncate text-[10px] font-semibold text-primary" title={card.title}>
        {card.title || 'Sem título'}
      </span>
      {marked && (
        <span className="text-[9px] uppercase tracking-wide text-red-400">{status}</span>
      )}
    </div>
  )
}

function dockStartsCollapsed() {
  return typeof window !== 'undefined' && window.matchMedia('(max-width: 639px)').matches
}

export default function ChatAvatarDock({ adventureId, storyText, refreshKey }) {
  const [cards, setCards] = useState([])
  // No celular o retrato cobre o texto; começa fechado.
  const [collapsed, setCollapsed] = useState(dockStartsCollapsed)
  // Refs dos TalkingAvatar por card.id — o speaker do bus usa o índice 0.
  const avatarRefs = useRef(new Map())

  const reload = useCallback(async () => {
    try {
      setCards(await api.listCards(adventureId))
    } catch { /* dock é acessório — falha silenciosa */ }
  }, [adventureId])

  useEffect(() => {
    reload()
    // O card novo chega depois da resposta, quando o passe dos NPCs termina.
    const soon = window.setTimeout(reload, 8000)
    const later = window.setTimeout(reload, 16000)
    return () => {
      window.clearTimeout(soon)
      window.clearTimeout(later)
    }
  }, [reload, refreshKey, storyText])

  const present = cards.filter(
    (c) => c.type === 'character' && mentionsNpc(storyText, [c.title, ...(c.triggers || [])]),
  )

  // Um speaker por dock: fala sempre pelo primeiro avatar visível (índice 0
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
        title={collapsed ? 'Mostrar avatares dos NPCs' : 'Esconder avatares dos NPCs'}
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
