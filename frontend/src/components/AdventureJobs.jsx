import { useCallback, useEffect, useRef, useState } from 'react'
import { createPortal } from 'react-dom'
import {
  AlertCircle,
  Check,
  ListChecks,
  LoaderCircle,
  RefreshCw,
  RotateCcw,
  X,
} from 'lucide-react'
import { api } from '../api.js'
import { jobTypeLabel } from '../sceneClip.js'
import { useToast } from './Toast.jsx'

const ACTIVE_STATUSES = new Set(['preparing', 'pending', 'running'])
const FINISHED_STATUSES = new Set(['done', 'error', 'canceled'])

const STATUS_LABELS = {
  preparing: 'Preparando',
  pending: 'Na fila',
  running: 'Executando',
  done: 'Concluído',
  error: 'Falhou',
  canceled: 'Cancelado',
}

const STATUS_CLASSES = {
  preparing: 'border-warn-500/30 bg-warn-500/10 text-warn-200',
  pending: 'border-subtle bg-elevated text-secondary',
  running: 'border-info/30 bg-info/10 text-info',
  done: 'border-success/30 bg-success/10 text-success',
  error: 'border-danger-700/40 bg-danger-800/15 text-danger-300',
  canceled: 'border-subtle bg-elevated/60 text-muted',
}

const TYPE_LABELS = {
  character_portrait: 'Retrato de personagem',
  scene_backdrop: 'Fundo da cena',
  see_image: 'Imagem da cena',
  scene_clip: jobTypeLabel('scene_clip'),
  npc_day_tick: 'Simulação do dia',
  card_assets: 'Assets do card',
}

function formatDate(value) {
  if (!value) return ''
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return ''
  return new Intl.DateTimeFormat('pt-BR', {
    day: '2-digit',
    month: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
  }).format(date)
}

