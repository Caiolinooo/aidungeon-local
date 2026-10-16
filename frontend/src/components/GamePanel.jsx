import { useEffect, useRef, useState } from 'react'

// Painel de controles dinâmico (v0.7.0) — renderiza os widgets do manifesto
// gerado pela IA (ui_profile): dice / stats / inventory / character.
//
//  - dice:      rolagem local (RNG do cliente); o resultado pode virar ação
//               ("Send") ou texto no input ("+ Input").
//  - stats:     steppers −/+ com barra quando o stat tem max; clamp min/max.
//  - inventory: chips com qty; toque insere "use <item>" no input; add/remove.
//  - character: ficha chave-valor editável inline (Enter/blur salva).
//
// Toda mutação de estado sobe via onStateChange(next) — o pai (Play) faz
// debounce e persiste em PUT /api/adventures/:id/ui-state, que o backend
// injeta no contexto da IA como "[Player state]".

function rollDie(sides) {
  return 1 + Math.floor(Math.random() * sides)
}

// --------------------------------------------------------------------- dice
function DiceWidget({ widget, disabled, onSend, onInsert }) {
  const [rolling, setRolling] = useState(null) // sides do dado girando
  const [display, setDisplay] = useState(null) // valor exibido na animação
  const [result, setResult] = useState(null) // { sides, value }
  const timerRef = useRef(null)

  useEffect(() => () => clearInterval(timerRef.current), [])

  const roll = (sides) => {
    if (disabled || rolling !== null) return
    setResult(null)
    setRolling(sides)
    const started = Date.now()
    timerRef.current = setInterval(() => {
      setDisplay(rollDie(sides))
      if (Date.now() - started > 500) {
        clearInterval(timerRef.current)
        const value = rollDie(sides)
        setRolling(null)
        setDisplay(value)
        setResult({ sides, value })
      }
    }, 60)
  }

  return (
    <div className="flex flex-col gap-2">
      <div className="flex flex-wrap gap-2">
        {widget.dice.map((sides) => (
          <button
            key={sides}
            onClick={() => roll(sides)}
            disabled={disabled || rolling !== null}
            className="min-h-11 min-w-11 rounded-lg border border-zinc-600 bg-zinc-800 px-3 py-2 text-sm font-bold text-zinc-100 hover:border-emerald-600 hover:bg-zinc-700 active:scale-95 disabled:opacity-40"
          >
            d{sides}
          </button>
        ))}
      </div>
      {(rolling !== null || result) && (
        <div className="flex items-center gap-3">
          <span
            className={`flex h-12 w-12 items-center justify-center rounded-lg text-xl font-black ${
              rolling !== null
                ? 'animate-pulse bg-zinc-800 text-zinc-400'
                : 'bg-emerald-700 text-white'
            }`}
          >
            {display}
          </span>
          {result && (
            <>
              <span className="text-xs text-zinc-500">d{result.sides}</span>
              <button
                onClick={() => onSend('do', `rolls a d${result.sides} and gets ${result.value}`)}
                disabled={disabled}
                className="min-h-11 rounded-lg bg-emerald-700 px-3 py-2 text-xs font-semibold text-white hover:bg-emerald-600 disabled:opacity-40"
              >
                Send roll
              </button>
              <button
                onClick={() => onInsert(`rolls a d${result.sides}: ${result.value} — `)}
                disabled={disabled}
                className="min-h-11 rounded-lg border border-zinc-700 px-3 py-2 text-xs text-zinc-300 hover:bg-zinc-800 disabled:opacity-40"
              >
                + Input
              </button>
            </>
          )}
        </div>
      )}
    </div>
  )
}

