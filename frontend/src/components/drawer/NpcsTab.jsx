import { useCallback, useEffect, useRef, useState } from 'react'
import { api } from '../../api.js'
import { emotionForLine } from '../../ttsText.js'
import { useToast } from '../Toast.jsx'
import { CORE_STATS, roll4d6DropLowest } from '../../utils.js'

const VOICES = ['', 'M1', 'M2', 'M3', 'F1', 'F2', 'F3']
const EMPTY_CARD = {
  type: 'character',
  triggers: [],
  entry: '',
  pinned: false,
  use_for_character_creation: false,
}

// Iniciais para o placeholder quando o NPC ainda não tem retrato.
function initials(title) {
  const words = (title || '').trim().split(/\s+/).filter(Boolean)
  if (words.length === 0) return '?'
  return words.slice(0, 2).map((w) => w[0].toUpperCase()).join('')
}

// Frase curta p/ o preview de voz do NPC (v0.4.0): primeira sentença do
// entry/description (cap 200 chars); fallback com o nome quando vazio.
function previewText(form) {
  const src = (form.entry || '').trim()
  if (src) {
    const m = src.match(/^[\s\S]{1,200}?[.!?](?:\s|$)/)
    return (m ? m[0] : src.slice(0, 200)).trim()
  }
  const name = (form.title || '').trim()
  return name ? `Greetings, traveler. I am ${name}.` : 'Greetings, traveler.'
}

// Miniatura do retrato. <img> não envia o header Bearer, então a imagem é
// baixada via fetch autenticado (api.fetchImageBlob) e exibida como blob URL.
function PortraitThumb({ card, activeJob }) {
  const [url, setUrl] = useState(null)

  useEffect(() => {
    if (!card.portrait?.id) {
      setUrl(null)
      return undefined
    }
    let stale = false
    let objectUrl = null
    api.fetchImageBlob(card.portrait.id)
      .then((blob) => {
        objectUrl = URL.createObjectURL(blob)
        if (stale) URL.revokeObjectURL(objectUrl)
        else setUrl(objectUrl)
      })
      .catch(() => { /* placeholder com iniciais */ })
    return () => {
      stale = true
      if (objectUrl) URL.revokeObjectURL(objectUrl)
    }
  }, [card.portrait?.id])

  return (
    <div className="relative h-24 w-24 shrink-0 overflow-hidden rounded-lg border border-zinc-700 bg-zinc-800">
      {url ? (
        <img src={url} alt={card.title || 'NPC portrait'} className="h-full w-full object-cover" />
      ) : (
        <div className="flex h-full w-full items-center justify-center text-2xl font-semibold text-zinc-600">
          {initials(card.title)}
        </div>
      )}
      {activeJob && (
        <div className="absolute inset-0 flex flex-col items-center justify-center gap-1 bg-black/60">
          <div className="h-5 w-5 animate-spin rounded-full border-2 border-zinc-500 border-t-emerald-400" />
          <span className="text-[10px] font-medium text-zinc-200">
            {activeJob.status === 'pending' ? 'Queued…' : 'Generating…'}
          </span>
        </div>
      )}
    </div>
  )
}

const SHEET_FIELDS = [
  'class', 'appearance', 'body', 'clothing', 'personality', 'humor', 'speech',
  'goals', 'background', 'lifestyle', 'habits', 'likes', 'sentiments', 'notes',
]
const FIELD_LABELS = {
  class: 'Classe/Papel',
  appearance: 'Aparência',
  body: 'Corpo',
  clothing: 'Roupa',
  personality: 'Personalidade',
  humor: 'Humor',
  speech: 'Fala',
  goals: 'Objetivos',
  background: 'História',
  lifestyle: 'Rotina',
  habits: 'Manias',
  likes: 'Gosta / não gosta',
  sentiments: 'Sentimentos',
  notes: 'Notas',
}
const REGEN_FIELDS = new Set([
  'personality', 'humor', 'speech', 'goals', 'background',
  'lifestyle', 'habits', 'likes', 'sentiments',
])
const STAT_KEYS = CORE_STATS.map(([key]) => key)
// Mesmos nomes e ponto de partida de backend/app/npc_bonds.py.
const BOND_KEYS = ['relationship', 'trust', 'fear', 'desire', 'boldness']
const BOND_LABELS = {
  relationship: 'Relation',
  trust: 'Trust',
  fear: 'Fear',
  desire: 'Desire',
  boldness: 'Bold',
}
const BOND_DEFAULTS = { relationship: 40, trust: 40, fear: 15, desire: 15, boldness: 40 }

