import { useCallback, useEffect, useState } from 'react'
import { api } from '../../api.js'
import { useToast } from '../Toast.jsx'
import PortraitAvatar2D from '../avatar/PortraitAvatar2D.jsx'
import { parseSheet, visualStateOf } from '../avatar/avatarMotion.js'

function fieldText(value) {
  const text = String(value ?? '').trim()
  return text || '—'
}

// Galeria 2D dos retratos reais. Selecionar um personagem mostra o portrait,
// a ação, a emoção e a pose que já estão na ficha. Nada aqui escolhe malha,
// corpo ou humor manual.
export default function AvatarsTab({ adventure }) {
  const toast = useToast()
  const [cards, setCards] = useState(null)
  const [selectedId, setSelectedId] = useState(null)

  const reload = useCallback(async () => {
    try {
      setCards(await api.listCards(adventure.id))
    } catch (e) {
      toast(e.message)
    }
  }, [adventure.id, toast])

  useEffect(() => {
    reload()
  }, [reload])

  useEffect(() => {
    const onLive = (ev) => {
      const detail = ev.detail || {}
      if (detail.adventureId !== adventure.id) return
      if (detail.type !== 'portrait_ready' && detail.type !== 'card_changed') return
      reload()
    }
    const onSync = (ev) => {
      if ((ev.detail || {}).adventureId !== adventure.id) return
      reload()
    }
    window.addEventListener('aidungeon-live', onLive)
    window.addEventListener('aidungeon-cards-sync', onSync)
    return () => {
      window.removeEventListener('aidungeon-live', onLive)
      window.removeEventListener('aidungeon-cards-sync', onSync)
    }
  }, [adventure.id, reload])

  if (cards === null) return <p className="text-sm text-muted">Carregando…</p>

  const npcs = cards.filter((c) => c.type === 'character')
  if (npcs.length === 0) {
    return (
      <p className="text-sm text-muted">
        Nenhum personagem ainda. Crie fichas na aba NPCs para ver os retratos.
      </p>
    )
  }

  const selected = npcs.find((c) => c.id === selectedId) || npcs[0]
  const visual = visualStateOf(selected)
  const status = parseSheet(selected).status || ''

  return (
    <div>
      <h3 className="mb-2 text-sm font-semibold uppercase tracking-wide text-secondary">
        Retratos ({npcs.length})
      </h3>

      <div className="mb-3 flex gap-2 overflow-x-auto pb-1">
        {npcs.map((c) => {
          const active = selected?.id === c.id
          return (
            <button
              key={c.id}
              type="button"
              onClick={() => setSelectedId(c.id)}
              className={`flex w-16 shrink-0 flex-col items-center gap-1 rounded-lg border p-1 ${
                active
                  ? 'border-accent-pri bg-accent-pri/10'
                  : 'border-subtle hover:bg-elevated'
              }`}
            >
              <PortraitAvatar2D
                imageId={c.portrait?.id}
                name={c.title || 'Sem título'}
                visual={visualStateOf(c)}
                status={parseSheet(c).status || ''}
                className="h-16 w-full rounded-md"
              />
              <span className="max-w-full truncate text-[10px] font-medium text-secondary">
                {c.title || 'Sem título'}
              </span>
            </button>
          )
        })}
      </div>

      <PortraitAvatar2D
        imageId={selected.portrait?.id}
        name={selected.title || 'Sem título'}
        visual={visual}
        status={status}
        className="h-80 w-full rounded-xl border border-subtle"
      />
      {!selected.portrait?.id && (
        <p className="mt-2 text-xs text-muted">Este personagem ainda não tem retrato.</p>
      )}

      <dl className="mt-3 grid grid-cols-3 gap-2 text-xs">
        <div className="rounded-lg border border-subtle bg-surface px-2 py-1.5">
          <dt className="text-[10px] uppercase tracking-wide text-muted">Emoção</dt>
          <dd className="truncate text-secondary" title={visual?.emotion || ''}>{fieldText(visual?.emotion)}</dd>
        </div>
        <div className="rounded-lg border border-subtle bg-surface px-2 py-1.5">
          <dt className="text-[10px] uppercase tracking-wide text-muted">Ação</dt>
          <dd className="truncate text-secondary" title={visual?.action || ''}>{fieldText(visual?.action)}</dd>
        </div>
        <div className="rounded-lg border border-subtle bg-surface px-2 py-1.5">
          <dt className="text-[10px] uppercase tracking-wide text-muted">Pose</dt>
          <dd className="truncate text-secondary" title={visual?.pose || ''}>{fieldText(visual?.pose)}</dd>
        </div>
      </dl>
      {status && (
        <p className="mt-2 text-[10px] uppercase tracking-wide text-red-400">{status}</p>
      )}
    </div>
  )
}
