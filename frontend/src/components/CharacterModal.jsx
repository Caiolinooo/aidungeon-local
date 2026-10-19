import { useEffect, useState } from 'react'
import { api } from '../api.js'
import Modal from './Modal.jsx'
import { useToast } from './Toast.jsx'

const STAT_SUGGESTIONS = ['For', 'Des', 'Con', 'Int', 'Sab', 'Car']

const inputCls =
  'w-full rounded-lg border border-zinc-700 bg-zinc-800 px-3 py-2 text-sm text-zinc-100 outline-none focus:border-emerald-600 disabled:opacity-60'
const labelCls = 'mb-1 block text-xs font-semibold uppercase tracking-wide text-zinc-500'

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

function statsToRows(stats) {
  if (!stats || typeof stats !== 'object' || Array.isArray(stats)) return []
  return Object.entries(stats).map(([key, value]) => ({
    key: String(key),
    value: value == null ? '' : String(value),
  }))
}

function rowsToStats(rows) {
  const stats = {}
  for (const row of rows) {
    const key = (row.key || '').trim()
    if (!key) continue
    const raw = String(row.value ?? '').trim()
    stats[key] = /^-?\d+$/.test(raw) ? Number(raw) : raw
  }
  return stats
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
    stats: statsToRows(sheet.stats),
  }
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
  const [stats, setStats] = useState([])
  const [saving, setSaving] = useState(false)
  const [formError, setFormError] = useState('')
  const [applied, setApplied] = useState(undefined)

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
    setStats(next.stats)
    setFormError('')
    setApplied(card ?? null)
  }, [card, loading])

  const patchStat = (index, fields) => {
    setStats((rows) => rows.map((row, i) => (i === index ? { ...row, ...fields } : row)))
  }

  const addStat = (key = '') => {
    const name = key.trim()
    if (name && stats.some((row) => row.key.trim().toLowerCase() === name.toLowerCase())) return
    setStats((rows) => [...rows, { key: name, value: '' }])
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
          appearance: appearance.trim(),
          body: body.trim(),
          clothing: clothing.trim(),
          status: status.trim(),
          notes: notes.trim(),
          class: classRole.trim(),
          stats: rowsToStats(stats),
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

  const busy = saving || loading
  const rulesText = (rules || '').trim()
  const formReady = !loading && applied === (card ?? null)

  return (
    <Modal title="🧙 Personagem" onClose={saving ? undefined : onClose} wide>
      <form onSubmit={save} className="flex flex-col gap-3">
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
              <span className={labelCls}>Status</span>
              <input
                value={status}
                onChange={(e) => setStatus(e.target.value)}
                placeholder="vivo, ferido, desaparecido…"
                disabled={busy}
                className={inputCls}
              />
            </label>

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

            <div>
              <div className="mb-1 flex items-center justify-between gap-2">
                <span className={labelCls}>Atributos</span>
                <button
                  type="button"
                  onClick={() => addStat('')}
                  disabled={busy}
                  className="rounded-lg border border-zinc-700 px-2 py-0.5 text-xs text-zinc-300 hover:bg-zinc-800 disabled:opacity-40"
                >
                  +
                </button>
              </div>
              <div className="mb-2 flex flex-wrap gap-1.5">
                {STAT_SUGGESTIONS.map((key) => (
                  <button
                    key={key}
                    type="button"
                    onClick={() => addStat(key)}
                    disabled={busy}
                    className="rounded-full border border-zinc-700 px-2 py-0.5 text-[11px] text-zinc-300 hover:bg-zinc-800 disabled:opacity-40"
                  >
                    + {key}
                  </button>
                ))}
              </div>
              {stats.length === 0 ? (
                <p className="text-xs text-zinc-500">Nenhum atributo. Use as sugestões ou +.</p>
              ) : (
                <ul className="flex flex-col gap-1.5">
                  {stats.map((row, i) => (
                    <li key={i} className="flex items-center gap-1.5">
                      <input
                        value={row.key}
                        onChange={(e) => patchStat(i, { key: e.target.value })}
                        placeholder="For"
                        aria-label={`Atributo ${i + 1}`}
                        disabled={busy}
                        className={`${inputCls} w-28 shrink-0`}
                      />
                      <input
                        value={row.value}
                        onChange={(e) => patchStat(i, { value: e.target.value })}
                        placeholder="10"
                        aria-label={`Valor do atributo ${i + 1}`}
                        disabled={busy}
                        className={`${inputCls} min-w-0 flex-1`}
                      />
                      <button
                        type="button"
                        onClick={() => setStats((rows) => rows.filter((_, j) => j !== i))}
                        disabled={busy}
                        aria-label={`Remover atributo ${row.key || i + 1}`}
                        className="shrink-0 rounded-lg px-2 py-1 text-sm text-zinc-400 hover:bg-zinc-800 hover:text-zinc-100 disabled:opacity-40"
                      >
                        ✕
                      </button>
                    </li>
                  ))}
                </ul>
              )}
            </div>

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

        {formError && <p className="text-sm text-red-300">{formError}</p>}

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
      </form>
    </Modal>
  )
}
