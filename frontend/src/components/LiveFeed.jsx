import { useCallback, useEffect, useRef, useState } from 'react'
import { createPortal } from 'react-dom'
import { Activity, ChevronDown, Pause, Play, X } from 'lucide-react'
import { api } from '../api.js'
import { jobTypeLabel } from '../sceneClip.js'
import { useToast } from './Toast.jsx'

const FILTERS = [
  { id: 'all', label: 'Tudo' },
  { id: 'life', label: 'Vida' },
  { id: 'narrative', label: 'História' },
  { id: 'jobs', label: 'Mídia' },
]

const TYPE_LABELS = {
  life_tick_completed: 'Vida',
  sim_changed: 'Simulação',
  action: 'Narrativa',
  player_action: 'Jogador',
  turn_done: 'Turno',
  job_progress: 'Mídia',
  job_queued: 'Mídia',
  backdrop_ready: 'Cenário',
  clip_ready: 'Clipe',
  see_ready: 'Imagem',
  portrait_ready: 'Retrato',
  turnaround_ready: 'Referências 360°',
  adjudication: 'Reação',
  card_changed: 'Ficha',
  undo: 'Desfazer',
  generation_failed: 'Erro',
  generation_cancelled: 'Cancelado',
  see_failed: 'Imagem',
  stale_work_dropped: 'Obsoleto',
  life_playback_changed: 'Tempo',
  turn_changed: 'Turno',
  action_edited: 'Edição',
  ui_changed: 'Painel',
  reset_required: 'Sincronização',
}

function categoryOf(type) {
  if (type?.startsWith('life_') || type === 'sim_changed') return 'life'
  if (type?.includes('job') || type?.includes('see') || type?.includes('portrait') || type?.includes('turnaround') || type?.includes('backdrop') || type?.includes('card_assets') || type?.includes('clip')) return 'jobs'
  return 'narrative'
}

function liveJobTypeLabel(type) {
  if (type === 'character_turnaround') return 'Referências 360°'
  return jobTypeLabel(type) || type || 'Job'
}

function eventText(event) {
  const type = event.type || 'evento'
  const data = event.data || event.payload || {}
  if (type === 'life_tick_completed') {
    const day = data.day ?? data.sim_day
    const minute = data.minute ?? data.sim_minute
    const clock = minute == null ? '' : ` ${String(Math.floor(minute / 60)).padStart(2, '0')}:${String(minute % 60).padStart(2, '0')}`
    const decisions = Array.isArray(data.activities)
      ? data.activities.slice(0, 3).map((row) => {
        const why = row.reason ? ` (${row.reason})` : ''
        return `${row.name || 'NPC'}: ${row.activity || '…'}${why}`
      }).join('; ')
      : ''
    return `Dia ${day ?? '—'}${clock}: o mundo avançou.${decisions ? ` ${decisions}` : ''}`
  }
  if (type === 'stale_work_dropped') return 'Trabalho obsoleto descartado.'
  if (type === 'life_playback_changed') return data.running ? 'Tempo automático iniciado.' : 'Tempo automático pausado.'
  if (type === 'turn_changed') return data.turn_user ? `A vez passou para ${data.turn_user}.` : 'A vez mudou.'
  if (type === 'action_edited') return 'Texto de uma ação editado.'
  if (type === 'see_failed') return data.message || 'A imagem falhou.'
  if (type === 'ui_changed') return 'O painel do jogo mudou.'
  if (type === 'action') return data.text || 'Nova resposta da IA.'
  if (type === 'player_action') return data.text || 'Nova ação do jogador.'
  if (type === 'job_progress') {
    const name = liveJobTypeLabel(data.type)
    return `${name}: ${data.status || 'atualizado'}.`
  }
  if (type === 'job_queued') {
    const name = liveJobTypeLabel(data.type)
    return `${name} entrou na fila.`
  }
  if (type === 'clip_ready') return 'Clipe de 5 s pronto.'
  if (type === 'see_ready') return 'Imagem da cena concluída.'
  if (type === 'backdrop_ready') return 'Fundo da cena atualizado.'
  if (type === 'portrait_ready') return 'Retrato atualizado.'
  if (type === 'turnaround_ready') return 'Referências 360° atualizadas.'
  if (type === 'adjudication') {
    const outcome = typeof data.outcome === 'string' ? data.outcome : ''
    const names = Array.isArray(data.reactions)
      ? data.reactions.map((row) => row?.npc).filter(Boolean).slice(0, 3).join(', ')
      : ''
    return [outcome, names].filter(Boolean).join(' · ') || 'Reação do turno.'
  }
  if (type === 'generation_failed') return data.message || 'A geração falhou.'
  if (type === 'generation_cancelled') return 'Geração cancelada.'
  if (type === 'undo') return 'O último turno foi desfeito.'
  if (type === 'reset_required') return 'A sincronização foi reiniciada.'
  return data.message || data.text || TYPE_LABELS[type] || type
}

