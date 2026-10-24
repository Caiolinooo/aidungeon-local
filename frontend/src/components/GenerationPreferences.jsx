import { useState } from 'react'
import { SlidersHorizontal, X } from 'lucide-react'
import { api } from '../api.js'
import { useToast } from './Toast.jsx'

const DEFAULTS = { max_tokens: 300, temperature: 0.85, auto_narrate: false, auto_see: false }

export default function GenerationPreferences({ adventure, onSaved }) {
  const toast = useToast()
  const [open, setOpen] = useState(false)
  const [saving, setSaving] = useState(false)
  const stored = adventure?.ui_state?._generation || {}
  const value = { ...DEFAULTS, ...stored }

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
        type="button"
        onClick={() => setOpen((current) => !current)}
        aria-label="Preferências desta aventura"
        aria-expanded={open}
        title="Resposta, voz e imagem desta aventura"
        className="flex h-10 w-10 items-center justify-center rounded-full border border-subtle bg-surface text-secondary hover:bg-elevated hover:text-primary transition"
      >
        <SlidersHorizontal className="h-4 w-4 text-accent-pri" />
      </button>
      {open && (
        <section aria-label="Preferências desta aventura" className="absolute right-0 top-12 z-50 w-72 rounded-xl border border-subtle bg-surface p-4 shadow-2xl">
          <div className="mb-3 flex items-center justify-between">
            <div>
              <h2 className="text-sm font-semibold text-primary">Desta aventura</h2>
              <p className="text-[11px] text-muted">Não altera os defaults globais.</p>
            </div>
            <button type="button" onClick={() => setOpen(false)} aria-label="Fechar preferências" className="rounded-lg p-1.5 text-secondary hover:bg-elevated">
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
      )}
    </div>
  )
}
