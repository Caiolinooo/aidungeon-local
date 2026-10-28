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

// Retratos 2D, carregados sob demanda.
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
  tab,
  setTab,
  onClose,
  isDocked = false,
  controlsComponent = null,
  simVersion = 0,
}) {
  return (
    <aside
      className={`flex h-full min-w-0 max-w-full flex-col bg-surface border-subtle ${
        isDocked
          ? 'w-full border-l'
          : 'h-full w-[min(24rem,calc(100%-1rem))] border-l shadow-2xl pb-safe'
      }`}
    >
      {/* Inspector Header with Tab Navigation */}
      <div className="flex items-start justify-between gap-1.5 border-b border-subtle px-3 py-2 pt-[max(0.5rem,env(safe-area-inset-top))] bg-surface/90">
        <div className="grid min-w-0 flex-1 grid-cols-3 gap-1">
          {INSPECTOR_TABS.map((t) => {
            const Icon = t.icon
            const active = tab === t.id
            return (
              <button
                key={t.id}
                onClick={() => setTab(t.id)}
                className={`flex min-h-14 flex-col items-center justify-center gap-1 rounded-xl border px-1 py-1.5 text-[10px] font-medium leading-tight transition ${
                  active
                    ? 'border-accent-pri bg-accent-pri/15 text-accent-pri'
                    : 'border-subtle bg-elevated/40 text-secondary hover:bg-elevated hover:text-primary'
                }`}
              >
                <Icon className="h-4 w-4" />
                <span className="max-w-full truncate">{t.label}</span>
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
          <Suspense fallback={<p className="py-8 text-center text-xs text-muted">Carregando retratos…</p>}>
            <AvatarsTab adventure={adventure} />
          </Suspense>
        )}
        {tab === 'consistency' && <ConsistencyTab adventure={adventure} />}
        {tab === 'config' && <ConfigTab adventure={adventure} />}
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
