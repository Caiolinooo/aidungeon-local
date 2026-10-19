import TagInput from './TagInput.jsx'

// Lista de story cards compartilhada pelo modal "Generate with AI" e pelo
// ScenarioEditor. `children` entra na barra do cabeçalho, ao lado do 🌐 web.
const inputCls =
  'w-full rounded-lg border border-zinc-700 bg-zinc-800 px-3 py-2 text-sm text-zinc-100 outline-none focus:border-emerald-600 disabled:opacity-60'

const BLANK_CARD = { title: '', type: '', triggers: [], entry: '' }

export default function StoryCardsEditor({
  cards,
  onChange,
  busy = false,
  onAiCard,
  webOn = false,
  onToggleWeb,
  children,
}) {
  const list = Array.isArray(cards) ? cards : []

  const patch = (index, fields) => {
    onChange(list.map((card, j) => (j === index ? { ...card, ...fields } : card)))
  }

  const showToolbar = typeof onToggleWeb === 'function' || children

  return (
    <div className="flex flex-col gap-2">
      {showToolbar && (
        <div className="flex flex-wrap items-center justify-end gap-2">
          {typeof onToggleWeb === 'function' && (
            <label
              className="flex items-center gap-1.5 text-[11px] text-zinc-400"
              title="Search the web when refining with AI"
            >
              <input
                type="checkbox"
                checked={!!webOn}
                onChange={(e) => onToggleWeb(e.target.checked)}
                disabled={busy}
                className="accent-emerald-600"
              />
              🌐 web
            </label>
          )}
          {children}
        </div>
      )}

      {list.map((card, i) => (
        <div key={i} className="rounded-lg border border-zinc-800 bg-zinc-800/40 p-3">
          <div className="flex flex-wrap gap-2">
            <input
              value={card.title ?? ''}
              onChange={(e) => patch(i, { title: e.target.value })}
              placeholder="Title"
              disabled={busy}
              className={`${inputCls} min-w-[10rem] flex-1`}
            />
            <input
              value={card.type ?? ''}
              onChange={(e) => patch(i, { type: e.target.value })}
              placeholder="Type (character, location…)"
              disabled={busy}
              className={`${inputCls} w-full sm:w-44`}
            />
            {typeof onAiCard === 'function' && (
              <button
                type="button"
                onClick={() => onAiCard(i)}
                disabled={busy}
                title="Enrich this card with AI"
                className="shrink-0 rounded-lg border border-zinc-700 px-3 text-xs text-zinc-300 hover:bg-zinc-800 disabled:opacity-40"
              >
                ✨ AI
              </button>
            )}
            <button
              type="button"
              onClick={() => onChange(list.filter((_, j) => j !== i))}
              disabled={busy}
              title="Remove card"
              className="shrink-0 rounded-lg border border-red-900 px-3 text-xs text-red-400 hover:bg-red-950 disabled:opacity-40"
            >
              Remove
            </button>
          </div>
          <div className="mt-2">
            <TagInput
              value={card.triggers ?? []}
              onChange={(triggers) => patch(i, { triggers })}
              disabled={busy}
              placeholder="Trigger words (Enter to add)"
            />
          </div>
          <textarea
            value={card.entry ?? ''}
            onChange={(e) => patch(i, { entry: e.target.value })}
            rows={3}
            disabled={busy}
            placeholder="Card text injected into the AI context"
            className={`${inputCls} mt-2 resize-y`}
          />
        </div>
      ))}

      <button
        type="button"
        onClick={() => onChange([...list, { ...BLANK_CARD, triggers: [] }])}
        disabled={busy}
        className="rounded-lg border border-dashed border-zinc-700 py-2 text-sm text-zinc-400 hover:border-zinc-500 hover:text-zinc-200 disabled:opacity-40"
      >
        + Add story card
      </button>
    </div>
  )
}