// -------------------------------------------------------------------- stats
function StatsWidget({ widget, values, disabled, onChange }) {
  const bump = (stat, delta) => {
    const current = Number(values?.[stat.name] ?? stat.value ?? 0)
    let next = current + delta
    if (stat.min != null) next = Math.max(stat.min, next)
    if (stat.max != null) next = Math.min(stat.max, next)
    onChange({ ...(values || {}), [stat.name]: next })
  }

  return (
    <div className="flex flex-col gap-2">
      {widget.stats.map((stat) => {
        const value = Number(values?.[stat.name] ?? stat.value ?? 0)
        const pct = stat.max ? Math.max(0, Math.min(100, (value / stat.max) * 100)) : null
        const low = pct !== null && pct <= 25
        return (
          <div key={stat.name} className="flex items-center gap-2">
            <span className="w-20 shrink-0 truncate text-xs font-semibold uppercase tracking-wide text-zinc-400">
              {stat.name}
            </span>
            <button
              onClick={() => bump(stat, -1)}
              disabled={disabled || (stat.min != null && value <= stat.min)}
              aria-label={`Decrease ${stat.name}`}
              className="min-h-11 min-w-11 shrink-0 rounded-lg border border-zinc-700 bg-zinc-800 text-lg font-bold text-zinc-200 hover:bg-zinc-700 active:scale-95 disabled:opacity-30"
            >
              −
            </button>
            <div className="min-w-0 flex-1">
              <div className="text-center text-sm font-bold tabular-nums text-zinc-100">
                {value}
                {stat.max != null && <span className="text-zinc-500">/{stat.max}</span>}
              </div>
              {pct !== null && (
                <div className="mt-0.5 h-1.5 overflow-hidden rounded-full bg-zinc-800">
                  <div
                    className={`h-full rounded-full transition-all ${low ? 'bg-red-600' : 'bg-emerald-600'}`}
                    style={{ width: `${pct}%` }}
                  />
                </div>
              )}
            </div>
            <button
              onClick={() => bump(stat, +1)}
              disabled={disabled || (stat.max != null && value >= stat.max)}
              aria-label={`Increase ${stat.name}`}
              className="min-h-11 min-w-11 shrink-0 rounded-lg border border-zinc-700 bg-zinc-800 text-lg font-bold text-zinc-200 hover:bg-zinc-700 active:scale-95 disabled:opacity-30"
            >
              +
            </button>
          </div>
        )
      })}
    </div>
  )
}

// ---------------------------------------------------------------- inventory
function InventoryWidget({ widget, items, disabled, onChange, onInsert }) {
  const [newItem, setNewItem] = useState('')
  const list = Array.isArray(items) ? items : widget.items || []

  const add = () => {
    const name = newItem.trim()
    if (!name) return
    const existing = list.find((i) => i.name.toLowerCase() === name.toLowerCase())
    const next = existing
      ? list.map((i) => (i === existing ? { ...i, qty: i.qty + 1 } : i))
      : [...list, { name, qty: 1 }]
    setNewItem('')
    onChange(next)
  }

  const remove = (idx) => {
    const item = list[idx]
    const next = item.qty > 1
      ? list.map((i, j) => (j === idx ? { ...i, qty: i.qty - 1 } : i))
      : list.filter((_, j) => j !== idx)
    onChange(next)
  }

  return (
    <div className="flex flex-col gap-2">
      {list.length === 0 && (
        <p className="text-xs text-zinc-600">Empty — add what you pick up.</p>
      )}
      <div className="flex flex-wrap gap-1.5">
        {list.map((item, idx) => (
          <span
            key={`${item.name}-${idx}`}
            className="inline-flex min-h-11 items-center gap-1 rounded-full border border-zinc-700 bg-zinc-800 pl-3 pr-1 text-xs text-zinc-200"
          >
            <button
              onClick={() => onInsert(`use the ${item.name} `)}
              disabled={disabled}
              title={`Insert "use the ${item.name}" into the input`}
              className="py-2 hover:text-emerald-300 disabled:opacity-40"
            >
              {item.name}
              {item.qty > 1 && <span className="ml-1 text-zinc-500">×{item.qty}</span>}
            </button>
            <button
              onClick={() => remove(idx)}
              disabled={disabled}
              aria-label={`Remove ${item.name}`}
              className="min-h-11 min-w-11 -my-2 rounded-full text-zinc-500 hover:text-red-400 disabled:opacity-40"
            >
              ×
            </button>
          </span>
        ))}
      </div>
      <div className="flex gap-2">
        <input
          value={newItem}
          onChange={(e) => setNewItem(e.target.value)}
          onKeyDown={(e) => e.key === 'Enter' && (e.preventDefault(), add())}
          placeholder="Add item…"
          disabled={disabled}
          className="min-h-11 min-w-0 flex-1 rounded-lg border border-zinc-700 bg-zinc-800 px-3 py-2 text-sm text-zinc-100 outline-none placeholder:text-zinc-500 focus:border-emerald-600 disabled:opacity-50"
        />
        <button
          onClick={add}
          disabled={disabled || !newItem.trim()}
          className="min-h-11 shrink-0 rounded-lg border border-zinc-700 px-3 py-2 text-xs font-semibold text-zinc-200 hover:bg-zinc-800 disabled:opacity-40"
        >
          + Add
        </button>
      </div>
    </div>
  )
}

