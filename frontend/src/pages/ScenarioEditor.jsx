import { useCallback, useEffect, useState } from 'react'
import { Link, useNavigate, useParams } from 'react-router-dom'
import { api } from '../api.js'
import { useToast } from '../components/Toast.jsx'
import TagInput from '../components/TagInput.jsx'

// Tela 3 — Editor de Scenario (form completo, PLANO.md §4.5).
//  - /scenarios/new      → criação (POST /api/scenarios)
//  - /scenarios/:id/edit → SOMENTE LEITURA: o contrato (docs/openapi.yaml) não
//                          define PUT/PATCH /api/scenarios/{id} na v1.
export default function ScenarioEditor() {
  const { id } = useParams()
  const readOnly = !!id
  const toast = useToast()
  const navigate = useNavigate()

  const [form, setForm] = useState({
    title: '',
    description: '',
    ai_instructions: '',
    plot_essentials: '',
    authors_note: '',
    opening_text: '',
    tags: [],
    third_person: false,
  })
  const [loading, setLoading] = useState(readOnly)
  const [error, setError] = useState(null)
  const [saving, setSaving] = useState(false)

  const load = useCallback(async () => {
    if (!readOnly) return
    setError(null)
    setLoading(true)
    try {
      const s = await api.getScenario(id)
      setForm({
        title: s.title ?? '',
        description: s.description ?? '',
        ai_instructions: s.ai_instructions ?? '',
        plot_essentials: s.plot_essentials ?? '',
        authors_note: s.authors_note ?? '',
        opening_text: s.opening_text ?? '',
        tags: s.tags ?? [],
        third_person: !!s.third_person,
      })
    } catch (e) {
      setError(e)
    } finally {
      setLoading(false)
    }
  }, [id, readOnly])

  useEffect(() => {
    load()
  }, [load])

  const save = async () => {
    if (!form.title.trim()) {
      toast('Title is required.')
      return
    }
    setSaving(true)
    try {
      await api.createScenario(form)
      toast('Scenario created.', 'ok')
      navigate('/')
    } catch (e) {
      toast(e.message)
      setSaving(false)
    }
  }

  const inputCls =
    'w-full rounded-lg border border-zinc-700 bg-zinc-800 px-3 py-2 text-sm text-zinc-100 outline-none focus:border-emerald-600 disabled:opacity-60'
  const labelCls = 'mb-1 block text-xs font-semibold uppercase tracking-wide text-zinc-500'
  const hintCls = 'mb-1 block text-xs text-zinc-600'

  if (loading) return <div className="flex h-full items-center justify-center text-zinc-500">Loading…</div>

  if (error) {
    return (
      <div className="flex h-full flex-col items-center justify-center gap-4 p-8 text-center">
        <p className="text-red-300">
          {error.status === 0 ? 'Cannot reach the backend server. Is it running?' : error.message}
        </p>
        <div className="flex gap-2">
          <button onClick={load} className="rounded-lg bg-emerald-700 px-4 py-2 text-sm font-semibold text-white hover:bg-emerald-600">
            ↻ Retry
          </button>
          <Link to="/" className="rounded-lg border border-zinc-700 px-4 py-2 text-sm text-zinc-300 hover:bg-zinc-800">
            ← Back to home
          </Link>
        </div>
      </div>
    )
  }

  return (
    <div className="mx-auto h-full max-w-3xl overflow-y-auto px-4 py-8">
      <header className="mb-6">
        <Link to="/" className="text-xs text-zinc-500 hover:text-zinc-300">
          ← Home
        </Link>
        <h1 className="mt-1 text-2xl font-bold text-zinc-100">
          {readOnly ? `Scenario — ${form.title}` : 'New Scenario'}
        </h1>
      </header>

      {readOnly && (
        <div className="mb-6 rounded-lg border border-amber-900 bg-amber-950/50 px-4 py-3 text-sm text-amber-200">
          Editing a saved scenario: v1 does not support PUT (API contract). This view is
          read-only — create a new scenario instead.
        </div>
      )}

      <fieldset disabled={readOnly || saving} className="flex flex-col gap-4">
        <label className="block">
          <span className={labelCls}>Title *</span>
          <input value={form.title} onChange={(e) => setForm({ ...form, title: e.target.value })} className={inputCls} />
        </label>

        <label className="block">
          <span className={labelCls}>Description</span>
          <textarea value={form.description} onChange={(e) => setForm({ ...form, description: e.target.value })} rows={2} className={`${inputCls} resize-y`} />
        </label>

        <label className="block">
          <span className={labelCls}>AI Instructions</span>
          <span className={hintCls}>Style/system prompt for the narrator (top of the context).</span>
          <textarea value={form.ai_instructions} onChange={(e) => setForm({ ...form, ai_instructions: e.target.value })} rows={4} className={`${inputCls} resize-y`} />
        </label>

        <label className="block">
          <span className={labelCls}>Plot Essentials (Memory)</span>
          <span className={hintCls}>Fixed block: who you are, the world, the rules.</span>
          <textarea value={form.plot_essentials} onChange={(e) => setForm({ ...form, plot_essentials: e.target.value })} rows={4} className={`${inputCls} resize-y`} />
        </label>

        <label className="block">
          <span className={labelCls}>Author's Note</span>
          <span className={hintCls}>Tone note injected near the end of the context.</span>
          <textarea value={form.authors_note} onChange={(e) => setForm({ ...form, authors_note: e.target.value })} rows={3} className={`${inputCls} resize-y`} />
        </label>

        <label className="block">
          <span className={labelCls}>Opening Text</span>
          <span className={hintCls}>The story's first block (becomes the start action of new adventures).</span>
          <textarea value={form.opening_text} onChange={(e) => setForm({ ...form, opening_text: e.target.value })} rows={6} className={`${inputCls} resize-y font-prose`} />
        </label>

        <label className="block">
          <span className={labelCls}>Tags</span>
          <TagInput value={form.tags} onChange={(tags) => setForm({ ...form, tags })} disabled={readOnly} placeholder="e.g. fantasy" />
        </label>

        <label className="flex items-center gap-2 text-sm text-zinc-300">
          <input
            type="checkbox"
            checked={form.third_person}
            onChange={(e) => setForm({ ...form, third_person: e.target.checked })}
            className="accent-emerald-600"
          />
          Third person narration (default is 2nd person, like AI Dungeon)
        </label>
      </fieldset>

      {!readOnly && (
        <div className="mt-6 flex justify-end gap-2">
          <Link to="/" className="rounded-lg border border-zinc-700 px-4 py-2 text-sm text-zinc-300 hover:bg-zinc-800">
            Cancel
          </Link>
          <button
            onClick={save}
            disabled={saving}
            className="rounded-lg bg-emerald-700 px-5 py-2 text-sm font-semibold text-white hover:bg-emerald-600 disabled:opacity-50"
          >
            {saving ? 'Creating…' : 'Create scenario'}
          </button>
        </div>
      )}
    </div>
  )
}
