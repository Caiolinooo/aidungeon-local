import { useCallback, useEffect, useRef, useState } from 'react'
import { api } from '../../api.js'
import { emotionForLine } from '../../ttsText.js'
import { useToast } from '../Toast.jsx'
import VoiceCloneModal from '../VoiceCloneModal.jsx'
import { useConfirm } from '../../confirm.jsx'
import { CORE_STATS, roll4d6DropLowest } from '../../utils.js'

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
    <div className="relative h-24 w-24 shrink-0 overflow-hidden rounded-lg border border-subtle bg-surface">
      {url ? (
        <img src={url} alt={card.title || 'NPC portrait'} className="h-full w-full object-cover" />
      ) : (
        <div className="flex h-full w-full items-center justify-center text-2xl font-semibold text-muted">
          {initials(card.title)}
        </div>
      )}
      {activeJob && (
        <div className="absolute inset-0 flex flex-col items-center justify-center gap-1 bg-elevated/60">
          <div className="h-5 w-5 animate-spin rounded-full border-2 border-subtle border-t-accent-pri" />
          <span className="text-[10px] font-medium text-primary">
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
    Object.keys(sheet.bonds || {}).length > 0 ||
    Object.keys(sheet.body_state || {}).length > 0 ||
    Boolean(sheet.expression)
}

// Painel da ficha estruturada (v0.5.0): exibição + edição mínima dos campos.
function SheetPanel({ sheet, onChange, onRegenerateField }) {
  const inputCls =
    'w-full min-w-0 rounded-md border border-subtle bg-surface px-2 py-1 text-xs outline-none focus:border-accent-pri'
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
  const body = sheet.body_state && typeof sheet.body_state === 'object'
    ? sheet.body_state : {}
  const conditions = Array.isArray(body.conditions) ? body.conditions : []
  const setPain = (value) => {
    const n = Math.max(0, Math.min(10, Number.parseInt(value, 10) || 0))
    onChange({ ...sheet, body_state: { ...body, v: body.v || 1, pain: n, conditions } })
  }
  return (
    <div className="flex flex-col gap-2 rounded-lg border border-subtle bg-elevated/60 p-2">
      <label className="flex items-center gap-2">
        <span className="text-[10px] font-semibold uppercase tracking-wide text-muted">Estado</span>
        <select
          value={sheet.status || ''}
          onChange={(e) => set('status', e.target.value)}
          className={inputCls}
        >
          <option value="">desconhecido</option>
          <option value="alive">vivo</option>
          <option value="injured">ferido</option>
          <option value="missing">desaparecido</option>
          <option value="dead">morto</option>
        </select>
      </label>
      <div className="rounded-md border border-subtle bg-surface/60 p-2">
        <div className="flex items-center justify-between gap-2">
          <span className="text-[10px] font-semibold uppercase tracking-wide text-muted">
            Corpo e dor
          </span>
          <label className="flex items-center gap-1 text-[10px] text-muted">
            Dor
            <input
              type="number" inputMode="numeric" min={0} max={10} size={2}
              value={body.pain ?? 0}
              onChange={(e) => setPain(e.target.value)}
              className={numberCls}
            />
            /10
          </label>
        </div>
        {sheet.expression && (
          <p className="mt-1 text-[11px] text-secondary">Expressão: {sheet.expression}</p>
        )}
        {conditions.length === 0 ? (
          <p className="mt-1 text-[11px] text-muted">Sem ferimentos registrados.</p>
        ) : (
          <ul className="mt-1 flex flex-col gap-1">
            {conditions.map((condition, index) => (
              <li key={condition.id || index} className="rounded border border-subtle/70 px-1.5 py-1 text-[11px] text-secondary">
                <div className="flex flex-wrap items-center gap-1">
                  <span className="font-medium text-primary">
                    {condition.kind || 'condição'}
                  </span>
                  {condition.location ? <span>· {condition.location}</span> : null}
                  <span>· sev {condition.severity ?? '?'}</span>
                  {condition.permanent ? <span className="text-accent-pri">· permanente</span> : null}
                  {condition.active === false ? <span className="text-muted">· curada</span> : null}
                  {condition.portrait_visible ? <span className="text-accent-pri">· retrato</span> : null}
                  {condition.scene_visible && !condition.portrait_visible
                    ? <span className="text-muted">· cena</span> : null}
                </div>
                {condition.description ? <p>{condition.description}</p> : null}
              </li>
            ))}
          </ul>
        )}
      </div>
      {SHEET_FIELDS.map((key) => (
        <label key={key} className="flex flex-col gap-1">
          <span className="flex items-center justify-between text-[10px] font-semibold uppercase tracking-wide text-muted">
            <span>{FIELD_LABELS[key] || key}</span>
            {REGEN_FIELDS.has(key) && (
              <button
                type="button"
                title={`Regenerate ${FIELD_LABELS[key] || key}`}
                onClick={(e) => {
                  e.preventDefault()
                  onRegenerateField?.(key)
                }}
                className="rounded border border-subtle px-1 text-[10px] normal-case tracking-normal text-secondary hover:bg-elevated"
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
          <span className="text-[10px] font-semibold uppercase tracking-wide text-muted">Stats</span>
          <button
            type="button"
            title="Rolar 4d6, descarta o menor"
            onClick={() => {
              const stats = { ...(sheet.stats || {}) }
              for (const key of STAT_KEYS) stats[key] = roll4d6DropLowest()
              onChange({ ...sheet, stats })
            }}
            className="h-7 min-w-7 rounded border border-subtle px-1.5 text-xs text-primary hover:bg-elevated"
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
                <span className="truncate text-[10px] uppercase text-muted" title={key}>{label}</span>
                <div className="flex items-center gap-0.5">
                  <button
                    type="button"
                    aria-label={`Diminuir ${key}`}
                    disabled={shown == null || shown <= 1}
                    onClick={() => setStat(key, String(shown - 1))}
                    className="h-7 w-7 shrink-0 rounded border border-subtle text-sm leading-none text-primary hover:bg-elevated disabled:opacity-40"
                  >
                    −
                  </button>
                  <span className="w-6 shrink-0 text-center text-xs tabular-nums text-primary">
                    {shown ?? '—'}
                  </span>
                  <button
                    type="button"
                    aria-label={`Aumentar ${key}`}
                    disabled={shown != null && shown >= 20}
                    onClick={() => setStat(key, String(shown == null ? 1 : shown + 1))}
                    className="h-7 w-7 shrink-0 rounded border border-subtle text-sm leading-none text-primary hover:bg-elevated disabled:opacity-40"
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
        <span className="text-[10px] font-semibold uppercase tracking-wide text-muted">
          Bonds
        </span>
        <p className="text-[10px] text-muted">Atualizado a cada turno, a partir do que você faz.</p>
        <div className="mt-1 grid grid-cols-1 gap-1">
          {BOND_KEYS.map((key) => (
            <label key={key} className="flex min-w-0 items-center gap-2">
              <span className="w-16 shrink-0 text-[10px] uppercase text-muted" title={key}>
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
  clonedVoices, onVoiceSaved, onVoiceDeleted,
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
  const [cloneOpen, setCloneOpen] = useState(false)
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
    'w-full rounded-lg border border-subtle bg-surface px-3 py-2 text-sm outline-none focus:border-accent-pri'

  return (
    <li className="min-w-0 rounded-lg border border-subtle bg-surface/50 p-3">
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
            <span className="flex items-center justify-between text-[10px] font-semibold uppercase tracking-wide text-muted">
              <span>Portrait prompt</span>
              <button
                type="button"
                title="Regenerate portrait prompt"
                onClick={() => onRegenerateField?.(card, 'portrait_prompt')}
                className="rounded border border-subtle px-1 text-[10px] normal-case tracking-normal text-secondary hover:bg-elevated"
              >
                ↻
              </button>
            </span>
            <textarea
              value={form.portrait_prompt}
              onChange={(e) => setForm({ ...form, portrait_prompt: e.target.value })}
              rows={2}
              placeholder="deixe vazio e a IA escreve um a partir da descrição"
              className={`${inputCls} resize-y`}
            />
          </label>
          <label className="flex items-center gap-2 text-sm text-secondary">
            <span className="text-xs font-semibold uppercase tracking-wide text-muted">Voice</span>
            <select
              value={form.voice}
              onChange={(e) => setForm({ ...form, voice: e.target.value })}
              className="rounded-lg border border-subtle bg-surface px-2 py-1 text-sm outline-none focus:border-accent-pri"
            >
              <option value="">Sem voz</option>
              {['M1', 'M2', 'M3', 'F1', 'F2', 'F3'].map((v) => (
                <option key={v} value={v}>{v}</option>
              ))}
              {(clonedVoices || []).map((c) => (
                <option key={c.id} value={c.id}>{c.name}</option>
              ))}
              {form.voice && form.voice.startsWith('cv_') && !(clonedVoices || []).some((c) => c.id === form.voice) && (
                <option value={form.voice}>{form.voice}</option>
              )}
            </select>
            <button
              type="button"
              onClick={() => setCloneOpen(true)}
              className="rounded-lg border border-subtle px-2 py-1 text-xs"
            >
              Clonar
            </button>
            {cloneOpen && (
              <VoiceCloneModal
                open
                assign={{
                  kind: 'card',
                  adventureId,
                  cardId: card.id,
                  cardName: card.title || 'Personagem',
                }}
                onClose={(result) => {
                  if (result?.deleted) {
                    setForm((f) => (f.voice === result.deleted ? { ...f, voice: '' } : f))
                    onVoiceDeleted?.(result.deleted)
                    return
                  }
                  setCloneOpen(false)
                  if (!result?.id) return
                  setForm((f) => ({ ...f, voice: result.id }))
                  onVoiceSaved?.(result)
                }}
              />
            )}
            <button
              onClick={previewVoice}
              disabled={form.voice === '' || previewing}
              title={form.voice === '' ? 'Pick a voice to preview' : `Preview voice ${form.voice}`}
              className="rounded-md border border-subtle px-2 py-1 text-xs text-secondary hover:bg-elevated disabled:cursor-not-allowed disabled:opacity-40"
            >
              {previewing ? '⏳' : '▶'}
            </button>
          </label>
          <button
            onClick={() => setSheetOpen((o) => !o)}
            className="flex items-center gap-1 text-left text-xs font-semibold uppercase tracking-wide text-muted hover:text-secondary"
          >
            <span>{sheetOpen ? '▾' : '▸'}</span>
            <span>Character sheet</span>
            {sheetHasContent(parseSheet(card.sheet)) && (
              <span className="h-1.5 w-1.5 rounded-full bg-accent-500" title="Sheet filled" />
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
      <div className="mt-2 min-w-0 break-words rounded-lg border border-subtle bg-elevated/40 p-2 text-xs text-secondary">
        <p className="text-[10px] font-semibold uppercase tracking-wide text-muted">State</p>
        <p>mood: {life.mood || '—'}</p>
        <p>activity: {life.activity || '—'}</p>
        <p>day: {life.day ?? 0}</p>
        <p>pain: {form.sheet?.body_state?.pain ?? 0}/10</p>
        {form.sheet?.expression ? <p>expression: {form.sheet.expression}</p> : null}
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
          className="max-w-full min-w-0 rounded-md border border-subtle bg-surface px-2 py-1 text-xs text-secondary outline-none focus:border-accent-pri"
        >
          <option value="reference">Referência (NPC fictício)</option>
          <option value="real_person">Pessoa real</option>
        </select>
        <button
          type="button"
          onClick={() => fileRef.current?.click()}
          className="rounded-md border border-subtle px-2 py-1 text-xs text-secondary hover:bg-elevated"
        >
          Upload photo
        </button>
        <button
          type="button"
          onClick={() => onDeleteReference?.(card)}
          className="rounded-md border border-subtle px-2 py-1 text-xs text-secondary hover:bg-elevated"
        >
          Remove photo
        </button>
        <button
          type="button"
          onClick={() => onDeletePortrait?.(card)}
          className="rounded-md border border-danger-900 px-2 py-1 text-xs text-danger-400 hover:bg-danger-950"
        >
          Delete portrait
        </button>
        <button
          type="button"
          disabled={!referencePath}
          onClick={() => onPortraitFromPhoto?.(card)}
          title={referencePath ? 'Gerar um retrato novo a partir da foto enviada' : 'Upload a photo first'}
          className="rounded-md border border-accent-800 px-2 py-1 text-xs text-accent-pri hover:bg-accent-950 disabled:cursor-not-allowed disabled:opacity-40"
        >
          New image from photo
        </button>
      </div>
      <div className="mt-2 flex w-full min-w-0 flex-wrap justify-end gap-1">
        <button
          onClick={() => onSave(card, form)}
          disabled={!dirty || saving}
          className="rounded-md bg-accent-pri px-2 py-1 text-xs font-medium text-canvas hover:bg-accent-hov disabled:opacity-50"
        >
          {saving ? 'Salvando…' : 'Save'}
        </button>
        <button
          onClick={() => onEnrich(card)}
          disabled={enriching || regenerating}
          title="A IA preenche a ficha e um prompt detalhado de retrato a partir da descrição"
          className="rounded-md border border-accent-800 px-2 py-1 text-xs text-accent-pri hover:bg-accent-950 disabled:opacity-50"
        >
          {enriching ? 'Enriching…' : '✨ Enrich'}
        </button>
        <button
          onClick={() => onRegenerateAll?.(card)}
          disabled={regenerating}
          title="Overwrite personality, humor, speech, goals, background, lifestyle, habits, likes and sentiments"
          className="rounded-md border border-subtle px-2 py-1 text-xs text-secondary hover:bg-elevated disabled:opacity-50"
        >
          {regenerating ? 'Regenerating…' : '🎲 Regenerate all'}
        </button>
        <button
          onClick={() => onRegenerate(card)}
          disabled={!!job && (job.status === 'pending' || job.status === 'running')}
          title="Nova imagem deste NPC, a partir do rosto salvo"
          className="rounded-md border border-subtle px-2 py-1 text-xs text-secondary hover:bg-elevated disabled:opacity-50"
        >
          Regenerate portrait
        </button>
        <button
          onClick={() => onDelete(card)}
          className="rounded-md border border-danger-900 px-2 py-1 text-xs text-danger-400 hover:bg-danger-950"
        >
          Apagar
        </button>
      </div>
    </li>
  )
}

// Aba NPCs: roster dos cards type==='character' da aventura, com retratos
// gerados pela fila assíncrona de jobs (v0.3.0).
export default function NpcsTab({ adventure }) {
  const toast = useToast()
  const { ask, bar } = useConfirm()
  const [npcName, setNpcName] = useState('')
  const [namingNpc, setNamingNpc] = useState(false)
  const [cards, setCards] = useState(null)
  const [jobs, setJobs] = useState([])
  const [savingId, setSavingId] = useState(null)
  const [enrichingId, setEnrichingId] = useState(null)
  const [regeneratingId, setRegeneratingId] = useState(null)
  const [dismissed, setDismissed] = useState(() => new Set())
  const [clonedVoices, setClonedVoices] = useState([])
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

  useEffect(() => {
    api.voice.list().then((data) => setClonedVoices(data.cloned || [])).catch(() => setClonedVoices([]))
  }, [])

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

  const addNpc = async (name) => {
    const title = String(name || '').trim()
    if (!title) return
    setNamingNpc(false)
    try {
      await api.createCard(adventure.id, { ...EMPTY_CARD, title })
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
    if (!ask(`regen-all-${card.id}`, 'Regenerar todos os campos deste personagem? Os valores atuais são substituídos.')) return
    setRegeneratingId(card.id)
    try {
      const data = await api.regenerateCard(adventure.id, card.id, { scope: 'all', overwrite: true })
      applyRegenerated(card.id, data)
      toast('Campos do personagem regenerados.', 'ok')
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
    if (!ask(`portrait-${card.id}`, 'Apagar este retrato?')) return
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
    if (!ask(`npc-${card.id}`, `Apagar o NPC "${card.title || 'Sem título'}"?`)) return
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

  if (cards === null) return <p className="text-sm text-muted">Carregando…</p>

  return (
    <div>
      {bar}
      {namingNpc && (
        <div className="mb-2 flex flex-col gap-1">
          <input
            value={npcName}
            onChange={(e) => setNpcName(e.target.value)}
            placeholder="Nome do NPC"
            className="rounded-lg border border-subtle bg-surface px-2 py-1 text-sm text-primary outline-none focus:border-accent-pri"
          />
          <div className="flex gap-2">
            <button type="button" className="text-xs underline" onClick={() => addNpc(npcName)}>Confirmar</button>
            <button type="button" className="text-xs underline" onClick={() => setNamingNpc(false)}>Cancelar</button>
          </div>
        </div>
      )}
      <div className="mb-3 flex items-center justify-between">
        <h3 className="text-sm font-semibold uppercase tracking-wide text-secondary">
          NPCs ({npcs.length})
        </h3>
        <div className="flex gap-1">
          <button
            onClick={() => { setNpcName(''); setNamingNpc(true) }}
            className="rounded-md bg-accent-pri px-3 py-1.5 text-sm font-medium text-canvas hover:bg-accent-hov"
          >
            + Adicionar NPC
          </button>
          <button
            onClick={simulateDay}
            title="Invent one off-screen day for every NPC"
            className="rounded-md border border-subtle px-3 py-1.5 text-sm text-secondary hover:bg-elevated"
          >
            ⏩ Simulate a day
          </button>
          <button
            onClick={generateAll}
            title="Only NPCs that don't have a portrait yet. Saved faces stay."
            className="rounded-md border border-subtle px-3 py-1.5 text-sm text-secondary hover:bg-elevated"
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
          placeholder="Buscar NPCs por nome ou descrição…"
          className="w-full rounded-xl border border-subtle bg-surface px-3 py-1.5 text-xs text-primary placeholder-muted outline-none focus:border-accent-pri transition"
        />
      </div>


      {npcs.length === 0 && (
        <p className="rounded-lg border border-dashed border-subtle p-4 text-sm text-muted">
          No NPCs yet. NPCs are fichas of type "character" — add one and
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
                clonedVoices={clonedVoices}
                onVoiceSaved={(voice) => setClonedVoices((list) => (
                  list.some((c) => c.id === voice.id) ? list : [...list, { id: voice.id, name: voice.name }]
                ))}
                onVoiceDeleted={(id) => setClonedVoices((list) => list.filter((c) => c.id !== id))}
              />
              {showError && (
                <div className="mt-1 flex items-start justify-between gap-2 rounded-lg border border-danger-900 bg-danger-950/60 px-3 py-2 text-xs text-danger-300">
                  <span className="break-words">Portrait generation failed: {job.error}</span>
                  <button
                    onClick={() => dismissError(job.id)}
                    className="shrink-0 text-secondary hover:text-primary"
                    aria-label="Dispensar"
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
