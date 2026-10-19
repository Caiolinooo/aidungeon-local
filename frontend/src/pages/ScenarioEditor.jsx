import { useCallback, useEffect, useRef, useState } from 'react'
import { Link, useNavigate, useParams } from 'react-router-dom'
import { api } from '../api.js'
import { useToast } from '../components/Toast.jsx'
import TagInput from '../components/TagInput.jsx'
import StoryCardsEditor from '../components/StoryCardsEditor.jsx'

// Editor de Scenario.
//  - /scenarios/new      → POST /api/scenarios
//  - /scenarios/:id/edit → PATCH /api/scenarios/{id}; duplicar ou apagar
function draftLanguage() {
  const lang = (typeof navigator !== 'undefined' && navigator.language) || 'pt'
  const sub = String(lang).slice(0, 2).toLowerCase()
  return /^[a-z]{2}$/.test(sub) ? sub : 'pt'
}

const EMPTY = {
  title: '',
  description: '',
  ai_instructions: '',
  plot_essentials: '',
  authors_note: '',
  opening_text: '',
  image_style: '',
  tags: [],
  story_cards: [],
  third_person: false,
}

const miniBtnCls =
  'shrink-0 rounded-md border border-zinc-700 px-2 py-0.5 text-[11px] text-zinc-400 hover:bg-zinc-800 hover:text-zinc-200 disabled:opacity-40'

