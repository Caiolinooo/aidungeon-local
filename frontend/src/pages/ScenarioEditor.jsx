import { useCallback, useEffect, useRef, useState } from 'react'
import { Link, useNavigate, useParams } from 'react-router-dom'
import { api } from '../api.js'
import { useToast } from '../components/Toast.jsx'
import TagInput from '../components/TagInput.jsx'
import StoryCardsEditor from '../components/StoryCardsEditor.jsx'
import { useConfirm } from '../confirm.jsx'

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
  const { ask, bar } = useConfirm()
  const [refineField, setRefineField] = useState(null)
  const [refineText, setRefineText] = useState('')

  const [form, setForm] = useState(EMPTY)
  const [loading, setLoading] = useState(isEdit)
  const [error, setError] = useState(null)
  const [pending, setPending] = useState(null)
  const [regen, setRegen] = useState(null)
  const [webOn, setWebOn] = useState(false)
  const [webSources, setWebSources] = useState([])
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

  const regenerateEverything = async () => {
    const brief = (form.description || form.title || '').trim()
    if (brief.length < 3) {
      toast('A descrição precisa de pelo menos 3 caracteres.')
      return
    }
    if (opRef.current) return
    opRef.current = true
    setPending('all')
    try {
      const count = Math.min(8, Math.max(3, (form.story_cards || []).length || 5))
      const draft = await api.assistantGenerate({
        brief,
        current: payload(),
        language: draftLanguage(),
        image_style: form.image_style || '',
        card_count: count,
        pov: form.third_person ? 'third' : 'second',
        deep_characters: true,
        web_search_mode: webOn ? 'always' : 'off',
      })
      setForm({
        title: draft.title ?? form.title,
        description: draft.description ?? '',
        ai_instructions: draft.ai_instructions ?? '',
        plot_essentials: draft.plot_essentials ?? '',
        authors_note: draft.authors_note ?? '',
        opening_text: draft.opening_text ?? '',
        image_style: draft.image_style ?? '',
        tags: draft.tags ?? [],
        story_cards: Array.isArray(draft.story_cards) ? draft.story_cards : [],
        third_person: !!draft.third_person || form.third_person,
      })
      toast('Scenario regenerated.', 'ok')
    } catch (e) {
      toast(e.message)
    } finally {
      opRef.current = false
      setPending(null)
    }
  }

  const noteWeb = (sources) => {
    setWebSources(sources || [])
    if (sources?.length) toast(`Enriquecido com ${sources.length} fontes da web.`, 'ok')
    else if (webOn) toast('A busca não encontrou fontes úteis; o conteúdo foi gerado sem elas.')
  }

  const refine = async (field, instruction = '') => {
    if (opRef.current) return
    opRef.current = true
    setRegen(field)
    setRefineField(null)
    try {
      const current =
        field === 'tags' ? (form.tags ?? []).join(', ')
        : field === 'story_cards' ? undefined
        : form[field]
      const r = await api.assistantField({
        field,
        current,
        draft: form,
        instruction: String(instruction || '').trim() || undefined,
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

  const enrichCard = async (index, instruction = '') => {
    if (opRef.current) return
    const card = (form.story_cards ?? [])[index]
    if (!card) return
    setRefineField(null)
    const concept = String(card.title ?? '').trim()
    if (concept.length < 2) {
      toast('O título da ficha precisa de pelo menos 2 caracteres antes de enriquecer.')
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
        instruction: String(instruction || '').trim() || undefined,
        web_search: webOn,
        language: draftLanguage(),
      })
      if (!r?.card) {
        toast('A IA não devolveu a ficha.')
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

  const addCardWithAi = async (conceptText = '') => {
    if (opRef.current) return
    setRefineField(null)
    const concept = String(conceptText || '').trim()
    if (concept.length < 2) {
      toast('Digite um nome ou conceito (mínimo de 2 caracteres).')
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
        toast('A IA não devolveu a ficha.')
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
      toast('O título é obrigatório.')
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
      toast('O título é obrigatório.')
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
    const title = form.title.trim() || 'este cenário'
    if (!ask('delete-scenario', `Apagar o cenário "${title}"? As aventuras que nasceram dele continuam.`)) return
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

  const openRefine = (field) => {
    setRefineField(field)
    setRefineText('')
  }

  const confirmRefine = () => {
    if (!refineField) return
    if (refineField === 'card-new') addCardWithAi(refineText)
    else if (String(refineField).startsWith('card-')) enrichCard(Number(String(refineField).slice(5)), refineText)
    else refine(refineField, refineText)
  }

  const regenBtn = (field) => (
    <button
      type="button"
      onClick={() => openRefine(field)}
      disabled={busy}
      title="Regerar este campo sem perder a coerência com o resto do cenário"
      className={miniBtnCls}
    >
      {regen === field ? '… gerando' : '↻ Regenerate'}
    </button>
  )

  const fieldHeader = (field, label, hint) => (
    <span className="mb-1 flex flex-col gap-1">
      <span className="flex items-center justify-between gap-2">
        <span>
          <span className={labelCls}>{label}</span>
          {hint && <span className={hintCls}>{hint}</span>}
        </span>
        {regenBtn(field)}
      </span>
      {refineField === field && (
        <span className="flex flex-col gap-1">
          <input
            value={refineText}
            onChange={(e) => setRefineText(e.target.value)}
            placeholder="Instrução de refinamento (opcional)"
            className={inputCls}
          />
          <span className="flex gap-2">
            <button type="button" onClick={confirmRefine} className={miniBtnCls}>Confirmar</button>
            <button type="button" onClick={() => setRefineField(null)} className={miniBtnCls}>Cancelar</button>
          </span>
        </span>
      )}
    </span>
  )

  if (loading) return <div className="flex h-full items-center justify-center text-zinc-500">Carregando…</div>

  if (error) {
    return (
      <div className="flex h-full flex-col items-center justify-center gap-4 p-8 text-center">
        <p className="text-red-300">
          {error.status === 0 ? 'Não foi possível falar com o servidor. Is it running?' : error.message}
        </p>
        <div className="flex gap-2">
          <button onClick={load} className="rounded-lg bg-emerald-700 px-4 py-2 text-sm font-semibold text-white hover:bg-emerald-600">
            ↻ Tentar de novo
          </button>
          <Link to="/" className="rounded-lg border border-zinc-700 px-4 py-2 text-sm text-zinc-300 hover:bg-zinc-800">
            ← Voltar ao início
          </Link>
        </div>
      </div>
    )
  }

  return (
    <div className="mx-auto h-full max-w-3xl overflow-y-auto px-4 py-8">
      {bar}
      <header className="mb-6">
        <Link to="/" className="text-xs text-zinc-500 hover:text-zinc-300">
          ← Home
        </Link>
        <div className="mt-1 flex items-center justify-between gap-3">
          <h1 className="text-2xl font-bold text-zinc-100">
            {isEdit ? (form.title || 'Editar cenário') : 'Novo cenário'}
          </h1>
          <button
            type="button"
            onClick={regenerateEverything}
            disabled={busy}
            className="shrink-0 rounded-md border border-zinc-700 px-3 py-1.5 text-sm text-zinc-300 hover:bg-zinc-800 disabled:opacity-40"
          >
            {pending === 'all' ? '… gerando' : '🎲 Regenerate everything'}
          </button>
        </div>
      </header>

      <fieldset disabled={busy} className="m-0 flex min-w-0 flex-col gap-4 border-0 p-0">
        <label className="block">
          {fieldHeader('title', 'Título *')}
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
          {fieldHeader('ai_instructions', 'AI Instructions', 'Instruções de estilo para o narrador (topo do contexto).')}
          <textarea
            value={form.ai_instructions}
            onChange={(e) => setForm((f) => ({ ...f, ai_instructions: e.target.value }))}
            rows={4}
            className={`${inputCls} resize-y`}
          />
        </label>

        <label className="block">
          {fieldHeader('plot_essentials', 'Essenciais da trama (Memória)', 'Bloco fixo: quem você é, o mundo, as regras.')}
          <textarea
            value={form.plot_essentials}
            onChange={(e) => setForm((f) => ({ ...f, plot_essentials: e.target.value }))}
            rows={4}
            className={`${inputCls} resize-y`}
          />
        </label>

        <label className="block">
          {fieldHeader('authors_note', "Author's Note", 'Nota de tom injetada perto do fim do contexto.')}
          <textarea
            value={form.authors_note}
            onChange={(e) => setForm((f) => ({ ...f, authors_note: e.target.value }))}
            rows={3}
            className={`${inputCls} resize-y`}
          />
        </label>

        <label className="block">
          {fieldHeader('opening_text', 'Texto de abertura', "O primeiro bloco da história (vira a ação inicial das aventuras novas).")}
          <textarea
            value={form.opening_text}
            onChange={(e) => setForm((f) => ({ ...f, opening_text: e.target.value }))}
            rows={6}
            className={`${inputCls} resize-y font-prose`}
          />
        </label>

        <label className="block">
          {fieldHeader('image_style', 'Estilo visual (geração de imagem)')}
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
            <div className={labelCls}>Fichas ({(form.story_cards ?? []).length})</div>
            <div className={hintCls}>NPCs, lugares, facções — entram no contexto quando uma palavra-gatilho aparece.</div>
          </div>
          {String(refineField || '').startsWith('card-') && (
            <div className="mb-2 flex flex-col gap-1">
              <input
                value={refineText}
                onChange={(e) => setRefineText(e.target.value)}
                placeholder={refineField === 'card-new' ? 'Nome ou conceito da ficha' : 'Instrução para esta ficha (opcional)'}
                className={inputCls}
              />
              <div className="flex gap-2">
                <button type="button" onClick={confirmRefine} className={miniBtnCls}>Confirmar</button>
                <button type="button" onClick={() => setRefineField(null)} className={miniBtnCls}>Cancelar</button>
              </div>
            </div>
          )}
          <StoryCardsEditor
            cards={form.story_cards}
            onChange={(story_cards) => setForm((f) => ({ ...f, story_cards }))}
            busy={busy}
            onAiCard={(index) => openRefine(`card-${index}`)}
            webOn={webOn}
            onToggleWeb={setWebOn}
          >
            <button type="button" onClick={() => openRefine('card-new')} disabled={busy} className={miniBtnCls}>
              + Ficha com IA
            </button>
            {regenBtn('story_cards')}
          </StoryCardsEditor>
          {webSources.length > 0 && (
            <div className="mt-2 rounded-lg border border-zinc-800 bg-zinc-800/40 p-3">
              <p className="mb-2 text-xs font-semibold uppercase tracking-wide text-zinc-400">
                Fontes da pesquisa
              </p>
              <ul className="space-y-1 text-xs text-zinc-300">
                {webSources.map((source, index) => (
                  <li key={`${source.url}-${index}`}>
                    <a href={source.url} target="_blank" rel="noreferrer" className="text-emerald-400 underline">
                      {source.title}
                    </a>
                    {source.snippet && <span className="block text-zinc-500">{source.snippet}</span>}
                  </li>
                ))}
              </ul>
            </div>
          )}
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
            {pending === 'delete' ? 'Apagando…' : 'Delete'}
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
            {pending === 'save' ? 'Salvando…' : 'Salvar'}
          </button>
        </div>
      ) : (
        <div className="mt-6 flex justify-end gap-2">
          <Link to="/" className="rounded-lg border border-zinc-700 px-4 py-2 text-sm text-zinc-300 hover:bg-zinc-800">
            Cancelar
          </Link>
          <button
            onClick={save}
            disabled={busy}
            className="rounded-lg bg-emerald-700 px-5 py-2 text-sm font-semibold text-white hover:bg-emerald-600 disabled:opacity-50"
          >
            {pending === 'save' ? 'Criando…' : 'Criar cenário'}
          </button>
        </div>
      )}
    </div>
  )
}
