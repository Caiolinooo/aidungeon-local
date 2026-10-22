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

function characterTitle(name, description) {
  const n = (name || '').trim()
  if (n) return n.slice(0, 80)
  const first = (description || '').split('\n')[0].trim()
  if (first && first.length <= 60 && !/[,.;:!?]/.test(first)) return first.slice(0, 80)
  return 'Protagonista'
}

// config/settings.json → scheduler.timeouts.assistant
const ASSISTANT_TIMEOUT_S = 180
const CLIENT_DEADLINE_CAP_S = 240

function clientDeadlineMs(assistantTimeoutS = ASSISTANT_TIMEOUT_S) {
  const seconds = Number(assistantTimeoutS)
  const base = Number.isFinite(seconds) && seconds > 0 ? seconds : ASSISTANT_TIMEOUT_S
  return Math.min(base * 1.2, CLIENT_DEADLINE_CAP_S) * 1000
}

function explainError(err) {
  if (err?.name === 'AbortError') return 'Tempo limite da criação. O rascunho foi mantido.'
  const code = err?.code
  const hint = typeof err?.hint === 'string' ? err.hint.trim() : ''
  let message
  if (code === 'generation_failed') {
    message = 'A IA devolveu uma resposta que não deu para interpretar. O rascunho foi mantido — tente gerar de novo.'
  } else if (code === 'validation_error') {
    message = err.message
  } else if (code === 'llm_timeout') {
    message = 'O modelo de IA não respondeu a tempo.'
  } else if (code === 'llm_degraded') {
    message = 'Servidor de IA instável; tente de novo em 1 min.'
  } else if (code === 'unreachable') {
    message = 'Servidor de IA indisponível.'
  } else if (code === 'auth_error') {
    message = 'Credencial do modelo inválida.'
  } else if (code === 'config_error') {
    message = hint || err?.message || 'Configuração do modelo inválida.'
  } else if (code === 'error') {
    message = 'Falha no modelo de IA.'
  } else if (code === 'timeout' || code === 'network_error' || err?.status === 0) {
    message = 'O servidor de IA não respondeu a tempo. O rascunho foi mantido — tente de novo.'
  } else {
    message = err?.message || 'Algo deu errado.'
  }
  if (hint && hint !== message) return `${message}\n${hint}`
  return message
}

function stageFor(elapsedSec, webMode, deepCharacters) {
  if (elapsedSec < 4 && webMode !== 'off') return '🌐 Buscando na web…'
  if (elapsedSec < 25 || !deepCharacters) return '✍️ Escrevendo o cenário…'
  return '🃏 Montando as fichas…'
}

