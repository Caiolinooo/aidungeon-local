// Cross-check: o cruzamento entre o que o LLM escreveu, a ficha do NPC, o
// prompt do retrato, a imagem gerada, a memória e o embedding.
//
// Backend: GET /api/adventures/{id}/consistency  → relatório
//          POST /api/adventures/{id}/consistency/repair {check} → repara um item

import { useCallback, useEffect, useState } from 'react'
import { api } from '../../api.js'
import { useToast } from '../Toast.jsx'
import {
  ShieldCheck,
  CircleCheck,
  CircleX,
  AlertTriangle,
  RefreshCw,
  ChevronDown,
  ChevronRight,
  ScanLine,
} from 'lucide-react'

const STATUS = {
  ok: {
    label: 'OK',
    text: 'text-accent-pri',
    chip: 'border-accent-pri/40 bg-accent-pri/10 text-accent-pri',
    Icon: CircleCheck,
  },
  warn: {
    label: 'Aviso',
    text: 'text-accent-gld',
    chip: 'border-accent-gld/40 bg-accent-gld/10 text-accent-gld',
    Icon: AlertTriangle,
  },
  error: {
    label: 'Erro',
    text: 'text-red-400',
    chip: 'border-red-400/40 bg-red-400/10 text-red-400',
    Icon: CircleX,
  },
}

function itemLine(item) {
  if (!item || typeof item !== 'object') return String(item ?? '')
  if (item.title) {
    const missing = [
      ...(item.missing_fields || []),
      ...(item.missing_stats || []).map((s) => `stats.${s}`),
    ]
    return missing.length
      ? `${item.title} — faltam ${missing.join(', ')}`
      : String(item.title)
  }
  if (item.reason) return `${item.title ? `${item.title}: ` : ''}${item.reason}`
  if (item.name) return String(item.name)
  if (item.field) return String(item.field)
  return Object.entries(item)
    .map(([k, v]) => `${k}: ${typeof v === 'object' ? JSON.stringify(v) : v}`)
    .join(' · ')
}

export default function ConsistencyTab({ adventure }) {
  const toast = useToast()
  const [report, setReport] = useState(null)
  const [loading, setLoading] = useState(false)
  const [repairing, setRepairing] = useState(null)
  const [open, setOpen] = useState({})

  const load = useCallback(async () => {
    setLoading(true)
    try {
      const data = await api.getConsistency(adventure.id)
      setReport(data)
    } catch (e) {
      setReport(null)
      toast(e.message)
    } finally {
      setLoading(false)
    }
  }, [adventure.id, toast])

  useEffect(() => {
    load()
  }, [load])

  const repair = async (checkId) => {
    setRepairing(checkId)
    try {
      const res = await api.repairConsistency(adventure.id, checkId)
      const fixed = Array.isArray(res?.repaired) ? res.repaired.length : 0
      const errors = Array.isArray(res?.errors) ? res.errors : []
      if (errors.length) {
        const why = errors.slice(0, 2).map((e) => String(e).slice(0, 110)).join(' · ')
        const more = errors.length > 2 ? ` (+${errors.length - 2})` : ''
        toast(`Reparado: ${fixed}. Pendência: ${why}${more}`, 'error')
      } else {
        toast(fixed ? `Reparado: ${fixed} item(ns).` : 'Nada a reparar.', 'ok')
      }
      await load()
    } catch (e) {
      toast(e.message)
    } finally {
      setRepairing(null)
    }
  }

  const checks = report?.checks || []
  const counts = report?.counts || {}

  return (
    <div className="flex flex-col gap-3">
      <div className="flex items-center justify-between gap-2">
        <div className="flex min-w-0 items-center gap-2">
          <ShieldCheck className="h-4 w-4 shrink-0 text-accent-pri" />
          <div className="min-w-0">
            <p className="text-sm font-semibold text-primary">Cross-check</p>
            <p className="truncate text-[11px] text-muted">
              {report
                ? `${checks.length} verificações · ${counts.ok || 0} ok · ${counts.warn || 0} aviso(s) · ${counts.error || 0} erro(s)`
                : 'Ficha ↔ retrato ↔ imagem ↔ memória ↔ embedding'}
            </p>
          </div>
        </div>
        <button
          onClick={load}
          disabled={loading}
          className="flex shrink-0 items-center gap-1.5 rounded-lg border border-accent-pri/40 bg-accent-pri/20 px-3 py-1 text-xs font-semibold text-accent-pri transition hover:bg-accent-pri/30 disabled:opacity-50"
        >
          <RefreshCw className={`h-3.5 w-3.5 ${loading ? 'animate-spin' : ''}`} />
          <span>{loading ? 'Checando…' : 'Checar'}</span>
        </button>
      </div>

      {report && !report.ok && (
        <div className="rounded-xl border border-red-400/40 bg-red-400/10 px-3 py-2 text-[11px] text-red-300">
          O projeto tem pontos fora de sintonia. Repare o que for possível ou ajuste a
          mão.
        </div>
      )}

      <div className="flex flex-col gap-2">
        {checks.map((c) => {
          const st = STATUS[c.status] || STATUS.warn
          const Icon = st.Icon
          const isOpen = !!open[c.id]
          const items = c.items || []
          return (
            <div
              key={c.id}
              className="rounded-xl border border-subtle bg-surface p-3"
            >
              <div className="flex items-start gap-2">
                <Icon className={`mt-0.5 h-4 w-4 shrink-0 ${st.text}`} />
                <div className="min-w-0 flex-1">
                  <p className="text-xs font-semibold text-primary">{c.name}</p>
                  <p className="mt-0.5 text-[11px] leading-relaxed text-secondary">
                    {c.detail}
                  </p>
                </div>
                <span
                  className={`shrink-0 rounded-md border px-1.5 py-0.5 text-[10px] font-semibold ${st.chip}`}
                >
                  {st.label}
                </span>
              </div>

              {items.length > 0 && (
                <button
                  onClick={() => setOpen((s) => ({ ...s, [c.id]: !s[c.id] }))}
                  className="mt-2 flex items-center gap-1 text-[11px] font-medium text-accent-pri hover:underline"
                >
                  {isOpen ? (
                    <ChevronDown className="h-3 w-3" />
                  ) : (
                    <ChevronRight className="h-3 w-3" />
                  )}
                  <span>
                    {items.length} item(ns)
                  </span>
                </button>
              )}

              {isOpen && items.length > 0 && (
                <ul className="mt-1.5 flex flex-col gap-1 border-l border-subtle pl-3">
                  {items.map((item, i) => (
                    <li key={i} className="text-[11px] leading-relaxed text-secondary">
                      {itemLine(item)}
                    </li>
                  ))}
                </ul>
              )}

              {c.status !== 'ok' && c.repairable !== false && (
                <button
                  onClick={() => repair(c.id)}
                  disabled={repairing === c.id}
                  className="mt-2 flex w-full items-center justify-center gap-1.5 rounded-lg border border-subtle bg-elevated px-3 py-1.5 text-[11px] font-semibold text-primary transition hover:border-accent-pri/40 hover:text-accent-pri disabled:opacity-50"
                >
                  <ScanLine className="h-3.5 w-3.5" />
                  <span>
                    {repairing === c.id ? 'Reparando…' : 'Reparar com IA'}
                  </span>
                </button>
              )}
            </div>
          )
        })}

        {!loading && checks.length === 0 && (
          <p className="py-6 text-center text-xs text-muted">
            Nenhuma verificação disponível para esta aventura.
          </p>
        )}
      </div>
    </div>
  )
}
