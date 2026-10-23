import { useEffect, useState } from 'react'
import { api } from '../api.js'
import Modal from './Modal.jsx'
import { useToast } from './Toast.jsx'
import { CHARACTER_PRESETS, CORE_STATS, abilityMod, roll4d6DropLowest } from '../utils.js'

const CORE_KEYS = new Set(CORE_STATS.map(([key]) => key))
const POINT_MIN = 8
const POINT_MAX = 15
const POINT_BUDGET = 27
// Espelha backend/app/stats.py POINT_BUY_COSTS (tabela 5e, não valor-8).
const POINT_BUY_COSTS = { 8: 0, 9: 1, 10: 2, 11: 3, 12: 4, 13: 5, 14: 7, 15: 9 }

function pointCost(value) {
  return POINT_BUY_COSTS[value] ?? 0
}
const ROLL_MIN = 3
const ROLL_MAX = 18

const inputCls =
  'w-full rounded-lg border border-zinc-700 bg-zinc-800 px-3 py-2 text-sm text-zinc-100 outline-none focus:border-emerald-600 disabled:opacity-60'
const labelCls = 'mb-1 block text-xs font-semibold uppercase tracking-wide text-zinc-500'
const stepBtnCls =
  'h-7 w-7 shrink-0 rounded-md border border-zinc-600 bg-zinc-800 text-base leading-none text-zinc-100 hover:bg-zinc-700 disabled:opacity-40 sm:h-8 sm:w-8 touch-manipulation'

function clamp(n, min, max) {
  return Math.max(min, Math.min(max, n))
}

function emptyCore() {
  return { str: 8, dex: 8, con: 8, int: 8, wis: 8, cha: 8 }
}

function pointsSpent(core) {
  return CORE_STATS.reduce((sum, [key]) => sum + pointCost(core[key] ?? POINT_MIN), 0)
}

function fitPointBuy(core) {
  const next = emptyCore()
  for (const [key] of CORE_STATS) {
    const n = Number.parseInt(core?.[key], 10)
    next[key] = clamp(Number.isFinite(n) ? n : POINT_MIN, POINT_MIN, POINT_MAX)
  }
  let guard = 64
  while (pointsSpent(next) > POINT_BUDGET && guard > 0) {
    guard -= 1
    let target = CORE_STATS[0][0]
    for (const [key] of CORE_STATS) {
      if (next[key] > next[target]) target = key
    }
    if (next[target] <= POINT_MIN) break
    next[target] -= 1
  }
  return next
}

function fitRoll(core) {
  const next = emptyCore()
  for (const [key] of CORE_STATS) {
    const n = Number.parseInt(core?.[key], 10)
    next[key] = clamp(Number.isFinite(n) ? n : 10, ROLL_MIN, ROLL_MAX)
  }
  return next
}

function coreFitsPointBuy(core) {
  const legal = CORE_STATS.every(([key]) => {
    const n = core[key]
    return Number.isInteger(n) && n >= POINT_MIN && n <= POINT_MAX
  })
  return legal && pointsSpent(core) <= POINT_BUDGET
}

function parseSheet(sheet) {
  if (!sheet) return {}
  if (typeof sheet === 'object') return sheet
  if (typeof sheet !== 'string') return {}
  try {
    const obj = JSON.parse(sheet)
    return obj && typeof obj === 'object' ? obj : {}
  } catch {
    return {}
  }
}

function splitStats(stats) {
  const core = emptyCore()
  const extras = []
  let sawCore = false
  if (stats && typeof stats === 'object' && !Array.isArray(stats)) {
    for (const [rawKey, rawVal] of Object.entries(stats)) {
      const key = String(rawKey).trim()
      if (!key) continue
      const lower = key.toLowerCase()
      if (CORE_KEYS.has(lower)) {
        const n = Number.parseInt(rawVal, 10)
        if (Number.isFinite(n)) {
          core[lower] = n
          sawCore = true
        }
        continue
      }
      extras.push({ key, value: rawVal == null ? '' : String(rawVal) })
    }
  }
  const mode = sawCore && !coreFitsPointBuy(core) ? 'roll' : 'points'
  return {
    mode,
    core: mode === 'roll' ? fitRoll(core) : fitPointBuy(core),
    extras,
  }
}

