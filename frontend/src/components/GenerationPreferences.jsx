import { useEffect, useRef, useState } from 'react'
import { createPortal } from 'react-dom'
import { SlidersHorizontal, X } from 'lucide-react'
import { api } from '../api.js'
import { useToast } from './Toast.jsx'

const DEFAULTS = { max_tokens: 300, temperature: 0.85, auto_narrate: false, auto_see: false }
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

export default function GenerationPreferences({ adventure, onSaved }) {
  const toast = useToast()
  const triggerRef = useRef(null)
  const [open, setOpen] = useState(false)
  const [anchor, setAnchor] = useState(null)
  const [saving, setSaving] = useState(false)
  const stored = adventure?.ui_state?._generation || {}
  const value = { ...DEFAULTS, ...stored }

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

  const save = async (patch) => {
    const next = { ...value, ...patch }
    setSaving(true)
    try {
      const state = { ...(adventure.ui_state || {}), _generation: next }
      await api.putUiState(adventure.id, state)
      onSaved(state)
    } catch (e) {
      toast(e.message || 'Não foi possível salvar as preferências.')
    } finally {
      setSaving(false)
    }
  }

  return (
    <div className="relative shrink-0">
      <button
        ref={triggerRef}
        type="button"
        onClick={toggle}
        aria-label="Preferências desta aventura"
        aria-expanded={open}
        title="Resposta, voz e imagem desta aventura"
        className="flex h-9 w-9 items-center justify-center rounded-full border border-subtle bg-surface text-secondary transition hover:bg-elevated hover:text-primary sm:h-10 sm:w-10"
      >
        <SlidersHorizontal className="h-4 w-4 text-accent-pri" />
      </button>
      {open && anchor && createPortal(
        <div className="fixed inset-0 z-50">
          <button
            type="button"
            className="absolute inset-0 cursor-default"
            aria-label="Fechar preferências"
            onClick={() => setOpen(false)}
          />
          <section
            aria-label="Preferências desta aventura"
            style={anchor}
            className="fixed z-10 max-h-[min(70dvh,var(--app-height,100dvh))] max-w-[min(25rem,calc(100vw-1.5rem))] overflow-y-auto rounded-xl border border-subtle bg-surface p-4 shadow-2xl max-md:inset-x-3 md:w-[min(25rem,calc(100vw-1.5rem))]"
          >
            <div className="mb-3 flex items-center justify-between">
              <div>
                <h2 className="text-sm font-semibold text-primary">Desta aventura</h2>
                <p className="text-[11px] text-muted">Não altera os defaults globais.</p>
              </div>
              <button type="button" onClick={() => setOpen(false)} aria-label="Fechar preferências" className="flex min-h-11 min-w-11 items-center justify-center rounded-lg text-secondary hover:bg-elevated">
                <X className="h-4 w-4" />
              </button>
            </div>
            <div className="space-y-3">
              <label className="block text-xs text-secondary">
                <span className="mb-1 block font-semibold">Tamanho da resposta</span>
                <select
                  value={value.max_tokens}
                  disabled={saving}
                  onChange={(e) => save({ max_tokens: Number(e.target.value) })}
                  className="w-full rounded-lg border border-subtle bg-elevated px-3 py-2 text-primary"
                >
                  <option value={180}>Curta</option>
                  <option value={300}>Média</option>
                  <option value={600}>Longa</option>
                </select>
              </label>
              <label className="block text-xs text-secondary">
                <span className="mb-1 block font-semibold">Criatividade</span>
                <select
                  value={value.temperature}
                  disabled={saving}
                  onChange={(e) => save({ temperature: Number(e.target.value) })}
                  className="w-full rounded-lg border border-subtle bg-elevated px-3 py-2 text-primary"
                >
                  <option value={0.45}>Mais fiel</option>
                  <option value={0.85}>Equilibrada</option>
                  <option value={1.15}>Mais criativa</option>
                </select>
              </label>
              <label className="flex items-center gap-2 text-xs text-secondary">
                <input type="checkbox" checked={value.auto_narrate} disabled={saving} onChange={(e) => save({ auto_narrate: e.target.checked })} />
                Narrar novas respostas em voz alta
              </label>
              <label className="flex items-center gap-2 text-xs text-secondary">
                <input type="checkbox" checked={value.auto_see} disabled={saving} onChange={(e) => save({ auto_see: e.target.checked })} />
                Gerar imagem após novas respostas
              </label>
            </div>
          </section>
        </div>,
        document.body,
      )}
    </div>
  )
}
