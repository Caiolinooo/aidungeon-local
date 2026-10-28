import { useCallback, useEffect, useState } from 'react'
import { api } from '../api.js'
import { getWorldMap, revealLocation, travelTo } from '../api/worldMap.js'
import { useToast } from './Toast.jsx'

function LocationImage({ location, onLoad }) {
  const [src, setSrc] = useState(null)
  useEffect(() => {
    let alive = true
    if (!location.image?.id) {
      setSrc(null)
      return undefined
    }
    api.fetchImageBlob(location.image.id)
      .then((blob) => {
        if (!alive) return
        const url = URL.createObjectURL(blob)
        setSrc(url)
        onLoad?.(url)
      })
      .catch(() => { if (alive) setSrc(null) })
    return () => { alive = false }
  }, [location.image?.id])
  useEffect(() => () => { if (src) URL.revokeObjectURL(src) }, [src])
  if (!src) {
    return (
      <div className="flex h-32 items-center justify-center rounded-lg border border-dashed border-subtle bg-elevated/40 text-xs text-muted">
        Sem imagem do local
      </div>
    )
  }
  return <img src={src} alt={location.title} className="h-32 w-full rounded-lg bg-black object-contain" />
}

function LocationCard({ location, current, onTravel, onReveal, onImage, busy }) {
  return (
    <article className={`rounded-xl border p-3 ${current ? 'border-accent-pri bg-accent-pri/10' : 'border-subtle bg-surface'}`}>
      <LocationImage location={location} />
      <div className="mt-2 flex items-start justify-between gap-2">
        <div className="min-w-0">
          <h3 className="truncate text-sm font-semibold text-primary">{location.title}</h3>
          {current && <span className="text-[10px] uppercase tracking-wide text-accent-pri">Local atual</span>}
        </div>
        <button
          type="button"
          onClick={() => onImage(location)}
          disabled={busy}
          className="shrink-0 rounded-md border border-subtle px-2 py-1 text-[11px] text-secondary hover:bg-elevated disabled:opacity-40"
        >
          Gerar imagem
        </button>
      </div>
      <p className="mt-1 line-clamp-3 text-xs text-secondary">{location.entry}</p>
      <div className="mt-2 flex flex-wrap gap-1">
        {(location.links || []).map((link) => (
          link.hidden ? (
            <span
              key={link.id || link.to_card_id}
              className="inline-flex items-center gap-1 rounded-full border border-dashed border-subtle px-2 py-0.5 text-[11px] text-muted"
            >
              Saída não revelada
              <button
                type="button"
                onClick={() => onTravel(link.to_card_id)}
                disabled={busy}
                className="rounded-full border border-accent-800 px-2 py-0.5 text-[11px] text-accent-pri hover:bg-accent-950 disabled:opacity-40"
              >
                Viajar
              </button>
              <button
                type="button"
                onClick={() => onReveal(link.to_card_id)}
                disabled={busy}
                className="rounded-full border border-subtle px-2 py-0.5 text-[11px] text-secondary hover:bg-elevated disabled:opacity-40"
              >
                Revelar
              </button>
            </span>
          ) : (
            <button
              key={link.id || link.to_card_id}
              type="button"
              onClick={() => onTravel(link.to_card_id)}
              disabled={busy}
              className="rounded-full border border-accent-800 px-2 py-0.5 text-[11px] text-accent-pri hover:bg-accent-950 disabled:opacity-40"
            >
              → {link.to_title}
            </button>
          )
        ))}
      </div>
    </article>
  )
}

function JournalList({ entries }) {
  if (!entries.length) {
    return <p className="text-xs text-muted">Nenhum evento registrado ainda.</p>
  }
  return (
    <ol className="space-y-2">
      {entries.map((entry) => (
        <li key={entry.id} className="rounded-lg border border-subtle bg-elevated/40 p-2">
          <div className="flex items-center justify-between gap-2">
            <strong className="text-xs text-primary">{entry.title}</strong>
            <span className="text-[10px] uppercase tracking-wide text-muted">{entry.kind}</span>
          </div>
          <p className="mt-1 text-xs text-secondary">{entry.text}</p>
          {entry.related?.length > 0 && (
            <div className="mt-1 flex flex-wrap gap-1">
              {entry.related.map((item, index) => (
                <span key={index} className="rounded-full bg-surface px-2 py-0.5 text-[10px] text-muted">
                  {item.title || item.type}
                </span>
              ))}
            </div>
          )}
        </li>
      ))}
    </ol>
  )
}