function buildStats(core, extras) {
  const stats = {}
  for (const [key] of CORE_STATS) stats[key] = core[key]
  for (const row of extras) {
    const key = (row.key || '').trim()
    if (!key || CORE_KEYS.has(key.toLowerCase())) continue
    const raw = String(row.value ?? '').trim()
    stats[key] = /^-?\d+$/.test(raw) ? Number(raw) : raw
  }
  return stats
}

const WIZARD_STEPS = ['Conceito', 'Atributos', 'Revisão']
const STAT_SHORT = { str: 'For', dex: 'Des', con: 'Con', int: 'Int', wis: 'Sab', cha: 'Car' }

function presetSuggestion(preset) {
  return preset.appearanceHints.join('. ')
}

function isPresetAppearance(text) {
  const trimmed = (text || '').trim()
  if (!trimmed) return true
  return CHARACTER_PRESETS.some(
    (preset) => preset.appearanceHints.includes(trimmed) || presetSuggestion(preset) === trimmed,
  )
}

function blankFromCard(card) {
  const sheet = parseSheet(card?.sheet)
  return {
    title: card?.title || '',
    classRole: sheet.class || sheet.role || sheet.classe || '',
    appearance: sheet.appearance || '',
    body: sheet.body || '',
    clothing: sheet.clothing || '',
    status: sheet.status || '',
    notes: sheet.notes || '',
    portrait: card?.portrait_prompt || '',
    ...splitStats(sheet.stats),
  }
}

