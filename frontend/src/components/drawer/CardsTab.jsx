import { useCallback, useEffect, useState, useMemo } from 'react'
import { useConfirm } from '../../confirm.jsx'
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
  RefreshCw,
} from 'lucide-react'

const EMPTY_FORM = {
  title: '',
  type: 'other',
  triggers: [],
  entry: '',
  pinned: false,
  use_for_character_creation: false,
  portrait_prompt: '',
  sheet: null,
}

function draftLanguage() {
  return (navigator.language || 'pt').slice(0, 2)
}
const TYPE_HINTS = ['all', 'character', 'location', 'item', 'faction', 'concept', 'other']
const CARD_TYPES = ['character', 'location', 'item', 'faction', 'concept', 'other']
const STAT_KEYS = ['str', 'dex', 'con', 'int', 'wis', 'cha']
const STATUS_STYLE = {
  dead: 'bg-red-950/80 text-red-300 border border-red-800',
  missing: 'bg-amber-950/80 text-amber-200 border border-amber-800',
  injured: 'bg-warn-950/80 text-warn-200 border border-warn-800',
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
  const [enriching, setEnriching] = useState(false)
  const [webOn, setWebOn] = useState(false)
  const [aiCreating, setAiCreating] = useState(false)

  // Search & Filter state
  const [searchQuery, setSearchQuery] = useState('')
  const [typeFilter, setTypeFilter] = useState('all')
  const [pinnedOnly, setPinnedOnly] = useState(false)

  // Selection & Bulk Actions
  const [selectedIds, setSelectedIds] = useState(new Set())
  const [bulkType, setBulkType] = useState('concept')
  const [bulkLoading, setBulkLoading] = useState(false)
  const [aiBox, setAiBox] = useState('')
  const [aiText, setAiText] = useState('')
  const { ask, bar } = useConfirm()

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
    if (!ask('bulk-delete', `Apagar ${count} ficha${count > 1 ? 's' : ''} selecionada${count > 1 ? 's' : ''}? Isso não volta.`)) return
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
        portrait_prompt: card.portrait_prompt ?? '',
        sheet: readSheet(card),
      })
    } else {
      setEditing('new')
      setForm(EMPTY_FORM)
    }
  }

  const cardPayload = (source) => {
    const payload = {
      title: source.title,
      type: source.type,
      triggers: source.triggers,
      entry: source.entry,
      pinned: source.pinned,
      use_for_character_creation: source.use_for_character_creation,
      portrait_prompt: source.portrait_prompt ?? '',
    }
    // StoryCardUpsert.sheet is a dict. The list endpoint stores it as a JSON string.
    const sheet = readSheet({ sheet: source.sheet })
    if (sheet) payload.sheet = sheet
    return payload
  }

  const save = async () => {
    setSaving(true)
    try {
      const payload = cardPayload(form)
      if (editing === 'new') await api.createCard(adventure.id, payload)
      else await api.updateCard(adventure.id, editing.id, payload)
      setEditing(null)
      toast('Ficha salva.', 'ok')
      reload()
    } catch (e) {
      toast(e.message)
    } finally {
      setSaving(false)
    }
  }

  const enrichWithAi = async (instruction = '') => {
    setAiBox('')
    setEnriching(true)
    try {
      const r = await api.assistantCard({
        concept: form.title || 'card',
        card_type: form.type,
        adventure_id: adventure.id,
        current_card: form,
        instruction: String(instruction || '').trim() || undefined,
        web_search: webOn,
        language: draftLanguage(),
      })
      const card = r?.card
      if (!card) {
        toast('A IA não devolveu a ficha.')
        return
      }
      setForm((prev) => ({
        ...prev,
        title: card.title ?? prev.title,
        triggers: Array.isArray(card.triggers) ? card.triggers : prev.triggers,
        entry: card.entry ?? prev.entry,
        ...(CARD_TYPES.includes(card.type) ? { type: card.type } : {}),
        ...(card.sheet && typeof card.sheet === 'object' ? { sheet: card.sheet } : {}),
        ...(card.portrait_prompt ? { portrait_prompt: card.portrait_prompt } : {}),
      }))
      if (r.web_sources?.length) toast(`Enriquecido com ${r.web_sources.length} fontes da web.`, 'ok')
      else toast('Ficha enriquecida. Revise e salve.', 'ok')
    } catch (e) {
      toast(e.message)
    } finally {
      setEnriching(false)
    }
  }

  const createWithAi = async (concept = '') => {
    setAiBox('')
    const name = String(concept || '').trim()
    if (!name) {
      toast('O nome ou o conceito da ficha é obrigatório.')
      return
    }
    setAiCreating(true)
    try {
      const r = await api.assistantCard({
        concept: name,
        card_type: 'character',
        adventure_id: adventure.id,
        web_search: true,
        language: draftLanguage(),
      })
      if (!r?.card) {
        toast('A IA não devolveu a ficha.')
        return
      }
      const card = { ...r.card }
      delete card.web_sources
      await api.createCard(adventure.id, card)
      if (r.web_sources?.length) toast(`Enriquecido com ${r.web_sources.length} fontes da web.`, 'ok')
      else toast('Ficha criada.', 'ok')
      reload()
    } catch (e) {
      toast(e.message)
    } finally {
      setAiCreating(false)
    }
  }

  const remove = async (card) => {
    if (!ask(`delete-${card.id}`, `Apagar a ficha "${card.title || 'Sem título'}"?`)) return
    try {
      await api.deleteCard(adventure.id, card.id)
      toast('Ficha apagada.', 'ok')
      reload()
    } catch (e) {
      toast(e.message)
    }
  }

  if (cards === null) return <p className="text-sm text-muted">Carregando fichas…</p>

  const isAllSelected = filteredCards.length > 0 && selectedIds.size === filteredCards.length

  return (
    <div className="space-y-4">
      {bar}
      {aiBox === 'new' && (
        <div className="flex flex-col gap-1">
          <input
            value={aiText}
            onChange={(e) => setAiText(e.target.value)}
            placeholder="Nome ou conceito da ficha"
            className="w-full rounded-lg border border-subtle bg-surface px-2 py-1 text-xs text-primary outline-none focus:border-accent-pri"
          />
          <div className="flex gap-2">
            <button type="button" className="text-xs underline" onClick={() => createWithAi(aiText)}>Confirmar</button>
            <button type="button" className="text-xs underline" onClick={() => setAiBox('')}>Cancelar</button>
          </div>
        </div>
      )}
      {/* Top Header */}
      <div className="flex items-center justify-between gap-2">
        <h3 className="text-xs font-semibold uppercase tracking-wider text-muted">
          Fichas ({cards.length})
        </h3>
        <div className="flex items-center gap-2">
          <button
            type="button"
            onClick={() => { setAiText(''); setAiBox('new') }}
            disabled={aiCreating}
            className="flex items-center gap-1.5 rounded-lg border border-subtle bg-surface px-3 py-1.5 text-xs font-semibold text-secondary hover:text-primary hover:bg-elevated transition disabled:opacity-50"
          >
            <RefreshCw className={`w-3.5 h-3.5 ${aiCreating ? 'animate-spin' : 'hidden'}`} />
            <span>{aiCreating ? 'Criando…' : '🌐 Ficha com IA'}</span>
          </button>
          <button
            onClick={() => openEditor(null)}
            className="flex items-center gap-1.5 rounded-lg bg-accent-pri px-3 py-1.5 text-xs font-semibold text-canvas shadow hover:bg-accent-hov transition"
          >
            <Plus className="w-4 h-4" />
            <span>Nova ficha</span>
          </button>
        </div>
      </div>

      {/* Search Input */}
      <div className="relative">
        <Search className="w-4 h-4 absolute left-3 top-2.5 text-muted pointer-events-none" />
        <input
          type="text"
          value={searchQuery}
          onChange={(e) => setSearchQuery(e.target.value)}
          placeholder="Buscar fichas por nome, texto ou gatilhos…"
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
                ? 'bg-accent-pri text-canvas shadow-sm'
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
          <span>Fixadas</span>
        </button>
      </div>

      {/* Bulk Action Toolbar */}
      {selectedIds.size > 0 && (
        <div className="rounded-xl border border-accent-pri/50 bg-elevated p-3 shadow-md animate-slide-in-top">
          <div className="flex flex-wrap items-center justify-between gap-2">
            <div className="flex items-center gap-2">
              <span className="text-xs font-semibold text-primary">
                {selectedIds.size} selected
              </span>
              <button
                onClick={toggleSelectAll}
                className="text-[11px] text-accent-pri hover:underline"
              >
                {isAllSelected ? 'Deselect all' : 'Selecionar todas'}
              </button>
            </div>

            <div className="flex flex-wrap items-center gap-1.5">
              <button
                onClick={() => handleBulkPin(true)}
                disabled={bulkLoading}
                title="Fixar fichas selecionadas"
                className="flex items-center gap-1 rounded-lg border border-subtle bg-surface px-2.5 py-1 text-xs text-secondary hover:text-primary hover:bg-elevated transition disabled:opacity-50"
              >
                <Pin className="w-3.5 h-3.5 text-amber-500" />
                <span>Fixar</span>
              </button>

              <button
                onClick={() => handleBulkPin(false)}
                disabled={bulkLoading}
                title="Desafixar fichas selecionadas"
                className="flex items-center gap-1 rounded-lg border border-subtle bg-surface px-2.5 py-1 text-xs text-secondary hover:text-primary hover:bg-elevated transition disabled:opacity-50"
              >
                <PinOff className="w-3.5 h-3.5 text-muted" />
                <span>Desafixar</span>
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
                title="Apagar fichas selecionadas"
                className="flex items-center gap-1 rounded-lg border border-red-900/60 bg-red-950/40 px-2.5 py-1 text-xs text-red-400 hover:bg-red-900/60 transition disabled:opacity-50"
              >
                <Trash2 className="w-3.5 h-3.5" />
                <span>Apagar</span>
              </button>
            </div>
          </div>
        </div>
      )}

      {/* Empty State */}
      {filteredCards.length === 0 && (
        <div className="rounded-xl border border-dashed border-subtle p-6 text-center text-xs text-muted">
          {searchQuery || typeFilter !== 'all' || pinnedOnly ? (
            <p>Nenhuma ficha corresponde à busca ou aos filtros.</p>
          ) : (
            <p>
              No fichas yet. Characters, locations, and lore encountered in your adventure will be saved here.
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
                  aria-label={isSelected ? 'Desmarcar ficha' : 'Marcar ficha'}
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
                      {c.title || 'Sem título'}
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
                    title="Editar ficha"
                    className="rounded-lg border border-subtle p-1.5 text-secondary hover:text-primary hover:bg-elevated transition"
                  >
                    <Edit2 className="w-3.5 h-3.5" />
                  </button>
                  <button
                    onClick={() => remove(c)}
                    title="Apagar ficha"
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
          title={editing === 'new' ? 'Nova ficha' : `Edit: ${editing.title || 'Sem título'}`}
          onClose={() => setEditing(null)}
          wide
        >
          <div className="flex flex-col gap-4 text-primary">
            <label className="block">
              <span className="mb-1 block text-xs font-semibold uppercase tracking-wider text-muted">
                Título
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
                  <span>Fixada (sempre no contexto da IA)</span>
                </label>
                <label className="flex items-center gap-2 text-xs text-secondary cursor-pointer">
                  <input
                    type="checkbox"
                    checked={form.use_for_character_creation}
                    onChange={(e) => setForm({ ...form, use_for_character_creation: e.target.checked })}
                    className="accent-emerald-600 rounded"
                  />
                  <span>Usar na criação de personagem</span>
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

            <div className="mt-2 flex flex-wrap items-center justify-between gap-2 border-t border-subtle pt-3">
              <div className="flex items-center gap-2">
                <button
                  type="button"
                  onClick={() => { setAiText(''); setAiBox(aiBox === 'enrich' ? '' : 'enrich') }}
                  disabled={enriching || saving}
                  className="flex items-center gap-1.5 rounded-xl border border-accent-pri/40 bg-accent-pri/20 px-3 py-2 text-xs font-semibold text-accent-pri hover:bg-accent-pri/30 transition disabled:opacity-50"
                >
                  <RefreshCw className={`w-3.5 h-3.5 ${enriching ? 'animate-spin' : 'hidden'}`} />
                  <span>{enriching ? 'Enriching…' : '✨ Enriquecer com IA'}</span>
                </button>
                <label className="flex items-center gap-1.5 text-xs text-secondary cursor-pointer">
                  <input
                    type="checkbox"
                    checked={webOn}
                    onChange={(e) => setWebOn(e.target.checked)}
                    disabled={enriching}
                    className="accent-emerald-600 rounded"
                  />
                  <span>🌐 web</span>
                </label>
                {aiBox === 'enrich' && (
                  <span className="flex items-center gap-1">
                    <input
                      value={aiText}
                      onChange={(e) => setAiText(e.target.value)}
                      placeholder="Instrução (opcional)"
                      className="rounded-lg border border-subtle bg-surface px-2 py-1 text-xs text-primary outline-none focus:border-accent-pri"
                    />
                    <button type="button" className="text-xs underline" onClick={() => enrichWithAi(aiText)}>Confirmar</button>
                  </span>
                )}
              </div>
              <div className="flex gap-2">
                <button
                  type="button"
                  onClick={() => setEditing(null)}
                  className="rounded-xl border border-subtle px-4 py-2 text-xs text-secondary hover:bg-elevated transition"
                >
                  Cancelar
                </button>
                <button
                  type="button"
                  onClick={save}
                  disabled={saving || enriching}
                  className="rounded-xl bg-accent-pri px-4 py-2 text-xs font-semibold text-canvas hover:bg-accent-hov shadow transition disabled:opacity-50"
                >
                  {saving ? 'Salvando…' : 'Salvar ficha'}
                </button>
              </div>
            </div>
          </div>
        </Modal>
      )}
    </div>
  )
}