// ---------------------------------------------------------------- character
function CharacterWidget({ widget, values, disabled, onChange }) {
  const [editing, setEditing] = useState(null) // nome do campo em edição
  const [draft, setDraft] = useState('')

  const save = (name) => {
    onChange({ ...(values || {}), [name]: draft.trim() })
    setEditing(null)
  }

  return (
    <dl className="flex flex-col gap-1">
      {widget.fields.map((field) => {
        const value = values?.[field.name] ?? field.value ?? ''
        return (
          <div key={field.name} className="flex items-baseline gap-2 text-sm">
            <dt className="w-24 shrink-0 truncate text-xs font-semibold uppercase tracking-wide text-zinc-500">
              {field.name}
            </dt>
            {editing === field.name ? (
              <input
                value={draft}
                onChange={(e) => setDraft(e.target.value)}
                onBlur={() => save(field.name)}
                onKeyDown={(e) => {
                  if (e.key === 'Enter') { e.preventDefault(); save(field.name) }
                  if (e.key === 'Escape') setEditing(null)
                }}
                autoFocus
                className="min-w-0 flex-1 rounded border border-emerald-700 bg-zinc-800 px-2 py-1 text-sm text-zinc-100 outline-none"
              />
            ) : (
              <dd
                onClick={() => {
                  if (disabled) return
                  setEditing(field.name)
                  setDraft(value)
                }}
                title={disabled ? undefined : 'Tap to edit'}
                className={`min-w-0 flex-1 truncate rounded px-2 py-1.5 text-zinc-100 ${
                  disabled ? '' : 'cursor-text hover:bg-zinc-800'
                }`}
              >
                {value || <span className="text-zinc-600">—</span>}
              </dd>
            )}
          </div>
        )
      })}
    </dl>
  )
}

