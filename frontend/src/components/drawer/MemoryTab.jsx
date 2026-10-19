import { useCallback, useEffect, useState } from 'react'
import { api } from '../../api.js'
import { useToast } from '../Toast.jsx'
import Modal from '../Modal.jsx'
import {
  Brain,
  RefreshCw,
  Plus,
  Edit2,
  Trash2,
  Save,
  Clock,
  Sparkles,
  Layers,
  ChevronDown,
  ChevronRight,
} from 'lucide-react'

export default function MemoryTab({ adventure }) {
  const toast = useToast()
  const [mem, setMem] = useState(null)
  const [plot, setPlot] = useState('')
  const [note, setNote] = useState('')
  const [saving, setSaving] = useState(false)
  const [syncing, setSyncing] = useState(false)
  const [showBank, setShowBank] = useState(true)

  // Memory items state
  const [memoriesList, setMemoriesList] = useState([])
  const [regenerating, setRegenerating] = useState(false)
  const [editingMemory, setEditingMemory] = useState(null) // null | 'new' | memory
  const [memoryForm, setMemoryForm] = useState({ text: '', seq_start: '', seq_end: '' })
  const [memorySaving, setMemorySaving] = useState(false)

  const load = useCallback(async () => {
    try {
      const [m, bank] = await Promise.all([
        api.getMemory(adventure.id),
        api.listMemories(adventure.id).catch(() => null),
      ])
      setMem(m)
      setPlot(m.plot_essentials ?? '')
      setNote(m.authors_note ?? '')
      setMemoriesList(bank || m.memories || [])
    } catch (e) {
      toast(e.message)
      setMem({})
      setMemoriesList([])
    }
  }, [adventure.id, toast])

  useEffect(() => {
    load()
  }, [load])

  const saveMeta = async () => {
    setSaving(true)
    try {
      const m = await api.putMemory(adventure.id, { plot_essentials: plot, authors_note: note })
      setMem(m)
      toast('Memory metadata saved.', 'ok')
    } catch (e) {
      toast(e.message)
    } finally {
      setSaving(false)
    }
  }

  // Fills the textareas only — the user reviews and clicks Save.
  const syncPlot = async () => {
    setSyncing(true)
    try {
      const language = (navigator.language || 'pt').slice(0, 2)
      const r = await api.assistantSyncPlot({ adventure_id: adventure.id, language })
      if (typeof r?.plot_essentials === 'string') setPlot(r.plot_essentials)
      if (typeof r?.authors_note === 'string') setNote(r.authors_note)
      if (r?.summary_delta) toast(r.summary_delta, 'ok')
    } catch (e) {
      toast(e.message)
    } finally {
      setSyncing(false)
    }
  }

  // Regenerate Memory Bank
  const handleRegenerate = async () => {
    if (!window.confirm('Regenerate memory bank? This will recompute summaries and embedding vectors for all story actions.')) return
    setRegenerating(true)
    try {
      const res = await api.regenerateMemories(adventure.id)
      toast(`Regenerated ${res.count ?? 0} memories successfully!`, 'ok')
      load()
    } catch (e) {
      toast(e.message)
    } finally {
      setRegenerating(false)
    }
  }

  // Open edit modal
  const openEditModal = (item) => {
    if (item === 'new') {
      setEditingMemory('new')
      setMemoryForm({ text: '', seq_start: '', seq_end: '' })
    } else {
      setEditingMemory(item)
      setMemoryForm({
        text: item.text || '',
        seq_start: item.seq_start != null ? String(item.seq_start) : '',
        seq_end: item.seq_end != null ? String(item.seq_end) : '',
      })
    }
  }

  // Save memory item (create or patch)
  const saveMemoryItem = async () => {
    if (!memoryForm.text.trim()) {
      toast('Memory text cannot be empty.')
      return
    }
    setMemorySaving(true)
    try {
      const payload = {
        text: memoryForm.text.trim(),
        seq_start: memoryForm.seq_start !== '' ? Number(memoryForm.seq_start) : undefined,
        seq_end: memoryForm.seq_end !== '' ? Number(memoryForm.seq_end) : undefined,
      }
      if (editingMemory === 'new') {
        await api.createMemory(adventure.id, payload)
        toast('Memory added.', 'ok')
      } else {
        await api.patchMemory(adventure.id, editingMemory.id, payload)
        toast('Memory updated.', 'ok')
      }
      setEditingMemory(null)
      load()
    } catch (e) {
      toast(e.message)
    } finally {
      setMemorySaving(false)
    }
  }

  // Delete memory item
  const deleteMemoryItem = async (item) => {
    if (!window.confirm('Delete this memory permanently?')) return
    try {
      await api.deleteMemory(adventure.id, item.id)
      toast('Memory deleted.', 'ok')
      load()
    } catch (e) {
      toast(e.message)
    }
  }

  if (mem === null) return <p className="text-sm text-muted">Loading memory bank…</p>

  return (
    <div className="space-y-6 text-primary">
      {/* Plot Essentials (Memory) */}
      <div className="rounded-xl border border-subtle bg-surface p-4 space-y-3">
        <div className="flex items-start justify-between gap-2">
          <div>
            <span className="block text-xs font-semibold uppercase tracking-wider text-muted">
              Plot Essentials (Memory)
            </span>
            <span className="block text-xs text-secondary mt-0.5">
              Fixed context block near top of AI prompt: who you are, world lore, main objective.
            </span>
          </div>
          <button
            type="button"
            onClick={syncPlot}
            disabled={syncing}
            className="flex shrink-0 items-center gap-1.5 rounded-lg bg-accent-pri/20 border border-accent-pri/40 px-3 py-1 text-xs font-semibold text-accent-pri hover:bg-accent-pri/30 transition disabled:opacity-50"
          >
            <RefreshCw className={`w-3.5 h-3.5 ${syncing ? 'animate-spin' : 'hidden'}`} />
            <span>{syncing ? 'Syncing…' : '✨ Sync Plot with AI'}</span>
          </button>
        </div>
        <textarea
          value={plot}
          onChange={(e) => setPlot(e.target.value)}
          rows={6}
          placeholder="e.g. You are Gareth, a rogue seeking the Sunstone of Alden..."
          className="w-full resize-y rounded-xl border border-subtle bg-surface-input px-3 py-2 text-xs text-primary outline-none focus:border-accent-pri transition font-sans leading-relaxed"
        />

        {/* Author's Note */}
        <div className="pt-2">
          <span className="block text-xs font-semibold uppercase tracking-wider text-muted">
            Author's Note
          </span>
          <span className="block text-xs text-secondary mt-0.5">
            Style/tone instructions injected near the prompt bottom (high recency influence).
          </span>
        </div>
        <textarea
          value={note}
          onChange={(e) => setNote(e.target.value)}
          rows={3}
          placeholder="e.g. Dark fantasy, vivid sensory descriptions, maintain tense atmosphere..."
          className="w-full resize-y rounded-xl border border-subtle bg-surface-input px-3 py-2 text-xs text-primary outline-none focus:border-accent-pri transition font-sans leading-relaxed"
        />

        <div className="flex justify-end pt-1">
          <button
            onClick={saveMeta}
            disabled={saving}
            className="flex items-center gap-1.5 rounded-xl bg-accent-pri px-4 py-2 text-xs font-semibold text-white shadow hover:bg-accent-hov transition disabled:opacity-50"
          >
            <Save className="w-3.5 h-3.5" />
            <span>{saving ? 'Saving…' : 'Save Essentials & Note'}</span>
          </button>
        </div>
      </div>

      {/* Story Summary Card */}
      <div className="rounded-xl border border-subtle bg-surface p-4 space-y-2">
        <span className="block text-xs font-semibold uppercase tracking-wider text-muted">
          Story Summary (Rolling Overview)
        </span>
        <p className="whitespace-pre-wrap rounded-lg border border-subtle bg-elevated/40 p-3 text-xs text-secondary leading-relaxed font-serif">
          {mem.story_summary || 'No summary generated yet — automatically maintained as your adventure progresses.'}
        </p>
      </div>

      {/* Interactive Memory Bank Section */}
      <div className="rounded-xl border border-subtle bg-surface p-4 space-y-4">
        <div className="flex items-center justify-between gap-2 flex-wrap">
          <button
            onClick={() => setShowBank(!showBank)}
            className="flex items-center gap-1.5 text-xs font-semibold uppercase tracking-wider text-muted hover:text-primary transition"
          >
            {showBank ? <ChevronDown className="w-4 h-4 text-accent-pri" /> : <ChevronRight className="w-4 h-4 text-accent-pri" />}
            <Brain className="w-4 h-4 text-accent-pri" />
            <span>Memory Bank ({memoriesList.length})</span>
          </button>

          <div className="flex items-center gap-2">
            <button
              onClick={() => openEditModal('new')}
              className="flex items-center gap-1 rounded-lg border border-subtle bg-elevated px-2.5 py-1 text-xs text-secondary hover:text-primary transition"
            >
              <Plus className="w-3.5 h-3.5" />
              <span>Add</span>
            </button>
            <button
              onClick={handleRegenerate}
              disabled={regenerating}
              title="Re-run automatic memory extraction and vector embeddings across all turns"
              className="flex items-center gap-1.5 rounded-lg bg-accent-pri/20 border border-accent-pri/40 px-3 py-1 text-xs font-semibold text-accent-pri hover:bg-accent-pri/30 transition disabled:opacity-50"
            >
              <RefreshCw className={`w-3.5 h-3.5 ${regenerating ? 'animate-spin' : ''}`} />
              <span>{regenerating ? 'Regenerating…' : 'Regenerate Bank'}</span>
            </button>
          </div>
        </div>

        {showBank && (
          <div className="space-y-2 pt-1">
            {memoriesList.length === 0 ? (
              <p className="text-xs text-muted border border-dashed border-subtle rounded-xl p-4 text-center">
                No memories stored yet. Memories will be extracted automatically during gameplay, or you can add manual notes above.
              </p>
            ) : (
              <ul className="flex flex-col gap-2.5 max-h-96 overflow-y-auto pr-1">
                {memoriesList.map((m) => (
                  <li
                    key={m.id}
                    className="group rounded-xl border border-subtle bg-elevated/40 p-3 hover:bg-elevated transition space-y-2"
                  >
                    <div className="flex items-start justify-between gap-2">
                      <div className="flex items-center gap-2 flex-wrap">
                        {(m.seq_start != null || m.seq_end != null) && (
                          <span className="flex items-center gap-1 rounded-md bg-accent-pri/10 border border-accent-pri/30 px-2 py-0.5 text-[10px] font-mono font-medium text-accent-pri">
                            <Clock className="w-2.5 h-2.5" />
                            <span>Actions {m.seq_start ?? '1'}–{m.seq_end ?? '?'}</span>
                          </span>
                        )}
                        {m.embedding_dim && (
                          <span className="rounded-md bg-surface border border-subtle px-1.5 py-0.5 text-[10px] font-mono text-muted">
                            Vector ({m.embedding_dim}d)
                          </span>
                        )}
                      </div>

                      <div className="flex shrink-0 items-center gap-1">
                        <button
                          onClick={() => openEditModal(m)}
                          title="Edit memory"
                          className="rounded-lg p-1 text-secondary hover:text-primary hover:bg-surface transition"
                        >
                          <Edit2 className="w-3.5 h-3.5" />
                        </button>
                        <button
                          onClick={() => deleteMemoryItem(m)}
                          title="Delete memory"
                          className="rounded-lg p-1 text-red-400 hover:text-red-300 hover:bg-red-950/40 transition"
                        >
                          <Trash2 className="w-3.5 h-3.5" />
                        </button>
                      </div>
                    </div>

                    <p className="text-xs text-secondary leading-relaxed font-sans">
                      {m.text}
                    </p>
                  </li>
                ))}
              </ul>
            )}
          </div>
        )}
      </div>

      {/* Edit / Add Memory Modal */}
      {editingMemory && (
        <Modal
          title={editingMemory === 'new' ? 'Add Memory Entry' : 'Edit Memory Entry'}
          onClose={() => setEditingMemory(null)}
        >
          <div className="space-y-4 text-primary">
            <label className="block">
              <span className="mb-1 block text-xs font-semibold uppercase tracking-wider text-muted">
                Memory Text
              </span>
              <textarea
                value={memoryForm.text}
                onChange={(e) => setMemoryForm({ ...memoryForm, text: e.target.value })}
                rows={5}
                placeholder="Key event, revealed secret, or lasting story development..."
                className="w-full resize-y rounded-xl border border-subtle bg-surface px-3 py-2 text-xs text-primary outline-none focus:border-accent-pri transition leading-relaxed"
              />
            </label>

            <div className="grid grid-cols-2 gap-3">
              <label className="block">
                <span className="mb-1 block text-xs font-semibold uppercase tracking-wider text-muted">
                  Start Action #
                </span>
                <input
                  type="number"
                  min="1"
                  value={memoryForm.seq_start}
                  onChange={(e) => setMemoryForm({ ...memoryForm, seq_start: e.target.value })}
                  placeholder="e.g. 1"
                  className="w-full rounded-xl border border-subtle bg-surface px-3 py-2 text-xs text-primary outline-none focus:border-accent-pri transition"
                />
              </label>

              <label className="block">
                <span className="mb-1 block text-xs font-semibold uppercase tracking-wider text-muted">
                  End Action #
                </span>
                <input
                  type="number"
                  min="1"
                  value={memoryForm.seq_end}
                  onChange={(e) => setMemoryForm({ ...memoryForm, seq_end: e.target.value })}
                  placeholder="e.g. 10"
                  className="w-full rounded-xl border border-subtle bg-surface px-3 py-2 text-xs text-primary outline-none focus:border-accent-pri transition"
                />
              </label>
            </div>

            <div className="mt-4 flex justify-end gap-2 border-t border-subtle pt-3">
              <button
                type="button"
                onClick={() => setEditingMemory(null)}
                className="rounded-xl border border-subtle px-4 py-2 text-xs text-secondary hover:bg-elevated transition"
              >
                Cancel
              </button>
              <button
                type="button"
                onClick={saveMemoryItem}
                disabled={memorySaving}
                className="rounded-xl bg-accent-pri px-4 py-2 text-xs font-semibold text-white hover:bg-accent-hov shadow transition disabled:opacity-50"
              >
                {memorySaving ? 'Saving…' : 'Save Memory'}
              </button>
            </div>
          </div>
        </Modal>
      )}
    </div>
  )
}