function AttributesEditor({
  busy,
  statMode,
  core,
  remaining,
  spent,
  extras,
  switchMode,
  resetPoints,
  rollAll,
  stepCore,
  patchExtra,
  setExtras,
}) {
  return (
    <div className="min-w-0">
      <div className="mb-2 flex flex-wrap items-center justify-between gap-2">
        <span className={`${labelCls} mb-0`}>Atributos</span>
        <div
          className="inline-flex rounded-lg border border-zinc-700 p-0.5"
          role="group"
          aria-label="Modo dos atributos"
        >
          <button
            type="button"
            aria-pressed={statMode === 'points'}
            disabled={busy}
            onClick={() => switchMode('points')}
            className={`rounded-md px-2.5 py-1 text-xs font-medium disabled:opacity-40 ${
              statMode === 'points' ? 'bg-emerald-700 text-white' : 'text-zinc-400 hover:text-zinc-100'
            }`}
          >
            Pontos
          </button>
          <button
            type="button"
            aria-pressed={statMode === 'roll'}
            disabled={busy}
            onClick={() => switchMode('roll')}
            className={`rounded-md px-2.5 py-1 text-xs font-medium disabled:opacity-40 ${
              statMode === 'roll' ? 'bg-emerald-700 text-white' : 'text-zinc-400 hover:text-zinc-100'
            }`}
          >
            Sorteio
          </button>
        </div>
      </div>

      {statMode === 'points' ? (
        <div className="mb-2">
          <div className="flex items-center justify-between gap-2">
            <p className={`text-xs font-medium ${remaining === 0 ? 'text-amber-300' : 'text-zinc-300'}`}>
              Pontos restantes: {remaining}
            </p>
            <button
              type="button"
              onClick={resetPoints}
              disabled={busy}
              className="rounded-md border border-zinc-700 px-2 py-0.5 text-xs text-zinc-300 hover:bg-zinc-800 disabled:opacity-40"
            >
              Zerar
            </button>
          </div>
          <div className={`mt-1 h-1.5 overflow-hidden rounded-full bg-zinc-800 ${remaining === 0 ? 'ring-1 ring-amber-400/80' : ''}`}>
            <div
              className={`h-full ${remaining === 0 ? 'bg-amber-500' : 'bg-emerald-600'}`}
              style={{ width: `${Math.min(100, (spent / POINT_BUDGET) * 100)}%` }}
            />
          </div>
        </div>
      ) : (
        <div className="mb-2 flex flex-wrap gap-1.5">
          <button
            type="button"
            onClick={rollAll}
            disabled={busy}
            className="rounded-md border border-zinc-700 px-2 py-1 text-xs text-zinc-200 hover:bg-zinc-800 disabled:opacity-40"
          >
            🎲 Rolar 4d6
          </button>
        </div>
      )}

      <div className="grid grid-cols-2 gap-2 sm:grid-cols-3">
        {CORE_STATS.map(([key, label]) => {
          const value = core[key]
          const min = statMode === 'points' ? POINT_MIN : ROLL_MIN
          const max = statMode === 'points' ? POINT_MAX : ROLL_MAX
          const stepCost = pointCost(value + 1) - pointCost(value)
          const plusBlocked = statMode === 'points' && (value >= max || remaining < stepCost)
          return (
            <div key={key} className="min-w-0 rounded-lg border border-zinc-800 bg-zinc-950/40 px-1.5 py-1">
              <div className="text-[11px] font-semibold leading-tight text-zinc-100">{label}</div>
              <div className="flex items-baseline justify-between gap-1">
                <span className="font-mono text-[10px] text-zinc-500">{key}</span>
                <span className="font-mono text-[11px] text-emerald-300">{abilityMod(value)}</span>
              </div>
              <div className="mt-1 flex items-center justify-between gap-0.5">
                <button
                  type="button"
                  aria-label={`Diminuir ${label}`}
                  disabled={busy || value <= min}
                  onClick={() => stepCore(key, -1)}
                  className={stepBtnCls}
                >
                  −
                </button>
                <input
                  readOnly
                  tabIndex={-1}
                  value={value}
                  aria-label={label}
                  className="w-10 rounded-md border border-zinc-700 bg-zinc-800 px-0 py-1 text-center text-sm font-semibold tabular-nums text-zinc-100 outline-none sm:w-14"
                />
                <button
                  type="button"
                  aria-label={`Aumentar ${label}`}
                  disabled={busy || value >= max || plusBlocked}
                  onClick={() => stepCore(key, 1)}
                  className={stepBtnCls}
                >
                  +
                </button>
              </div>
            </div>
          )
        })}
      </div>

      <div className="mt-2">
        <div className="mb-1 flex items-center justify-between gap-2">
          <span className="text-[10px] font-semibold uppercase tracking-wide text-zinc-500">Extras</span>
          <button
            type="button"
            onClick={() => setExtras((rows) => [...rows, { key: '', value: '' }])}
            disabled={busy}
            className="rounded-md border border-zinc-700 px-2 py-0.5 text-xs text-zinc-300 hover:bg-zinc-800 disabled:opacity-40"
          >
            +
          </button>
        </div>
        {extras.length === 0 ? (
          <p className="text-[11px] text-zinc-500">hp e outros, se precisar.</p>
        ) : (
          <ul className="flex flex-col gap-1.5">
            {extras.map((row, i) => (
              <li key={i} className="flex min-w-0 items-center gap-1.5">
                <input
                  value={row.key}
                  onChange={(e) => patchExtra(i, { key: e.target.value })}
                  placeholder="hp"
                  aria-label={`Atributo extra ${i + 1}`}
                  disabled={busy}
                  className={`${inputCls} w-20 shrink-0 px-2 sm:w-28`}
                />
                <input
                  value={row.value}
                  onChange={(e) => patchExtra(i, { value: e.target.value })}
                  placeholder="10"
                  aria-label={`Valor do extra ${i + 1}`}
                  disabled={busy}
                  className={`${inputCls} min-w-0 flex-1 px-2`}
                />
                <button
                  type="button"
                  onClick={() => setExtras((rows) => rows.filter((_, j) => j !== i))}
                  disabled={busy}
                  aria-label={`Remover atributo ${row.key || i + 1}`}
                  className="h-8 w-8 shrink-0 rounded-lg text-sm text-zinc-400 hover:bg-zinc-800 hover:text-zinc-100 disabled:opacity-40"
                >
                  ✕
                </button>
              </li>
            ))}
          </ul>
        )}
      </div>
    </div>
  )
}

