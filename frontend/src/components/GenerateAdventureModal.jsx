import { useEffect, useRef, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { api } from '../api.js'
import { useToast } from './Toast.jsx'
import Modal from './Modal.jsx'
import TagInput from './TagInput.jsx'
import StoryCardsEditor from './StoryCardsEditor.jsx'

// Modal "Generate with AI": ideia → rascunho revisável → cenário + aventura.
// Passo 1 manda gênero, tom, POV, protagonista, estilo visual, cards e web search.
// Passo 2 edita cada campo (↻ pede uma instrução opcional) e refina cards com IA.

function draftLanguage() {
  const lang = (typeof navigator !== 'undefined' && navigator.language) || 'pt'
  const sub = String(lang).slice(0, 2).toLowerCase()
  return /^[a-z]{2}$/.test(sub) ? sub : 'pt'
}

function stageFor(elapsedSec, webMode) {
  if (elapsedSec < 4 && webMode !== 'off') return '🌐 Searching the web…'
  if (elapsedSec < 25) return '✍️ Writing scenario…'
  return '🃏 Building story cards…'
}

const miniBtnCls =
  'shrink-0 rounded-md border border-zinc-700 px-2 py-0.5 text-[11px] text-zinc-400 hover:bg-zinc-800 hover:text-zinc-200 disabled:opacity-40'

export default function GenerateAdventureModal({ onClose }) {
  const toast = useToast()
  const navigate = useNavigate()
  const [brief, setBrief] = useState('')
  const [draft, setDraft] = useState(null)
  const [generating, setGenerating] = useState(false)
  const [stage, setStage] = useState('')
  const [regen, setRegen] = useState(null)
  const [creating, setCreating] = useState(false)

  const [genre, setGenre] = useState('')
  const [tone, setTone] = useState('')
  const [pov, setPov] = useState('second')
  const [cardCount, setCardCount] = useState('5')
  const [langChoice, setLangChoice] = useState('')
  const [imageStyle, setImageStyle] = useState('')
  const [protagonistName, setProtagonistName] = useState('')
  const [protagonistRole, setProtagonistRole] = useState('')
  const [protagonistAppearance, setProtagonistAppearance] = useState('')
  const [webSearchMode, setWebSearchMode] = useState('auto')
  const [webOnRefine, setWebOnRefine] = useState(false)

  const opRef = useRef(false)
  const timerRef = useRef(null)
  const language = langChoice || draftLanguage()

  useEffect(() => () => clearInterval(timerRef.current), [])

  const busy = generating || creating || regen !== null

  const noteWeb = (sources) => {
    if (sources?.length) toast(`Enriched with ${sources.length} web sources.`, 'ok')
  }

  // ---------------------------------------------------- passo 1: gerar draft
  const generate = async () => {
    const b = brief.trim()
    if (b.length < 3) {
      toast('Describe your idea first (min. 3 characters).')
      return
    }
    if (opRef.current) return
    opRef.current = true
    setGenerating(true)
    const started = Date.now()
    const webMode = webSearchMode
    const tick = () => setStage(stageFor((Date.now() - started) / 1000, webMode))
    tick()
    const timer = setInterval(tick, 1000)
    timerRef.current = timer
    try {
      setDraft(await api.assistantScenario({
        brief: b,
        language,
        genre: genre.trim() || undefined,
        tone: tone.trim() || undefined,
        pov,
        protagonist_name: protagonistName.trim() || undefined,
        protagonist_role: protagonistRole.trim() || undefined,
        protagonist_appearance: protagonistAppearance.trim() || undefined,
        image_style: imageStyle.trim() || undefined,
        card_count: Number(cardCount) || 5,
        web_search_mode: webMode,
      }))
    } catch (e) {
      toast(e.message)
    } finally {
      clearInterval(timer)
      timerRef.current = null
      opRef.current = false
      setGenerating(false)
      setStage('')
    }
  }

  // --------------------------------------------- passo 2: regenerar 1 campo
  const regenerate = async (field) => {
    if (!draft || opRef.current) return
    const typed = window.prompt('Refinement instruction (optional):')
    if (typed === null || opRef.current) return
    const instruction = typed.trim()
    opRef.current = true
    setRegen(field)
    try {
      const current =
        field === 'tags' ? (draft.tags ?? []).join(', ')
        : field === 'story_cards' ? undefined
        : draft[field]
      const r = await api.assistantField({
        field,
        brief: brief.trim(),
        current,
        draft,
        language,
        instruction: instruction || undefined,
        web_search: webOnRefine,
      })
      setDraft((d) => {
        const next = { ...d }
        if (field === 'story_cards') {
          if (Array.isArray(r.story_cards)) next.story_cards = r.story_cards
        } else if (field === 'tags') {
          next.tags = String(r.value ?? '').split(',').map((t) => t.trim()).filter(Boolean)
        } else {
          next[field] = r.value ?? ''
        }
        if (r?.web_sources?.length) next.web_sources = r.web_sources
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
    if (!draft || opRef.current) return
    const card = (draft.story_cards ?? [])[index]
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
        draft,
        current_card: card,
        instruction: typed.trim() || undefined,
        web_search: webOnRefine,
        language,
      })
      if (!r?.card) {
        toast('The assistant returned no card.')
        return
      }
      setDraft((d) => ({
        ...d,
        story_cards: (d.story_cards ?? []).map((c, j) => (j === index ? { ...c, ...r.card } : c)),
        ...(r.web_sources?.length ? { web_sources: r.web_sources } : {}),
      }))
      noteWeb(r.web_sources)
    } catch (e) {
      toast(e.message)
    } finally {
      opRef.current = false
      setRegen(null)
    }
  }

  const addCardWithAi = async () => {
    if (!draft || opRef.current) return
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
        draft,
        web_search: webOnRefine,
        language,
      })
      if (!r?.card) {
        toast('The assistant returned no card.')
        return
      }
      setDraft((d) => ({
        ...d,
        story_cards: [...(d.story_cards ?? []), r.card],
        ...(r.web_sources?.length ? { web_sources: r.web_sources } : {}),
      }))
      noteWeb(r.web_sources)
    } catch (e) {
      toast(e.message)
    } finally {
      opRef.current = false
      setRegen(null)
    }
  }

  // ------------------------------------------------------ persistir e jogar
  const create = async () => {
    if (!draft?.title?.trim()) {
      toast('Title is required.')
      return
    }
    if (opRef.current) return
    opRef.current = true
    setCreating(true)
    try {
      const scenario = await api.createScenario({
        title: draft.title.trim(),
        description: draft.description ?? '',
        ai_instructions: draft.ai_instructions ?? '',
        plot_essentials: draft.plot_essentials ?? '',
        authors_note: draft.authors_note ?? '',
        opening_text: draft.opening_text ?? '',
        image_style: draft.image_style ?? '',
        tags: draft.tags ?? [],
        story_cards: draft.story_cards ?? [],
        third_person: !!draft.third_person,
      })
      const adv = await api.createAdventure({ scenario_id: scenario.id })
      navigate(`/play/${adv.id}`)
    } catch (e) {
      toast(e.message)
      opRef.current = false
      setCreating(false)
    }
  }

  const inputCls =
    'w-full rounded-lg border border-zinc-700 bg-zinc-800 px-3 py-2 text-sm text-zinc-100 outline-none focus:border-emerald-600 disabled:opacity-60'
  const labelCls = 'text-xs font-semibold uppercase tracking-wide text-zinc-500'
  const hintCls = 'block text-xs text-zinc-600'

  const regenBtn = (field) => (
    <button
      type="button"
      onClick={() => regenerate(field)}
      disabled={busy}
      title="Regenerate this field keeping coherence with the rest of the draft"
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

  const paramLabel = (text) => <span className={`${labelCls} mb-1 block`}>{text}</span>

  return (
    <Modal title="Generate with AI" onClose={busy ? undefined : onClose} wide>
      {!draft ? (
        <div className="flex flex-col gap-4">
          <p className="text-sm text-zinc-400">
            Describe the adventure you want — in any language. The AI drafts a full scenario
            (title, plot, opening, story cards) in that language for you to review before creating.
          </p>
          <fieldset disabled={generating} className="m-0 flex min-w-0 flex-col gap-4 border-0 p-0">
            <textarea
              value={brief}
              onChange={(e) => setBrief(e.target.value)}
              rows={4}
              autoFocus
              placeholder='e.g. "A steampunk pirate hunting a sky leviathan across floating islands"'
              className={`${inputCls} resize-y`}
            />
            <div className="grid gap-3 sm:grid-cols-2">
              <label className="block">
                {paramLabel('Genre / Setting')}
                <input
                  value={genre}
                  onChange={(e) => setGenre(e.target.value)}
                  placeholder="e.g. dark fantasy, floating islands"
                  className={inputCls}
                />
              </label>
              <label className="block">
                {paramLabel('Tone & Pacing')}
                <input
                  value={tone}
                  onChange={(e) => setTone(e.target.value)}
                  placeholder="e.g. tense, fast, grim"
                  className={inputCls}
                />
              </label>
              <label className="block">
                {paramLabel('Narrative POV')}
                <select value={pov} onChange={(e) => setPov(e.target.value)} className={inputCls}>
                  <option value="second">Second person</option>
                  <option value="third">Third person</option>
                  <option value="first">First person</option>
                </select>
              </label>
              <label className="block">
                {paramLabel('Story cards')}
                <select value={cardCount} onChange={(e) => setCardCount(e.target.value)} className={inputCls}>
                  {[3, 4, 5, 6, 7, 8].map((n) => (
                    <option key={n} value={String(n)}>{n}</option>
                  ))}
                </select>
              </label>
              <label className="block">
                {paramLabel('Language')}
                <select value={langChoice} onChange={(e) => setLangChoice(e.target.value)} className={inputCls}>
                  <option value="">Auto (browser default)</option>
                  <option value="pt">Português</option>
                  <option value="en">English</option>
                  <option value="es">Español</option>
                </select>
              </label>
              <label className="block">
                {paramLabel('Visual style for images')}
                <input
                  value={imageStyle}
                  onChange={(e) => setImageStyle(e.target.value)}
                  placeholder="e.g. ink sketch, dramatic lighting"
                  className={inputCls}
                />
              </label>
              <label className="block sm:col-span-2">
                {paramLabel('Protagonist name')}
                <input
                  value={protagonistName}
                  onChange={(e) => setProtagonistName(e.target.value)}
                  placeholder="e.g. Alusair"
                  autoComplete="off"
                  className={inputCls}
                />
              </label>
              <label className="block">
                {paramLabel('Role')}
                <input
                  value={protagonistRole}
                  onChange={(e) => setProtagonistRole(e.target.value)}
                  placeholder="e.g. exiled knight"
                  className={inputCls}
                />
              </label>
              <label className="block">
                {paramLabel('Appearance')}
                <input
                  value={protagonistAppearance}
                  onChange={(e) => setProtagonistAppearance(e.target.value)}
                  placeholder="e.g. short black hair, plate armor"
                  className={inputCls}
                />
              </label>
              <label className="block">
                {paramLabel('Web search')}
                <select value={webSearchMode} onChange={(e) => setWebSearchMode(e.target.value)} className={inputCls}>
                  <option value="auto">Auto</option>
                  <option value="always">Always</option>
                  <option value="off">Off</option>
                </select>
              </label>
            </div>
          </fieldset>
          <div className="flex items-center justify-between gap-2">
            <span className="text-xs text-zinc-600">
              {generating
                ? `${stage || 'Generating…'} This often takes about a minute.`
                : 'Nothing is saved until you click "Create Adventure".'}
            </span>
            <div className="flex gap-2">
              <button
                onClick={onClose}
                disabled={generating}
                className="rounded-lg border border-zinc-700 px-4 py-2 text-sm text-zinc-300 hover:bg-zinc-800 disabled:opacity-50"
              >
                Cancel
              </button>
              <button
                onClick={generate}
                disabled={generating || brief.trim().length < 3}
                className="rounded-lg bg-emerald-700 px-4 py-2 text-sm font-semibold text-white hover:bg-emerald-600 disabled:opacity-50"
              >
                {generating ? 'Generating…' : 'Generate draft'}
              </button>
            </div>
          </div>
        </div>
      ) : (
        <fieldset disabled={busy} className="m-0 flex min-w-0 flex-col gap-4 border-0 p-0">
          {!!draft.web_sources?.length && (
            <div className="rounded-lg border border-zinc-800 bg-zinc-900/40 px-3 py-2">
              <div className="mb-1 text-xs font-semibold uppercase tracking-wide text-zinc-500">
                🌐 Sources consulted
              </div>
              <ul className="flex flex-col gap-1 text-sm">
                {draft.web_sources.map((s, i) => {
                  const safe = typeof s.url === 'string' && /^https?:\/\//i.test(s.url)
                  return (
                    <li key={`${s.url || ''}-${i}`}>
                      {safe ? (
                        <a
                          href={s.url}
                          target="_blank"
                          rel="noreferrer"
                          className="break-words text-emerald-400 hover:underline"
                        >
                          {s.title || s.url}
                        </a>
                      ) : (
                        <span className="break-words text-zinc-300">{s.title}</span>
                      )}
                    </li>
                  )
                })}
              </ul>
            </div>
          )}

          <label className="block">
            {fieldHeader('title', 'Title *')}
            <input
              value={draft.title ?? ''}
              onChange={(e) => setDraft({ ...draft, title: e.target.value })}
              className={inputCls}
            />
          </label>

          <label className="block">
            {fieldHeader('description', 'Description')}
            <textarea
              value={draft.description ?? ''}
              onChange={(e) => setDraft({ ...draft, description: e.target.value })}
              rows={2}
              className={`${inputCls} resize-y`}
            />
          </label>

          <label className="block">
            {fieldHeader('ai_instructions', 'AI Instructions', 'Style/system prompt for the narrator.')}
            <textarea
              value={draft.ai_instructions ?? ''}
              onChange={(e) => setDraft({ ...draft, ai_instructions: e.target.value })}
              rows={4}
              className={`${inputCls} resize-y`}
            />
          </label>

          <label className="block">
            {fieldHeader('plot_essentials', 'Plot Essentials (Memory)', 'Fixed block: who you are, the world, the rules.')}
            <textarea
              value={draft.plot_essentials ?? ''}
              onChange={(e) => setDraft({ ...draft, plot_essentials: e.target.value })}
              rows={4}
              className={`${inputCls} resize-y`}
            />
          </label>

          <label className="block">
            {fieldHeader('authors_note', "Author's Note", 'Tone note injected near the end of the context.')}
            <textarea
              value={draft.authors_note ?? ''}
              onChange={(e) => setDraft({ ...draft, authors_note: e.target.value })}
              rows={3}
              className={`${inputCls} resize-y`}
            />
          </label>

          <label className="block">
            {fieldHeader('opening_text', 'Opening Text', "The story's first block (becomes the start action).")}
            <textarea
              value={draft.opening_text ?? ''}
              onChange={(e) => setDraft({ ...draft, opening_text: e.target.value })}
              rows={6}
              className={`${inputCls} resize-y font-prose`}
            />
          </label>

          <label className="block">
            {fieldHeader('image_style', 'Visual Style (image generation)')}
            <input
              value={draft.image_style ?? ''}
              onChange={(e) => setDraft({ ...draft, image_style: e.target.value })}
              placeholder="e.g. dark cinematic photography, neon rain"
              className={inputCls}
            />
          </label>

          <div className="block">
            {fieldHeader('tags', 'Tags')}
            <TagInput
              value={draft.tags ?? []}
              onChange={(tags) => setDraft({ ...draft, tags })}
              disabled={busy}
              placeholder="e.g. fantasy"
            />
          </div>

          <div className="block">
            <div className="mb-1">
              <div className={labelCls}>Story Cards ({(draft.story_cards ?? []).length})</div>
              <div className={hintCls}>NPCs, places, factions — injected when a trigger word appears.</div>
            </div>
            <StoryCardsEditor
              cards={draft.story_cards ?? []}
              onChange={(story_cards) => setDraft((d) => ({ ...d, story_cards }))}
              busy={busy}
              onAiCard={enrichCard}
              webOn={webOnRefine}
              onToggleWeb={setWebOnRefine}
            >
              <button type="button" onClick={addCardWithAi} disabled={busy} className={miniBtnCls}>
                + Add story card with AI
              </button>
              {regenBtn('story_cards')}
            </StoryCardsEditor>
          </div>

          <div className="mt-2 flex items-center justify-between gap-2">
            <button
              type="button"
              onClick={() => setDraft(null)}
              className="text-sm text-zinc-500 hover:text-zinc-300"
            >
              ← Start over with a new idea
            </button>
            <button
              type="button"
              onClick={create}
              disabled={busy}
              className="rounded-lg bg-emerald-700 px-5 py-2 text-sm font-semibold text-white hover:bg-emerald-600 disabled:opacity-50"
            >
              {creating ? 'Creating…' : 'Create Adventure'}
            </button>
          </div>
        </fieldset>
      )}
    </Modal>
  )
}