export default function AdventureJobs({
  adventureId,
  onJobsChange,
  onRetrySee,
  watching = false,
}) {
  const toast = useToast()
  const [open, setOpen] = useState(false)
  const [jobs, setJobs] = useState([])
  const [loading, setLoading] = useState(false)
  const [loadError, setLoadError] = useState('')
  const [busyId, setBusyId] = useState(null)
  const inFlightRef = useRef(null)
  const mountedRef = useRef(true)
  const adventureRef = useRef(adventureId)
  adventureRef.current = adventureId

  useEffect(() => {
    mountedRef.current = true
    return () => {
      mountedRef.current = false
    }
  }, [])

  const fetchJobs = useCallback(async () => {
    if (!adventureId || inFlightRef.current === adventureId) return
    inFlightRef.current = adventureId
    setLoading(true)
    try {
      const list = await api.listJobs(adventureId)
      if (!mountedRef.current || adventureRef.current !== adventureId) return
      const next = Array.isArray(list) ? list : []
      setJobs(next)
      setLoadError('')
    } catch (e) {
      if (mountedRef.current && adventureRef.current === adventureId) {
        setLoadError(e.message || 'Não foi possível carregar os jobs.')
      }
    } finally {
      if (inFlightRef.current === adventureId) inFlightRef.current = null
      if (mountedRef.current && adventureRef.current === adventureId) setLoading(false)
    }
  }, [adventureId])

  useEffect(() => {
    setJobs([])
    setLoadError('')
    setLoading(false)
    fetchJobs()
  }, [fetchJobs])

  useEffect(() => {
    onJobsChange?.(jobs)
  }, [jobs, onJobsChange])

  useEffect(() => {
    if (!open) return undefined
    const onKey = (event) => {
      if (event.key === 'Escape') setOpen(false)
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [open])

  // Um único poll atende ao painel aberto e ao See; o bus cobre a atualização em segundo plano.
  useEffect(() => {
    if (!open && !watching) return undefined
    fetchJobs()
    const timer = window.setInterval(fetchJobs, 1500)
    return () => window.clearInterval(timer)
  }, [open, watching, fetchJobs])

  useEffect(() => {
    const onLive = (event) => {
      const detail = event.detail || {}
      if (detail.adventureId === adventureId && detail.type === 'job_changed') fetchJobs()
    }
    window.addEventListener('aidungeon-live', onLive)
    return () => window.removeEventListener('aidungeon-live', onLive)
  }, [adventureId, fetchJobs])

  const cancel = async (job) => {
    if (!ACTIVE_STATUSES.has(job.status) || busyId) return
    setBusyId(job.id)
    try {
      const updated = await api.cancelJob(job.id)
      setJobs((prev) => prev.map((item) => (
        item.id === job.id ? { ...item, ...(updated || {}), status: updated?.status || 'canceled' } : item
      )))
      toast(updated?.status === 'canceled' ? 'Job cancelado.' : 'Cancelamento solicitado.', 'ok')
      fetchJobs()
    } catch (e) {
      toast(e.message || 'Não foi possível cancelar o job.')
      fetchJobs()
    } finally {
      setBusyId(null)
    }
  }

  const canRetry = (job) => {
    if (!FINISHED_STATUSES.has(job.status) || job.status === 'done') return false
    if (job.type === 'scene_backdrop') return true
    if (job.type === 'character_portrait') return Boolean(job.ref_id)
    return job.type === 'see_image' && Boolean(onRetrySee)
  }

  const retry = async (job) => {
    if (!canRetry(job) || busyId) return
    setBusyId(job.id)
    try {
      if (job.type === 'scene_backdrop') {
        await api.syncScene(adventureId, { retry: true })
      } else if (job.type === 'character_portrait') {
        await api.regeneratePortrait(adventureId, job.ref_id, {
          source: job.payload?.force_reference ? 'reference' : 'anchor',
        })
      } else {
        await onRetrySee(job)
      }
      toast('Nova tentativa iniciada.', 'ok')
      fetchJobs()
    } catch (e) {
      toast(e.message || 'Não foi possível tentar novamente.')
    } finally {
      setBusyId(null)
    }
  }

  const activeCount = jobs.filter((job) => ACTIVE_STATUSES.has(job.status)).length
  const sortedJobs = [...jobs].sort((a, b) => String(b.created_at || '').localeCompare(String(a.created_at || '')))
  const visibleJobs = sortedJobs.slice(0, 30)

  return (
    <div className="relative shrink-0">
      <button
        type="button"
        onClick={() => setOpen((value) => !value)}
        aria-label="Central de jobs da aventura"
        aria-expanded={open}
        title="Jobs de imagem e simulação"
        className="relative flex h-10 w-10 items-center justify-center rounded-full border border-subtle bg-surface text-secondary hover:bg-elevated hover:text-primary transition"
      >
        {activeCount > 0
          ? <LoaderCircle className="h-4 w-4 animate-spin text-accent-pri" />
          : <ListChecks className="h-4 w-4 text-accent-pri" />}
        {activeCount > 0 && (
          <span className="absolute -right-1 -top-1 flex min-w-4 items-center justify-center rounded-full bg-accent-pri px-1 text-[9px] font-bold leading-4 text-canvas">
            {activeCount}
          </span>
        )}
      </button>

      {open && createPortal(
        <div className="fixed inset-0 z-50">
          <button
            type="button"
            className="absolute inset-0 bg-black/60 lg:bg-transparent"
            aria-label="Fechar central de jobs"
            onClick={() => setOpen(false)}
          />
        <section
          aria-label="Jobs da aventura"
          className="absolute inset-y-0 right-0 z-10 flex h-full max-h-[100dvh] w-[min(22rem,calc(100%-1rem))] min-w-0 max-w-full flex-col overflow-hidden border-l border-subtle bg-surface shadow-2xl"
        >
          <div className="flex shrink-0 items-center justify-between border-b border-subtle px-3 py-2.5">
            <div>
              <h2 className="text-sm font-semibold text-primary">Jobs da aventura</h2>
              <p className="text-[11px] text-muted">Gerações de imagem e tarefas em segundo plano</p>
            </div>
            <div className="flex items-center gap-1">
              <button
                type="button"
                onClick={fetchJobs}
                disabled={loading}
                aria-label="Atualizar jobs"
                title="Atualizar jobs"
                className="rounded-lg p-2 text-secondary hover:bg-elevated hover:text-primary disabled:opacity-40"
              >
                <RefreshCw className={`h-4 w-4 ${loading ? 'animate-spin' : ''}`} />
              </button>
              <button
                type="button"
                onClick={() => setOpen(false)}
                aria-label="Fechar central de jobs"
                className="rounded-lg p-2 text-secondary hover:bg-elevated hover:text-primary"
              >
                <X className="h-4 w-4" />
              </button>
            </div>
          </div>

          {loadError && (
            <div role="alert" className="flex shrink-0 items-start gap-2 border-b border-danger-700/30 bg-danger-800/10 px-3 py-2 text-xs text-danger-300">
              <AlertCircle className="mt-0.5 h-3.5 w-3.5 shrink-0" />
              <span>{loadError} Use o botão atualizar para tentar de novo.</span>
            </div>
          )}

          <div className="min-h-0 overflow-y-auto p-2">
            {visibleJobs.length === 0 && !loading ? (
              <p className="px-3 py-8 text-center text-xs text-muted">Nenhum job registrado nesta aventura.</p>
            ) : (
              <ul className="space-y-2">
                {visibleJobs.map((job) => {
                  const active = ACTIVE_STATUSES.has(job.status)
                  const busy = busyId === job.id
                  const superseded = job.status === 'canceled' && job.error === 'superseded'
                  return (
                    <li key={job.id} className="rounded-lg border border-subtle bg-elevated/35 p-2.5">
                      <div className="flex items-start justify-between gap-2">
                        <div className="min-w-0">
                          <p className="text-xs font-semibold text-primary">{TYPE_LABELS[job.type] || job.type}</p>
                          <p className="mt-0.5 truncate font-mono text-[10px] text-muted" title={job.id}>#{job.id}</p>
                        </div>
                        <span className={`shrink-0 rounded-full border px-2 py-0.5 text-[10px] font-semibold ${STATUS_CLASSES[job.status] || 'border-subtle text-secondary'}`}>
                          {active && <LoaderCircle className="mr-1 inline h-2.5 w-2.5 animate-spin" />}
                          {job.status === 'done' && <Check className="mr-1 inline h-2.5 w-2.5" />}
                          {superseded ? 'Substituído' : (STATUS_LABELS[job.status] || job.status)}
                        </span>
                      </div>

                      {job.error && !superseded && (
                        <p role="alert" className="mt-2 whitespace-pre-wrap break-words rounded-md bg-danger-800/10 p-2 text-[11px] leading-relaxed text-danger-300">
                          {String(job.error)}
                        </p>
                      )}
                      {superseded && (
                        <p className="mt-2 text-[11px] text-muted">Uma animação mais recente desta imagem assumiu a fila.</p>
                      )}

                      <div className="mt-2 flex items-center justify-between gap-2">
                        <span className="text-[10px] text-muted">{formatDate(job.created_at)}</span>
                        <div className="flex gap-1.5">
                          {canRetry(job) && (
                            <button
                              type="button"
                              onClick={() => retry(job)}
                              disabled={Boolean(busyId)}
                              className="flex items-center gap-1 rounded-md border border-subtle px-2 py-1 text-[10px] font-semibold text-secondary hover:bg-elevated hover:text-primary disabled:opacity-40"
                            >
                              <RotateCcw className={`h-3 w-3 ${busy ? 'animate-spin' : ''}`} />
                              Tentar de novo
                            </button>
                          )}
                          {active && (
                            <button
                              type="button"
                              onClick={() => cancel(job)}
                              disabled={Boolean(busyId)}
                              className="flex items-center gap-1 rounded-md border border-danger-700/40 px-2 py-1 text-[10px] font-semibold text-danger-300 hover:bg-danger-800/20 disabled:opacity-40"
                            >
                              <X className="h-3 w-3" />
                              {busy ? 'Cancelando…' : 'Cancelar'}
                            </button>
                          )}
                        </div>
                      </div>
                    </li>
                  )
                })}
              </ul>
            )}
            {sortedJobs.length > visibleJobs.length && (
              <p className="px-2 pb-1 pt-3 text-center text-[10px] text-muted">
                Mostrando os 30 jobs mais recentes de {sortedJobs.length}.
              </p>
            )}
          </div>
        </section>
        </div>, document.body)}
    </div>
  )
}
