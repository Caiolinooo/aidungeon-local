import { useCallback, useEffect, useState, useMemo } from 'react'
import { api } from '../../api.js'
import { useToast } from '../Toast.jsx'
import Modal from '../Modal.jsx'
import TagInput from '../TagInput.jsx'
import {
  Search,
  Plus,
  Trash2,
  Pin,
  PinOff,
  CheckSquare,
  Square,
  Edit2,
  Filter,
  Check,
  ChevronDown,
  X,
} from 'lucide-react'

const EMPTY_FORM = { title: '', type: 'other', triggers: [], entry: '', pinned: false, use_for_character_creation: false }
const TYPE_HINTS = ['all', 'character', 'location', 'item', 'faction', 'concept', 'other']
const CARD_TYPES = ['character', 'location', 'item', 'faction', 'concept', 'other']
const STAT_KEYS = ['str', 'dex', 'con', 'int', 'wis', 'cha']
const STATUS_STYLE = {
  dead: 'bg-red-950/80 text-red-300 border border-red-800',
  missing: 'bg-amber-950/80 text-amber-200 border border-amber-800',
  injured: 'bg-orange-950/80 text-orange-200 border border-orange-800',
  alive: 'bg-emerald-950/80 text-emerald-300 border border-emerald-800',
}

function readSheet(card) {
  const raw = card?.sheet
  if (!raw) return null
  if (typeof raw === 'object') return raw
  try {
    const parsed = JSON.parse(raw)
    return parsed && typeof parsed === 'object' ? parsed : null
  } catch {
    return null
  }
}

