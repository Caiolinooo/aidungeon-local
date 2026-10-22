import { useEffect, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { api } from '../../api.js'
import { clearToken } from '../../auth.js'
import { useToast } from '../Toast.jsx'
import { fmtDate } from '../../utils.js'

const RULES_MAX = 4000

function rulesTextFrom(res) {
  if (typeof res?.character_rules === 'string') return res.character_rules.slice(0, RULES_MAX)
  if (typeof res?.adventure?.character_rules === 'string') {
    return res.adventure.character_rules.slice(0, RULES_MAX)
  }
  return null
}

// O apply devolve o registro da aventura, sem actions/cards/memories.
// O PATCH devolve o estado completo. Nos dois casos o registro entra por cima
// e as coleções que a resposta não traz continuam as da aventura aberta.
function mergeRulesUpdate(current, res) {
  const text = rulesTextFrom(res)
  const fallback = text == null ? (current?.character_rules || '') : text
  if (!res || typeof res !== 'object' || !res.id) {
    return { ...current, character_rules: fallback }
  }
  return { ...current, ...res, character_rules: text == null ? (res.character_rules ?? fallback) : text }
}

// Aba Detalhes: metadados da aventura + regras de personagem editáveis.
// Também concentra as ações de sessão: Export backup e Sair (logout).
export default function DetailsTab({ adventure, onAdventureChange }) {
  const toast = useToast()
  const navigate = useNavigate()
  const [exporting, setExporting] = useState(false)
  const [rules, setRules] = useState(() => (adventure?.character_rules || '').slice(0, RULES_MAX))
  const [rulesets, setRulesets] = useState([])
  const [rulesetId, setRulesetId] = useState('')
  const [saving, setSaving] = useState(false)
  const [applying, setApplying] = useState('')

  useEffect(() => {
    setRules((adventure?.character_rules || '').slice(0, RULES_MAX))
  }, [adventure?.id, adventure?.character_rules])

  useEffect(() => {
    let cancelled = false
    api.listRulesets()
      .then((rows) => { if (!cancelled) setRulesets(rows) })
      .catch((e) => { if (!cancelled) toast(e.message) })
    return () => { cancelled = true }
  }, [toast])

  const rows = [
    ['Source', adventure.source === 'import' ? 'Imported backup' : 'New'],
    ['Actions', adventure.actions?.length ?? '—'],
    ['Fichas', adventure.story_cards?.length ?? '—'],
    ['Memories', adventure.memories?.length ?? '—'],
    ['Third person', adventure.third_person ? 'Yes' : 'No (2nd person)'],
    ['Created', fmtDate(adventure.created_at)],
    ['Updated', fmtDate(adventure.updated_at)],
  ]

  // Download autenticado: <a href> não leva o header Authorization (Bearer),
  // então baixamos como blob via api.js e disparamos o save no navegador.
  const doExport = async () => {
    setExporting(true)
    try {
      const blob = await api.downloadExport(adventure.id)
      const url = URL.createObjectURL(blob)
      const a = document.createElement('a')
      const safeTitle = (adventure.title || 'adventure').replace(/[^\w\- ]+/g, '').trim() || 'adventure'
      a.href = url
      a.download = `${safeTitle}-backup.zip`
      document.body.appendChild(a)
      a.click()
      a.remove()
      URL.revokeObjectURL(url)
    } catch (e) {
      toast(e.message)
    } finally {
      setExporting(false)
    }
  }

  const logout = () => {
    clearToken()
    navigate('/login', { replace: true })
  }

  const saveRules = async () => {
    const text = rules.slice(0, RULES_MAX)
    setSaving(true)
    try {
      const updated = await api.updateAdventure(adventure.id, { character_rules: text })
      const next = mergeRulesUpdate(adventure, updated?.id ? updated : { character_rules: text })
      setRules((next.character_rules || '').slice(0, RULES_MAX))
      onAdventureChange?.(next)
      toast('Regras salvas.', 'ok')
    } catch (e) {
      toast(e.message)
    } finally {
      setSaving(false)
    }
  }

  const applyRuleset = async (mode) => {
    if (!rulesetId) {
      toast('Escolha um ruleset.')
      return
    }
    setApplying(mode)
    try {
      let current = adventure
      if (rules !== (adventure.character_rules || '')) {
        const saved = await api.updateAdventure(adventure.id, { character_rules: rules.slice(0, RULES_MAX) })
        current = saved?.id ? saved : { ...adventure, character_rules: rules.slice(0, RULES_MAX) }
      }
      const res = await api.applyRuleset(adventure.id, rulesetId, mode)
      const next = mergeRulesUpdate(current, res)
      setRules((next.character_rules || '').slice(0, RULES_MAX))
      onAdventureChange?.(next)
      toast(mode === 'append' ? 'Regras acrescentadas.' : 'Regras substituídas.', 'ok')
    } catch (e) {
      toast(e.message)
    } finally {
      setApplying('')
    }
  }

  const savedRules = adventure.character_rules || ''
  const rulesBusy = saving || applying !== ''

  return (
    <div className="flex flex-col gap-4">
      <div>
        <span className="mb-1 block text-xs font-semibold uppercase tracking-wide text-muted">Título</span>
        <p className="text-lg font-semibold text-primary">{adventure.title}</p>
      </div>

      <div>
        <span className="mb-1 block text-xs font-semibold uppercase tracking-wide text-muted">Description</span>
        <p className="whitespace-pre-wrap text-sm text-secondary">{adventure.description || '—'}</p>
      </div>

      <div>
        <span className="mb-1 block text-xs font-semibold uppercase tracking-wide text-muted">Tags</span>
        <div className="flex flex-wrap gap-1.5">
          {(adventure.tags ?? []).length === 0 && <span className="text-sm text-muted">—</span>}
          {(adventure.tags ?? []).map((t) => (
            <span key={t} className="rounded-full bg-elevated px-2.5 py-0.5 text-xs text-primary">
              {t}
            </span>
          ))}
        </div>
      </div>

      <section className="rounded-xl border border-subtle bg-canvas/40 p-3">
        <div className="mb-1 flex items-center justify-between gap-2">
          <span className="text-xs font-semibold uppercase tracking-wide text-muted">Regras da aventura</span>
          <span className={`text-xs ${rules.length >= RULES_MAX ? 'text-danger-400' : 'text-muted'}`}>
            {rules.length}/{RULES_MAX}
          </span>
        </div>
        <textarea
          value={rules}
          onChange={(e) => setRules(e.target.value.slice(0, RULES_MAX))}
          rows={6}
          disabled={rulesBusy}
          placeholder="Regras de criação de personagem desta aventura"
          className="w-full resize-y rounded-lg border border-subtle bg-surface px-3 py-2 text-sm text-primary outline-none focus:border-accent-pri disabled:opacity-60"
        />
        <div className="mt-2 flex justify-end">
          <button
            type="button"
            onClick={saveRules}
            disabled={rulesBusy || rules === savedRules}
            className="rounded-lg bg-accent-pri px-3 py-1.5 text-xs font-semibold text-white hover:bg-accent-hov disabled:opacity-50"
          >
            {saving ? 'Salvando…' : 'Salvar'}
          </button>
        </div>
        <label className="mt-3 block">
          <span className="mb-1 block text-xs font-semibold uppercase tracking-wide text-muted">Ruleset</span>
          <select
            value={rulesetId}
            disabled={rulesBusy}
            onChange={(e) => setRulesetId(e.target.value)}
            className="w-full rounded-lg border border-subtle bg-surface px-3 py-2 text-sm text-primary outline-none focus:border-accent-pri disabled:opacity-60"
          >
            <option value="">Escolha um ruleset</option>
            {rulesets.map((r) => (
              <option key={r.id} value={String(r.id)}>
                {r.name}{r.builtin ? ' (padrão)' : ''}
              </option>
            ))}
          </select>
        </label>
        <div className="mt-2 flex gap-2">
          <button
            type="button"
            onClick={() => applyRuleset('replace')}
            disabled={rulesBusy || !rulesetId}
            className="rounded-lg border border-subtle px-3 py-1.5 text-xs text-primary hover:bg-surface disabled:opacity-50"
          >
            {applying === 'replace' ? 'Substituindo…' : 'Substituir'}
          </button>
          <button
            type="button"
            onClick={() => applyRuleset('append')}
            disabled={rulesBusy || !rulesetId}
            className="rounded-lg border border-subtle px-3 py-1.5 text-xs text-primary hover:bg-surface disabled:opacity-50"
          >
            {applying === 'append' ? 'Acrescentando…' : 'Acrescentar'}
          </button>
        </div>
      </section>

      <dl className="grid grid-cols-2 gap-x-4 gap-y-2 text-sm">
        {rows.map(([k, v]) => (
          <div key={k} className="flex justify-between gap-2 border-b border-subtle/60 pb-1">
            <dt className="text-muted">{k}</dt>
            <dd className="text-right text-secondary">{v}</dd>
          </div>
        ))}
      </dl>

      <button
        onClick={doExport}
        disabled={exporting}
        className="mt-2 rounded-lg border border-subtle px-4 py-2 text-center text-sm text-secondary hover:bg-surface disabled:opacity-50"
      >
        {exporting ? 'Exporting…' : '⬇ Export backup (.zip — AI Dungeon format)'}
      </button>

      <button
        onClick={logout}
        className="rounded-lg border border-danger-900 px-4 py-2 text-center text-sm text-danger-300 hover:bg-danger-950/60"
      >
        Sair (encerrar sessão)
      </button>
    </div>
  )
}