// sheet armazenada (JSON string) → dict editável (''/inválido → {}).
function parseSheet(raw) {
  if (!raw) return {}
  try {
    const obj = JSON.parse(raw)
    return obj && typeof obj === 'object' && !Array.isArray(obj) ? obj : {}
  } catch {
    return {}
  }
}

// true se a ficha tem algum conteúdo visível (ignora stats default).
function sheetHasContent(sheet) {
  return SHEET_FIELDS.some((k) => (sheet[k] || '').trim()) ||
    Object.keys(sheet.stats || {}).length > 0 ||
    Object.keys(sheet.bonds || {}).length > 0
}

// Painel da ficha estruturada (v0.5.0): exibição + edição mínima dos campos.
function SheetPanel({ sheet, onChange, onRegenerateField }) {
  const inputCls =
    'w-full min-w-0 rounded-md border border-zinc-700 bg-zinc-800 px-2 py-1 text-xs outline-none focus:border-emerald-600'
  // Sem as setas nativas: num input estreito elas cobrem o dígito (40 vira "4(").
  const numberCls =
    `${inputCls} px-1 text-center [appearance:textfield] [&::-webkit-inner-spin-button]:appearance-none [&::-webkit-outer-spin-button]:appearance-none`
  const set = (key, value) => onChange({ ...sheet, [key]: value })
  const setStat = (key, value) => {
    const n = Math.max(1, Math.min(20, Number.parseInt(value, 10) || 0))
    onChange({ ...sheet, stats: { ...(sheet.stats || {}), [key]: n } })
  }
  const setBond = (key, value) => {
    const n = Math.max(0, Math.min(100, Number.parseInt(value, 10) || 0))
    onChange({
      ...sheet,
      bonds: { ...BOND_DEFAULTS, ...(sheet.bonds || {}), [key]: n },
    })
  }
  return (
    <div className="flex flex-col gap-2 rounded-lg border border-zinc-700 bg-zinc-900/60 p-2">
      <label className="flex items-center gap-2">
        <span className="text-[10px] font-semibold uppercase tracking-wide text-zinc-500">Status</span>
        <select
          value={sheet.status || ''}
          onChange={(e) => set('status', e.target.value)}
          className={inputCls}
        >
          <option value="">unknown</option>
          <option value="alive">alive</option>
          <option value="injured">injured</option>
          <option value="missing">missing</option>
          <option value="dead">dead</option>
        </select>
      </label>
      {SHEET_FIELDS.map((key) => (
        <label key={key} className="flex flex-col gap-1">
          <span className="flex items-center justify-between text-[10px] font-semibold uppercase tracking-wide text-zinc-500">
            <span>{FIELD_LABELS[key] || key}</span>
            {REGEN_FIELDS.has(key) && (
              <button
                type="button"
                title={`Regenerate ${FIELD_LABELS[key] || key}`}
                onClick={(e) => {
                  e.preventDefault()
                  onRegenerateField?.(key)
                }}
                className="rounded border border-zinc-700 px-1 text-[10px] normal-case tracking-normal text-zinc-300 hover:bg-zinc-700"
              >
                ↻
              </button>
            )}
          </span>
          <textarea
            value={sheet[key] ?? ''}
            onChange={(e) => set(key, e.target.value)}
            rows={key === 'class' ? 1 : 2}
            className={`${inputCls} resize-y`}
          />
        </label>
      ))}
      <div className="min-w-0">
        <div className="flex items-center justify-between gap-2">
          <span className="text-[10px] font-semibold uppercase tracking-wide text-zinc-500">Stats</span>
          <button
            type="button"
            title="Rolar 4d6, descarta o menor"
            onClick={() => {
              const stats = { ...(sheet.stats || {}) }
              for (const key of STAT_KEYS) stats[key] = roll4d6DropLowest()
              onChange({ ...sheet, stats })
            }}
            className="h-7 min-w-7 rounded border border-zinc-700 px-1.5 text-xs text-zinc-200 hover:bg-zinc-700"
          >
            🎲
          </button>
        </div>
        <div className="mt-1 grid grid-cols-2 gap-1.5">
          {STAT_KEYS.map((key) => {
            const parsed = Number.parseInt(sheet.stats?.[key], 10)
            const shown = Number.isFinite(parsed) ? Math.max(1, Math.min(20, parsed)) : null
            const label = CORE_STATS.find(([k]) => k === key)?.[1] || key
            return (
              <div key={key} className="min-w-0">
                <span className="truncate text-[10px] uppercase text-zinc-500" title={key}>{label}</span>
                <div className="flex items-center gap-0.5">
                  <button
                    type="button"
                    aria-label={`Diminuir ${key}`}
                    disabled={shown == null || shown <= 1}
                    onClick={() => setStat(key, String(shown - 1))}
                    className="h-7 w-7 shrink-0 rounded border border-zinc-700 text-sm leading-none text-zinc-100 hover:bg-zinc-700 disabled:opacity-40"
                  >
                    −
                  </button>
                  <span className="w-6 shrink-0 text-center text-xs tabular-nums text-zinc-100">
                    {shown ?? '—'}
                  </span>
                  <button
                    type="button"
                    aria-label={`Aumentar ${key}`}
                    disabled={shown != null && shown >= 20}
                    onClick={() => setStat(key, String(shown == null ? 1 : shown + 1))}
                    className="h-7 w-7 shrink-0 rounded border border-zinc-700 text-sm leading-none text-zinc-100 hover:bg-zinc-700 disabled:opacity-40"
                  >
                    +
                  </button>
                </div>
              </div>
            )
          })}
        </div>
      </div>
      <div className="min-w-0">
        <span className="text-[10px] font-semibold uppercase tracking-wide text-zinc-500">
          Bonds
        </span>
        <p className="text-[10px] text-zinc-600">Updated each turn from what you do.</p>
        <div className="mt-1 grid grid-cols-1 gap-1">
          {BOND_KEYS.map((key) => (
            <label key={key} className="flex min-w-0 items-center gap-2">
              <span className="w-16 shrink-0 text-[10px] uppercase text-zinc-500" title={key}>
                {BOND_LABELS[key]}
              </span>
              <input
                type="number" inputMode="numeric" min={0} max={100} size={3}
                title={key}
                value={sheet.bonds?.[key] ?? BOND_DEFAULTS[key]}
                onChange={(e) => setBond(key, e.target.value)}
                className={numberCls}
              />
            </label>
          ))}
        </div>
      </div>
    </div>
  )
}

