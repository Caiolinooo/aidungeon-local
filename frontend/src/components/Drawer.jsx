import { useState } from 'react'
import CardsTab from './drawer/CardsTab.jsx'
import NpcsTab from './drawer/NpcsTab.jsx'
import MemoryTab from './drawer/MemoryTab.jsx'
import ConfigTab from './drawer/ConfigTab.jsx'
import DetailsTab from './drawer/DetailsTab.jsx'

const TABS = [
  { id: 'cards', label: 'Story Cards' },
  { id: 'npcs', label: 'NPCs' },
  { id: 'memory', label: 'Memory' },
  { id: 'config', label: 'Config' },
  { id: 'details', label: 'Details' },
]

// Drawer lateral (aberto pelo hambúrguer do header da tela de jogo).
export default function Drawer({ open, onClose, adventure, onAdventureChange }) {
  const [tab, setTab] = useState('cards')
  if (!open) return null

  return (
    <div className="fixed inset-0 z-30 flex justify-end bg-black/60" onMouseDown={(e) => e.target === e.currentTarget && onClose()}>
      <aside className="flex h-full w-full max-w-md flex-col border-l border-zinc-800 bg-zinc-900 shadow-2xl">
        <div className="flex items-center justify-between border-b border-zinc-800 px-4 py-3">
          <div className="flex gap-1">
            {TABS.map((t) => (
              <button
                key={t.id}
                onClick={() => setTab(t.id)}
                className={`rounded-md px-3 py-1.5 text-sm font-medium ${
                  tab === t.id ? 'bg-zinc-700 text-zinc-100' : 'text-zinc-400 hover:text-zinc-200'
                }`}
              >
                {t.label}
              </button>
            ))}
          </div>
          <button onClick={onClose} className="text-zinc-400 hover:text-zinc-100" aria-label="Close panel">
            ✕
          </button>
        </div>
        <div className="flex-1 overflow-y-auto p-4">
          {tab === 'cards' && <CardsTab adventure={adventure} />}
          {tab === 'npcs' && <NpcsTab adventure={adventure} />}
          {tab === 'memory' && <MemoryTab adventure={adventure} />}
          {tab === 'config' && <ConfigTab />}
          {tab === 'details' && <DetailsTab adventure={adventure} onAdventureChange={onAdventureChange} />}
        </div>
      </aside>
    </div>
  )
}