export default function GamePanel({
  profile, state, disabled, regenerating,
  onStateChange, onSend, onInsert, onRegenerate, onClose,
  inline = false,
}) {
  const widgets = profile?.widgets ?? []
  if (widgets.length === 0) return null

  if (inline) {
    return (
      <div className="space-y-4">
        <div className="flex items-center justify-between gap-2 pb-2 border-b border-subtle">
          <span className="text-xs font-semibold uppercase tracking-wider text-muted">
            Game Controls
          </span>
          {onRegenerate && (
            <button
              onClick={onRegenerate}
              disabled={disabled || regenerating}
              title="Ask AI to redesign these controls"
              className="text-xs text-accent-pri hover:underline disabled:opacity-50"
            >
              {regenerating ? 'Redesigning…' : '↻ Redesign'}
            </button>
          )}
        </div>
        <div className="flex flex-col gap-3">
          {widgets.map((widget) => (
            <section
              key={widget.id}
              className="rounded-xl border border-subtle bg-surface p-3"
            >
              <h3 className="mb-2 text-xs font-bold uppercase tracking-wide text-muted">
                {widget.label}
              </h3>
              {widget.type === 'dice' && (
                <DiceWidget widget={widget} disabled={disabled} onSend={onSend} onInsert={onInsert} />
              )}
              {widget.type === 'stats' && (
                <StatsWidget
                  widget={widget}
                  values={state?.[widget.id]}
                  disabled={disabled}
                  onChange={(next) => onStateChange({ ...state, [widget.id]: next })}
                />
              )}
              {widget.type === 'inventory' && (
                <InventoryWidget
                  widget={widget}
                  items={state?.[widget.id]}
                  disabled={disabled}
                  onChange={(next) => onStateChange({ ...state, [widget.id]: next })}
                  onInsert={onInsert}
                />
              )}
              {widget.type === 'character' && (
                <CharacterWidget
                  widget={widget}
                  values={state?.[widget.id]}
                  disabled={disabled}
                  onChange={(next) => onStateChange({ ...state, [widget.id]: next })}
                />
              )}
            </section>
          ))}
        </div>
      </div>
    )
  }

  return (
    <div className="border-t border-subtle bg-surface/95 backdrop-blur">
      <div className="mx-auto max-w-3xl px-3 py-2">
        <div className="mb-2 flex items-center gap-2">
          <span className="text-[10px] font-semibold uppercase tracking-wide text-muted">
            Game controls
          </span>
          <span className="flex-1" />
          <button
            onClick={onRegenerate}
            disabled={disabled || regenerating}
            title="Ask the AI to redesign these controls"
            className="min-h-11 rounded-md px-2 text-xs text-secondary hover:bg-elevated hover:text-primary disabled:opacity-40"
          >
            {regenerating ? '…' : '↻ Redesign'}
          </button>
          {onClose && (
            <button
              onClick={onClose}
              aria-label="Close panel"
              className="min-h-11 rounded-md px-2 text-secondary hover:bg-elevated hover:text-primary"
            >
              ✕
            </button>
          )}
        </div>
        {/* mobile: cards em fila rolável; desktop: grade de 2 colunas */}
        <div className="flex snap-x gap-3 overflow-x-auto pb-2 sm:grid sm:grid-cols-2 sm:overflow-visible">

          {widgets.map((widget) => (
            <section
              key={widget.id}
              className="min-w-[16rem] shrink-0 snap-start rounded-xl border border-zinc-800 bg-zinc-900/70 p-3 sm:min-w-0 sm:shrink"
            >
              <h3 className="mb-2 text-xs font-bold uppercase tracking-wide text-zinc-400">
                {widget.label}
              </h3>
              {widget.type === 'dice' && (
                <DiceWidget widget={widget} disabled={disabled} onSend={onSend} onInsert={onInsert} />
              )}
              {widget.type === 'stats' && (
                <StatsWidget
                  widget={widget}
                  values={state?.[widget.id]}
                  disabled={disabled}
                  onChange={(next) => onStateChange({ ...state, [widget.id]: next })}
                />
              )}
              {widget.type === 'inventory' && (
                <InventoryWidget
                  widget={widget}
                  items={state?.[widget.id]}
                  disabled={disabled}
                  onChange={(next) => onStateChange({ ...state, [widget.id]: next })}
                  onInsert={onInsert}
                />
              )}
              {widget.type === 'character' && (
                <CharacterWidget
                  widget={widget}
                  values={state?.[widget.id]}
                  disabled={disabled}
                  onChange={(next) => onStateChange({ ...state, [widget.id]: next })}
                />
              )}
            </section>
          ))}
        </div>
      </div>
    </div>
  )
}