// Linha de um NPC: retrato + campos editáveis inline + ações.
function NpcRow({
  card, job, adventureId, onSave, onRegenerate, onEnrich, onDelete,
  onRegenerateField, onRegenerateAll, onUploadPhoto, onDeleteReference,
  onDeletePortrait, onPortraitFromPhoto, saving, enriching, regenerating,
}) {
  const toast = useToast()
  const [form, setForm] = useState({
    title: card.title ?? '',
    entry: card.entry ?? '',
    portrait_prompt: card.portrait_prompt ?? '',
    voice: card.voice ?? '',
    sheet: parseSheet(card.sheet),
  })
  const [sheetOpen, setSheetOpen] = useState(false)
  const [memories, setMemories] = useState([])
  const [previewing, setPreviewing] = useState(false)
  const [photoMode, setPhotoMode] = useState('reference')
  const previewAudioRef = useRef(null)
  const fileRef = useRef(null)
  const referencePath = form.sheet?.reference?.image_path || ''
  const life = form.sheet?.life || {}

  useEffect(() => {
    let cancelled = false
    api.listNpcMemories(adventureId, card.id)
      .then((data) => {
        if (!cancelled) setMemories((data?.memories || []).slice(0, 3))
      })
      .catch(() => { if (!cancelled) setMemories([]) })
    return () => { cancelled = true }
  }, [adventureId, card.id, card.updated_at, card.sheet])

  // Para o preview de voz pendurado se a linha desmontar no meio do play.
  useEffect(() => {
    return () => {
      if (previewAudioRef.current) {
        previewAudioRef.current.onended = null
        previewAudioRef.current.pause()
        URL.revokeObjectURL(previewAudioRef.current.src)
      }
    }
  }, [])

  // Preview TTS (v0.4.0): sintetiza uma frase curta do NPC com a voz do card.
  const previewVoice = async () => {
    if (previewing || form.voice === '') return
    setPreviewing(true)
    try {
      const line = previewText(form)
      const blob = await api.voice.tts(line, form.voice, {
        emotion: emotionForLine(line),
        adventureId,
      })
      const url = URL.createObjectURL(blob)
      const audio = new Audio(url)
      previewAudioRef.current = audio
      audio.onended = () => {
        URL.revokeObjectURL(url)
        previewAudioRef.current = null
      }
      await audio.play()
    } catch (e) {
      toast(e.message)
    } finally {
      setPreviewing(false)
    }
  }

  // Recria o form quando o card muda no backend (ex.: após salvar/reload).
  useEffect(() => {
    setForm({
      title: card.title ?? '',
      entry: card.entry ?? '',
      portrait_prompt: card.portrait_prompt ?? '',
      voice: card.voice ?? '',
      sheet: parseSheet(card.sheet),
    })
  }, [card])

  const dirty =
    form.title !== (card.title ?? '') ||
    form.entry !== (card.entry ?? '') ||
    form.portrait_prompt !== (card.portrait_prompt ?? '') ||
    form.voice !== (card.voice ?? '') ||
    JSON.stringify(form.sheet) !== JSON.stringify(parseSheet(card.sheet))

  const inputCls =
    'w-full rounded-lg border border-zinc-700 bg-zinc-800 px-3 py-2 text-sm outline-none focus:border-emerald-600'

  return (
    <li className="min-w-0 rounded-lg border border-zinc-800 bg-zinc-800/50 p-3">
      <div className="flex min-w-0 gap-3">
        <PortraitThumb card={card} activeJob={job && (job.status === 'pending' || job.status === 'running') ? job : null} />
        <div className="flex min-w-0 flex-1 flex-col gap-2">
          <input
            value={form.title}
            onChange={(e) => setForm({ ...form, title: e.target.value })}
            placeholder="NPC name"
            className={inputCls}
          />
          <textarea
            value={form.entry}
            onChange={(e) => setForm({ ...form, entry: e.target.value })}
            rows={3}
            placeholder="NPC description"
            className={`${inputCls} resize-y`}
          />
          <label className="flex flex-col gap-1">
            <span className="flex items-center justify-between text-[10px] font-semibold uppercase tracking-wide text-zinc-500">
              <span>Portrait prompt</span>
              <button
                type="button"
                title="Regenerate portrait prompt"
                onClick={() => onRegenerateField?.(card, 'portrait_prompt')}
                className="rounded border border-zinc-700 px-1 text-[10px] normal-case tracking-normal text-zinc-300 hover:bg-zinc-700"
              >
                ↻
              </button>
            </span>
            <textarea
              value={form.portrait_prompt}
              onChange={(e) => setForm({ ...form, portrait_prompt: e.target.value })}
              rows={2}
              placeholder="leave empty and the AI writes one from the description"
              className={`${inputCls} resize-y`}
            />
          </label>
          <label className="flex items-center gap-2 text-sm text-zinc-300">
            <span className="text-xs font-semibold uppercase tracking-wide text-zinc-500">Voice</span>
            <select
              value={form.voice}
              onChange={(e) => setForm({ ...form, voice: e.target.value })}
              className="rounded-lg border border-zinc-700 bg-zinc-800 px-2 py-1 text-sm outline-none focus:border-emerald-600"
            >
              {VOICES.map((v) => (
                <option key={v} value={v}>{v === '' ? 'No voice' : v}</option>
              ))}
            </select>
            <button
              onClick={previewVoice}
              disabled={form.voice === '' || previewing}
              title={form.voice === '' ? 'Pick a voice to preview' : `Preview voice ${form.voice}`}
              className="rounded-md border border-zinc-700 px-2 py-1 text-xs text-zinc-300 hover:bg-zinc-700 disabled:cursor-not-allowed disabled:opacity-40"
            >
              {previewing ? '⏳' : '▶'}
            </button>
          </label>
          <button
            onClick={() => setSheetOpen((o) => !o)}
            className="flex items-center gap-1 text-left text-xs font-semibold uppercase tracking-wide text-zinc-500 hover:text-zinc-300"
          >
            <span>{sheetOpen ? '▾' : '▸'}</span>
            <span>Character sheet</span>
            {sheetHasContent(parseSheet(card.sheet)) && (
              <span className="h-1.5 w-1.5 rounded-full bg-emerald-500" title="Sheet filled" />
            )}
          </button>
          {sheetOpen && (
            <SheetPanel
              sheet={form.sheet}
              onChange={(s) => setForm({ ...form, sheet: s })}
              onRegenerateField={(field) => onRegenerateField?.(card, field)}
            />
          )}
        </div>
      </div>
      <div className="mt-2 min-w-0 break-words rounded-lg border border-zinc-800 bg-zinc-900/40 p-2 text-xs text-zinc-400">
        <p className="text-[10px] font-semibold uppercase tracking-wide text-zinc-500">State</p>
        <p>mood: {life.mood || '—'}</p>
        <p>activity: {life.activity || '—'}</p>
        <p>day: {life.day ?? 0}</p>
        {memories.length > 0 && (
          <ul className="mt-1 list-disc pl-4">
            {memories.map((memory) => (
              <li key={memory.id}>{memory.text}</li>
            ))}
          </ul>
        )}
      </div>
      <div className="mt-2 flex w-full min-w-0 flex-wrap items-center gap-1">
        <input
          ref={fileRef}
          type="file"
          accept="image/jpeg,image/png,image/webp"
          className="hidden"
          onChange={(e) => {
            const file = e.target.files?.[0]
            e.target.value = ''
            if (file) onUploadPhoto?.(card, file, photoMode)
          }}
        />
        <select
          value={photoMode}
          onChange={(e) => setPhotoMode(e.target.value)}
          className="max-w-full min-w-0 rounded-md border border-zinc-700 bg-zinc-800 px-2 py-1 text-xs text-zinc-300 outline-none focus:border-emerald-600"
        >
          <option value="reference">Reference (fictional NPC)</option>
          <option value="real_person">Real person</option>
        </select>
        <button
          type="button"
          onClick={() => fileRef.current?.click()}
          className="rounded-md border border-zinc-700 px-2 py-1 text-xs text-zinc-300 hover:bg-zinc-700"
        >
          Upload photo
        </button>
        <button
          type="button"
          onClick={() => onDeleteReference?.(card)}
          className="rounded-md border border-zinc-700 px-2 py-1 text-xs text-zinc-300 hover:bg-zinc-700"
        >
          Remove photo
        </button>
        <button
          type="button"
          onClick={() => onDeletePortrait?.(card)}
          className="rounded-md border border-red-900 px-2 py-1 text-xs text-red-400 hover:bg-red-950"
        >
          Delete portrait
        </button>
        <button
          type="button"
          disabled={!referencePath}
          onClick={() => onPortraitFromPhoto?.(card)}
          title={referencePath ? 'Generate a new portrait from the uploaded photo' : 'Upload a photo first'}
          className="rounded-md border border-emerald-800 px-2 py-1 text-xs text-emerald-300 hover:bg-emerald-950 disabled:cursor-not-allowed disabled:opacity-40"
        >
          New image from photo
        </button>
      </div>
      <div className="mt-2 flex w-full min-w-0 flex-wrap justify-end gap-1">
        <button
          onClick={() => onSave(card, form)}
          disabled={!dirty || saving}
          className="rounded-md bg-emerald-700 px-2 py-1 text-xs font-medium text-white hover:bg-emerald-600 disabled:opacity-50"
        >
          {saving ? 'Saving…' : 'Save'}
        </button>
        <button
          onClick={() => onEnrich(card)}
          disabled={enriching || regenerating}
          title="AI fills the character sheet and a detailed portrait prompt from the description"
          className="rounded-md border border-emerald-800 px-2 py-1 text-xs text-emerald-300 hover:bg-emerald-950 disabled:opacity-50"
        >
          {enriching ? 'Enriching…' : '✨ Enrich'}
        </button>
        <button
          onClick={() => onRegenerateAll?.(card)}
          disabled={regenerating}
          title="Overwrite personality, humor, speech, goals, background, lifestyle, habits, likes and sentiments"
          className="rounded-md border border-zinc-700 px-2 py-1 text-xs text-zinc-300 hover:bg-zinc-700 disabled:opacity-50"
        >
          {regenerating ? 'Regenerating…' : '🎲 Regenerate all'}
        </button>
        <button
          onClick={() => onRegenerate(card)}
          disabled={!!job && (job.status === 'pending' || job.status === 'running')}
          title="New picture of this NPC, starting from the saved face"
          className="rounded-md border border-zinc-700 px-2 py-1 text-xs text-zinc-300 hover:bg-zinc-700 disabled:opacity-50"
        >
          Regenerate portrait
        </button>
        <button
          onClick={() => onDelete(card)}
          className="rounded-md border border-red-900 px-2 py-1 text-xs text-red-400 hover:bg-red-950"
        >
          Delete
        </button>
      </div>
    </li>
  )
}

