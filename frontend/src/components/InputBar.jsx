import { useEffect, useRef, useState } from 'react'

const TABS = [
  { id: 'do', label: 'Do', placeholder: 'What do you do?' },
  { id: 'say', label: 'Say', placeholder: 'What do you say?' },
  { id: 'story', label: 'Story', placeholder: 'What happens next?' },
]

// Barra de entrada fixa no rodapé: tabs [Do | Say | Story], textarea
// (Enter envia, Shift+Enter quebra linha) e botão Send.
// Desabilitada durante a geração (backend também responde 409).
// onTextChange(text) (opcional): avisa o pai quando o usuário digita/limpa —
// usado pelo Play p/ esconder as "choices" enquanto há texto no input.
export default function InputBar({ disabled, onSend, onTextChange }) {
  const [tab, setTab] = useState('do')
  const [text, setText] = useState('')
  const areaRef = useRef(null)

  const active = TABS.find((t) => t.id === tab)

  const submit = () => {
    const t = text.trim()
    if (!t || disabled) return
    onSend(tab, t)
    setText('')
    onTextChange?.('')
  }

  // auto-resize da textarea (2..10 linhas)
  useEffect(() => {
    const el = areaRef.current
    if (!el) return
    el.style.height = 'auto'
    el.style.height = `${Math.min(el.scrollHeight, 240)}px`
  }, [text])

  return (
    <div className="border-t border-zinc-800 bg-zinc-950/95 px-3 pb-3 pt-2 backdrop-blur">
      <div className="mx-auto max-w-3xl">
        <div className="mb-2 flex gap-1">
          {TABS.map((t) => (
            <button
              key={t.id}
              onClick={() => setTab(t.id)}
              disabled={disabled}
              className={`rounded-md px-4 py-1.5 text-sm font-medium transition-colors ${
                tab === t.id
                  ? 'bg-emerald-700 text-white'
                  : 'bg-zinc-800 text-zinc-400 hover:bg-zinc-700 hover:text-zinc-200'
              } disabled:opacity-50`}
            >
              {t.label}
            </button>
          ))}
        </div>
        <div className="flex items-end gap-2">
          <textarea
            ref={areaRef}
            value={text}
            onChange={(e) => {
              setText(e.target.value)
              onTextChange?.(e.target.value)
            }}
            onKeyDown={(e) => {
              if (e.key === 'Enter' && !e.shiftKey) {
                e.preventDefault() // Enter envia; Shift+Enter quebra linha
                submit()
              }
            }}
            disabled={disabled}
            rows={2}
            placeholder={disabled ? 'AI is writing…' : active.placeholder}
            className="max-h-60 flex-1 resize-none rounded-lg border border-zinc-700 bg-zinc-800 px-3 py-2 font-prose text-zinc-100 outline-none placeholder:text-zinc-500 focus:border-emerald-600 disabled:opacity-60"
          />
          <button
            onClick={submit}
            disabled={disabled || !text.trim()}
            className="rounded-lg bg-emerald-700 px-5 py-2.5 font-semibold text-white hover:bg-emerald-600 disabled:cursor-not-allowed disabled:opacity-40"
          >
            Send
          </button>
        </div>
      </div>
    </div>
  )
}
