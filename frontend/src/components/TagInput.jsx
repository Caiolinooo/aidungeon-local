import { useState } from 'react'

// Input de "chips" para tags e triggers: Enter/vírgula adiciona, ✕ remove.
export default function TagInput({ value = [], onChange, placeholder = 'Digite e pressione Enter', disabled = false }) {
  const [draft, setDraft] = useState('')

  const add = () => {
    const t = draft.trim().replace(/,+$/, '')
    if (t && !value.includes(t)) onChange([...value, t])
    setDraft('')
  }

  return (
    <div
      className={`flex min-h-[42px] flex-wrap items-center gap-1.5 rounded-lg border border-zinc-700 bg-zinc-800 px-2 py-1.5 ${
        disabled ? 'opacity-60' : ''
      }`}
    >
      {value.map((tag) => (
        <span
          key={tag}
          className="flex items-center gap-1 rounded-full bg-zinc-700 px-2.5 py-0.5 text-xs text-zinc-200"
        >
          {tag}
          {!disabled && (
            <button
              type="button"
              onClick={() => onChange(value.filter((t) => t !== tag))}
              className="text-zinc-400 hover:text-zinc-100"
              aria-label={`Remover ${tag}`}
            >
              ✕
            </button>
          )}
        </span>
      ))}
      {!disabled && (
        <input
          value={draft}
          onChange={(e) => {
            if (e.target.value.endsWith(',')) {
              setDraft(e.target.value)
              add()
            } else setDraft(e.target.value)
          }}
          onKeyDown={(e) => {
            if (e.key === 'Enter') {
              e.preventDefault()
              add()
            } else if (e.key === 'Backspace' && !draft && value.length) {
              onChange(value.slice(0, -1))
            }
          }}
          onBlur={add}
          placeholder={value.length ? '' : placeholder}
          className="min-w-[8rem] flex-1 bg-transparent px-1 py-0.5 text-sm outline-none placeholder:text-zinc-500"
        />
      )}
    </div>
  )
}