const miniBtnCls =
  'shrink-0 rounded-md border border-subtle px-2 py-0.5 text-[11px] text-secondary hover:bg-surface hover:text-primary disabled:opacity-40'

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
  const [heroMode, setHeroMode] = useState('describe')
  const [heroStep, setHeroStep] = useState(0)
  const [heroText, setHeroText] = useState('')
  const [heroGoal, setHeroGoal] = useState('')
  const [webSearchMode, setWebSearchMode] = useState('auto')
  const [webOnRefine, setWebOnRefine] = useState(false)
  const [deepCharacters, setDeepCharacters] = useState(true)
  const [rulesets, setRulesets] = useState([])
  const [rulesetId, setRulesetId] = useState('')
  const [rulesPreview, setRulesPreview] = useState(false)
  const [panelError, setPanelError] = useState(null)
  const [refineField, setRefineField] = useState(null)
  const [refineText, setRefineText] = useState('')

  const opRef = useRef(false)
  const timerRef = useRef(null)
  const abortRef = useRef(null)
  const abortReasonRef = useRef(null)
  const mountedRef = useRef(true)
  const language = langChoice || draftLanguage()

  useEffect(() => () => {
    mountedRef.current = false
    abortReasonRef.current = abortReasonRef.current || 'unmount'
    clearInterval(timerRef.current)
    abortRef.current?.abort()
  }, [])

  const closeModal = () => {
    abortReasonRef.current = 'close'
    abortRef.current?.abort()
    onClose?.()
  }

  useEffect(() => {
    let cancelled = false
    api.listRulesets()
      .then((rows) => { if (!cancelled) setRulesets(rows) })
      .catch((e) => { if (!cancelled) toast(e.message) })
    return () => { cancelled = true }
  }, [toast])

  const busy = generating || creating || regen !== null

  const noteWeb = (sources) => {
    if (sources?.length) toast(`Enriquecido com ${sources.length} fontes da web.`, 'ok')
  }

  // ---------------------------------------------------- passo 1: gerar draft
  const generate = async () => {
    const b = brief.trim()
    if (b.length < 3) {
      toast('Descreva a ideia primeiro (mínimo de 3 caracteres).')
      return
    }
    if (opRef.current) return
    opRef.current = true
    setGenerating(true)
    const started = Date.now()
    const webMode = webSearchMode
    const deep = deepCharacters
    const tick = () => setStage(stageFor((Date.now() - started) / 1000, webMode, deep))
    tick()
    const timer = setInterval(tick, 1000)
    timerRef.current = timer
    abortReasonRef.current = null
    const ctrl = new AbortController()
    abortRef.current = ctrl
    const deadline = window.setTimeout(() => {
      abortReasonRef.current = 'timeout'
      ctrl.abort()
    }, clientDeadlineMs())
    try {
      const pickedRules = rulesets.find((r) => String(r.id) === String(rulesetId))
      const described = heroMode === 'describe'
        ? heroText.trim()
        : (heroGoal.trim() ? `O que quer: ${heroGoal.trim()}` : '')
      const payload = {
        brief: b,
        language,
        genre: genre.trim() || undefined,
        tone: tone.trim() || undefined,
        pov,
        protagonist_name: protagonistName.trim() || undefined,
        protagonist_role: protagonistRole.trim() || undefined,
        protagonist_appearance: protagonistAppearance.trim() || undefined,
        protagonist_description: described || undefined,
        ruleset_name: pickedRules?.name || undefined,
        ruleset_rules: pickedRules ? String(pickedRules.rules || '').slice(0, 900) : undefined,
        image_style: imageStyle.trim() || undefined,
        card_count: Number(cardCount) || 5,
        web_search_mode: webMode,
      }
      const ask = (count) => {
        const withCount = { ...payload, card_count: count }
        const signal = ctrl.signal
        return deepCharacters
          ? api.assistantGenerate({
            ...withCount,
            genre: genre.trim(),
            tone: tone.trim(),
            protagonist_name: protagonistName.trim(),
            protagonist_role: protagonistRole.trim(),
            protagonist_appearance: protagonistAppearance.trim(),
            protagonist_description: described,
            ruleset_name: pickedRules?.name || '',
            ruleset_rules: pickedRules ? String(pickedRules.rules || '').slice(0, 900) : '',
            image_style: imageStyle.trim(),
            deep_characters: true,
          }, { signal })
          : api.assistantScenario({ ...withCount, signal })
      }
      const count = Number(cardCount) || 5
      try {
        setDraft(await ask(count))
      } catch (err) {
        if (count > 3 && err?.code === 'generation_failed') setDraft(await ask(3))
        else throw err
      }
      setPanelError(null)
    } catch (e) {
      if (e?.name === 'AbortError') {
        if (abortReasonRef.current === 'timeout' && mountedRef.current) {
          const message = 'Tempo limite da criação. O rascunho foi mantido.'
          toast(message)
          setPanelError({ message, retry: () => generate() })
        }
      } else {
        abortReasonRef.current = 'failure'
        ctrl.abort()
        if (mountedRef.current) {
          setPanelError({ message: explainError(e), retry: () => generate() })
        }
      }
    } finally {
      clearTimeout(deadline)
      clearInterval(timer)
      timerRef.current = null
      if (abortRef.current === ctrl) abortRef.current = null
      opRef.current = false
      if (mountedRef.current) {
        setGenerating(false)
        setStage('')
      }
    }
  }

  // --------------------------------------------- passo 2: regenerar 1 campo
  const openRefine = (field) => {
    setRefineField(field)
    setRefineText('')
  }

  const regenerate = async (field, instruction = '') => {
    if (!draft || opRef.current) return
    opRef.current = true
    setRegen(field)
    setRefineField(null)
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
        instruction: String(instruction || '').trim() || undefined,
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
      setPanelError({ message: explainError(e), retry: () => regenerate(field, instruction) })
    } finally {
      opRef.current = false
      setRegen(null)
    }
  }

  const enrichCard = async (index, instruction = '') => {
    if (!draft || opRef.current) return
    const card = (draft.story_cards ?? [])[index]
    if (!card) return
    const concept = String(card.title ?? '').trim()
    if (concept.length < 2) {
      setPanelError({ message: 'O título da ficha precisa de pelo menos 2 caracteres antes de refinar.', retry: null })
      return
    }
    opRef.current = true
    setRegen(`card-${index}`)
    setRefineField(null)
    try {
      const r = await api.assistantCard({
        concept,
        card_type: card.type || 'character',
        draft,
        current_card: card,
        instruction: String(instruction || '').trim() || undefined,
        web_search: webOnRefine,
        language,
      })
      if (!r?.card) {
        toast('A IA não devolveu a ficha.')
        return
      }
      setDraft((d) => ({
        ...d,
        story_cards: (d.story_cards ?? []).map((c, j) => (j === index ? { ...c, ...r.card } : c)),
        ...(r.web_sources?.length ? { web_sources: r.web_sources } : {}),
      }))
      noteWeb(r.web_sources)
    } catch (e) {
      setPanelError({ message: explainError(e), retry: () => enrichCard(index, instruction) })
    } finally {
      opRef.current = false
      setRegen(null)
    }
  }

  const addCardWithAi = async (conceptText = '') => {
    if (!draft || opRef.current) return
    const concept = String(conceptText || '').trim()
    if (concept.length < 2) {
      setPanelError({ message: 'Escreva um nome ou conceito para a ficha (mínimo de 2 caracteres).', retry: null })
      return
    }
    opRef.current = true
    setRegen('card-new')
    setRefineField(null)
    try {
      const r = await api.assistantCard({
        concept,
        card_type: 'character',
        draft,
        web_search: webOnRefine,
        language,
      })
      if (!r?.card) {
        toast('A IA não devolveu a ficha.')
        return
      }
      setDraft((d) => ({
        ...d,
        story_cards: [...(d.story_cards ?? []), r.card],
        ...(r.web_sources?.length ? { web_sources: r.web_sources } : {}),
      }))
      noteWeb(r.web_sources)
    } catch (e) {
      setPanelError({ message: explainError(e), retry: () => addCardWithAi(concept) })
    } finally {
      opRef.current = false
      setRegen(null)
    }
  }

  const confirmRefine = () => {
    if (!refineField) return
    if (refineField === 'card-new') {
      addCardWithAi(refineText)
      return
    }
    if (refineField.startsWith('card-')) {
      enrichCard(Number(refineField.slice(5)), refineText)
      return
    }
    regenerate(refineField, refineText)
  }

  // ------------------------------------------------------ persistir e jogar
  const create = async () => {
    if (!draft?.title?.trim()) {
      setPanelError({ message: 'O título é obrigatório para criar a aventura.', retry: null })
      return
    }
    if (opRef.current) return
    opRef.current = true
    setCreating(true)
    setPanelError(null)
    try {
      const picked = rulesets.find((r) => String(r.id) === String(rulesetId))
      const described = heroMode === 'describe' ? heroText.trim() : heroGoal.trim()
      const title = characterTitle(protagonistName, heroMode === 'describe' ? heroText : '')
      const adv = await api.createAdventureFromDraft({
        draft: {
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
        },
        character: {
          title,
          sheet: {
            appearance: protagonistAppearance.trim(),
            class: protagonistRole.trim(),
            notes: described,
          },
        },
        ruleset_id: picked ? String(picked.id) : null,
      })
      navigate(`/play/${adv.id}`)
    } catch (e) {
      setPanelError({ message: explainError(e), retry: () => create() })
      opRef.current = false
      setCreating(false)
    }
  }

  const inputCls =
    'w-full rounded-lg border border-subtle bg-surface px-3 py-2 text-sm text-primary outline-none focus:border-accent-pri disabled:opacity-60'
  const labelCls = 'text-xs font-semibold uppercase tracking-wide text-muted'
  const hintCls = 'block text-xs text-muted'

  const refineBox = (field, placeholder) => (
    refineField === field ? (
      <span className="mt-1 flex flex-col gap-1">
        <input
          value={refineText}
          onChange={(e) => setRefineText(e.target.value)}
          placeholder={placeholder}
          className={inputCls}
        />
        <span className="flex gap-2">
          <button type="button" onClick={confirmRefine} disabled={busy} className={miniBtnCls}>
            Confirmar
          </button>
          <button type="button" onClick={() => setRefineField(null)} className={miniBtnCls}>
            Cancelar
          </button>
        </span>
      </span>
    ) : null
  )

  const regenBtn = (field) => (
    <button
      type="button"
      onClick={() => openRefine(field)}
      disabled={busy}
      title="Refinar este campo sem perder o resto do rascunho"
      className={miniBtnCls}
    >
      {regen === field ? '… gerando' : '↻ Refinar'}
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
      {refineBox(field, 'Instrução de refinamento (opcional)')}
    </span>
  )

  const paramLabel = (text) => <span className={`${labelCls} mb-1 block`}>{text}</span>
  const selectedRuleset = rulesets.find((r) => String(r.id) === String(rulesetId)) || null
  const selectedRules = String(selectedRuleset?.rules || '').slice(0, 4000)

  return (
    <Modal title="Gerar com IA" onClose={closeModal} wide>
      {panelError && (
        <div className="mb-3 rounded-lg border border-danger-800 bg-danger-950/60 p-3 text-sm">
          <p className="whitespace-pre-line">{panelError.message}</p>
          {panelError.retry && (
            <button
              type="button"
              onClick={() => {
                const go = panelError.retry
                setPanelError(null)
                go()
              }}
              className="mt-2 text-sm font-semibold underline"
            >
              Tentar de novo
            </button>
          )}
        </div>
      )}
      <div className="mb-4">
        <label className="block">
          {paramLabel('Sistema de regras (opcional)')}
          <select
            value={rulesetId}
            disabled={busy}
            onChange={(e) => {
              setRulesetId(e.target.value)
              setRulesPreview(false)
            }}
            className={inputCls}
          >
            <option value="">Nenhum</option>
            {rulesets.map((r) => (
              <option key={r.id} value={String(r.id)}>
                {r.name}{r.builtin ? ' (padrão)' : ''}
              </option>
            ))}
          </select>
        </label>
        {selectedRuleset && (
          <div className="mt-2">
            <button
              type="button"
              onClick={() => setRulesPreview((open) => !open)}
              disabled={busy}
              className="text-xs text-accent-pri hover:underline disabled:opacity-50"
            >
              {rulesPreview ? 'Ocultar prévia' : 'Ver prévia'}
            </button>
            {rulesPreview && (
              <pre className="mt-2 max-h-48 overflow-auto whitespace-pre-wrap rounded-lg border border-subtle bg-canvas/60 p-3 text-xs text-secondary">
                {selectedRules || '—'}
              </pre>
            )}
            <p className="mt-1 text-xs text-muted">
              Ao criar, estas regras substituem character_rules ({selectedRules.length}/4000).
            </p>
          </div>
        )}
      </div>
      {!draft ? (
        <div className="flex flex-col gap-4">
          <p className="text-sm text-secondary">
            Descreva a aventura em qualquer língua. O rascunho (título, abertura e cards)
            sai nessa língua para você revisar antes de criar. O ruleset e o personagem
            abaixo entram na história e viram a ficha de quem joga.
          </p>
          <fieldset disabled={generating} className="m-0 flex min-w-0 flex-col gap-4 border-0 p-0">
            <label className="block">
              {paramLabel('A ideia')}
              <textarea
                value={brief}
                onChange={(e) => setBrief(e.target.value)}
                rows={4}
                autoFocus
                placeholder="Ex.: um pirata a vapor caçando um leviatã do céu entre ilhas flutuantes"
                className={`${inputCls} resize-y`}
              />
            </label>
            <section className="rounded-xl border border-subtle bg-elevated/40 p-3">
              <div className="mb-3 flex flex-wrap items-center justify-between gap-2">
                <span className={labelCls}>Seu personagem</span>
                <div className="flex rounded-lg border border-subtle p-0.5 text-xs">
                  <button
                    type="button"
                    disabled={generating}
                    onClick={() => setHeroMode('describe')}
                    className={`rounded-md px-2.5 py-1 ${heroMode === 'describe' ? 'bg-accent-pri text-canvas' : 'text-secondary hover:text-primary'}`}
                  >
                    Descrever
                  </button>
                  <button
                    type="button"
                    disabled={generating}
                    onClick={() => setHeroMode('steps')}
                    className={`rounded-md px-2.5 py-1 ${heroMode === 'steps' ? 'bg-accent-pri text-canvas' : 'text-secondary hover:text-primary'}`}
                  >
                    Passo a passo
                  </button>
                </div>
              </div>
              {heroMode === 'describe' ? (
                <div className="flex flex-col gap-3">
                  <label className="block">
                    {paramLabel('Nome')}
                    <input
                      value={protagonistName}
                      onChange={(e) => setProtagonistName(e.target.value)}
                      placeholder="Ex.: Alusair. Pode ficar no texto, se preferir."
                      autoComplete="off"
                      className={inputCls}
                    />
                  </label>
                  <label className="block">
                    {paramLabel('Quem você é')}
                    <textarea
                      value={heroText}
                      onChange={(e) => setHeroText(e.target.value)}
                      rows={4}
                      placeholder="De onde veio, o que faz, como se parece e o que procura. A aventura nasce em volta disso."
                      className={`${inputCls} resize-y`}
                    />
                  </label>
                </div>
              ) : (
                <div className="flex flex-col gap-3">
                  <p className="text-xs text-muted">Pergunta {heroStep + 1} de 4. Pode pular.</p>
                  {heroStep === 0 && (
                    <label className="block">
                      {paramLabel('Como você se chama?')}
                      <input
                        value={protagonistName}
                        onChange={(e) => setProtagonistName(e.target.value)}
                        placeholder="Ex.: Alusair"
                        autoComplete="off"
                        className={inputCls}
                      />
                    </label>
                  )}
                  {heroStep === 1 && (
                    <label className="block">
                      {paramLabel('Qual é o seu papel?')}
                      <input
                        value={protagonistRole}
                        onChange={(e) => setProtagonistRole(e.target.value)}
                        placeholder="Ex.: cavaleiro exilado"
                        className={inputCls}
                      />
                    </label>
                  )}
                  {heroStep === 2 && (
                    <label className="block">
                      {paramLabel('Como você se parece?')}
                      <input
                        value={protagonistAppearance}
                        onChange={(e) => setProtagonistAppearance(e.target.value)}
                        placeholder="Ex.: cabelo preto curto, armadura de placas"
                        className={inputCls}
                      />
                    </label>
                  )}
                  {heroStep === 3 && (
                    <label className="block">
                      {paramLabel('O que você quer nesta história?')}
                      <textarea
                        value={heroGoal}
                        onChange={(e) => setHeroGoal(e.target.value)}
                        rows={3}
                        placeholder="Ex.: recuperar o selo do rei antes que a frota parta"
                        className={`${inputCls} resize-y`}
                      />
                    </label>
                  )}
                  <div className="flex justify-between">
                    <button
                      type="button"
                      disabled={heroStep === 0}
                      onClick={() => setHeroStep((step) => Math.max(0, step - 1))}
                      className={miniBtnCls}
                    >
                      Voltar
                    </button>
                    <button
                      type="button"
                      disabled={heroStep === 3}
                      onClick={() => setHeroStep((step) => Math.min(3, step + 1))}
                      className={miniBtnCls}
                    >
                      Próxima
                    </button>
                  </div>
                </div>
              )}
              <p className="mt-2 text-xs text-muted">
                Em branco, a ficha fica para o jogo. Com nome, ela já nasce salva.
              </p>
            </section>
            <div className="grid gap-3 sm:grid-cols-2">
              <label className="block">
                {paramLabel('Gênero / cenário')}
                <input
                  value={genre}
                  onChange={(e) => setGenre(e.target.value)}
                  placeholder="Ex.: fantasia sombria, ilhas flutuantes"
                  className={inputCls}
                />
              </label>
              <label className="block">
                {paramLabel('Tom e ritmo')}
                <input
                  value={tone}
                  onChange={(e) => setTone(e.target.value)}
                  placeholder="Ex.: tenso, rápido, sombrio"
                  className={inputCls}
                />
              </label>
              <label className="block">
                {paramLabel('Ponto de vista')}
                <select value={pov} onChange={(e) => setPov(e.target.value)} className={inputCls}>
                  <option value="second">Segunda pessoa (você)</option>
                  <option value="third">Terceira pessoa</option>
                  <option value="first">Primeira pessoa (eu)</option>
                </select>
              </label>
              <label className="block">
                {paramLabel('Cards da história')}
                <select value={cardCount} onChange={(e) => setCardCount(e.target.value)} className={inputCls}>
                  {[3, 4, 5, 6, 7, 8].map((n) => (
                    <option key={n} value={String(n)}>{n}</option>
                  ))}
                </select>
              </label>
              <label className="block">
                {paramLabel('Língua')}
                <select value={langChoice} onChange={(e) => setLangChoice(e.target.value)} className={inputCls}>
                  <option value="">Automática (o navegador)</option>
                  <option value="pt">Português</option>
                  <option value="en">English</option>
                  <option value="es">Español</option>
                </select>
              </label>
              <label className="block">
                {paramLabel('Estilo das imagens')}
                <input
                  value={imageStyle}
                  onChange={(e) => setImageStyle(e.target.value)}
                  placeholder="Ex.: nanquim, luz dramática"
                  className={inputCls}
                />
              </label>
              <label className="flex items-center gap-2 text-sm text-secondary sm:col-span-2">
                <input
                  type="checkbox"
                  checked={deepCharacters}
                  onChange={(e) => setDeepCharacters(e.target.checked)}
                  className="accent-accent-600"
                />
                Fichas profundas (NPCs completos)
              </label>
              <label className="block">
                {paramLabel('Pesquisa na web')}
                <select value={webSearchMode} onChange={(e) => setWebSearchMode(e.target.value)} className={inputCls}>
                  <option value="auto">Automática</option>
                  <option value="always">Sempre</option>
                  <option value="off">Desligada</option>
                </select>
              </label>
            </div>
          </fieldset>
          <div className="flex items-center justify-between gap-2">
            <span className="text-xs text-muted">
              {generating
                ? `${stage || 'Gerando…'} 2 a 6 minutos com o modelo local.`
                : 'Nada é salvo até você clicar em Criar aventura.'}
            </span>
            <div className="flex gap-2">
              <button
                onClick={closeModal}
                disabled={creating}
                className="rounded-lg border border-subtle px-4 py-2 text-sm text-secondary hover:bg-surface disabled:opacity-50"
              >
                Cancelar
              </button>
              <button
                onClick={generate}
                disabled={generating || brief.trim().length < 3}
                className="rounded-lg bg-accent-pri px-4 py-2 text-sm font-semibold text-canvas hover:bg-accent-hov disabled:opacity-50"
              >
                {generating ? 'Gerando…' : 'Gerar rascunho'}
              </button>
            </div>
          </div>
        </div>
      ) : (
        <fieldset disabled={busy} className="m-0 flex min-w-0 flex-col gap-4 border-0 p-0">
          {!!draft.web_sources?.length && (
            <div className="rounded-lg border border-subtle bg-elevated/40 px-3 py-2">
              <div className="mb-1 text-xs font-semibold uppercase tracking-wide text-muted">
                🌐 Fontes consultadas
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
                          className="break-words text-accent-pri hover:underline"
                        >
                          {s.title || s.url}
                        </a>
                      ) : (
                        <span className="break-words text-secondary">{s.title}</span>
                      )}
                    </li>
                  )
                })}
              </ul>
            </div>
          )}

          <label className="block">
            {fieldHeader('title', 'Título *')}
            <input
              value={draft.title ?? ''}
              onChange={(e) => setDraft({ ...draft, title: e.target.value })}
              className={inputCls}
            />
          </label>

          <label className="block">
            {fieldHeader('description', 'Descrição')}
            <textarea
              value={draft.description ?? ''}
              onChange={(e) => setDraft({ ...draft, description: e.target.value })}
              rows={2}
              className={`${inputCls} resize-y`}
            />
          </label>

          <label className="block">
            {fieldHeader('ai_instructions', 'Instruções da IA', 'Instruções de estilo para o narrador.')}
            <textarea
              value={draft.ai_instructions ?? ''}
              onChange={(e) => setDraft({ ...draft, ai_instructions: e.target.value })}
              rows={4}
              className={`${inputCls} resize-y`}
            />
          </label>

          <label className="block">
            {fieldHeader('plot_essentials', 'Essenciais da trama (Memória)', 'Bloco fixo: quem você é, o mundo, as regras.')}
            <textarea
              value={draft.plot_essentials ?? ''}
              onChange={(e) => setDraft({ ...draft, plot_essentials: e.target.value })}
              rows={4}
              className={`${inputCls} resize-y`}
            />
          </label>

          <label className="block">
            {fieldHeader('authors_note', "Author's Note", 'Nota de tom injetada perto do fim do contexto.')}
            <textarea
              value={draft.authors_note ?? ''}
              onChange={(e) => setDraft({ ...draft, authors_note: e.target.value })}
              rows={3}
              className={`${inputCls} resize-y`}
            />
          </label>

          <label className="block">
            {fieldHeader('opening_text', 'Texto de abertura', "O primeiro bloco da história (vira a ação inicial).")}
            <textarea
              value={draft.opening_text ?? ''}
              onChange={(e) => setDraft({ ...draft, opening_text: e.target.value })}
              rows={6}
              className={`${inputCls} resize-y font-prose`}
            />
          </label>

          <label className="block">
            {fieldHeader('image_style', 'Estilo visual (geração de imagem)')}
            <input
              value={draft.image_style ?? ''}
              onChange={(e) => setDraft({ ...draft, image_style: e.target.value })}
              placeholder="ex.: dark cinematic photography, neon rain"
              className={inputCls}
            />
          </label>

          <div className="block">
            {fieldHeader('tags', 'Etiquetas')}
            <TagInput
              value={draft.tags ?? []}
              onChange={(tags) => setDraft({ ...draft, tags })}
              disabled={busy}
              placeholder="ex.: fantasy"
            />
          </div>

          <div className="block">
            <div className="mb-1">
              <div className={labelCls}>Fichas ({(draft.story_cards ?? []).length})</div>
              <div className={hintCls}>NPCs, lugares, facções — entram no contexto quando uma palavra-gatilho aparece.</div>
            </div>
            {refineBox(
              refineField && String(refineField).startsWith('card-') ? refineField : '',
              refineField === 'card-new'
                ? 'Nome ou conceito da ficha'
                : 'Instrução para esta ficha (opcional)',
            )}
            <StoryCardsEditor
              cards={draft.story_cards ?? []}
              onChange={(story_cards) => setDraft((d) => ({ ...d, story_cards }))}
              busy={busy}
              onAiCard={(index) => openRefine(`card-${index}`)}
              webOn={webOnRefine}
              onToggleWeb={setWebOnRefine}
            >
              <button type="button" onClick={() => openRefine('card-new')} disabled={busy} className={miniBtnCls}>
                + Ficha com IA
              </button>
              {regenBtn('story_cards')}
            </StoryCardsEditor>
          </div>

          <div className="mt-2 flex items-center justify-between gap-2">
            <button
              type="button"
              onClick={() => setDraft(null)}
              className="text-sm text-muted hover:text-secondary"
            >
              ← Outra ideia
            </button>
            <div className="flex flex-col items-end gap-1">
              {!draft.title?.trim() && (
                <p className="text-xs text-warn-200">O título é obrigatório para criar a aventura.</p>
              )}
              <button
                type="button"
                onClick={create}
                disabled={busy || !draft.title?.trim()}
                className="rounded-lg bg-accent-pri px-5 py-2 text-sm font-semibold text-canvas hover:bg-accent-hov disabled:opacity-50"
              >
                {creating ? 'Criando…' : 'Criar aventura'}
              </button>
            </div>
          </div>
        </fieldset>
      )}
    </Modal>
  )
}
