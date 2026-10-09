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

function DockAvatar({ card, avatarRef }) {
  const cfg = parseAvatarConfig(card.vrm_id)
  return (
    <div className="flex w-24 flex-col items-center gap-1">
      <div className="h-32 w-24 overflow-hidden rounded-lg border border-zinc-700 bg-zinc-900/80">
        <TalkingAvatar ref={avatarRef} cfg={cfg} view="upper" />
      </div>
      <span className="max-w-full truncate text-[10px] font-medium text-zinc-300" title={card.title}>
        {card.title || 'Untitled'}
      </span>
    </div>
  )
}

export default function ChatAvatarDock({ adventureId, storyText, refreshKey }) {
  const [cards, setCards] = useState([])
  const [collapsed, setCollapsed] = useState(false)
  // Refs dos TalkingAvatar por card.id — o speaker do bus usa o índice 0.
  const avatarRefs = useRef(new Map())

  const reload = useCallback(async () => {
    try {
      setCards(await api.listCards(adventureId))
    } catch { /* dock é acessório — falha silenciosa */ }
  }, [adventureId])

  useEffect(() => {
    reload()
  }, [reload, refreshKey])

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
    <div className="absolute right-2 top-2 z-20 flex flex-col items-end gap-1">
      <button
        onClick={() => setCollapsed((c) => !c)}
        title={collapsed ? 'Show NPC avatars' : 'Hide NPC avatars'}
        className="rounded-md border border-zinc-700 bg-zinc-900/90 px-2 py-1 text-[10px] text-zinc-400 hover:text-zinc-200"
      >
        {collapsed ? `◂ NPCs (${present.length})` : '▸'}
      </button>
      {!collapsed && (
        <div className="flex flex-col gap-2">
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