export default function ScenarioEditor() {
  const { id } = useParams()
  const isEdit = !!id
  const toast = useToast()
  const navigate = useNavigate()

  const [form, setForm] = useState(EMPTY)
  const [loading, setLoading] = useState(isEdit)
  const [error, setError] = useState(null)
  const [pending, setPending] = useState(null)
  const [regen, setRegen] = useState(null)
  const [webOn, setWebOn] = useState(false)
  const opRef = useRef(false)

  const busy = pending !== null || regen !== null

  const load = useCallback(async () => {
    if (!id) return
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
        image_style: s.image_style ?? '',
        tags: s.tags ?? [],
        story_cards: Array.isArray(s.story_cards) ? s.story_cards : [],
        third_person: !!s.third_person,
      })
    } catch (e) {
      setError(e)
    } finally {
      setLoading(false)
    }
  }, [id])

  useEffect(() => {
    load()
  }, [load])

  // Troca de :id (ex.: duplicar) reaproveita esta tela — solta o lock do save anterior.
  useEffect(() => {
    opRef.current = false
    setPending(null)
    setRegen(null)
  }, [id])

  const payload = () => ({ ...form, title: form.title.trim() })

  const noteWeb = (sources) => {
    if (sources?.length) toast(`Enriched with ${sources.length} web sources.`, 'ok')
  }

  const refine = async (field) => {
    if (opRef.current) return
    const typed = window.prompt('Refinement instruction (optional):')
    if (typed === null || opRef.current) return
    const instruction = typed.trim()
    opRef.current = true
    setRegen(field)
    try {
      const current =
        field === 'tags' ? (form.tags ?? []).join(', ')
        : field === 'story_cards' ? undefined
        : form[field]
      const r = await api.assistantField({
        field,
        current,
        draft: form,
        instruction: instruction || undefined,
        web_search: webOn,
        language: draftLanguage(),
      })
      setForm((f) => {
        const next = { ...f }
        if (field === 'story_cards') {
          if (Array.isArray(r.story_cards)) next.story_cards = r.story_cards
        } else if (field === 'tags') {
          next.tags = String(r.value ?? '').split(',').map((t) => t.trim()).filter(Boolean)
        } else {
          next[field] = r.value ?? ''
        }
        return next
      })
      noteWeb(r?.web_sources)
    } catch (e) {
      toast(e.message)
    } finally {
      opRef.current = false
      setRegen(null)
    }
  }

  const enrichCard = async (index) => {
    if (opRef.current) return
    const card = (form.story_cards ?? [])[index]
    if (!card) return
    const typed = window.prompt('Instruction for this card (optional)')
    if (typed === null || opRef.current) return
    const concept = String(card.title ?? '').trim()
    if (concept.length < 2) {
      toast('Card title must be at least 2 characters before AI enrich.')
      return
    }
    opRef.current = true
    setRegen(`card-${index}`)
    try {
      const r = await api.assistantCard({
        concept,
        card_type: card.type || 'character',
        draft: form,
        current_card: card,
        instruction: typed.trim() || undefined,
        web_search: webOn,
        language: draftLanguage(),
      })
      if (!r?.card) {
        toast('The assistant returned no card.')
        return
      }
      setForm((f) => ({
        ...f,
        story_cards: (f.story_cards ?? []).map((c, j) => (j === index ? { ...c, ...r.card } : c)),
      }))
      noteWeb(r?.web_sources)
    } catch (e) {
      toast(e.message)
    } finally {
      opRef.current = false
      setRegen(null)
    }
  }

  const addCardWithAi = async () => {
    if (opRef.current) return
    const typed = window.prompt('Card name or concept')
    if (typed === null || opRef.current) return
    const concept = typed.trim()
    if (concept.length < 2) {
      toast('Enter a card name or concept (min. 2 characters).')
      return
    }
    opRef.current = true
    setRegen('card-new')
    try {
      const r = await api.assistantCard({
        concept,
        card_type: 'character',
        draft: form,
        web_search: webOn,
        language: draftLanguage(),
      })
      if (!r?.card) {
        toast('The assistant returned no card.')
        return
      }
      setForm((f) => ({ ...f, story_cards: [...(f.story_cards ?? []), r.card] }))
      noteWeb(r?.web_sources)
    } catch (e) {
      toast(e.message)
    } finally {
      opRef.current = false
      setRegen(null)
    }
  }

  const save = async () => {
    if (!form.title.trim()) {
      toast('Title is required.')
      return
    }
    if (opRef.current) return
    opRef.current = true
    setPending('save')
    try {
      if (isEdit) {
        await api.updateScenario(id, payload())
        toast('Scenario saved.', 'ok')
        opRef.current = false
        setPending(null)
      } else {
        await api.createScenario(payload())
        toast('Scenario created.', 'ok')
        navigate('/')
      }
    } catch (e) {
      toast(e.message)
      opRef.current = false
      setPending(null)
    }
  }

  const duplicate = async () => {
    if (!form.title.trim()) {
      toast('Title is required.')
      return
    }
    if (opRef.current) return
    opRef.current = true
    setPending('copy')
    try {
      const created = await api.createScenario({ ...payload(), title: `${form.title.trim()} (copy)` })
      toast('Scenario duplicated.', 'ok')
      navigate(`/scenarios/${created.id}/edit`)
    } catch (e) {
      toast(e.message)
      opRef.current = false
      setPending(null)
    }
  }

  const remove = async () => {
    const title = form.title.trim() || 'this scenario'
    if (!window.confirm(`Delete scenario "${title}"? Adventures that started from it keep running.`)) return
    if (opRef.current) return
    opRef.current = true
    setPending('delete')
    try {
      await api.deleteScenario(id)
      navigate('/')
    } catch (e) {
      toast(e.message)
      opRef.current = false
      setPending(null)
    }
  }

  const inputCls =
    'w-full rounded-lg border border-zinc-700 bg-zinc-800 px-3 py-2 text-sm text-zinc-100 outline-none focus:border-emerald-600 disabled:opacity-60'
  const labelCls = 'text-xs font-semibold uppercase tracking-wide text-zinc-500'
  const hintCls = 'block text-xs text-zinc-600'

  const regenBtn = (field) => (
    <button
      type="button"
      onClick={() => refine(field)}
      disabled={busy}
      title="Regenerate this field keeping coherence with the rest of the scenario"
      className={miniBtnCls}
    >
      {regen === field ? '… generating' : '↻ Regenerate'}
    </button>
  )

  const fieldHeader = (field, label, hint) => (
    <span className="mb-1 flex items-center justify-between gap-2">
      <span>
        <span className={labelCls}>{label}</span>
        {hint && <span className={hintCls}>{hint}</span>}
      </span>
      {regenBtn(field)}
    </span>
  )

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
          {isEdit ? (form.title || 'Edit Scenario') : 'New Scenario'}
        </h1>
      </header>

      <fieldset disabled={busy} className="m-0 flex min-w-0 flex-col gap-4 border-0 p-0">
        <label className="block">
          {fieldHeader('title', 'Title *')}
          <input
            value={form.title}
            onChange={(e) => setForm((f) => ({ ...f, title: e.target.value }))}
            className={inputCls}
          />
        </label>

        <label className="block">
          {fieldHeader('description', 'Description')}
          <textarea
            value={form.description}
            onChange={(e) => setForm((f) => ({ ...f, description: e.target.value }))}
            rows={2}
            className={`${inputCls} resize-y`}
          />
        </label>

        <label className="block">
          {fieldHeader('ai_instructions', 'AI Instructions', 'Style/system prompt for the narrator (top of the context).')}
          <textarea
            value={form.ai_instructions}
            onChange={(e) => setForm((f) => ({ ...f, ai_instructions: e.target.value }))}
            rows={4}
            className={`${inputCls} resize-y`}
          />
        </label>

        <label className="block">
          {fieldHeader('plot_essentials', 'Plot Essentials (Memory)', 'Fixed block: who you are, the world, the rules.')}
          <textarea
            value={form.plot_essentials}
            onChange={(e) => setForm((f) => ({ ...f, plot_essentials: e.target.value }))}
            rows={4}
            className={`${inputCls} resize-y`}
          />
        </label>

        <label className="block">
          {fieldHeader('authors_note', "Author's Note", 'Tone note injected near the end of the context.')}
          <textarea
            value={form.authors_note}
            onChange={(e) => setForm((f) => ({ ...f, authors_note: e.target.value }))}
            rows={3}
            className={`${inputCls} resize-y`}
          />
        </label>

        <label className="block">
          {fieldHeader('opening_text', 'Opening Text', "The story's first block (becomes the start action of new adventures).")}
          <textarea
            value={form.opening_text}
            onChange={(e) => setForm((f) => ({ ...f, opening_text: e.target.value }))}
            rows={6}
            className={`${inputCls} resize-y font-prose`}
          />
        </label>

        <label className="block">
          {fieldHeader('image_style', 'Visual Style (image generation)')}
          <input
            value={form.image_style}
            onChange={(e) => setForm((f) => ({ ...f, image_style: e.target.value }))}
            placeholder="e.g. dark cinematic photography, neon rain"
            className={inputCls}
          />
        </label>

        <div className="block">
          {fieldHeader('tags', 'Tags')}
          <TagInput
            value={form.tags}
            onChange={(tags) => setForm((f) => ({ ...f, tags }))}
            disabled={busy}
            placeholder="e.g. fantasy"
          />
        </div>

        <div className="block">
          <div className="mb-1">
            <div className={labelCls}>Story Cards ({(form.story_cards ?? []).length})</div>
            <div className={hintCls}>NPCs, places, factions — injected when a trigger word appears.</div>
          </div>
          <StoryCardsEditor
            cards={form.story_cards}
            onChange={(story_cards) => setForm((f) => ({ ...f, story_cards }))}
            busy={busy}
            onAiCard={enrichCard}
            webOn={webOn}
            onToggleWeb={setWebOn}
          >
            <button type="button" onClick={addCardWithAi} disabled={busy} className={miniBtnCls}>
              + Add story card with AI
            </button>
            {regenBtn('story_cards')}
          </StoryCardsEditor>
        </div>

        <label className="flex items-center gap-2 text-sm text-zinc-300">
          <input
            type="checkbox"
            checked={form.third_person}
            onChange={(e) => setForm((f) => ({ ...f, third_person: e.target.checked }))}
            className="accent-emerald-600"
          />
          Third person narration (default is 2nd person, like AI Dungeon)
        </label>
      </fieldset>

      {isEdit ? (
        <div className="mt-6 flex flex-wrap items-center justify-end gap-2">
          <button
            type="button"
            onClick={remove}
            disabled={busy}
            className="rounded-lg border border-red-900 px-4 py-2 text-sm text-red-400 hover:bg-red-950 disabled:opacity-50"
          >
            {pending === 'delete' ? 'Deleting…' : 'Delete'}
          </button>
          <button
            type="button"
            onClick={duplicate}
            disabled={busy}
            className="rounded-lg border border-zinc-700 px-4 py-2 text-sm text-zinc-300 hover:bg-zinc-800 disabled:opacity-50"
          >
            {pending === 'copy' ? 'Duplicating…' : 'Duplicate'}
          </button>
          <button
            type="button"
            onClick={save}
            disabled={busy}
            className="rounded-lg bg-emerald-700 px-5 py-2 text-sm font-semibold text-white hover:bg-emerald-600 disabled:opacity-50"
          >
            {pending === 'save' ? 'Saving…' : 'Save changes'}
          </button>
        </div>
      ) : (
        <div className="mt-6 flex justify-end gap-2">
          <Link to="/" className="rounded-lg border border-zinc-700 px-4 py-2 text-sm text-zinc-300 hover:bg-zinc-800">
            Cancel
          </Link>
          <button
            onClick={save}
            disabled={busy}
            className="rounded-lg bg-emerald-700 px-5 py-2 text-sm font-semibold text-white hover:bg-emerald-600 disabled:opacity-50"
          >
            {pending === 'save' ? 'Creating…' : 'Create scenario'}
          </button>
        </div>
      )}
    </div>
  )
}
