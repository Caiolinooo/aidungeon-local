import React, { useMemo, useState } from 'react'
import { Check, KeyRound, Layers, Plus, Settings2 } from 'lucide-react'

const KIND_LABELS = {
  llm: 'LLM',
  image: 'Imagem',
  voice: 'Voz',
  embeddings: 'Embeddings',
}

export default function ProfileQuickBar({
  kind,
  profiles = [],
  activeProfileId = null,
  activatingId = null,
  onActivate,
  onQuickSaveCurrent,
  onManageProfiles,
}) {
  const [savingOpen, setSavingOpen] = useState(false)
  const [newName, setNewName] = useState('')
  const [saving, setSaving] = useState(false)

  const matchingProfiles = useMemo(
    () => profiles.filter((p) => Array.isArray(p.capabilities) && p.capabilities.includes(kind)),
    [profiles, kind],
  )

  const handleQuickSave = async (e) => {
    e.preventDefault()
    const trimmed = newName.trim()
    if (!trimmed || !onQuickSaveCurrent) return
    setSaving(true)
    try {
      await onQuickSaveCurrent(trimmed)
      setNewName('')
      setSavingOpen(false)
    } finally {
      setSaving(false)
    }
  }

  return (
    <div className="rounded-2xl border border-subtle bg-surface p-3.5 space-y-2.5">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div className="flex items-center gap-1.5">
          <Layers className="w-3.5 h-3.5 text-accent-pri" />
          <span className="text-[11px] font-bold uppercase tracking-wider text-primary">
            Perfis salvos ({KIND_LABELS[kind] || kind})
          </span>
          <span className="text-[11px] text-muted">
            — troque de provedor em 1 clique sem redigitar API
          </span>
        </div>

        <div className="flex items-center gap-1.5">
          {onQuickSaveCurrent && (
            <button
              type="button"
              onClick={() => setSavingOpen((v) => !v)}
              className="flex items-center gap-1 rounded-lg border border-subtle bg-elevated px-2.5 py-1 text-[11px] font-semibold text-secondary hover:text-primary transition"
            >
              <Plus className="w-3 h-3" />
              <span>Salvar atual</span>
            </button>
          )}
          {onManageProfiles && (
            <button
              type="button"
              onClick={onManageProfiles}
              className="flex items-center gap-1 rounded-lg border border-accent-pri/40 bg-accent-pri/10 px-2.5 py-1 text-[11px] font-semibold text-accent-pri hover:bg-accent-pri/20 transition"
            >
              <Settings2 className="w-3 h-3" />
              <span>Gerenciar perfis</span>
            </button>
          )}
        </div>
      </div>

      {savingOpen && (
        <form onSubmit={handleQuickSave} className="flex flex-wrap items-center gap-2 rounded-xl border border-subtle bg-elevated/60 p-2.5">
          <input
            type="text"
            value={newName}
            onChange={(e) => setNewName(e.target.value)}
            placeholder={`Nome do perfil (ex.: OpenAI, OpenRouter, Local ${KIND_LABELS[kind]})`}
            className="flex-1 min-w-[200px] rounded-lg border border-subtle bg-surface px-2.5 py-1.5 text-xs text-primary outline-none focus:border-accent-pri"
            autoFocus
          />
          <button
            type="submit"
            disabled={saving || !newName.trim()}
            className="rounded-lg bg-accent-pri px-3 py-1.5 text-xs font-semibold text-canvas hover:bg-accent-hov transition disabled:opacity-50"
          >
            {saving ? 'Salvando…' : 'Salvar perfil'}
          </button>
          <button
            type="button"
            onClick={() => { setSavingOpen(false); setNewName('') }}
            className="rounded-lg border border-subtle px-2.5 py-1.5 text-xs text-secondary hover:text-primary"
          >
            Cancelar
          </button>
        </form>
      )}

      {matchingProfiles.length === 0 ? (
        <div className="flex flex-wrap items-center justify-between gap-2 rounded-xl border border-dashed border-subtle bg-elevated/30 px-3 py-2 text-xs text-muted">
          <span>
            Nenhum perfil salvo para {KIND_LABELS[kind]} ainda. Salve este provedor ou crie perfis em{' '}
            <strong className="text-secondary">Perfis e APIs</strong> para reaproveitar chaves entre modalidades.
          </span>
        </div>
      ) : (
        <div className="flex flex-wrap gap-1.5">
          {matchingProfiles.map((prof) => {
            const isActive = activeProfileId === prof.id
            const isBusy = activatingId === prof.id
            const hasKey = Boolean(prof.api_key_set || prof[kind]?.api_key_set)
            const sharedCount = (prof.capabilities || []).length
            return (
              <button
                key={prof.id}
                type="button"
                disabled={Boolean(activatingId)}
                onClick={() => onActivate && onActivate(prof.id, [kind])}
                title={`Ativar "${prof.name}" em ${KIND_LABELS[kind]}${sharedCount > 1 ? ` (compartilha API com ${sharedCount} modalidades)` : ''}`}
                className={`flex items-center gap-1.5 rounded-xl border px-3 py-1.5 text-xs font-medium transition disabled:opacity-60 ${
                  isActive
                    ? 'border-accent-pri bg-accent-pri text-canvas font-semibold shadow-sm'
                    : 'border-subtle bg-elevated/60 text-primary hover:border-accent-pri/50 hover:bg-elevated'
                }`}
              >
                {isActive && <Check className="w-3.5 h-3.5 shrink-0" />}
                <span>{isBusy ? 'Ativando…' : prof.name}</span>
                {hasKey && (
                  <KeyRound
                    className={`w-3 h-3 shrink-0 ${isActive ? 'text-canvas/90' : 'text-accent-pri'}`}
                    aria-label="Chave API salva"
                  />
                )}
                {sharedCount > 1 && (
                  <span
                    className={`rounded px-1 py-0.5 text-[9px] font-bold uppercase ${
                      isActive ? 'bg-canvas/20 text-canvas' : 'bg-surface text-muted border border-subtle'
                    }`}
                  >
                    {sharedCount}x
                  </span>
                )}
              </button>
            )
          })}
        </div>
      )}
    </div>
  )
}