export default function WorldMap({ adventure, open = true, onClose }) {
  const toast = useToast()
  const [data, setData] = useState(null)
  const [loading, setLoading] = useState(false)
  const [busy, setBusy] = useState(false)
  const [tab, setTab] = useState('map')
  const [showGenerate, setShowGenerate] = useState(false)
  const [name, setName] = useState('')
  const [reference, setReference] = useState('')
  const [webSearch, setWebSearch] = useState(true)
  const [worldType, setWorldType] = useState('real')

  const load = useCallback(async () => {
    if (!adventure?.id) return
    setLoading(true)
    try {
      setData(await getWorldMap(adventure.id))
    } catch (e) {
      toast(e.message)
    } finally {
      setLoading(false)
    }
  }, [adventure?.id, toast])

  useEffect(() => { if (open) load() }, [open, load])
  useEffect(() => {
    if (data?.world_type) setWorldType(data.world_type)
  }, [data?.world_type])

  const changeWorldType = async (value) => {
    setWorldType(value)
    try {
      await api.setWorldType(adventure.id, value)
      await load()
    } catch (e) {
      toast(e.message)
    }
  }


  const travel = async (cardId) => {
    setBusy(true)
    try {
      await travelTo(adventure.id, cardId)
      toast('Viagem realizada.', 'ok')
      await load()
    } catch (e) {
      toast(e.message)
    } finally {
      setBusy(false)
    }
  }

  const reveal = async (cardId) => {
    setBusy(true)
    try {
      setData(await revealLocation(adventure.id, cardId))
      toast('Local revelado.', 'ok')
    } catch (e) {
      toast(e.message)
    } finally {
      setBusy(false)
    }
  }

  const generateImage = async (location) => {
    setBusy(true)
    try {
      const result = await api.generateLocationImage(adventure.id, location.id)
      toast(result.status === 'ready' ? 'Imagem pronta.' : 'Imagem em geração.', 'ok')
      await load()
    } catch (e) {
      toast(e.message)
    } finally {
      setBusy(false)
    }
  }

  const generateLocation = async () => {
    if (name.trim().length < 2) {
      toast('Digite o nome do local.')
      return
    }
    setBusy(true)
    try {
      await api.generateLocation(adventure.id, {
        name: name.trim(), reference: reference.trim(), web_search: webSearch,
      })
      toast('Local criado e conectado ao mapa.', 'ok')
      setName('')
      setReference('')
      setShowGenerate(false)
      await load()
    } catch (e) {
      toast(e.message)
    } finally {
      setBusy(false)
    }
  }

  if (!open) return null
  const locations = data?.locations || []
  const entries = data?.journal || []

  return (
    <div className="flex h-full min-h-0 flex-col bg-canvas text-primary">
      <div className="flex items-center justify-between border-b border-subtle p-3">
        <div className="flex gap-1">
          <button onClick={() => setTab('map')} className={`rounded-md px-3 py-1.5 text-xs ${tab === 'map' ? 'bg-accent-pri text-canvas' : 'bg-elevated text-secondary'}`}>Mapa</button>
          <button onClick={() => setTab('journal')} className={`rounded-md px-3 py-1.5 text-xs ${tab === 'journal' ? 'bg-accent-pri text-canvas' : 'bg-elevated text-secondary'}`}>Journal</button>
        </div>
        <div className="flex gap-1">
          <button onClick={() => setShowGenerate((v) => !v)} className="rounded-md border border-accent-800 px-2 py-1 text-xs text-accent-pri">+ Local com IA</button>
          <button onClick={onClose} className="rounded-md px-2 text-secondary">✕</button>
        </div>
      </div>
      {data?.progression && (
        <div className="border-b border-subtle p-3">
          <div className="flex flex-wrap items-center justify-between gap-2">
            <div className="flex items-center gap-2 text-xs text-secondary">
              <span>Nível {data.progression.level}</span>
              <span>XP {data.progression.xp}</span>
              <span>Skills: {(data.progression.unlocked || []).slice(-4).join(', ') || 'inicial'}</span>
            </div>
            <select value={worldType} onChange={(e) => changeWorldType(e.target.value)} className="rounded-md border border-subtle bg-surface px-2 py-1 text-xs text-secondary">
              <option value="real">Mundo real</option>
              <option value="fictional">Mundo fictício</option>
              <option value="franchise">Universo de história</option>
              <option value="alternate_history">História alternativa</option>
            </select>
          </div>
        </div>
      )}
      {showGenerate && (
        <div className="space-y-2 border-b border-subtle p-3">
          <input value={name} onChange={(e) => setName(e.target.value)} placeholder="Nome do local real ou fictício" className="w-full rounded-lg border border-subtle bg-surface px-3 py-2 text-sm" />
          <textarea value={reference} onChange={(e) => setReference(e.target.value)} placeholder="Referência: cidade real, universo fictício, descrição ou detalhes visuais" rows={2} className="w-full rounded-lg border border-subtle bg-surface px-3 py-2 text-sm" />
          <label className="flex items-center gap-2 text-xs text-secondary">
            <input type="checkbox" checked={webSearch} onChange={(e) => setWebSearch(e.target.checked)} />
            Pesquisar referência na internet
          </label>
          <button onClick={generateLocation} disabled={busy} className="rounded-md bg-accent-pri px-3 py-1.5 text-xs text-canvas disabled:opacity-40">Gerar e conectar</button>
        </div>
      )}
      <div className="min-h-0 flex-1 overflow-y-auto p-3">
        {loading && <p className="text-xs text-muted">Carregando mapa…</p>}
        {!loading && tab === 'map' && (
          locations.length ? (
            <div className="grid grid-cols-1 gap-3 md:grid-cols-2 xl:grid-cols-3">
              {locations.map((location) => (
                <LocationCard key={location.id} location={location} current={location.current} onTravel={travel} onReveal={reveal} onImage={generateImage} busy={busy} />
              ))}
            </div>
          ) : (
            <div className="rounded-xl border border-dashed border-subtle p-6 text-center text-sm text-muted">
              Nenhum local no mapa. Crie um local com IA para começar.
            </div>
          )
        )}
        {!loading && tab === 'journal' && <JournalList entries={entries} />}
      </div>
    </div>
  )
}