export default function CardsTab({ adventure }) {
  const toast = useToast()
  const [cards, setCards] = useState(null)
  const [editing, setEditing] = useState(null) // null | 'new' | card
  const [form, setForm] = useState(EMPTY_FORM)
  const [saving, setSaving] = useState(false)

  // Search & Filter state
  const [searchQuery, setSearchQuery] = useState('')
  const [typeFilter, setTypeFilter] = useState('all')
  const [pinnedOnly, setPinnedOnly] = useState(false)

  // Selection & Bulk Actions
  const [selectedIds, setSelectedIds] = useState(new Set())
  const [bulkType, setBulkType] = useState('concept')
  const [bulkLoading, setBulkLoading] = useState(false)

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

  // Filtered Cards
  const filteredCards = useMemo(() => {
    if (!cards) return []
    return cards.filter((card) => {
      if (typeFilter !== 'all' && (card.type || 'other').toLowerCase() !== typeFilter.toLowerCase()) {
        return false
      }
      if (pinnedOnly && !card.pinned) {
        return false
      }
      if (searchQuery.trim()) {
        const q = searchQuery.toLowerCase()
        const titleMatch = (card.title || '').toLowerCase().includes(q)
        const entryMatch = (card.entry || '').toLowerCase().includes(q)
        const typeMatch = (card.type || '').toLowerCase().includes(q)
        const triggerMatch = (card.triggers || []).some((t) => t.toLowerCase().includes(q))
        if (!titleMatch && !entryMatch && !typeMatch && !triggerMatch) {
          return false
        }
      }
      return true
    })
  }, [cards, searchQuery, typeFilter, pinnedOnly])

  const toggleSelectCard = (id) => {
    setSelectedIds((prev) => {
      const next = new Set(prev)
      if (next.has(id)) next.delete(id)
      else next.add(id)
      return next
    })
  }

  const toggleSelectAll = () => {
    if (selectedIds.size === filteredCards.length && filteredCards.length > 0) {
      setSelectedIds(new Set())
    } else {
      setSelectedIds(new Set(filteredCards.map((c) => c.id)))
    }
  }

  // Bulk operations
  const handleBulkDelete = async () => {
    const count = selectedIds.size
    if (!count) return
    if (!window.confirm(`Delete ${count} selected story card${count > 1 ? 's' : ''}? This cannot be undone.`)) return
    setBulkLoading(true)
    try {
      await api.bulkCards(adventure.id, {
        action: 'delete',
        card_ids: Array.from(selectedIds),
      })
      toast(`Deleted ${count} card${count > 1 ? 's' : ''}.`, 'ok')
      setSelectedIds(new Set())
      reload()
    } catch (e) {
      toast(e.message)
    } finally {
      setBulkLoading(false)
    }
  }

  const handleBulkPin = async (pinned) => {
    const count = selectedIds.size
    if (!count) return
    setBulkLoading(true)
    try {
      await api.bulkCards(adventure.id, {
        action: pinned ? 'pin' : 'unpin',
        card_ids: Array.from(selectedIds),
      })
      toast(`${pinned ? 'Pinned' : 'Unpinned'} ${count} card${count > 1 ? 's' : ''}.`, 'ok')
      setSelectedIds(new Set())
      reload()
    } catch (e) {
      toast(e.message)
    } finally {
      setBulkLoading(false)
    }
  }

  const handleBulkRetype = async () => {
    const count = selectedIds.size
    if (!count) return
    setBulkLoading(true)
    try {
      await api.bulkCards(adventure.id, {
        action: 'retype',
        card_ids: Array.from(selectedIds),
        new_type: bulkType,
      })
      toast(`Changed type of ${count} card${count > 1 ? 's' : ''} to "${bulkType}".`, 'ok')
      setSelectedIds(new Set())
      reload()
    } catch (e) {
      toast(e.message)
    } finally {
      setBulkLoading(false)
    }
  }

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

  if (cards === null) return <p className="text-sm text-muted">Loading cards…</p>

  const isAllSelected = filteredCards.length > 0 && selectedIds.size === filteredCards.length

  return (
    <div className="space-y-4">
      {/* Top Header */}
      <div className="flex items-center justify-between gap-2">
        <h3 className="text-xs font-semibold uppercase tracking-wider text-muted">
          Story Cards ({cards.length})
        </h3>
        <button
          onClick={() => openEditor(null)}
          className="flex items-center gap-1.5 rounded-lg bg-accent-pri px-3 py-1.5 text-xs font-semibold text-white shadow hover:bg-accent-hov transition"
        >
          <Plus className="w-4 h-4" />
          <span>New Card</span>
        </button>
      </div>

      {/* Search Input */}
      <div className="relative">
        <Search className="w-4 h-4 absolute left-3 top-2.5 text-muted pointer-events-none" />
        <input
          type="text"
          value={searchQuery}
          onChange={(e) => setSearchQuery(e.target.value)}
          placeholder="Search cards by name, entry, triggers…"
          className="w-full rounded-xl border border-subtle bg-surface pl-9 pr-8 py-2 text-xs text-primary placeholder-muted outline-none focus:border-accent-pri transition"
        />
        {searchQuery && (
          <button
            onClick={() => setSearchQuery('')}
            className="absolute right-2.5 top-2.5 text-muted hover:text-primary"
          >
            <X className="w-3.5 h-3.5" />
          </button>
        )}
      </div>

      {/* Type Filter Pills & Pinned Toggle */}
      <div className="flex flex-wrap items-center gap-1.5">
        {TYPE_HINTS.map((t) => (
          <button
            key={t}
            onClick={() => setTypeFilter(t)}
            className={`rounded-lg px-2.5 py-1 text-[11px] font-medium capitalize transition ${
              typeFilter === t
                ? 'bg-accent-pri text-white shadow-sm'
                : 'border border-subtle bg-surface text-secondary hover:text-primary hover:bg-elevated'
            }`}
          >
            {t}
          </button>
        ))}

        <button
          onClick={() => setPinnedOnly(!pinnedOnly)}
          className={`flex items-center gap-1 rounded-lg px-2.5 py-1 text-[11px] font-medium transition ${
            pinnedOnly
              ? 'bg-amber-600 text-white shadow-sm'
              : 'border border-subtle bg-surface text-secondary hover:text-primary hover:bg-elevated'
          }`}
        >
          <Pin className="w-3 h-3" />
          <span>Pinned</span>
        </button>
      </div>

      {/* Bulk Action Toolbar */}
      {selectedIds.size > 0 && (
        <div className="rounded-xl border border-accent-pri/50 bg-elevated p-3 shadow-md animate-in slide-in-from-top-2">
          <div className="flex flex-wrap items-center justify-between gap-2">
            <div className="flex items-center gap-2">
              <span className="text-xs font-semibold text-primary">
                {selectedIds.size} selected
              </span>
              <button
                onClick={toggleSelectAll}
                className="text-[11px] text-accent-pri hover:underline"
              >
                {isAllSelected ? 'Deselect all' : 'Select all'}
              </button>
            </div>

            <div className="flex flex-wrap items-center gap-1.5">
              <button
                onClick={() => handleBulkPin(true)}
                disabled={bulkLoading}
                title="Pin selected cards"
                className="flex items-center gap-1 rounded-lg border border-subtle bg-surface px-2.5 py-1 text-xs text-secondary hover:text-primary hover:bg-elevated transition disabled:opacity-50"
              >
                <Pin className="w-3.5 h-3.5 text-amber-500" />
                <span>Pin</span>
              </button>

              <button
                onClick={() => handleBulkPin(false)}
                disabled={bulkLoading}
                title="Unpin selected cards"
                className="flex items-center gap-1 rounded-lg border border-subtle bg-surface px-2.5 py-1 text-xs text-secondary hover:text-primary hover:bg-elevated transition disabled:opacity-50"
              >
                <PinOff className="w-3.5 h-3.5 text-muted" />
                <span>Unpin</span>
              </button>

              {/* Retype Dropdown */}
              <div className="flex items-center border border-subtle rounded-lg bg-surface overflow-hidden">
                <select
                  value={bulkType}
                  onChange={(e) => setBulkType(e.target.value)}
                  className="bg-transparent px-2 py-1 text-xs text-primary outline-none cursor-pointer"
                >
                  {CARD_TYPES.map((t) => (
                    <option key={t} value={t} className="bg-surface text-primary">
                      {t}
                    </option>
                  ))}
                </select>
                <button
                  onClick={handleBulkRetype}
                  disabled={bulkLoading}
                  className="border-l border-subtle px-2 py-1 text-xs font-medium text-accent-pri hover:bg-elevated transition disabled:opacity-50"
                >
                  Set Type
                </button>
              </div>

              <button
                onClick={handleBulkDelete}
                disabled={bulkLoading}
                title="Delete selected cards"
                className="flex items-center gap-1 rounded-lg border border-red-900/60 bg-red-950/40 px-2.5 py-1 text-xs text-red-400 hover:bg-red-900/60 transition disabled:opacity-50"
              >
                <Trash2 className="w-3.5 h-3.5" />
                <span>Delete</span>
              </button>
            </div>
          </div>
        </div>
      )}

      {/* Empty State */}
      {filteredCards.length === 0 && (
        <div className="rounded-xl border border-dashed border-subtle p-6 text-center text-xs text-muted">
          {searchQuery || typeFilter !== 'all' || pinnedOnly ? (
            <p>No cards match the active search or filters.</p>
          ) : (
            <p>
              No story cards yet. Characters, locations, and lore encountered in your adventure will be saved here.
            </p>
          )}
        </div>
      )}

      {/* Cards List */}
      <ul className="flex flex-col gap-2.5">
        {filteredCards.map((c) => {
          const sheet = readSheet(c)
          const isSelected = selectedIds.has(c.id)
          const stats = sheet?.stats && typeof sheet.stats === 'object'
            ? STAT_KEYS.filter((key) => sheet.stats[key] != null).map((key) => `${key} ${sheet.stats[key]}`)
            : []

          return (
            <li
              key={c.id}
              className={`rounded-xl border transition p-3.5 ${
                isSelected
                  ? 'border-accent-pri bg-elevated/90 ring-1 ring-accent-pri'
                  : 'border-subtle bg-surface hover:bg-elevated/40'
              }`}
            >
              <div className="flex items-start gap-3">
                {/* Select Checkbox */}
                <button
                  type="button"
                  onClick={() => toggleSelectCard(c.id)}
                  aria-label={isSelected ? 'Deselect card' : 'Select card'}
                  className="mt-0.5 text-secondary hover:text-accent-pri transition shrink-0"
                >
                  {isSelected ? (
                    <CheckSquare className="w-4 h-4 text-accent-pri" />
                  ) : (
                    <Square className="w-4 h-4 text-muted" />
                  )}
                </button>

                {/* Card Content */}
                <div className="min-w-0 flex-1">
                  <div className="flex items-center gap-2">
                    <span className="truncate font-semibold text-primary text-sm">
                      {c.title || 'Untitled'}
                    </span>
                    {c.pinned && (
                      <span title="Pinned — always included in LLM context" className="text-amber-500">
                        <Pin className="w-3.5 h-3.5 fill-amber-500" />
                      </span>
                    )}
                  </div>

                  <div className="mt-1 flex flex-wrap items-center gap-1.5 text-xs text-muted">
                    <span className="rounded-md bg-elevated border border-subtle px-1.5 py-0.5 text-[10px] uppercase font-semibold text-secondary">
                      {c.type || 'other'}
                    </span>
                    {sheet?.status && (
                      <span className={`rounded-md px-1.5 py-0.5 text-[10px] uppercase font-semibold ${STATUS_STYLE[sheet.status] || 'bg-elevated'}`}>
                        {sheet.status}
                      </span>
                    )}
                    {c.triggers?.length > 0 && (
                      <span className="text-[11px] text-muted truncate">
                        triggers: {c.triggers.slice(0, 4).join(', ')}{c.triggers.length > 4 ? '…' : ''}
                      </span>
                    )}
                  </div>

                  {c.entry && (
                    <p className="mt-1.5 line-clamp-2 text-xs text-secondary leading-relaxed">
                      {c.entry}
                    </p>
                  )}
                  {sheet?.appearance && (
                    <p className="mt-1 line-clamp-1 text-[11px] text-muted italic">
                      {sheet.appearance}
                    </p>
                  )}
                  {stats.length > 0 && (
                    <p className="mt-1 text-[10px] uppercase tracking-wider text-muted font-mono">
                      {stats.join(' · ')}
                    </p>
                  )}
                </div>

                {/* Actions */}
                <div className="flex shrink-0 items-center gap-1">
                  <button
                    onClick={() => openEditor(c)}
                    title="Edit card"
                    className="rounded-lg border border-subtle p-1.5 text-secondary hover:text-primary hover:bg-elevated transition"
                  >
                    <Edit2 className="w-3.5 h-3.5" />
                  </button>
                  <button
                    onClick={() => remove(c)}
                    title="Delete card"
                    className="rounded-lg border border-red-900/60 p-1.5 text-red-400 hover:bg-red-950 transition"
                  >
                    <Trash2 className="w-3.5 h-3.5" />
                  </button>
                </div>
              </div>
            </li>
          )
        })}
      </ul>

      {/* Edit / Create Modal */}
      {editing && (
        <Modal
          title={editing === 'new' ? 'New Story Card' : `Edit: ${editing.title || 'Untitled'}`}
          onClose={() => setEditing(null)}
          wide
        >
          <div className="flex flex-col gap-4 text-primary">
            <label className="block">
              <span className="mb-1 block text-xs font-semibold uppercase tracking-wider text-muted">
                Title
              </span>
              <input
                value={form.title}
                onChange={(e) => setForm({ ...form, title: e.target.value })}
                placeholder="e.g. Iron Gate, Sir Gareth, Obsidian Citadel"
                className="w-full rounded-xl border border-subtle bg-surface px-3 py-2 text-sm text-primary outline-none focus:border-accent-pri transition"
              />
            </label>

            <div className="grid grid-cols-2 gap-3">
              <label className="block">
                <span className="mb-1 block text-xs font-semibold uppercase tracking-wider text-muted">
                  Type
                </span>
                <select
                  value={form.type}
                  onChange={(e) => setForm({ ...form, type: e.target.value })}
                  className="w-full rounded-xl border border-subtle bg-surface px-3 py-2 text-sm text-primary outline-none focus:border-accent-pri transition capitalize cursor-pointer"
                >
                  {CARD_TYPES.map((t) => (
                    <option key={t} value={t}>
                      {t}
                    </option>
                  ))}
                </select>
              </label>

              <div className="flex flex-col justify-end gap-2 pb-1">
                <label className="flex items-center gap-2 text-xs text-secondary cursor-pointer">
                  <input
                    type="checkbox"
                    checked={form.pinned}
                    onChange={(e) => setForm({ ...form, pinned: e.target.checked })}
                    className="accent-emerald-600 rounded"
                  />
                  <span>Pinned (always in LLM context)</span>
                </label>
                <label className="flex items-center gap-2 text-xs text-secondary cursor-pointer">
                  <input
                    type="checkbox"
                    checked={form.use_for_character_creation}
                    onChange={(e) => setForm({ ...form, use_for_character_creation: e.target.checked })}
                    className="accent-emerald-600 rounded"
                  />
                  <span>Use for character creation</span>
                </label>
              </div>
            </div>

            <label className="block">
              <span className="mb-1 block text-xs font-semibold uppercase tracking-wider text-muted">
                Triggers (keywords, comma-separated)
              </span>
              <TagInput
                value={form.triggers}
                onChange={(triggers) => setForm({ ...form, triggers })}
                placeholder="e.g. dragon, wyrm, smaug"
              />
            </label>

            <label className="block">
              <span className="mb-1 block text-xs font-semibold uppercase tracking-wider text-muted">
                Lore / Entry
              </span>
              <textarea
                value={form.entry}
                onChange={(e) => setForm({ ...form, entry: e.target.value })}
                rows={5}
                placeholder="Detailed description, historical facts, secrets, or behavior rules..."
                className="w-full resize-y rounded-xl border border-subtle bg-surface px-3 py-2 text-sm text-primary outline-none focus:border-accent-pri transition"
              />
            </label>

            <div className="mt-2 flex justify-end gap-2 border-t border-subtle pt-3">
              <button
                type="button"
                onClick={() => setEditing(null)}
                className="rounded-xl border border-subtle px-4 py-2 text-xs text-secondary hover:bg-elevated transition"
              >
                Cancel
              </button>
              <button
                type="button"
                onClick={save}
                disabled={saving}
                className="rounded-xl bg-accent-pri px-4 py-2 text-xs font-semibold text-white hover:bg-accent-hov shadow transition disabled:opacity-50"
              >
                {saving ? 'Saving…' : 'Save Card'}
              </button>
            </div>
          </div>
        </Modal>
      )}
    </div>
  )
}
