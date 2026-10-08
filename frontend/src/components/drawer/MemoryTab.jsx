import { useCallback, useEffect, useState } from 'react'
import { api } from '../../api.js'
import { useToast } from '../Toast.jsx'

// Aba Memory: Plot Essentials + Author's Note editáveis (GET/PUT
// /api/adventures/:id/memory); Story Summary é read-only (mantido pelo
// summarizer do backend). Memory Bank (resumos automáticos) exibido read-only.
export default function MemoryTab({ adventure }) {
  const toast = useToast()
  const [mem, setMem] = useState(null)
  const [plot, setPlot] = useState('')
  const [note, setNote] = useState('')
  const [saving, setSaving] = useState(false)
  const [showBank, setShowBank] = useState(false)

  const load = useCallback(async () => {
    try {
      const m = await api.getMemory(adventure.id)
      setMem(m)
      setPlot(m.plot_essentials ?? '')
      setNote(m.authors_note ?? '')
    } catch (e) {
      toast(e.message)
      setMem({})
    }
  }, [adventure.id, toast])

  useEffect(() => {
    load()
  }, [load])

  const save = async () => {
    setSaving(true)
    try {
      const m = await api.putMemory(adventure.id, { plot_essentials: plot, authors_note: note })
      setMem(m)
      toast('Memory saved.', 'ok')
    } catch (e) {
      toast(e.message)
    } finally {
      setSaving(false)
    }
  }

  if (mem === null) return <p className="text-sm text-zinc-500">Loading…</p>

  const memories = adventure.memories ?? []

  return (
    <div className="flex flex-col gap-4">
      <label className="block">
        <span className="mb-1 block text-xs font-semibold uppercase tracking-wide text-zinc-500">
          Plot Essentials (Memory)
        </span>
        <span className="mb-1 block text-xs text-zinc-600">
          Fixed block near the top of the AI context: who you are, the world, the rules.
        </span>
        <textarea
          value={plot}
          onChange={(e) => setPlot(e.target.value)}
          rows={8}
          className="w-full resize-y rounded-lg border border-zinc-700 bg-zinc-800 px-3 py-2 text-sm outline-none focus:border-emerald-600"
        />
      </label>

      <label className="block">
        <span className="mb-1 block text-xs font-semibold uppercase tracking-wide text-zinc-500">
          Author's Note
        </span>
        <span className="mb-1 block text-xs text-zinc-600">
          Style/tone note injected near the end of the context (strong recency influence).
        </span>
        <textarea
          value={note}
          onChange={(e) => setNote(e.target.value)}
          rows={4}
          className="w-full resize-y rounded-lg border border-zinc-700 bg-zinc-800 px-3 py-2 text-sm outline-none focus:border-emerald-600"
        />
      </label>

      <div className="flex justify-end">
        <button
          onClick={save}
          disabled={saving}
          className="rounded-lg bg-emerald-700 px-4 py-2 text-sm font-semibold text-white hover:bg-emerald-600 disabled:opacity-50"
        >
          {saving ? 'Saving…' : 'Save memory'}
        </button>
      </div>

      <div>
        <span className="mb-1 block text-xs font-semibold uppercase tracking-wide text-zinc-500">
          Story Summary (read-only)
        </span>
        <p className="whitespace-pre-wrap rounded-lg border border-zinc-800 bg-zinc-800/40 p-3 text-sm text-zinc-400">
          {mem.story_summary || 'No summary yet — maintained automatically as the story grows.'}
        </p>
      </div>

      <div>
        <button
          onClick={() => setShowBank((v) => !v)}
          className="text-xs font-semibold uppercase tracking-wide text-zinc-500 hover:text-zinc-300"
        >
          {showBank ? '▾' : '▸'} Memory Bank ({memories.length})
        </button>
        {showBank && (
          <ul className="mt-2 flex max-h-64 flex-col gap-2 overflow-y-auto">
            {memories.length === 0 && (
              <li className="text-sm text-zinc-600">No automatic memories yet.</li>
            )}
            {memories.map((m) => (
              <li key={m.id} className="rounded-lg border border-zinc-800 bg-zinc-800/40 p-2 text-sm text-zinc-400">
                {m.text}
                {(m.seq_start != null || m.seq_end != null) && (
                  <span className="ml-2 text-xs text-zinc-600">
                    (actions {m.seq_start}–{m.seq_end})
                  </span>
                )}
              </li>
            ))}
          </ul>
        )}
      </div>
    </div>
  )
}