function popoverStyle(trigger) {
  const rect = trigger.getBoundingClientRect()
  const mobile = window.matchMedia('(max-width: 767px)').matches
  if (mobile) {
    const header = trigger.closest('header')
    const bottom = header ? header.getBoundingClientRect().bottom : rect.bottom
    return { top: Math.round(bottom + 8) }
  }
  const top = Math.round(rect.bottom + 8)
  const right = Math.max(12, Math.round(window.innerWidth - rect.right))
  const width = Math.min(400, window.innerWidth - 24)
  if (window.innerWidth - right - width < 12) return { top, left: 12 }
  return { top, right }
}

function eventTime(event) {
  const data = event.data || event.payload || {}
  const day = data.day ?? event.sim_day ?? data.sim_day
  const minute = data.minute ?? event.sim_minute ?? data.sim_minute
  if (day == null && event.created_at) return new Intl.DateTimeFormat('pt-BR', { hour: '2-digit', minute: '2-digit' }).format(new Date(event.created_at))
  if (day == null) return ''
  const clock = minute == null ? '' : ` ${String(Math.floor(minute / 60)).padStart(2, '0')}:${String(minute % 60).padStart(2, '0')}`
  return `Dia ${day}${clock}`
}

export default function LiveFeed({ adventureId }) {
  const toast = useToast()
  const [open, setOpen] = useState(false)
  const [events, setEvents] = useState([])
  const [filter, setFilter] = useState('all')
  const [npcFilter, setNpcFilter] = useState('all')
  const [locationFilter, setLocationFilter] = useState('all')
  const [severityFilter, setSeverityFilter] = useState('all')
  const [paused, setPaused] = useState(false)
  const [heldEvents, setHeldEvents] = useState([])
  const [loading, setLoading] = useState(false)
  const [loaded, setLoaded] = useState(false)
  const listRef = useRef(null)
  const triggerRef = useRef(null)
  const nearBottomRef = useRef(true)
  const revisionRef = useRef(0)
  const requestRef = useRef(0)
  const [anchor, setAnchor] = useState(null)

  const merge = useCallback((incoming) => {
    if (!incoming) return
    const rows = Array.isArray(incoming) ? incoming : [incoming]
    setEvents((current) => {
      const byId = new Map()
      for (const event of [...current, ...rows]) {
        const key = event.event_id ?? event.id ?? `${event.type}-${event.created_at || ''}-${eventText(event)}`
        byId.set(key, { ...event, event_id: event.event_id ?? event.id ?? key })
      }
      return [...byId.values()].sort((a, b) => {
        const byEvent = String(a.event_id).localeCompare(
          String(b.event_id), undefined, { numeric: true },
        )
        return byEvent || String(a.created_at || '').localeCompare(String(b.created_at || ''))
      })
    })
  }, [])

  const acceptLive = useCallback((event) => {
    revisionRef.current += 1
    if (paused) setHeldEvents((current) => [...current, event])
    else merge(event)
  }, [merge, paused])

  const load = useCallback(async () => {
    if (!adventureId) return
    const requestId = ++requestRef.current
    const startRevision = revisionRef.current
    setLoading(true)
    try {
      let after = 0
      let response
      const history = []
      do {
        response = await api.getEventHistory(adventureId, { after, limit: 100 })
        if (response.reset_required) {
          after = response.latest_event_id ?? after
          history.length = 0
        } else {
          history.push(...(response.events || []))
          after = response.next_cursor ?? response.after ?? after
        }
      } while (response.has_more && !response.reset_required)
      if (requestId !== requestRef.current || startRevision !== revisionRef.current) return
      merge(history.length ? history : response.events || response)
      setLoaded(true)
    } catch (e) {
      toast(e.message || 'Não foi possível carregar a atividade.')
    } finally {
      if (requestId === requestRef.current) setLoading(false)
    }
  }, [adventureId, merge, toast])

  useEffect(() => {
    setEvents([])
    setLoaded(false)
    if (open) load()
  }, [adventureId, open, load])

  useEffect(() => {
    const onLive = (event) => {
      const detail = event.detail || {}
      if (detail.adventureId !== adventureId) return
      const data = detail.data || {}
      acceptLive({
        event_id: detail.lastEventId || data.event_id || data.id || `live-${detail.type}-${Date.now()}-${Math.random()}`,
        type: detail.type,
        data,
        created_at: data.created_at || new Date().toISOString(),
      })
    }
    window.addEventListener('aidungeon-live', onLive)
    return () => window.removeEventListener('aidungeon-live', onLive)
  }, [adventureId, acceptLive])

  useEffect(() => {
    if (!paused && heldEvents.length) {
      merge(heldEvents)
      setHeldEvents([])
    }
  }, [paused, heldEvents, merge])

  useEffect(() => {
    const element = listRef.current
    if (element && nearBottomRef.current) element.scrollTop = element.scrollHeight
  }, [events, filter, open])

  useEffect(() => {
    if (!open) return undefined
    const place = () => {
      if (triggerRef.current) setAnchor(popoverStyle(triggerRef.current))
    }
    place()
    const onKey = (event) => {
      if (event.key === 'Escape') setOpen(false)
    }
    window.addEventListener('resize', place)
    window.visualViewport?.addEventListener('resize', place)
    document.addEventListener('keydown', onKey)
    return () => {
      window.removeEventListener('resize', place)
      window.visualViewport?.removeEventListener('resize', place)
      document.removeEventListener('keydown', onKey)
    }
  }, [open])

  const toggle = () => {
    if (triggerRef.current) setAnchor(popoverStyle(triggerRef.current))
    setOpen((current) => !current)
  }

  const valueOf = (event, keys) => {
    const data = event.data || event.payload || {}
    for (const key of keys) {
      const value = data[key] ?? event[key]
      if (typeof value === 'string' && value.trim()) return value.trim()
    }
    return ''
  }
  const npcs = [...new Set(events.map((event) => valueOf(event, ['npc', 'npc_name', 'character_name', 'actor'])).filter(Boolean))].sort()
  const locations = [...new Set(events.map((event) => valueOf(event, ['location', 'location_name', 'place'])).filter(Boolean))].sort()
  const severityOf = (event) => {
    const data = event.data || event.payload || {}
    const severity = String(data.severity || event.severity || '').toLowerCase()
    if (['info', 'warn', 'critical'].includes(severity)) return severity
    if (['generation_failed', 'see_failed'].includes(event.type)) return 'critical'
    if (['generation_cancelled', 'stale_work_dropped'].includes(event.type)) return 'warn'
    return 'info'
  }
  const visible = events.filter((event) =>
    (filter === 'all' || categoryOf(event.type) === filter)
    && (npcFilter === 'all' || valueOf(event, ['npc', 'npc_name', 'character_name', 'actor']) === npcFilter)
    && (locationFilter === 'all' || valueOf(event, ['location', 'location_name', 'place']) === locationFilter)
    && (severityFilter === 'all' || severityOf(event) === severityFilter))
  const activeCount = events.length

  return (
    <div className="relative shrink-0">
      <button
        ref={triggerRef}
        type="button"
        onClick={toggle}
        aria-label="Atividade do mundo"
        aria-expanded={open}
        title="Livefeed de vida, narrativa e mídia"
        className="relative flex h-9 w-9 items-center justify-center rounded-full border border-subtle bg-surface text-secondary transition hover:bg-elevated hover:text-primary sm:h-10 sm:w-10"
      >
        <Activity className="h-4 w-4 text-accent-pri" />
        {activeCount > 0 && <span className="absolute -right-1 -top-1 min-w-4 rounded-full bg-accent-pri px-1 text-center text-[9px] font-bold leading-4 text-canvas">{Math.min(activeCount, 99)}</span>}
      </button>
      {open && anchor && createPortal(
        <div className="fixed inset-0 z-50">
          <button
            type="button"
            className="absolute inset-0 cursor-default"
            aria-label="Fechar atividade"
            onClick={() => setOpen(false)}
          />
        <section
          aria-label="Atividade do mundo"
          style={anchor}
          className="fixed z-10 flex min-h-0 max-h-[min(70dvh,var(--app-height,100dvh))] max-w-[min(25rem,calc(100vw-1.5rem))] flex-col overflow-hidden rounded-xl border border-subtle bg-surface shadow-2xl max-md:inset-x-3 md:w-[min(25rem,calc(100vw-1.5rem))]"
        >
          <header className="flex shrink-0 items-center justify-between border-b border-subtle px-3 py-2.5">
            <div>
              <h2 className="text-sm font-semibold text-primary">Atividade do mundo</h2>
              <p className="text-[11px] text-muted">Eventos duráveis e atualizações ao vivo</p>
            </div>
            <button type="button" onClick={() => setOpen(false)} aria-label="Fechar atividade" className="flex min-h-11 min-w-11 items-center justify-center rounded-lg text-secondary hover:bg-elevated hover:text-primary">
              <X className="h-4 w-4" />
            </button>
          </header>
          <div className="flex shrink-0 gap-1 overflow-x-auto border-b border-subtle p-2" role="group" aria-label="Filtrar atividade">
            {FILTERS.map((item) => (
              <button key={item.id} type="button" onClick={() => setFilter(item.id)} aria-pressed={filter === item.id} className={`min-h-8 rounded-full px-3 text-xs ${filter === item.id ? 'bg-accent-pri text-canvas' : 'bg-elevated text-secondary'}`}>
                {item.label}
              </button>
            ))}
          </div>
          <div className="flex shrink-0 flex-wrap gap-2 border-b border-subtle p-2">
            <select aria-label="Filtrar por NPC" value={npcFilter} onChange={(event) => setNpcFilter(event.target.value)} className="min-h-8 max-w-28 rounded-md bg-elevated px-2 text-xs text-secondary">
              <option value="all">Todos NPCs</option>{npcs.map((npc) => <option key={npc} value={npc}>{npc}</option>)}
            </select>
            <select aria-label="Filtrar por local" value={locationFilter} onChange={(event) => setLocationFilter(event.target.value)} className="min-h-8 max-w-28 rounded-md bg-elevated px-2 text-xs text-secondary">
              <option value="all">Todos locais</option>{locations.map((location) => <option key={location} value={location}>{location}</option>)}
            </select>
            <select aria-label="Filtrar por gravidade" value={severityFilter} onChange={(event) => setSeverityFilter(event.target.value)} className="min-h-8 rounded-md bg-elevated px-2 text-xs text-secondary">
              <option value="all">Todas gravidades</option><option value="info">Info</option><option value="warn">Aviso</option><option value="critical">Crítico</option>
            </select>
            <button type="button" onClick={() => setPaused((value) => !value)} aria-pressed={paused} className="ml-auto inline-flex min-h-8 items-center gap-1 rounded-md bg-elevated px-2 text-xs text-secondary">
              {paused ? <Play className="h-3 w-3" /> : <Pause className="h-3 w-3" />}{paused ? `Retomar${heldEvents.length ? ` (${heldEvents.length})` : ''}` : 'Pausar feed'}
            </button>
          </div>
          <div
            ref={listRef}
            role="log"
            aria-live="polite"
            aria-label="Eventos recentes do jogo"
            onScroll={(event) => {
              const el = event.currentTarget
              nearBottomRef.current = el.scrollHeight - el.scrollTop - el.clientHeight < 48
            }}
            className="min-h-0 flex-1 overflow-y-auto p-2"
          >
            {visible.length === 0 ? (
              <p className="px-3 py-8 text-center text-xs text-muted">{loading ? 'Carregando atividade…' : loaded ? 'Nenhum evento neste filtro.' : 'Abra para carregar a atividade.'}</p>
            ) : (
              <ol className="space-y-1.5">
                {visible.map((event) => (
                  <li key={event.event_id} className="rounded-lg border border-subtle bg-elevated/35 px-2.5 py-2">
                    <div className="flex items-center justify-between gap-2 text-[10px] text-muted">
                      <span className="font-semibold uppercase tracking-wide text-accent-pri">{TYPE_LABELS[event.type] || 'Evento'}</span>
                      <time>{eventTime(event)}</time>
                    </div>
                    <p className="mt-1 whitespace-pre-wrap break-words text-xs leading-relaxed text-primary">{eventText(event)}</p>
                    {(event.data?.reason || event.data?.motive || event.payload?.reason || event.payload?.motive) && <p className="mt-1 text-[11px] leading-relaxed text-muted"><span className="font-semibold">Por que isto aconteceu:</span> {event.data?.reason || event.data?.motive || event.payload?.reason || event.payload?.motive}</p>}
                  </li>
                ))}
              </ol>
            )}
          </div>
          <button type="button" onClick={load} disabled={loading} className="flex min-h-11 shrink-0 items-center justify-center gap-1 border-t border-subtle text-xs text-secondary hover:bg-elevated disabled:opacity-40">
            <ChevronDown className={`h-3.5 w-3.5 ${loading ? 'animate-bounce' : ''}`} /> Atualizar histórico
          </button>
        </section>
        </div>,
        document.body,
      )}
    </div>
  )
}
