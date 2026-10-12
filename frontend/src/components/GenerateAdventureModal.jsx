import { useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { api } from '../api.js'
import { useToast } from './Toast.jsx'
import Modal from './Modal.jsx'
import TagInput from './TagInput.jsx'

// Modal "Generate with AI" (v2): cria uma aventura a partir de uma ideia.
//  Passo 1 — textarea com a ideia (qualquer idioma) → POST /api/assistant/scenario
//  Passo 2 — revisão do ScenarioDraft: TODOS os campos editáveis, cada um com
//            botão "Regenerate" (POST /api/assistant/field com o rascunho atual
//            p/ coerência cruzada; story_cards regenera a lista inteira).
//  "Create Adventure" persiste: POST /api/scenarios (com story_cards) e depois
//  POST /api/adventures {scenario_id} → navega p/ /play/:id.
// A geração via LLM pode levar ~30s — todos os estados de loading são visíveis.
// language segue o browser (pt → textos em português).

function draftLanguage() {
  const lang = (typeof navigator !== 'undefined' && navigator.language) || 'pt'
  const sub = String(lang).slice(0, 2).toLowerCase()
  return /^[a-z]{2}$/.test(sub) ? sub : 'pt'
}

export default function GenerateAdventureModal({ onClose }) {
  const toast = useToast()
  const navigate = useNavigate()
  const [brief, setBrief] = useState('')
  const [draft, setDraft] = useState(null) // ScenarioDraft (passo 2)
  const [generating, setGenerating] = useState(false)
  const [regen, setRegen] = useState(null) // campo em regeneração
  const [creating, setCreating] = useState(false)

  const busy = generating || creating || regen !== null

  // ---------------------------------------------------- passo 1: gerar draft
  const generate = async () => {
    const b = brief.trim()
    if (b.length < 3) {
      toast('Describe your idea first (min. 3 characters).')
      return
    }
    setGenerating(true)
    try {
      setDraft(await api.assistantScenario({ brief: b, language: draftLanguage() }))
    } catch (e) {
      toast(e.message)
    } finally {
      setGenerating(false)
    }
  }

  // --------------------------------------------- passo 2: regenerar 1 campo
  const regenerate = async (field) => {
    if (!draft || regen) return
    setRegen(field)
    try {
      const current =
        field === 'tags' ? (draft.tags ?? []).join(', ')
        : field === 'story_cards' ? undefined
        : draft[field]
      const r = await api.assistantField({
        field, brief: brief.trim(), current, draft, language: draftLanguage(),
      })
      if (field === 'story_cards') {
        // regenerar cards SUBSTITUI a lista inteira (decisão do frontend)
        if (Array.isArray(r.story_cards)) setDraft((d) => ({ ...d, story_cards: r.story_cards }))
      } else if (field === 'tags') {
        // contrato: tags voltam como string separada por vírgula
        const tags = String(r.value ?? '').split(',').map((t) => t.trim()).filter(Boolean)
        setDraft((d) => ({ ...d, tags }))
      } else {
        setDraft((d) => ({ ...d, [field]: r.value ?? '' }))
      }
    } catch (e) {
      toast(e.message)
    } finally {
      setRegen(null)
    }
  }

  // ------------------------------------------------------ persistir e jogar
  const create = async () => {
    if (!draft?.title?.trim()) {
      toast('Title is required.')
      return
    }
    setCreating(true)
    try {
      const scenario = await api.createScenario({
        title: draft.title.trim(),
        description: draft.description ?? '',
        ai_instructions: draft.ai_instructions ?? '',
        plot_essentials: draft.plot_essentials ?? '',
        authors_note: draft.authors_note ?? '',
        opening_text: draft.opening_text ?? '',
        tags: draft.tags ?? [],
        story_cards: draft.story_cards ?? [],
      })
      const adv = await api.createAdventure({ scenario_id: scenario.id })
      navigate(`/play/${adv.id}`)
    } catch (e) {
      toast(e.message)
      setCreating(false)
    }
  }

  const inputCls =
    'w-full rounded-lg border border-zinc-700 bg-zinc-800 px-3 py-2 text-sm text-zinc-100 outline-none focus:border-emerald-600 disabled:opacity-60'
  const labelCls = 'text-xs font-semibold uppercase tracking-wide text-zinc-500'
  const hintCls = 'block text-xs text-zinc-600'

  // Botãozinho "↻ Regenerate" ao lado do label de cada campo.
  const regenBtn = (field) => (
    <button
      type="button"
      onClick={() => regenerate(field)}
      disabled={busy}
      title="Regenerate this field keeping coherence with the rest of the draft"
      className="shrink-0 rounded-md border border-zinc-700 px-2 py-0.5 text-[11px] text-zinc-400 hover:bg-zinc-800 hover:text-zinc-200 disabled:opacity-40"
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

  return (
    <Modal title="Generate with AI" onClose={busy ? undefined : onClose} wide>
      {!draft ? (
        // -------------------------------------------------- passo 1: a ideia
        <div className="flex flex-col gap-4">
          <p className="text-sm text-zinc-400">
            Describe the adventure you want — in any language. The AI drafts a full scenario
            (title, plot, opening, story cards) in that language for you to review before creating.
          </p>
          <textarea
            value={brief}
            onChange={(e) => setBrief(e.target.value)}
            rows={4}
            autoFocus
            disabled={generating}
            placeholder='e.g. "A steampunk pirate hunting a sky leviathan across floating islands"'
            className={`${inputCls} resize-y`}
          />
          <div className="flex items-center justify-between gap-2">
            <span className="text-xs text-zinc-600">
              {generating ? 'Generating… the LLM can take up to ~30s.' : 'Nothing is saved until you click "Create Adventure".'}
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
        // --------------------------------------------- passo 2: revisar draft
        <fieldset disabled={busy} className="flex flex-col gap-4">
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
            {fieldHeader('story_cards', `Story Cards (${(draft.story_cards ?? []).length})`, 'NPCs, places, factions — injected when a trigger word appears.')}
            <div className="flex flex-col gap-2">
              {(draft.story_cards ?? []).map((card, i) => (
                <div key={i} className="rounded-lg border border-zinc-800 bg-zinc-800/40 p-3">
                  <div className="flex gap-2">
                    <input
                      value={card.title ?? ''}
                      onChange={(e) => updateCard(setDraft, i, { title: e.target.value })}
                      placeholder="Title"
                      className={`${inputCls} flex-1`}
                    />
                    <input
                      value={card.type ?? ''}
                      onChange={(e) => updateCard(setDraft, i, { type: e.target.value })}
                      placeholder="Type (character, location…)"
                      className={`${inputCls} w-44`}
                    />
                    <button
                      type="button"
                      onClick={() => setDraft((d) => ({ ...d, story_cards: d.story_cards.filter((_, j) => j !== i) }))}
                      title="Remove card"
                      className="shrink-0 rounded-lg border border-red-900 px-3 text-xs text-red-400 hover:bg-red-950"
                    >
                      Remove
                    </button>
                  </div>
                  <div className="mt-2">
                    <TagInput
                      value={card.triggers ?? []}
                      onChange={(triggers) => updateCard(setDraft, i, { triggers })}
                      disabled={busy}
                      placeholder="Trigger words (Enter to add)"
                    />
                  </div>
                  <textarea
                    value={card.entry ?? ''}
                    onChange={(e) => updateCard(setDraft, i, { entry: e.target.value })}
                    rows={3}
                    placeholder="Card text injected into the AI context"
                    className={`${inputCls} mt-2 resize-y`}
                  />
                </div>
              ))}
              <button
                type="button"
                onClick={() => setDraft((d) => ({ ...d, story_cards: [...(d.story_cards ?? []), { title: '', type: '', triggers: [], entry: '' }] }))}
                className="rounded-lg border border-dashed border-zinc-700 py-2 text-sm text-zinc-400 hover:border-zinc-500 hover:text-zinc-200"
              >
                + Add story card
              </button>
            </div>
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

// Atualiza campos de um story card do draft (imutável, por índice).
function updateCard(setDraft, index, patch) {
  setDraft((d) => ({
    ...d,
    story_cards: d.story_cards.map((c, j) => (j === index ? { ...c, ...patch } : c)),
  }))
}
