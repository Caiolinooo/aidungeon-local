import { useCallback, useEffect, useState } from 'react'
import { api } from '../../api.js'
import { useToast } from '../Toast.jsx'

const PACKS = [
  { id: '', label: 'Nenhum' },
  { id: 'd20', label: 'D20' },
  { id: 'd100', label: 'd100' },
]

const LIFE_BUTTONS = [
  { label: 'Esperar 1h', activity: 'other', minutes: 60 },
  { label: 'Dormir 8h', activity: 'sleep', minutes: 480 },
  { label: 'Comer', activity: 'eat', minutes: 30 },
  { label: 'Banho', activity: 'wash', minutes: 20 },
  { label: 'Socializar 1h', activity: 'social', minutes: 60 },
]

const NEED_ORDER = ['fome', 'energia', 'higiene', 'social', 'diversao', 'bexiga']
const NEED_LABEL = {
  fome: 'Fome',
  energia: 'Energia',
  higiene: 'Higiene',
  social: 'Social',
  diversao: 'Diversão',
  bexiga: 'Bexiga',
}

function Bar({ value, max, tone = 'bg-accent-pri' }) {
  const pct = max > 0 ? Math.max(0, Math.min(100, (value / max) * 100)) : 0
  return (
    <div className="h-2 overflow-hidden rounded-full bg-elevated">
      <div className={`h-full ${tone}`} style={{ width: `${pct}%` }} />
    </div>
  )
}

function Field({ label, value, onCommit, disabled }) {
  const [draft, setDraft] = useState(value ?? '')
  useEffect(() => setDraft(value ?? ''), [value])
  return (
    <label className="flex flex-col gap-1 text-[11px] text-muted">
      {label}
      <input
        type="number"
        disabled={disabled}
        value={draft}
        onChange={(e) => setDraft(e.target.value)}
        onBlur={() => {
          if (draft === '' || draft == null) return
          const next = Number(draft)
          if (!Number.isFinite(next) || next === value) return
          onCommit(next)
        }}
        className="rounded-lg border border-subtle bg-surface px-2 py-1 text-xs text-primary outline-none focus:border-accent-pri disabled:opacity-40"
      />
    </label>
  )
}

