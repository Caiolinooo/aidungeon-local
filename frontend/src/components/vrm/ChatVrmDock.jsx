// Dock VRM do chat (v0.5.0): mostra, na lateral do feed, os avatares 3D dos
// NPCs mencionados na narrativa mais recente — cores de card.vrm_id (que o
// botão 🎨 sincroniza do retrato via LLM vision) e emoção auto-detectada do
// texto (detectEmotion). Carregado via React.lazy (three.js fica fora do
// bundle inicial, mesmo padrão do AvatarsTab).
//
// Props:
//   adventureId  — aventura atual (busca os cards na API)
//   storyText    — último texto narrativo da IA (define presença + emoção)
//   refreshKey   — muda → refetch dos cards (ex.: drawer fechou após edição)
import { useCallback, useEffect, useState } from 'react'
import { api } from '../../api.js'
import { useToast } from '../Toast.jsx'
import VrmAvatar from './VrmAvatar.jsx'
import { parseVrmConfig, VRM_MODELS } from './vrmConfig.js'
import { detectEmotion, mentionsNpc } from './emotionDetect.js'

function DockAvatar({ card, storyText, onSync, syncing }) {
  const cfg = parseVrmConfig(card.vrm_id)
  const hit = detectEmotion(storyText, card.title)
  return (
    <div className="flex w-24 flex-col items-center gap-1">
      <div className="h-32 w-24 overflow-hidden rounded-lg border border-zinc-700 bg-zinc-900/80">
        <VrmAvatar
          url={VRM_MODELS[cfg.model] || VRM_MODELS.base}
          colors={{ hair: cfg.hair, eyes: cfg.eyes, skin: cfg.skin, top: cfg.top, bottom: cfg.bottom }}
          emotion={hit ? hit.emotion : cfg.emotion}
        />
      </div>
      <span className="max-w-full truncate text-[10px] font-medium text-zinc-300" title={card.title}>
        {card.title || 'Untitled'}
      </span>
      <button
        onClick={() => onSync(card)}
        disabled={syncing || !card.portrait}
        title={card.portrait ? 'Sync avatar colors from the portrait (AI vision)' : 'Generate a portrait first (NPCs tab)'}
        className="rounded border border-zinc-700 px-1.5 py-0.5 text-[10px] text-zinc-400 hover:bg-zinc-700 disabled:cursor-not-allowed disabled:opacity-40"
      >
        {syncing ? '…' : '🎨 portrait'}
      </button>
    </div>
  )
}

export default function ChatVrmDock({ adventureId, storyText, refreshKey }) {
  const toast = useToast()
  const [cards, setCards] = useState([])
  const [collapsed, setCollapsed] = useState(false)
  const [syncingId, setSyncingId] = useState(null)

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

  const sync = async (card) => {
    setSyncingId(card.id)
    try {
      const updated = await api.vrmFromPortrait(adventureId, card.id)
      setCards((cs) => cs.map((c) => (c.id === card.id ? updated : c)))
      toast('Avatar colors synced from portrait.', 'ok')
    } catch (e) {
      toast(e.message)
    } finally {
      setSyncingId(null)
    }
  }

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
              storyText={storyText}
              onSync={sync}
              syncing={syncingId === c.id}
            />
          ))}
        </div>
      )}
    </div>
  )
}
