import { useCallback, useEffect, useState } from 'react'
import { api } from '../../api.js'
import { useToast } from '../Toast.jsx'
import Modal from '../Modal.jsx'
import TagInput from '../TagInput.jsx'

const EMPTY_FORM = { title: '', type: 'other', triggers: [], entry: '', pinned: false, use_for_character_creation: false }
const TYPE_HINTS = ['character', 'location', 'item', 'faction', 'concept', 'other']

// Aba Story Cards: lista (pinned primeiro, ordenação do backend) + criar /
// editar / excluir via CRUD /api/adventures/:id/cards.
export default function CardsTab({ adventure }) {
  const toast = useToast()
  const [cards, setCards] = useState(null)
  const [editing, setEditing] = useState(null) // null | 'new' | card
  const [form, setForm] = useState(EMPTY_FORM)
  const [saving, setSaving] = useState(false)

  const reload = useCallback(async () => {
    try {
      setCards(await api.listCards(adventure.id))
    } catch (e) {
      toast(e.message)
      setCards([])
    }
  }, [adventure.id, toast])

  useEffect(() => {
    reload()
  }, [reload])

  const openEditor = (card) => {
    if (card) {
      setEditing(card)
      setForm({
        title: card.title ?? '',
        type: card.type ?? 'other',
        triggers: card.triggers ?? [],
        entry: card.entry ?? '',
        pinned: !!card.pinned,
        use_for_character_creation: !!card.use_for_character_creation,
      })
    } else {
      setEditing('new')
      setForm(EMPTY_FORM)
    }
  }

  const save = async () => {
    setSaving(true)
    try {
      if (editing === 'new') await api.createCard(adventure.id, form)
      else await api.updateCard(adventure.id, editing.id, form)
      setEditing(null)
      toast('Story card saved.', 'ok')
      reload()
    } catch (e) {
      toast(e.message)
    } finally {
      setSaving(false)
    }
  }

  const remove = async (card) => {
    if (!window.confirm(`Delete story card "${card.title || 'Untitled'}"?`)) return
    try {
      await api.deleteCard(adventure.id, card.id)
      toast('Story card deleted.', 'ok')
      reload()
    } catch (e) {
      toast(e.message)
    }
  }

  if (cards === null) return <p className="text-sm text-zinc-500">Loading…</p>

  return (
    <div>
      <div className="mb-3 flex items-center justify-between">
        <h3 className="text-sm font-semibold uppercase tracking-wide text-zinc-400">
          Story Cards ({cards.length})
        </h3>
        <button
          onClick={() => openEditor(null)}
          className="rounded-md bg-emerald-700 px-3 py-1.5 text-sm font-medium text-white hover:bg-emerald-600"
        >
          + New card
        </button>
      </div>

      {cards.length === 0 && (
        <p className="rounded-lg border border-dashed border-zinc-700 p-4 text-sm text-zinc-500">
          No story cards yet. Cards are injected into the AI context when one of their
          triggers appears in the recent story (or always, when pinned).
        </p>
      )}

      <ul className="flex flex-col gap-2">
        {cards.map((c) => (
          <li key={c.id} className="rounded-lg border border-zinc-800 bg-zinc-800/50 p-3">
            <div className="flex items-start justify-between gap-2">
              <div className="min-w-0">
                <div className="flex items-center gap-2">
                  <span className="truncate font-semibold text-zinc-100">{c.title || 'Untitled'}</span>
                  {c.pinned && <span title="Pinned — always in context">📌</span>}
                </div>
                <div className="mt-0.5 text-xs text-zinc-500">
                  <span className="rounded bg-zinc-700 px-1.5 py-0.5">{c.type || 'other'}</span>
                  {c.triggers?.length > 0 && (
                    <span className="ml-2">triggers: {c.triggers.slice(0, 4).join(', ')}{c.triggers.length > 4 ? '…' : ''}</span>
                  )}
                </div>
                {c.entry && (
                  <p className="mt-1 line-clamp-2 text-sm text-zinc-400">{c.entry}</p>
                )}
              </div>
              <div className="flex shrink-0 gap-1">
                <button
                  onClick={() => openEditor(c)}
                  className="rounded-md border border-zinc-700 px-2 py-1 text-xs text-zinc-300 hover:bg-zinc-700"
                >
                  Edit
                </button>
                <button
                  onClick={() => remove(c)}
                  className="rounded-md border border-red-900 px-2 py-1 text-xs text-red-400 hover:bg-red-950"
                >
                  Delete
                </button>
              </div>
            </div>
          </li>
        ))}
      </ul>

      {editing && (
        <Modal title={editing === 'new' ? 'New story card' : 'Edit story card'} onClose={() => setEditing(null)} wide>
          <div className="flex flex-col gap-3">
            <label className="block">
              <span className="mb-1 block text-xs font-semibold uppercase tracking-wide text-zinc-500">Title</span>
              <input
                value={form.title}
                onChange={(e) => setForm({ ...form, title: e.target.value })}
                className="w-full rounded-lg border border-zinc-700 bg-zinc-800 px-3 py-2 text-sm outline-none focus:border-emerald-600"
              />
            </label>
            <label className="block">
              <span className="mb-1 block text-xs font-semibold uppercase tracking-wide text-zinc-500">Type</span>
              <input
                value={form.type}
                onChange={(e) => setForm({ ...form, type: e.target.value })}
                list="card-type-hints"
                className="w-full rounded-lg border border-zinc-700 bg-zinc-800 px-3 py-2 text-sm outline-none focus:border-emerald-600"
              />
              <datalist id="card-type-hints">
                {TYPE_HINTS.map((t) => <option key={t} value={t} />)}
              </datalist>
            </label>
            <label className="block">
              <span className="mb-1 block text-xs font-semibold uppercase tracking-wide text-zinc-500">
                Triggers (keywords, case-insensitive)
              </span>
              <TagInput
                value={form.triggers}
                onChange={(triggers) => setForm({ ...form, triggers })}
                placeholder="e.g. Baldur's Gate"
              />
            </label>
            <label className="block">
              <span className="mb-1 block text-xs font-semibold uppercase tracking-wide text-zinc-500">Entry</span>
              <textarea
                value={form.entry}
                onChange={(e) => setForm({ ...form, entry: e.target.value })}
                rows={6}
                className="w-full resize-y rounded-lg border border-zinc-700 bg-zinc-800 px-3 py-2 text-sm outline-none focus:border-emerald-600"
              />
            </label>
            <div className="flex gap-5">
              <label className="flex items-center gap-2 text-sm text-zinc-300">
                <input
                  type="checkbox"
                  checked={form.pinned}
                  onChange={(e) => setForm({ ...form, pinned: e.target.checked })}
                  className="accent-emerald-600"
                />
                Pinned (always in context)
              </label>
              <label className="flex items-center gap-2 text-sm text-zinc-300">
                <input
                  type="checkbox"
                  checked={form.use_for_character_creation}
                  onChange={(e) => setForm({ ...form, use_for_character_creation: e.target.checked })}
                  className="accent-emerald-600"
                />
                Use for character creation
              </label>
            </div>
            <div className="mt-2 flex justify-end gap-2">
              <button
                onClick={() => setEditing(null)}
                className="rounded-lg border border-zinc-700 px-4 py-2 text-sm text-zinc-300 hover:bg-zinc-800"
              >
                Cancel
              </button>
              <button
                onClick={save}
                disabled={saving}
                className="rounded-lg bg-emerald-700 px-4 py-2 text-sm font-semibold text-white hover:bg-emerald-600 disabled:opacity-50"
              >
                {saving ? 'Saving…' : 'Save'}
              </button>
            </div>
          </div>
        </Modal>
      )}
    </div>
  )
}