export default function SimTab({ adventure, onAdventureChange, version = 0 }) {
  const toast = useToast()
  const [view, setView] = useState(null)
  const [busy, setBusy] = useState(false)
  const [itemName, setItemName] = useState('')
  const [skillName, setSkillName] = useState('')
  const [expr, setExpr] = useState('4dF')
  const [roll, setRoll] = useState(null)
  const [resolution, setResolution] = useState(null)
  const [weapon, setWeapon] = useState('')
  const [skillCheck, setSkillCheck] = useState('')

  const id = adventure?.id

  const load = useCallback(async () => {
    if (!id) return
    try {
      setView(await api.simGet(id))
    } catch (e) {
      toast(e.message)
    }
  }, [id, toast])

  useEffect(() => {
    load()
  }, [load, version])

  const applyView = (next) => {
    setView(next)
    if (onAdventureChange && adventure) {
      onAdventureChange({
        ...adventure,
        sim_state: { ...(adventure.sim_state || {}), pack: next?.pack || '' },
      })
    }
  }

  const run = async (fn) => {
    if (!id || busy) return
    setBusy(true)
    try {
      applyView(await fn())
    } catch (e) {
      toast(e.message)
    } finally {
      setBusy(false)
    }
  }

  if (!id) return null
  const actor = view?.actor || null
  const life = view?.life || null
  const lifeOn = !!life?.enabled
  const resources = actor?.resources || {}
  const hp = actor?.hp
  const hpMax = actor?.hp_max
  const inventory = Array.isArray(actor?.inventory) ? actor.inventory : []
  const proficient = Array.isArray(actor?.proficient) ? actor.proficient : []
  const needs = actor?.needs || null
  const npcNames = Object.fromEntries((life?.npcs || []).map((npc) => [npc.card_id, npc.name || npc.card_id]))

  const save = (body) => run(() => api.simPutActor(id, body))

  const act = (body) => run(async () => {
    const payload = await api.simAction(id, body)
    setResolution(payload?.resolution || null)
    return payload
  })

  return (
    <div className="flex flex-col gap-4 text-xs text-primary">
      <label className="flex flex-col gap-1 text-[11px] text-muted">
        Sistema
        <select
          value={view?.pack || ''}
          disabled={busy || !view}
          onChange={(e) => run(() => api.simSetPack(id, e.target.value))}
          className="rounded-lg border border-subtle bg-surface px-2 py-1.5 text-xs text-primary outline-none focus:border-accent-pri"
        >
          {PACKS.map((pack) => (
            <option key={pack.id || 'none'} value={pack.id}>{pack.label}</option>
          ))}
        </select>
      </label>

      <label className="flex items-center gap-2 text-xs">
        <input
          type="checkbox"
          checked={lifeOn}
          disabled={busy || !view}
          onChange={(e) => run(() => api.simSetLife(id, e.target.checked))}
        />
        Modo Vida
      </label>

      {actor && (
        <section className="flex flex-col gap-2 rounded-xl border border-subtle p-3">
          <div className="font-semibold">{actor.name || 'Personagem'}</div>
          {hpMax != null && (
            <div>
              <div className="mb-1 flex justify-between text-[11px] text-muted">
                <span>PV {hp}/{hpMax}</span>
                {actor.ac != null && <span>CA {actor.ac}</span>}
              </div>
              <Bar value={hp || 0} max={hpMax} tone={actor.down ? 'bg-danger-600' : 'bg-accent-hov'} />
            </div>
          )}
          <div className="grid grid-cols-2 gap-2">
            <Field label="PV" value={hp} disabled={busy} onCommit={(n) => save({ hp: n })} />
            <Field label="PV máx." value={hpMax} disabled={busy} onCommit={(n) => save({ hp_max: n })} />
            <Field label="Nível" value={actor.level} disabled={busy} onCommit={(n) => save({ level: n })} />
            <Field label="XP" value={actor.xp} disabled={busy} onCommit={(n) => save({ xp: n })} />
            <Field label="Ouro" value={resources.gold ?? 0} disabled={busy} onCommit={(n) => save({ gold: n })} />
            {resources.san != null && (
              <div className="flex flex-col justify-end text-[11px] text-muted">
                SAN
                <span className="text-sm text-primary">{resources.san}</span>
              </div>
            )}
          </div>
          {actor.down && <p className="text-[11px] text-danger-300">Condição: caído</p>}

          <div>
            <div className="mb-1 text-[11px] text-muted">Inventário</div>
            <ul className="flex flex-col gap-1">
              {inventory.map((row) => (
                <li key={row.name} className="flex items-center justify-between gap-2">
                  <span>{row.name} × {row.qty}</span>
                  <span className="flex items-center gap-2">
                    <button
                      type="button"
                      disabled={busy}
                      className="text-[11px] text-accent-pri hover:underline disabled:opacity-40"
                      onClick={() => act({ kind: 'use_item', item: row.name })}
                    >
                      usar
                    </button>
                    <button
                      type="button"
                      disabled={busy}
                      className="text-[11px] text-danger-300 hover:underline disabled:opacity-40"
                      onClick={() => save({
                        inventory: inventory
                          .map((item) => item.name === row.name ? { ...item, qty: item.qty - 1 } : item)
                          .filter((item) => item.qty > 0),
                      })}
                    >
                      remover
                    </button>
                  </span>
                </li>
              ))}
            </ul>
            <form
              className="mt-2 flex gap-1"
              onSubmit={(e) => {
                e.preventDefault()
                const name = itemName.trim()
                if (!name) return
                const next = inventory.some((row) => row.name === name)
                  ? inventory.map((row) => row.name === name ? { ...row, qty: row.qty + 1 } : row)
                  : [...inventory, { name, qty: 1 }]
                setItemName('')
                save({ inventory: next })
              }}
            >
              <input
                value={itemName}
                onChange={(e) => setItemName(e.target.value)}
                placeholder="item"
                className="min-w-0 flex-1 rounded-lg border border-subtle bg-surface px-2 py-1 text-xs outline-none focus:border-accent-pri"
              />
              <button type="submit" disabled={busy} className="rounded-lg bg-accent-pri px-2 py-1 text-[11px] font-semibold text-canvas disabled:opacity-40">
                +
              </button>
            </form>
          </div>

          <div>
            <div className="mb-1 text-[11px] text-muted">Perícias treinadas</div>
            <div className="flex flex-wrap gap-1">
              {proficient.map((name) => (
                <button
                  key={name}
                  type="button"
                  disabled={busy}
                  onClick={() => save({ proficient: proficient.filter((item) => item !== name) })}
                  className="rounded-full border border-subtle px-2 py-0.5 text-[11px] hover:bg-elevated"
                >
                  {name} ×
                </button>
              ))}
            </div>
            <form
              className="mt-2 flex gap-1"
              onSubmit={(e) => {
                e.preventDefault()
                const name = skillName.trim()
                if (!name || proficient.includes(name)) return
                setSkillName('')
                save({ proficient: [...proficient, name] })
              }}
            >
              <input
                value={skillName}
                onChange={(e) => setSkillName(e.target.value)}
                placeholder="perícia"
                className="min-w-0 flex-1 rounded-lg border border-subtle bg-surface px-2 py-1 text-xs outline-none focus:border-accent-pri"
              />
              <button type="submit" disabled={busy} className="rounded-lg border border-subtle px-2 py-1 text-[11px] disabled:opacity-40">
                treinar
              </button>
            </form>
          </div>
        </section>
      )}

      {life && (
        <section className="flex flex-col gap-2 rounded-xl border border-subtle p-3">
          <div className="font-semibold">
            Dia {life.day} ({life.weekday}, {life.season}), {life.clock}
          </div>
          {needs && lifeOn && (
            <div className="flex flex-col gap-1.5">
              <div className="text-[11px] text-muted">Humor {moodOf(needs)}</div>
              {NEED_ORDER.map((key) => {
                const value = needs[key] ?? 0
                const tone = value < 15 ? 'bg-danger-600' : value < 25 ? 'bg-warn-500' : 'bg-accent-pri'
                return (
                  <div key={key}>
                    <div className="mb-0.5 flex justify-between text-[11px] text-muted">
                      <span>{NEED_LABEL[key]}{value < 15 ? ' ⚠' : ''}</span>
                      <span>{value}</span>
                    </div>
                    <Bar value={value} max={100} tone={tone} />
                  </div>
                )
              })}
            </div>
          )}
          {lifeOn && (
            <div className="flex flex-wrap gap-1">
              {LIFE_BUTTONS.map((button) => (
                <button
                  key={button.label}
                  type="button"
                  disabled={busy}
                  onClick={() => run(() => api.simAdvance(id, button.minutes, button.activity))}
                  className="rounded-lg border border-subtle px-2 py-1 text-[11px] hover:bg-elevated disabled:opacity-40"
                >
                  {button.label}
                </button>
              ))}
            </div>
          )}
          {lifeOn && (life.reactions || []).length > 0 && (
            <div>
              <div className="mb-1 text-[11px] text-muted">Reações</div>
              <ul className="flex max-h-28 flex-col gap-0.5 overflow-y-auto text-[11px]">
                {(life.reactions || []).slice().reverse().map((item, index) => (
                  <li key={`${item.day}-${item.clock}-${index}`} className="text-warn-200">
                    <span className="text-muted">D{item.day} {item.clock}</span> {item.text}
                  </li>
                ))}
              </ul>
            </div>
          )}
          {(life.npcs || []).length > 0 && (
            <ul className="flex flex-col gap-1 text-[11px] text-secondary">
              {life.npcs.map((npc) => (
                <li key={npc.card_id}>
                  {npc.name || 'NPC'}: {npc.activity || '…'}{npc.place ? ` (${npc.place})` : ''}
                </li>
              ))}
            </ul>
          )}
          {(life.relations || []).length > 0 && (
            <ul className="flex flex-col gap-1 text-[11px] text-secondary">
              {life.relations.map((rel) => (
                <li key={`${rel.a}|${rel.b}`}>
                  {(npcNames[rel.a] || rel.a)}–{(npcNames[rel.b] || rel.b)}: {rel.label}
                </li>
              ))}
            </ul>
          )}
        </section>
      )}

      {(view?.foes || []).length > 0 && (
        <section className="flex flex-col gap-2 rounded-xl border border-subtle p-3">
          <div className="text-[11px] text-muted">Inimigos</div>
          <input
            value={weapon}
            onChange={(e) => setWeapon(e.target.value)}
            placeholder="arma (opcional)"
            className="rounded-lg border border-subtle bg-surface px-2 py-1 text-xs outline-none focus:border-accent-pri"
          />
          <ul className="flex flex-col gap-1">
            {view.foes.map((foe) => (
              <li key={foe.name} className="flex items-center justify-between gap-2">
                <span>{foe.name}: {foe.hp}/{foe.hp_max} PV</span>
                <button
                  type="button"
                  disabled={busy}
                  onClick={() => act({ kind: 'attack', target: foe.name, weapon: weapon.trim(), hostile: true })}
                  className="rounded-lg bg-danger-700/80 px-2 py-0.5 text-[11px] font-semibold text-white hover:bg-danger-600 disabled:opacity-40"
                >
                  Atacar
                </button>
              </li>
            ))}
          </ul>
        </section>
      )}

      {actor && view?.pack && (
        <section className="flex flex-col gap-2 rounded-xl border border-subtle p-3">
          <div className="text-[11px] text-muted">Ações rápidas</div>
          <form
            className="flex gap-1"
            onSubmit={(e) => {
              e.preventDefault()
              const name = skillCheck.trim()
              if (!name) return
              act({ kind: 'skill', skill: name, difficulty: 'medium' })
            }}
          >
            <input
              value={skillCheck}
              onChange={(e) => setSkillCheck(e.target.value)}
              placeholder={proficient.length ? `ex.: ${proficient[0]}` : 'perícia ou habilidade'}
              className="min-w-0 flex-1 rounded-lg border border-subtle bg-surface px-2 py-1 text-xs outline-none focus:border-accent-pri"
            />
            <button type="submit" disabled={busy} className="rounded-lg bg-accent-pri px-2 py-1 text-[11px] font-semibold text-canvas disabled:opacity-40">
              Testar
            </button>
          </form>
          {proficient.length > 0 && (
            <div className="flex flex-wrap gap-1">
              {proficient.map((name) => (
                <button
                  key={name}
                  type="button"
                  disabled={busy}
                  onClick={() => act({ kind: 'skill', skill: name, difficulty: 'medium' })}
                  className="rounded-full border border-subtle px-2 py-0.5 text-[11px] hover:bg-elevated disabled:opacity-40"
                >
                  {name}
                </button>
              ))}
            </div>
          )}
          {hpMax != null && (
            <div className="flex flex-wrap gap-1">
              <button
                type="button"
                disabled={busy}
                onClick={() => act({ kind: 'rest', minutes: 60 })}
                className="rounded-lg border border-subtle px-2 py-1 text-[11px] hover:bg-elevated disabled:opacity-40"
              >
                Descanso 1h
              </button>
              <button
                type="button"
                disabled={busy}
                onClick={() => act({ kind: 'rest', minutes: 480 })}
                className="rounded-lg border border-subtle px-2 py-1 text-[11px] hover:bg-elevated disabled:opacity-40"
              >
                Sono 8h
              </button>
            </div>
          )}
          {resolution && (
            <div className="flex flex-col gap-0.5 rounded-lg bg-elevated p-2 text-[11px]">
              {(resolution.steps || []).map((step, index) => (
                <div key={index} className="text-info">{step.text}</div>
              ))}
              {(resolution.changes || []).map((row, index) => (
                <div key={`c${index}`} className="text-muted">
                  {row.who} {row.what} {row.from}→{row.to}
                </div>
              ))}
              {resolution.time && (
                <div className="text-muted">
                  +{resolution.time.minutes} min (agora {resolution.time.to}, dia {resolution.time.day})
                </div>
              )}
              {(resolution.time?.reactions || []).map((text, index) => (
                <div key={`r${index}`} className="text-warn-200">{text}</div>
              ))}
            </div>
          )}
        </section>
      )}

      <form
        className="flex flex-col gap-2 rounded-xl border border-subtle p-3"
        onSubmit={async (e) => {
          e.preventDefault()
          const text = expr.trim()
          if (!text || busy) return
          setBusy(true)
          try {
            setRoll(await api.simRoll(id, text))
          } catch (err) {
            toast(err.message)
          } finally {
            setBusy(false)
          }
        }}
      >
        <div className="text-[11px] text-muted">Dado livre (3d6, 4dF, d100)</div>
        <div className="flex gap-1">
          <input
            value={expr}
            onChange={(e) => setExpr(e.target.value)}
            className="min-w-0 flex-1 rounded-lg border border-subtle bg-surface px-2 py-1 text-xs outline-none focus:border-accent-pri"
          />
          <button type="submit" disabled={busy} className="rounded-lg bg-accent-pri px-2 py-1 text-[11px] font-semibold text-canvas disabled:opacity-40">
            Rolar
          </button>
        </div>
        {roll && (
          <p className="text-[11px] text-secondary">
            {roll.expr}: [{(roll.rolls || []).join(', ')}] {roll.modifier ? (roll.modifier > 0 ? `+${roll.modifier}` : roll.modifier) : ''} = {roll.total}
          </p>
        )}
      </form>
    </div>
  )
}

function moodOf(needs) {
  const values = NEED_ORDER.map((key) => Number(needs?.[key] ?? 0))
  const avg = values.reduce((sum, n) => sum + n, 0) / values.length
  if (avg >= 75) return 'Ótimo'
  if (avg >= 55) return 'Bem'
  if (avg >= 35) return 'Ok'
  if (avg >= 15) return 'Mal'
  return 'Péssimo'
}
