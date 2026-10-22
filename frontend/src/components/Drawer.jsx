import { Suspense, lazy, useState } from 'react'
import CardsTab from './drawer/CardsTab.jsx'
import NpcsTab from './drawer/NpcsTab.jsx'
import MemoryTab from './drawer/MemoryTab.jsx'
import ConfigTab from './drawer/ConfigTab.jsx'
import DetailsTab from './drawer/DetailsTab.jsx'
import ConsistencyTab from './drawer/ConsistencyTab.jsx'
import SimTab from './drawer/SimTab.jsx'
import {
  BookOpen,
  Users,
  Brain,
  Sliders,
  Sparkles,
  Settings,
  Info,
  X,
  ChevronRight,
  ShieldCheck,
  Dices,
} from 'lucide-react'

// TalkingHead (three.js) is loaded on-demand
const AvatarsTab = lazy(() => import('./drawer/AvatarsTab.jsx'))

export const INSPECTOR_TABS = [
  { id: 'cards', label: 'Fichas', icon: BookOpen },
  { id: 'npcs', label: 'NPCs', icon: Users },
  { id: 'memory', label: 'Memória', icon: Brain },
  { id: 'controls', label: 'Controles', icon: Sliders },
  { id: 'avatars', label: 'Avatares', icon: Sparkles },
  { id: 'consistency', label: 'Checagem', icon: ShieldCheck },
  { id: 'config', label: 'Configurações', icon: Settings },
  { id: 'details', label: 'Detalhes', icon: Info },
  { id: 'sim', label: 'Vida/Regras', icon: Dices },
]

export function InspectorPane({
  adventure,
  onAdventureChange,
  storyText,
  tab,
  setTab,
  onClose,
  isDocked = false,
  controlsComponent = null,
  simVersion = 0,
}) {
  return (
    <aside
      className={`flex h-full flex-col bg-surface border-subtle ${
        isDocked ? 'w-full border-l' : 'w-full max-w-md border-l shadow-2xl pb-safe'
      }`}
    >
      {/* Inspector Header with Tab Navigation */}
      <div className="flex items-start justify-between gap-1.5 border-b border-subtle px-3 py-2 pt-[max(0.5rem,env(safe-area-inset-top))] bg-surface/90">
        <div className="flex min-w-0 flex-1 flex-wrap gap-1">
          {INSPECTOR_TABS.map((t) => {
            const Icon = t.icon
            const active = tab === t.id
            return (
              <button
                key={t.id}
                onClick={() => setTab(t.id)}
                className={`min-h-11 shrink-0 flex items-center gap-1 whitespace-nowrap rounded-lg px-2 py-1 text-[11px] font-medium transition ${
                  active
                    ? 'bg-accent-pri text-white shadow-sm font-semibold'
                    : 'text-secondary hover:text-primary hover:bg-elevated'
                }`}
              >
                <Icon className="w-3.5 h-3.5" />
                <span>{t.label}</span>
              </button>
            )
          })}
        </div>

        {onClose && (
          <button
            onClick={onClose}
            className="shrink-0 rounded-xl p-2 text-secondary hover:bg-elevated hover:text-primary transition"
            aria-label="Fechar painel"
          >
            <X className="w-4 h-4" />
          </button>
        )}
      </div>

      {/* Tab Content Body */}
      <div className="min-w-0 flex-1 overflow-x-hidden overflow-y-auto p-4 text-primary">
        {tab === 'cards' && <CardsTab adventure={adventure} />}
        {tab === 'npcs' && <NpcsTab adventure={adventure} />}
        {tab === 'memory' && <MemoryTab adventure={adventure} />}
        {tab === 'controls' && (
          <div>
            {controlsComponent ? (
              controlsComponent
            ) : (
              <p className="text-xs text-muted text-center py-8">
                No dynamic game controls generated for this adventure yet.
              </p>
            )}
          </div>
        )}
        {tab === 'avatars' && (
          <Suspense fallback={<p className="py-8 text-center text-xs text-muted">Carregando o visualizador 3D…</p>}>
            <AvatarsTab adventure={adventure} storyText={storyText} />
          </Suspense>
        )}
        {tab === 'consistency' && <ConsistencyTab adventure={adventure} />}
        {tab === 'config' && <ConfigTab />}
        {tab === 'details' && <DetailsTab adventure={adventure} onAdventureChange={onAdventureChange} />}
        {tab === 'sim' && (
          <SimTab adventure={adventure} onAdventureChange={onAdventureChange} version={simVersion} />
        )}
      </div>
    </aside>
  )
}

// Slide-over modal Drawer for mobile / narrow viewports
export default function Drawer({
  open,
  onClose,
  adventure,
  onAdventureChange,
  storyText,
  tab,
  setTab,
  controlsComponent,
  simVersion = 0,
}) {
  const [internalTab, setInternalTab] = useState('cards')
  const activeTab = tab || internalTab
  const handleTabChange = setTab || setInternalTab

  if (!open) return null

  return (
    <div
      className="fixed inset-0 z-40 flex justify-end bg-black/60 backdrop-blur-sm animate-fade-in"
      onPointerDown={(e) => e.target === e.currentTarget && onClose()}
    >
      <InspectorPane
        adventure={adventure}
        onAdventureChange={onAdventureChange}
        storyText={storyText}
        tab={activeTab}
        setTab={handleTabChange}
        onClose={onClose}
        isDocked={false}
        controlsComponent={controlsComponent}
        simVersion={simVersion}
      />
    </div>
  )
}