// Aba NPCs: roster dos cards type==='character' da aventura, com retratos
// gerados pela fila assíncrona de jobs (v0.3.0).
export default function NpcsTab({ adventure }) {
  const toast = useToast()
  const [cards, setCards] = useState(null)
  const [jobs, setJobs] = useState([])
  const [savingId, setSavingId] = useState(null)
  const [enrichingId, setEnrichingId] = useState(null)
  const [regeneratingId, setRegeneratingId] = useState(null)
  const [dismissed, setDismissed] = useState(() => new Set())
  const prevStatuses = useRef({})

  const reload = useCallback(async () => {
    try {
      setCards(await api.listCards(adventure.id))
    } catch (e) {
      toast(e.message)
    }
  }, [adventure.id, toast])

  useEffect(() => {
    reload()
  }, [reload])

  // Retrato, ficha e CRUD chegam pelo bus do Play. O poll abaixo fica de fallback.
  useEffect(() => {
    const onLive = (ev) => {
      const detail = ev.detail || {}
      if (detail.adventureId !== adventure.id) return
      if (detail.type !== 'portrait_ready'
        && detail.type !== 'card_assets_ready'
        && detail.type !== 'card_changed') return
      reload()
    }
    window.addEventListener('aidungeon-live', onLive)
    return () => window.removeEventListener('aidungeon-live', onLive)
  }, [adventure.id, reload])

  // Fallback: fila de jobs. Retrato pronto também recarrega se o evento SSE faltar.
  // npc_day_tick não tem evento próprio — continua por aqui.
  const fetchJobs = useCallback(async () => {
    try {
      const list = await api.listJobs(adventure.id)
      setJobs(list)
      let portraitDone = false
      for (const j of list) {
        if (j.type === 'character_portrait' || j.type === 'npc_day_tick') {
          const prev = prevStatuses.current[j.id]
          if (prev && prev !== 'done' && j.status === 'done') portraitDone = true
        }
        prevStatuses.current[j.id] = j.status
      }
      if (portraitDone) reload()
    } catch (e) {
      toast(e.message)
    }
  }, [adventure.id, reload, toast])

  // Poll inicial ao montar.
  useEffect(() => {
    fetchJobs()
  }, [fetchJobs])

  // Enquanto houver job pending/running, repolla a cada 3s; para quando
  // a fila esvaziar (o intervalo é recriado/destruído conforme hasActive).
  const hasActive = jobs.some((j) => j.status === 'pending' || j.status === 'running')
  useEffect(() => {
    if (!hasActive) return undefined
    const t = setInterval(fetchJobs, 3000)
    return () => clearInterval(t)
  }, [hasActive, fetchJobs])

  const [searchQuery, setSearchQuery] = useState('')
  const npcs = (cards || []).filter((c) => {
    if (c.type !== 'character') return false
    if (!searchQuery.trim()) return true
    const q = searchQuery.toLowerCase()
    return (c.title || '').toLowerCase().includes(q) || (c.entry || '').toLowerCase().includes(q)
  })

  // Último job (qualquer status) de cada NPC, para overlay/erro/disable.
  const latestJobFor = (cardId) => {
    let best = null
    for (const j of jobs) {
      if (j.type !== 'character_portrait' || j.ref_id !== cardId) continue
      if (!best || String(j.created_at) > String(best.created_at)) best = j
    }
    return best
  }

  const addNpc = async () => {
    const name = window.prompt('NPC name?')
    if (name === null) return
    try {
      await api.createCard(adventure.id, { ...EMPTY_CARD, title: name })
      toast('NPC created.', 'ok')
      reload()
    } catch (e) {
      toast(e.message)
    }
  }

  const simulateDay = async () => {
    try {
      await api.simulateDay(adventure.id)
      toast('Day queued.', 'ok')
      fetchJobs()
    } catch (e) {
      toast(e.message)
    }
  }

  const generateAll = async () => {
    try {
      const { jobs: queued } = await api.generateAllPortraits(adventure.id)
      toast(queued.length
        ? `${queued.length} portrait job(s) queued.`
        : 'Every NPC already has a saved portrait.', 'ok')
      fetchJobs()
    } catch (e) {
      toast(e.message)
    }
  }

  const save = async (card, form) => {
    // PATCH só dos campos alterados (omitidos são preservados pelo backend).
    const patch = {}
    if (form.title !== (card.title ?? '')) patch.title = form.title
    if (form.entry !== (card.entry ?? '')) patch.entry = form.entry
    if (form.portrait_prompt !== (card.portrait_prompt ?? '')) patch.portrait_prompt = form.portrait_prompt
    if (form.voice !== (card.voice ?? '')) patch.voice = form.voice
    if (JSON.stringify(form.sheet) !== JSON.stringify(parseSheet(card.sheet))) patch.sheet = form.sheet
    if (Object.keys(patch).length === 0) return
    setSavingId(card.id)
    try {
      await api.updateCard(adventure.id, card.id, patch)
      toast('NPC saved.', 'ok')
      reload()
    } catch (e) {
      toast(e.message)
    } finally {
      setSavingId(null)
    }
  }

  // v0.5.0: LLM preenche a ficha + portrait_prompt (síncrono) — o card
  // atualizado vem na resposta e substitui o item local, sem reload.
  const enrich = async (card) => {
    setEnrichingId(card.id)
    try {
      const updated = await api.enrichCard(adventure.id, card.id)
      setCards((cs) => cs.map((c) => (c.id === card.id ? updated : c)))
      toast('Sheet enriched.', 'ok')
    } catch (e) {
      toast(e.message)
    } finally {
      setEnrichingId(null)
    }
  }

  const applyRegenerated = (cardId, data) => {
    if (data?.card) setCards((cs) => cs.map((c) => (c.id === cardId ? data.card : c)))
  }

  const regenerateFields = async (card, field) => {
    setRegeneratingId(card.id)
    try {
      const data = await api.regenerateCard(adventure.id, card.id, { scope: 'field', field })
      applyRegenerated(card.id, data)
      toast('Field regenerated.', 'ok')
    } catch (e) {
      toast(e.message)
    } finally {
      setRegeneratingId(null)
    }
  }

  const regenerateAll = async (card) => {
    if (!window.confirm('Regenerate all character fields? This overwrites the current values.')) return
    setRegeneratingId(card.id)
    try {
      const data = await api.regenerateCard(adventure.id, card.id, { scope: 'all', overwrite: true })
      applyRegenerated(card.id, data)
      toast('Character fields regenerated.', 'ok')
    } catch (e) {
      toast(e.message)
    } finally {
      setRegeneratingId(null)
    }
  }

  const regenerate = async (card) => {
    try {
      await api.regeneratePortrait(adventure.id, card.id)
      toast('Portrait job queued.', 'ok')
      fetchJobs()
    } catch (e) {
      toast(e.message)
    }
  }

  const uploadPhoto = async (card, file, mode) => {
    try {
      const updated = await api.uploadReference(adventure.id, card.id, file, mode)
      setCards((cs) => cs.map((c) => (c.id === card.id ? updated : c)))
      toast('Photo saved.', 'ok')
    } catch (e) {
      toast(e.message)
    }
  }

  const deleteReference = async (card) => {
    try {
      const updated = await api.deleteReference(adventure.id, card.id)
      setCards((cs) => cs.map((c) => (c.id === card.id ? updated : c)))
      toast('Photo removed.', 'ok')
    } catch (e) {
      toast(e.message)
    }
  }

  const deletePortrait = async (card) => {
    if (!window.confirm('Delete this portrait?')) return
    try {
      await api.deletePortrait(adventure.id, card.id)
      toast('Portrait deleted.', 'ok')
      reload()
    } catch (e) {
      toast(e.message)
    }
  }

  const portraitFromPhoto = async (card) => {
    try {
      await api.regeneratePortrait(adventure.id, card.id, { source: 'reference' })
      toast('Portrait job queued.', 'ok')
      fetchJobs()
    } catch (e) {
      toast(e.message)
    }
  }

  const remove = async (card) => {
    if (!window.confirm(`Delete NPC "${card.title || 'Untitled'}"?`)) return
    try {
      await api.deleteCard(adventure.id, card.id)
      toast('NPC deleted.', 'ok')
      reload()
    } catch (e) {
      toast(e.message)
    }
  }

  const dismissError = (jobId) => {
    setDismissed((s) => new Set([...s, jobId]))
  }

  if (cards === null) return <p className="text-sm text-zinc-500">Loading…</p>

  return (
    <div>
      <div className="mb-3 flex items-center justify-between">
        <h3 className="text-sm font-semibold uppercase tracking-wide text-zinc-400">
          NPCs ({npcs.length})
        </h3>
        <div className="flex gap-1">
          <button
            onClick={addNpc}
            className="rounded-md bg-emerald-700 px-3 py-1.5 text-sm font-medium text-white hover:bg-emerald-600"
          >
            + Add NPC
          </button>
          <button
            onClick={simulateDay}
            title="Invent one off-screen day for every NPC"
            className="rounded-md border border-zinc-700 px-3 py-1.5 text-sm text-zinc-300 hover:bg-zinc-700"
          >
            ⏩ Simulate a day
          </button>
          <button
            onClick={generateAll}
            title="Only NPCs that don't have a portrait yet. Saved faces stay."
            className="rounded-md border border-zinc-700 px-3 py-1.5 text-sm text-zinc-300 hover:bg-zinc-700"
          >
            Generate missing portraits
          </button>
        </div>
      </div>

      <div className="mb-3">
        <input
          type="text"
          value={searchQuery}
          onChange={(e) => setSearchQuery(e.target.value)}
          placeholder="Search NPCs by name or description…"
          className="w-full rounded-xl border border-subtle bg-surface px-3 py-1.5 text-xs text-primary placeholder-muted outline-none focus:border-accent-pri transition"
        />
      </div>


      {npcs.length === 0 && (
        <p className="rounded-lg border border-dashed border-zinc-700 p-4 text-sm text-zinc-500">
          No NPCs yet. NPCs are story cards of type "character" — add one and
          generate its portrait.
        </p>
      )}

      <ul className="flex flex-col gap-2">
        {npcs.map((c) => {
          const job = latestJobFor(c.id)
          const showError = job && job.status === 'error' && !dismissed.has(job.id)
          return (
            <div key={c.id}>
              <NpcRow
                card={c}
                job={job}
                adventureId={adventure.id}
                saving={savingId === c.id}
                enriching={enrichingId === c.id}
                regenerating={regeneratingId === c.id}
                onSave={save}
                onRegenerate={regenerate}
                onEnrich={enrich}
                onRegenerateField={regenerateFields}
                onRegenerateAll={regenerateAll}
                onUploadPhoto={uploadPhoto}
                onDeleteReference={deleteReference}
                onDeletePortrait={deletePortrait}
                onPortraitFromPhoto={portraitFromPhoto}
                onDelete={remove}
              />
              {showError && (
                <div className="mt-1 flex items-start justify-between gap-2 rounded-lg border border-red-900 bg-red-950/60 px-3 py-2 text-xs text-red-300">
                  <span className="break-words">Portrait generation failed: {job.error}</span>
                  <button
                    onClick={() => dismissError(job.id)}
                    className="shrink-0 text-zinc-400 hover:text-zinc-100"
                    aria-label="Dismiss"
                  >
                    ✕
                  </button>
                </div>
              )}
            </div>
          )
        })}
      </ul>
    </div>
  )
}
