import { useCallback, useEffect, useRef, useState } from 'react'
import { api } from '../../api.js'
import { useToast } from '../Toast.jsx'

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

// Linha de um NPC: retrato + campos editáveis inline + ações.
function NpcRow({ card, job, onSave, onRegenerate, onDelete, saving }) {
  const toast = useToast()
  const [form, setForm] = useState({
    title: card.title ?? '',
    entry: card.entry ?? '',
    portrait_prompt: card.portrait_prompt ?? '',
    voice: card.voice ?? '',
  })
  const [previewing, setPreviewing] = useState(false)
  const previewAudioRef = useRef(null)

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
      const blob = await api.voice.tts(previewText(form), form.voice)
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
    })
  }, [card])

  const dirty =
    form.title !== (card.title ?? '') ||
    form.entry !== (card.entry ?? '') ||
    form.portrait_prompt !== (card.portrait_prompt ?? '') ||
    form.voice !== (card.voice ?? '')

  const inputCls =
    'w-full rounded-lg border border-zinc-700 bg-zinc-800 px-3 py-2 text-sm outline-none focus:border-emerald-600'

  return (
    <li className="rounded-lg border border-zinc-800 bg-zinc-800/50 p-3">
      <div className="flex gap-3">
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
          <textarea
            value={form.portrait_prompt}
            onChange={(e) => setForm({ ...form, portrait_prompt: e.target.value })}
            rows={2}
            placeholder="leave empty and the AI writes one from the description"
            className={`${inputCls} resize-y`}
          />
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
        </div>
      </div>
      <div className="mt-2 flex justify-end gap-1">
        <button
          onClick={() => onSave(card, form)}
          disabled={!dirty || saving}
          className="rounded-md bg-emerald-700 px-2 py-1 text-xs font-medium text-white hover:bg-emerald-600 disabled:opacity-50"
        >
          {saving ? 'Saving…' : 'Save'}
        </button>
        <button
          onClick={() => onRegenerate(card)}
          disabled={!!job && (job.status === 'pending' || job.status === 'running')}
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

  // Busca a fila de jobs e detecta transições p/ 'done' de retratos —
  // nesse caso recarrega os cards para o novo retrato aparecer ao vivo.
  const fetchJobs = useCallback(async () => {
    try {
      const list = await api.listJobs(adventure.id)
      setJobs(list)
      let portraitDone = false
      for (const j of list) {
        if (j.type === 'character_portrait') {
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

  const npcs = (cards || []).filter((c) => c.type === 'character')

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

  const generateAll = async () => {
    try {
      const { jobs: queued } = await api.generateAllPortraits(adventure.id)
      toast(`${queued.length} portrait job(s) queued.`, 'ok')
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

  const regenerate = async (card) => {
    try {
      await api.regeneratePortrait(adventure.id, card.id)
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
            onClick={generateAll}
            className="rounded-md border border-zinc-700 px-3 py-1.5 text-sm text-zinc-300 hover:bg-zinc-700"
          >
            Generate all portraits
          </button>
        </div>
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
                saving={savingId === c.id}
                onSave={save}
                onRegenerate={regenerate}
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
