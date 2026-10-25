import { useEffect, useRef, useState } from 'react'
import { api } from '../api.js'

// Painel de controles dinâmico (v0.7.0) — renderiza os widgets do manifesto
// gerado pela IA (ui_profile): dice / stats / inventory / character.
//
//  - dice:      rolagem no servidor (/sim/roll); o resultado pode virar ação
//               ("Send") ou texto no input ("+ Input").
//  - stats:     steppers −/+ com barra quando o stat tem max; clamp min/max.
//  - inventory: chips com qty; toque insere "use <item>" no input; add/remove.
//  - character: ficha chave-valor editável inline (Enter/blur salva).
//
// Toda mutação de estado sobe via onStateChange(next) — o pai (Play) faz
// debounce e persiste em PUT /api/adventures/:id/ui-state, que o backend
// injeta no contexto da IA como "[Player state]".

// --------------------------------------------------------------------- dice
function DiceWidget({ widget, disabled, onSend, onInsert, adventureId }) {
  const [rolling, setRolling] = useState(null) // sides do dado girando
  const [display, setDisplay] = useState(null) // valor exibido na animação
  const [result, setResult] = useState(null) // { sides, value }
  const timerRef = useRef(null)

  useEffect(() => () => clearInterval(timerRef.current), [])

  const roll = (sides) => {
    if (disabled || rolling !== null || !adventureId) return
    setResult(null)
    setRolling(sides)
    const started = Date.now()
    const pending = api.simRoll(adventureId, `d${sides}`).catch(() => null)
    timerRef.current = setInterval(() => {
      setDisplay(1 + Math.floor(Math.random() * sides))
      if (Date.now() - started > 500) {
        clearInterval(timerRef.current)
        pending.then((data) => {
          const value = typeof data?.total === 'number' ? data.total : null
          setRolling(null)
          setDisplay(value)
          setResult(value == null ? null : { sides, value })
        })
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
            className="min-h-11 min-w-11 rounded-lg border border-subtle bg-surface px-3 py-2 text-sm font-bold text-primary hover:border-accent-pri hover:bg-elevated active:scale-95 disabled:opacity-40"
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
                ? 'animate-pulse bg-surface text-secondary'
                : 'bg-accent-pri text-canvas'
            }`}
          >
            {display}
          </span>
          {result && (
            <>
              <span className="text-xs text-muted">d{result.sides}</span>
              <button
                onClick={() => onSend('do', `rolls a d${result.sides} and gets ${result.value}`)}
                disabled={disabled}
                className="min-h-11 rounded-lg bg-accent-pri px-3 py-2 text-xs font-semibold text-canvas hover:bg-accent-hov disabled:opacity-40"
              >
                Enviar rolagem
              </button>
              <button
                onClick={() => onInsert(`rolls a d${result.sides}: ${result.value} — `)}
                disabled={disabled}
                className="min-h-11 rounded-lg border border-subtle px-3 py-2 text-xs text-secondary hover:bg-surface disabled:opacity-40"
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
            <span className="w-20 shrink-0 truncate text-xs font-semibold uppercase tracking-wide text-secondary">
              {stat.name}
            </span>
            <button
              onClick={() => bump(stat, -1)}
              disabled={disabled || (stat.min != null && value <= stat.min)}
              aria-label={`Diminuir ${stat.name}`}
              className="min-h-11 min-w-11 shrink-0 rounded-lg border border-subtle bg-surface text-lg font-bold text-primary hover:bg-elevated active:scale-95 disabled:opacity-30"
            >
              −
            </button>
            <div className="min-w-0 flex-1">
              <div className="text-center text-sm font-bold tabular-nums text-primary">
                {value}
                {stat.max != null && <span className="text-muted">/{stat.max}</span>}
              </div>
              {pct !== null && (
                <div className="mt-0.5 h-1.5 overflow-hidden rounded-full bg-surface">
                  <div
                    className={`h-full rounded-full transition-all ${low ? 'bg-danger-600' : 'bg-accent-hov'}`}
                    style={{ width: `${pct}%` }}
                  />
                </div>
              )}
            </div>
            <button
              onClick={() => bump(stat, +1)}
              disabled={disabled || (stat.max != null && value >= stat.max)}
              aria-label={`Aumentar ${stat.name}`}
              className="min-h-11 min-w-11 shrink-0 rounded-lg border border-subtle bg-surface text-lg font-bold text-primary hover:bg-elevated active:scale-95 disabled:opacity-30"
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
        <p className="text-xs text-muted">Vazio — adicione o que você pegar.</p>
      )}
      <div className="flex flex-wrap gap-1.5">
        {list.map((item, idx) => (
          <span
            key={`${item.name}-${idx}`}
            className="inline-flex min-h-11 items-center gap-1 rounded-full border border-subtle bg-surface pl-3 pr-1 text-xs text-primary"
          >
            <button
              onClick={() => onInsert(`usa o item ${item.name} `)}
              disabled={disabled}
              title={`Inserir "usa o item ${item.name}" no campo`}
              className="py-2 hover:text-accent-pri disabled:opacity-40"
            >
              {item.name}
              {item.qty > 1 && <span className="ml-1 text-muted">×{item.qty}</span>}
            </button>
            <button
              onClick={() => remove(idx)}
              disabled={disabled}
              aria-label={`Remover ${item.name}`}
              className="min-h-11 min-w-11 -my-2 rounded-full text-muted hover:text-danger-400 disabled:opacity-40"
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
          placeholder="Adicionar item…"
          disabled={disabled}
          className="min-h-11 min-w-0 flex-1 rounded-lg border border-subtle bg-surface px-3 py-2 text-sm text-primary outline-none placeholder:text-muted focus:border-accent-pri disabled:opacity-50"
        />
        <button
          onClick={add}
          disabled={disabled || !newItem.trim()}
          className="min-h-11 shrink-0 rounded-lg border border-subtle px-3 py-2 text-xs font-semibold text-primary hover:bg-surface disabled:opacity-40"
        >
          + Adicionar
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
            <dt className="w-24 shrink-0 truncate text-xs font-semibold uppercase tracking-wide text-muted">
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
                className="min-w-0 flex-1 rounded border border-accent-pri bg-surface px-2 py-1 text-sm text-primary outline-none"
              />
            ) : (
              <dd
                onClick={() => {
                  if (disabled) return
                  setEditing(field.name)
                  setDraft(value)
                }}
                title={disabled ? undefined : 'Toque para editar'}
                className={`min-w-0 flex-1 truncate rounded px-2 py-1.5 text-primary ${
                  disabled ? '' : 'cursor-text hover:bg-surface'
                }`}
              >
                {value || <span className="text-muted">—</span>}
              </dd>
            )}
          </div>
        )
      })}
    </dl>
  )
}

function SceneControls({ state, disabled, onSend }) {
  const hud = Array.isArray(state?._hud) ? state._hud : []
  const actions = Array.isArray(state?._actions) ? state._actions : []
  if (hud.length === 0 && actions.length === 0) return null
  return (
    <div className="space-y-2">
      {hud.length > 0 && (
        <div className="flex flex-wrap gap-1.5">
          {hud.map((item) => (
            <span
              key={`${item.label}-${item.value}`}
              className="inline-flex min-h-7 items-center gap-1 rounded-full border border-subtle bg-elevated px-2.5 text-[11px] text-secondary"
            >
              <span className="font-semibold uppercase tracking-wide text-muted">{item.label}</span>
              <span className="text-primary">{item.value}</span>
            </span>
          ))}
        </div>
      )}
      {actions.length > 0 && (
        <div className="flex flex-wrap gap-1.5">
          {actions.map((action) => (
            <button
              key={`${action.type}-${action.text}`}
              type="button"
              onClick={() => onSend(action.type, action.text)}
              disabled={disabled}
              title={action.text}
              className="min-h-8 max-w-full rounded-full border border-accent-pri/35 bg-accent-pri/10 px-3 py-1 text-left text-xs text-accent-pri hover:bg-accent-pri/20 disabled:opacity-40"
            >
              {action.label}
            </button>
          ))}
        </div>
      )}
    </div>
  )
}

export default function GamePanel({
  profile, state, disabled, regenerating,
  onStateChange, onSend, onInsert, onRegenerate, onClose,
  inline = false, adventureId = null, packOn = false,
}) {
  const widgets = (profile?.widgets ?? []).filter((widget) =>
    !(packOn && (widget.type === 'stats' || widget.type === 'inventory')),
  )
  const hasRuntime = (state?._hud?.length ?? 0) > 0 || (state?._actions?.length ?? 0) > 0

  useEffect(() => {
    if (inline || !onClose) return undefined
    const closeOnEscape = (event) => {
      if (event.key === 'Escape') onClose()
    }
    window.addEventListener('keydown', closeOnEscape)
    return () => window.removeEventListener('keydown', closeOnEscape)
  }, [inline, onClose])

  if (widgets.length === 0 && !hasRuntime) return null

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
        <SceneControls state={state} disabled={disabled} onSend={onSend} />
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
                <DiceWidget widget={widget} disabled={disabled} onSend={onSend} onInsert={onInsert} adventureId={adventureId} />
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
    <div role="region" aria-label="Controles do jogo" className="fixed inset-x-0 bottom-0 z-40 max-h-[70dvh] w-full min-w-0 max-w-full shrink-0 overflow-y-auto overscroll-y-contain border-t border-subtle bg-surface/95 shadow-2xl backdrop-blur lg:static lg:z-auto lg:max-h-full lg:shadow-none">
      <div className="mx-auto w-full max-w-6xl px-3 py-2">
        <div className="mb-2 flex items-center gap-2">
          <span className="text-[10px] font-semibold uppercase tracking-wide text-muted">
            Game controls
          </span>
          <span className="flex-1" />
          <button
            onClick={onRegenerate}
            disabled={disabled || regenerating}
            title="Pedir à IA para redesenhar estes controles"
            className="min-h-11 rounded-md px-2 text-xs text-secondary hover:bg-elevated hover:text-primary disabled:opacity-40"
          >
            {regenerating ? '…' : '↻ Redesign'}
          </button>
          {onClose && (
            <button
              onClick={onClose}
              aria-label="Fechar painel"
              className="min-h-11 rounded-md px-2 text-secondary hover:bg-elevated hover:text-primary"
            >
              ✕
            </button>
          )}
        </div>
        <SceneControls state={state} disabled={disabled} onSend={onSend} />
        {/* mobile: cards em fila rolável; desktop: grade conforme a largura disponível */}
        <div className="mt-2 flex snap-x gap-3 overflow-x-auto pb-2 sm:grid sm:grid-cols-[repeat(auto-fit,minmax(min(100%,16rem),1fr))] sm:overflow-visible">

          {widgets.map((widget) => (
            <section
              key={widget.id}
              className="min-w-[16rem] shrink-0 snap-start rounded-xl border border-subtle bg-elevated/70 p-3 sm:min-w-0 sm:shrink"
            >
              <h3 className="mb-2 text-xs font-bold uppercase tracking-wide text-secondary">
                {widget.label}
              </h3>
              {widget.type === 'dice' && (
                <DiceWidget widget={widget} disabled={disabled} onSend={onSend} onInsert={onInsert} adventureId={adventureId} />
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