export default function CharacterModal({
  adventureId,
  rules = '',
  card = null,
  loading = false,
  onClose,
  onSaved,
}) {
  const toast = useToast()
  const [title, setTitle] = useState('')
  const [classRole, setClassRole] = useState('')
  const [appearance, setAppearance] = useState('')
  const [body, setBody] = useState('')
  const [clothing, setClothing] = useState('')
  const [status, setStatus] = useState('')
  const [notes, setNotes] = useState('')
  const [portrait, setPortrait] = useState('')
  const [statMode, setStatMode] = useState('points')
  const [core, setCore] = useState(emptyCore)
  const [extras, setExtras] = useState([])
  const [saving, setSaving] = useState(false)
  const [formError, setFormError] = useState('')
  const [applied, setApplied] = useState(undefined)
  const [guided, setGuided] = useState(false)
  const [step, setStep] = useState(0)
  const [presetId, setPresetId] = useState('')

  useEffect(() => {
    if (loading) return
    const next = blankFromCard(card)
    setTitle(next.title)
    setClassRole(next.classRole)
    setAppearance(next.appearance)
    setBody(next.body)
    setClothing(next.clothing)
    setStatus(next.status)
    setNotes(next.notes)
    setPortrait(next.portrait)
    setStatMode(next.mode)
    setCore(next.core)
    setExtras(next.extras)
    setFormError('')
    setApplied(card ?? null)
    setGuided(false)
    setStep(0)
    setPresetId('')
  }, [card, loading])

  const stepCore = (key, delta) => {
    setCore((prev) => {
      const cur = prev[key]
      if (statMode === 'points') {
        const next = cur + delta
        if (next < POINT_MIN || next > POINT_MAX) return prev
        if (delta > 0 && pointsSpent({ ...prev, [key]: next }) > POINT_BUDGET) return prev
        return { ...prev, [key]: next }
      }
      const next = clamp(cur + delta, ROLL_MIN, ROLL_MAX)
      if (next === cur) return prev
      return { ...prev, [key]: next }
    })
  }

  const switchMode = (next) => {
    if (next === statMode) return
    setCore((prev) => (next === 'points' ? fitPointBuy(prev) : fitRoll(prev)))
    setStatMode(next)
  }

  const resetPoints = () => {
    setStatMode('points')
    setCore(emptyCore())
  }

  const rollAll = () => {
    const next = emptyCore()
    for (const [key] of CORE_STATS) next[key] = roll4d6DropLowest()
    setStatMode('roll')
    setCore(next)
  }

  const applyPreset = (preset) => {
    setPresetId(preset.id)
    setClassRole(preset.classHint)
    setStatMode('points')
    setCore({ ...preset.stats })
    const suggestion = presetSuggestion(preset)
    setAppearance((current) => (isPresetAppearance(current) ? suggestion : current))
  }

  const goNext = () => {
    if (step === 0) {
      const name = title.trim()
      if (!name) {
        setFormError('Informe o nome.')
        return
      }
      if (name.length > 80) {
        setFormError('O nome deve ter no máximo 80 caracteres.')
        return
      }
    }
    setFormError('')
    setStep((current) => Math.min(WIZARD_STEPS.length - 1, current + 1))
  }

  const goBack = () => {
    setFormError('')
    setStep((current) => Math.max(0, current - 1))
  }

  const patchExtra = (index, fields) => {
    setExtras((rows) => rows.map((row, i) => (i === index ? { ...row, ...fields } : row)))
  }

  const save = async (e) => {
    e?.preventDefault()
    const name = title.trim()
    if (!name) {
      setFormError('Informe o nome.')
      return
    }
    if (name.length > 80) {
      setFormError('O nome deve ter no máximo 80 caracteres.')
      return
    }
    setFormError('')
    setSaving(true)
    try {
      const saved = await api.saveCharacter(adventureId, {
        title: name,
        sheet: {
          ...parseSheet(card?.sheet),
          appearance: appearance.trim(),
          body: body.trim(),
          clothing: clothing.trim(),
          status: status.trim(),
          notes: notes.trim(),
          class: classRole.trim(),
          stats: buildStats(core, extras),
        },
        portrait_prompt: portrait.trim(),
      })
      const next = saved && typeof saved === 'object' && 'card' in saved ? saved.card : saved
      onSaved?.(next || saved)
    } catch (err) {
      toast(err.message)
    } finally {
      setSaving(false)
    }
  }

  const submit = (event) => {
    if (guided && step !== WIZARD_STEPS.length - 1) {
      event.preventDefault()
      goNext()
      return
    }
    save(event)
  }

  const busy = saving || loading
  const rulesText = (rules || '').trim()
  const formReady = !loading && applied === (card ?? null)
  const storedSheet = parseSheet(card?.sheet)
  const bodyState = storedSheet.body_state && typeof storedSheet.body_state === 'object'
    ? storedSheet.body_state
    : {}
  const pain = Number(bodyState.pain) || 0
  const conditions = Array.isArray(bodyState.conditions)
    ? bodyState.conditions.filter((item) => item && item.active !== false)
    : []
  const expression = String(storedSheet.expression || '').trim()
  const spent = pointsSpent(core)
  const remaining = POINT_BUDGET - spent
  const selectedPreset = CHARACTER_PRESETS.find((preset) => preset.id === presetId) || null

  return (
    <Modal title="🧙 Personagem" onClose={saving ? undefined : onClose} wide>
      <form onSubmit={submit} className="flex flex-col gap-3">
        {rulesText && (
          <div className="rounded-lg border border-amber-700/60 bg-amber-950/50 px-3 py-2">
            <div className="text-[10px] font-bold uppercase tracking-wider text-amber-300">
              Regras desta aventura
            </div>
            <p className="mt-1 whitespace-pre-wrap text-sm text-amber-100">{rulesText}</p>
          </div>
        )}

        {!formReady ? (
          <p className="text-sm text-zinc-500">Carregando ficha…</p>
        ) : (
          <>
            <div
              className="inline-flex self-start rounded-lg border border-zinc-700 p-0.5"
              role="group"
              aria-label="Modo da ficha"
            >
              <button
                type="button"
                aria-pressed={!guided}
                disabled={busy}
                onClick={() => setGuided(false)}
                className={`rounded-md px-2.5 py-1 text-xs font-medium disabled:opacity-40 ${
                  !guided ? 'bg-emerald-700 text-white' : 'text-zinc-400 hover:text-zinc-100'
                }`}
              >
                Livre
              </button>
              <button
                type="button"
                aria-pressed={guided}
                disabled={busy}
                onClick={() => setGuided(true)}
                className={`rounded-md px-2.5 py-1 text-xs font-medium disabled:opacity-40 ${
                  guided ? 'bg-emerald-700 text-white' : 'text-zinc-400 hover:text-zinc-100'
                }`}
              >
                Guiado
              </button>
            </div>
            {guided ? (
              <div className="flex flex-col gap-3">
                <div className="flex flex-wrap gap-x-3 gap-y-1 text-xs" aria-label="Passos">
                  {WIZARD_STEPS.map((label, index) => (
                    <span key={label} className={index === step ? 'font-semibold text-emerald-300' : 'text-zinc-500'}>
                      {index + 1}. {label}
                    </span>
                  ))}
                </div>
                {step === 0 && (
                  <>
                    <label className="block">
                      <span className={labelCls}>Nome *</span>
                      <input
                        value={title}
                        onChange={(e) => setTitle(e.target.value.slice(0, 80))}
                        maxLength={80}
                        required
                        autoFocus
                        autoComplete="off"
                        placeholder="Nome do personagem"
                        disabled={busy}
                        className={inputCls}
                      />
                      <span className="mt-1 block text-right text-[10px] text-zinc-500">{title.trim().length}/80</span>
                    </label>
                    <div>
                      <span className={labelCls}>Conceito</span>
                      <div className="grid grid-cols-2 gap-2" role="group" aria-label="Arquétipos">
                        {CHARACTER_PRESETS.map((preset) => {
                          const selected = preset.id === presetId
                          return (
                            <button
                              key={preset.id}
                              type="button"
                              aria-pressed={selected}
                              disabled={busy}
                              onClick={() => applyPreset(preset)}
                              className={`rounded-lg border px-2.5 py-2 text-left disabled:opacity-40 ${
                                selected
                                  ? 'border-emerald-600 bg-emerald-950/40'
                                  : 'border-zinc-700 bg-zinc-950/40 hover:bg-zinc-800'
                              }`}
                            >
                              <span className="block text-sm font-semibold text-zinc-100">{preset.name}</span>
                              <span className="mt-0.5 block text-[10px] leading-snug text-zinc-500">{preset.blurb}</span>
                              <span className="mt-1 block font-mono text-[10px] leading-snug text-zinc-300">
                                {CORE_STATS.map(([key]) => `${STAT_SHORT[key]} ${preset.stats[key]}`).join(' · ')}
                              </span>
                            </button>
                          )
                        })}
                      </div>
                    </div>
                    <label className="block">
                      <span className={labelCls}>Classe / Papel</span>
                      <input
                        value={classRole}
                        onChange={(e) => setClassRole(e.target.value)}
                        placeholder="ex.: ladina, bardo, capitão"
                        disabled={busy}
                        className={inputCls}
                      />
                    </label>
                    <label className="block">
                      <span className={labelCls}>Aparência</span>
                      <textarea
                        value={appearance}
                        onChange={(e) => setAppearance(e.target.value)}
                        rows={2}
                        disabled={busy}
                        placeholder="rosto, cabelo, olhos, marcas"
                        className={`${inputCls} resize-y`}
                      />
                      {selectedPreset && (
                        <div className="mt-2 flex flex-wrap gap-1.5">
                          {selectedPreset.appearanceHints.map((tip) => (
                            <button
                              key={tip}
                              type="button"
                              disabled={busy}
                              onClick={() => setAppearance(tip)}
                              className="rounded-md border border-zinc-700 px-2 py-1 text-left text-[11px] leading-snug text-zinc-300 hover:bg-zinc-800 disabled:opacity-40"
                            >
                              {tip}
                            </button>
                          ))}
                        </div>
                      )}
                    </label>
                  </>
                )}
                {step === 1 && (
                  <AttributesEditor
                    busy={busy}
                    statMode={statMode}
                    core={core}
                    remaining={remaining}
                    spent={spent}
                    extras={extras}
                    switchMode={switchMode}
                    resetPoints={resetPoints}
                    rollAll={rollAll}
                    stepCore={stepCore}
                    patchExtra={patchExtra}
                    setExtras={setExtras}
                  />
                )}
                {step === 2 && (
                  <div className="flex flex-col gap-2 text-sm">
                    {[
                      ['Nome', title],
                      ['Classe', classRole],
                      ['Aparência', appearance],
                      ['Corpo', body],
                      ['Roupas', clothing],
                      ['Status', status],
                      ['Expressão', expression],
                      ['Dor', `${pain}/10`],
                      ['Condições', conditions.map((item) => item.description || item.kind).join(' · ')],
                      ['Notas', notes],
                      ['Retrato', portrait],
                    ].map(([label, value]) => (
                      <div key={label}>
                        <div className="text-[10px] font-semibold uppercase tracking-wide text-zinc-500">{label}</div>
                        <p className="whitespace-pre-wrap text-zinc-100">{String(value || '').trim() ? value : '—'}</p>
                      </div>
                    ))}
                    <div>
                      <div className="text-[10px] font-semibold uppercase tracking-wide text-zinc-500">
                        Atributos · {statMode === 'points' ? 'Pontos' : 'Sorteio'}
                      </div>
                      <ul className="mt-1 grid grid-cols-2 gap-1 sm:grid-cols-3">
                        {CORE_STATS.map(([key, label]) => (
                          <li key={key} className="rounded-md border border-zinc-800 px-2 py-1 text-xs">
                            <span className="text-zinc-500">{label}</span>{' '}
                            <span className="font-mono text-zinc-100">
                              {core[key]} ({abilityMod(core[key])})
                            </span>
                          </li>
                        ))}
                      </ul>
                      {extras.some((row) => (row.key || '').trim()) && (
                        <p className="mt-1 text-xs text-zinc-400">
                          {extras
                            .filter((row) => (row.key || '').trim())
                            .map((row) => `${row.key}: ${row.value}`)
                            .join(' · ')}
                        </p>
                      )}
                    </div>
                  </div>
                )}
              </div>
            ) : (
              <>
            <label className="block">
              <span className={labelCls}>Nome *</span>
              <input
                value={title}
                onChange={(e) => setTitle(e.target.value.slice(0, 80))}
                maxLength={80}
                required
                autoFocus
                autoComplete="off"
                placeholder="Nome do personagem"
                disabled={busy}
                className={inputCls}
              />
              <span className="mt-1 block text-right text-[10px] text-zinc-500">{title.trim().length}/80</span>
            </label>

            <label className="block">
              <span className={labelCls}>Classe / Papel</span>
              <input
                value={classRole}
                onChange={(e) => setClassRole(e.target.value)}
                placeholder="ex.: ladina, bardo, capitão"
                disabled={busy}
                className={inputCls}
              />
            </label>

            <label className="block">
              <span className={labelCls}>Aparência</span>
              <textarea
                value={appearance}
                onChange={(e) => setAppearance(e.target.value)}
                rows={2}
                disabled={busy}
                placeholder="rosto, cabelo, olhos, marcas"
                className={`${inputCls} resize-y`}
              />
            </label>

            <label className="block">
              <span className={labelCls}>Corpo</span>
              <textarea
                value={body}
                onChange={(e) => setBody(e.target.value)}
                rows={2}
                disabled={busy}
                placeholder="porte, altura, postura"
                className={`${inputCls} resize-y`}
              />
            </label>

            <label className="block">
              <span className={labelCls}>Roupas</span>
              <textarea
                value={clothing}
                onChange={(e) => setClothing(e.target.value)}
                rows={2}
                disabled={busy}
                placeholder="traje, armadura, acessórios"
                className={`${inputCls} resize-y`}
              />
            </label>

            <label className="block">
              <span className={labelCls}>Estado</span>
              <input
                value={status}
                onChange={(e) => setStatus(e.target.value)}
                placeholder="vivo, ferido, desaparecido…"
                disabled={busy}
                className={inputCls}
              />
            </label>

            <div className="rounded-lg border border-zinc-800 bg-zinc-950/40 p-3 text-sm">
              <div className="flex items-center justify-between gap-2">
                <span className={labelCls}>Corpo e dor</span>
                <span className="font-mono text-xs text-zinc-300">dor {pain}/10</span>
              </div>
              <p className="mt-1 text-xs text-zinc-400">
                {expression ? `Expressão: ${expression}` : 'Sem expressão registrada.'}
              </p>
              {conditions.length ? (
                <ul className="mt-2 flex flex-wrap gap-1.5">
                  {conditions.map((item, index) => (
                    <li
                      key={item.id || index}
                      className="rounded-full border border-zinc-700 px-2 py-0.5 text-[11px] text-zinc-200"
                    >
                      {item.description || item.kind}
                      {item.portrait_visible ? ' · visível no retrato' : ''}
                    </li>
                  ))}
                </ul>
              ) : (
                <p className="mt-1 text-xs text-zinc-500">Nenhuma condição ativa.</p>
              )}
            </div>

            <label className="block">
              <span className={labelCls}>Notas / Background</span>
              <textarea
                value={notes}
                onChange={(e) => setNotes(e.target.value)}
                rows={4}
                disabled={busy}
                placeholder="história, vínculos, segredos"
                className={`${inputCls} resize-y`}
              />
            </label>

            <AttributesEditor
              busy={busy}
              statMode={statMode}
              core={core}
              remaining={remaining}
              spent={spent}
              extras={extras}
              switchMode={switchMode}
              resetPoints={resetPoints}
              rollAll={rollAll}
              stepCore={stepCore}
              patchExtra={patchExtra}
              setExtras={setExtras}
            />

            <label className="block">
              <span className={labelCls}>Prompt de retrato (opcional)</span>
              <textarea
                value={portrait}
                onChange={(e) => setPortrait(e.target.value)}
                rows={2}
                disabled={busy}
                placeholder="prompt em inglês para o retrato"
                className={`${inputCls} resize-y`}
              />
            </label>
              </>
            )}
          </>
        )}

        {formError && <p className="text-sm text-red-300">{formError}</p>}

        {guided && formReady ? (
          <div className="flex justify-end gap-2 border-t border-zinc-800 pt-3">
            <button
              type="button"
              onClick={onClose}
              disabled={saving}
              className="rounded-lg border border-zinc-700 px-4 py-2 text-sm text-zinc-300 hover:bg-zinc-800 disabled:opacity-40"
            >
              Cancelar
            </button>
            {step > 0 && (
              <button
                type="button"
                onClick={goBack}
                disabled={saving}
                className="rounded-lg border border-zinc-700 px-4 py-2 text-sm text-zinc-300 hover:bg-zinc-800 disabled:opacity-40"
              >
                Voltar
              </button>
            )}
            {step < WIZARD_STEPS.length - 1 ? (
              <button
                type="button"
                onClick={goNext}
                disabled={saving || (step === 0 && !title.trim())}
                className="rounded-lg bg-emerald-700 px-4 py-2 text-sm font-semibold text-white hover:bg-emerald-600 disabled:opacity-50"
              >
                Continuar
              </button>
            ) : (
              <button
                type="submit"
                disabled={!formReady || saving || !title.trim()}
                className="rounded-lg bg-emerald-700 px-4 py-2 text-sm font-semibold text-white hover:bg-emerald-600 disabled:opacity-50"
              >
                {saving ? 'Salvando…' : 'Salvar'}
              </button>
            )}
          </div>
        ) : (
          <div className="flex justify-end gap-2 border-t border-zinc-800 pt-3">
            <button
              type="button"
              onClick={onClose}
              disabled={saving}
              className="rounded-lg border border-zinc-700 px-4 py-2 text-sm text-zinc-300 hover:bg-zinc-800 disabled:opacity-40"
            >
              Cancelar
            </button>
            <button
              type="submit"
              disabled={!formReady || saving || !title.trim()}
              className="rounded-lg bg-emerald-700 px-4 py-2 text-sm font-semibold text-white hover:bg-emerald-600 disabled:opacity-50"
            >
              {saving ? 'Salvando…' : 'Salvar'}
            </button>
          </div>
        )}
      </form>
    </Modal>
  )
}
