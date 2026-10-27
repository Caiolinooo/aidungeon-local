import { useState } from 'react'
import { createQuest, deleteQuest, transitionQuest } from '../../api/quests.js'

const STATUS_LABEL = {
  hidden: 'Oculta',
  planned: 'Planejada',
  available: 'Disponível',
  accepted: 'Aceita',
  completed: 'Concluída',
  failed: 'Falhou',
}

const NEXT = {
  hidden: [
    { status: 'planned', label: 'Planejar' },
    { status: 'available', label: 'Disponibilizar' },
  ],
  planned: [
    { status: 'available', label: 'Disponibilizar' },
  ],
  available: [
    { status: 'accepted', label: 'Aceitar' },
  ],
  accepted: [
    { status: 'completed', label: 'Concluir' },
    { status: 'failed', label: 'Falhar' },
  ],
  failed: [
    { status: 'planned', label: 'Replanejar' },
  ],
  completed: [],
}

const FACTS = [
  { id: 'hp', label: 'PV', numeric: true, op: 'lte' },
  { id: 'gold', label: 'Ouro', numeric: true, op: 'gte' },
  { id: 'san', label: 'SAN', numeric: true, op: 'lte' },
  { id: 'item', label: 'Item', numeric: false },
  { id: 'location', label: 'Local', numeric: false },
  { id: 'relation', label: 'Relação', numeric: true, op: 'gte' },
]

const OPS = [
  { id: 'lte', label: '≤' },
  { id: 'lt', label: '<' },
  { id: 'eq', label: '=' },
  { id: 'gte', label: '≥' },
  { id: 'gt', label: '>' },
  { id: 'ne', label: '≠' },
]

const REL_STATS = ['trust', 'fear', 'affinity', 'relationship', 'distrust', 'desire', 'boldness']

function Bar({ value, max }) {
  const pct = max > 0 ? Math.max(0, Math.min(100, (value / max) * 100)) : 0
  return (
    <div className="h-2 overflow-hidden rounded-full bg-elevated">
      <div className="h-full bg-accent-pri" style={{ width: `${pct}%` }} />
    </div>
  )
}

function triggerLine(trigger) {
  const when = (trigger.when || []).map((clause) => {
    if (clause.fact === 'item') return `item ${clause.name}`
    if (clause.fact === 'location') return `local ${clause.title || clause.place || clause.location_id || ''}`
    if (clause.fact === 'relation') {
      const who = clause.card_id || `${clause.a || ''}–${clause.b || ''}`
      return `relação ${who} ${clause.stat || ''} ${clause.op || 'gte'} ${clause.value}`
    }
    return `${clause.fact} ${clause.op || ''} ${clause.value ?? ''}`
  }).join(' e ')
  const effect = (trigger.apply || []).map((row) => row.status || (row.progress_delta ? `+${row.progress_delta}` : 'progresso')).join(', ')
  return `${when} → ${effect}${trigger.applied ? ' · aplicado' : ''}`
}

export default function QuestPanel({ adventureId, quests, triggers, busy, packOn, onRun }) {
  const [title, setTitle] = useState('')
  const [description, setDescription] = useState('')
  const [status, setStatus] = useState('planned')
  const [stagesText, setStagesText] = useState('')
  const [useTrigger, setUseTrigger] = useState(false)
  const [fact, setFact] = useState('hp')
  const [op, setOp] = useState('lte')
  const [amount, setAmount] = useState('5')
  const [name, setName] = useState('')
  const [who, setWho] = useState('')
  const [stat, setStat] = useState('trust')
  const [thenStatus, setThenStatus] = useState('available')

  const rows = Array.isArray(quests) ? quests : []
  const rules = Array.isArray(triggers) ? triggers : []

  const submit = (event) => {
    event.preventDefault()
    const nextTitle = title.trim()
    if (!nextTitle || busy) return
    const stages = stagesText.split(',').map((part) => part.trim()).filter(Boolean).map((part) => ({ title: part }))
    const body = {
      title: nextTitle,
      description: description.trim(),
      status,
      stages,
    }
    if (useTrigger) {
      const clause = { fact }
      if (fact === 'item') {
        if (!name.trim()) return
        clause.name = name.trim()
        clause.qty = 1
      } else if (fact === 'location') {
        if (!name.trim()) return
        clause.title = name.trim()
      } else if (fact === 'relation') {
        if (!who.trim() || amount === '' || Number.isNaN(Number(amount))) return
        clause.card_id = who.trim()
        clause.stat = stat
        clause.op = op
        clause.value = Number(amount)
      } else {
        if (amount === '' || Number.isNaN(Number(amount))) return
        clause.op = op
        clause.value = Number(amount)
      }
      body.triggers = [{ when: [clause], apply: [{ status: thenStatus }] }]
    }
    onRun(async () => {
      const next = await createQuest(adventureId, body)
      setTitle('')
      setDescription('')
      setStagesText('')
      setName('')
      setWho('')
      setUseTrigger(false)
      return next
    })
  }

  return (
    <section className="flex flex-col gap-3 rounded-xl border border-subtle p-3">
      <div className="flex items-center justify-between gap-2">
        <div className="font-semibold">Missões</div>
        <span className="text-[11px] text-muted">{packOn ? 'Pack ligado' : 'Pack desligado'}</span>
      </div>
      {!packOn && (
        <p className="text-[11px] text-muted">
          O pack está desligado. As missões locais continuam salvas neste estado.
        </p>
      )}
      {rows.length === 0 && <p className="text-[11px] text-muted">Nenhuma missão.</p>}
      <ul className="flex flex-col gap-2">
        {rows.map((quest) => {
          const progress = quest.progress || { value: 0, target: 1 }
          const ownTriggers = rules.filter((trigger) => (trigger.apply || []).some((effect) => effect.quest_id === quest.id))
          const actions = NEXT[quest.status] || []
          const locked = quest.status === 'completed' || quest.status === 'failed'
          return (
            <li key={quest.id} className="flex flex-col gap-1 rounded-lg bg-elevated p-2">
              <div className="flex items-start justify-between gap-2">
                <div>
                  <div className="font-semibold">{quest.title}</div>
                  <div className="text-[11px] text-muted">{STATUS_LABEL[quest.status] || quest.status}</div>
                </div>
                <button
                  type="button"
                  disabled={busy}
                  onClick={() => onRun(() => deleteQuest(adventureId, quest.id))}
                  className="text-[11px] text-danger-300 hover:underline disabled:opacity-40"
                >
                  apagar
                </button>
              </div>
              {quest.description && <p className="text-[11px] text-secondary">{quest.description}</p>}
              <div className="text-[11px] text-muted">Progresso {progress.value}/{progress.target}</div>
              <Bar value={progress.value || 0} max={progress.target || 1} />
              {(quest.stages || []).length > 0 && (
                <ul className="flex flex-col gap-1">
                  {quest.stages.map((stage) => (
                    <li key={stage.id} className="flex items-center gap-2 text-[11px]">
                      <input
                        type="checkbox"
                        checked={!!stage.done}
                        disabled={busy || locked}
                        onChange={(e) => onRun(() => transitionQuest(adventureId, quest.id, {
                          stage_id: stage.id,
                          done: e.target.checked,
                        }))}
                      />
                      <span className={stage.done ? 'text-muted line-through' : ''}>{stage.title}</span>
                    </li>
                  ))}
                </ul>
              )}
              {ownTriggers.map((trigger) => (
                <p key={trigger.id} className="text-[11px] text-info">{triggerLine(trigger)}</p>
              ))}
              <div className="flex flex-wrap gap-1">
                {actions.map((action) => (
                  <button
                    key={action.status}
                    type="button"
                    disabled={busy}
                    onClick={() => onRun(() => transitionQuest(adventureId, quest.id, { status: action.status }))}
                    className="rounded-lg bg-accent-pri px-2 py-0.5 text-[11px] font-semibold text-canvas disabled:opacity-40"
                  >
                    {action.label}
                  </button>
                ))}
              </div>
            </li>
          )
        })}
      </ul>
      <form className="flex flex-col gap-2" onSubmit={submit}>
        <div className="text-[11px] text-muted">Nova missão</div>
        <input
          value={title}
          onChange={(e) => setTitle(e.target.value)}
          placeholder="título"
          className="rounded-lg border border-subtle bg-surface px-2 py-1 text-xs outline-none focus:border-accent-pri"
        />
        <textarea
          value={description}
          onChange={(e) => setDescription(e.target.value)}
          placeholder="descrição"
          rows={2}
          className="rounded-lg border border-subtle bg-surface px-2 py-1 text-xs outline-none focus:border-accent-pri"
        />
        <input
          value={stagesText}
          onChange={(e) => setStagesText(e.target.value)}
          placeholder="estágios, separados por vírgula"
          className="rounded-lg border border-subtle bg-surface px-2 py-1 text-xs outline-none focus:border-accent-pri"
        />
        <select
          value={status}
          onChange={(e) => setStatus(e.target.value)}
          className="rounded-lg border border-subtle bg-surface px-2 py-1 text-xs outline-none focus:border-accent-pri"
        >
          {Object.entries(STATUS_LABEL).map(([id, label]) => (
            <option key={id} value={id}>{label}</option>
          ))}
        </select>
        <label className="flex items-center gap-2 text-[11px]">
          <input type="checkbox" checked={useTrigger} onChange={(e) => setUseTrigger(e.target.checked)} />
          Gatilho de estado
        </label>
        {useTrigger && (
          <div className="flex flex-col gap-1">
            <div className="flex gap-1">
              <select
                value={fact}
                onChange={(e) => {
                  const next = FACTS.find((row) => row.id === e.target.value)
                  setFact(e.target.value)
                  if (next?.op) setOp(next.op)
                }}
                className="rounded-lg border border-subtle bg-surface px-2 py-1 text-xs outline-none focus:border-accent-pri"
              >
                {FACTS.map((row) => <option key={row.id} value={row.id}>{row.label}</option>)}
              </select>
              {(fact === 'hp' || fact === 'gold' || fact === 'san' || fact === 'relation') && (
                <select
                  value={op}
                  onChange={(e) => setOp(e.target.value)}
                  className="rounded-lg border border-subtle bg-surface px-2 py-1 text-xs outline-none focus:border-accent-pri"
                >
                  {OPS.map((row) => <option key={row.id} value={row.id}>{row.label}</option>)}
                </select>
              )}
            </div>
            {fact === 'relation' && (
              <div className="flex gap-1">
                <input
                  value={who}
                  onChange={(e) => setWho(e.target.value)}
                  placeholder="personagem"
                  className="min-w-0 flex-1 rounded-lg border border-subtle bg-surface px-2 py-1 text-xs outline-none focus:border-accent-pri"
                />
                <select
                  value={stat}
                  onChange={(e) => setStat(e.target.value)}
                  className="rounded-lg border border-subtle bg-surface px-2 py-1 text-xs outline-none focus:border-accent-pri"
                >
                  {REL_STATS.map((row) => <option key={row} value={row}>{row}</option>)}
                </select>
              </div>
            )}
            {(fact === 'item' || fact === 'location') && (
              <input
                value={name}
                onChange={(e) => setName(e.target.value)}
                placeholder={fact === 'item' ? 'nome do item' : 'nome do local'}
                className="rounded-lg border border-subtle bg-surface px-2 py-1 text-xs outline-none focus:border-accent-pri"
              />
            )}
            {(fact === 'hp' || fact === 'gold' || fact === 'san' || fact === 'relation') && (
              <input
                type="number"
                value={amount}
                onChange={(e) => setAmount(e.target.value)}
                className="rounded-lg border border-subtle bg-surface px-2 py-1 text-xs outline-none focus:border-accent-pri"
              />
            )}
            <select
              value={thenStatus}
              onChange={(e) => setThenStatus(e.target.value)}
              className="rounded-lg border border-subtle bg-surface px-2 py-1 text-xs outline-none focus:border-accent-pri"
            >
              {Object.entries(STATUS_LABEL).map(([id, label]) => (
                <option key={id} value={id}>{label}</option>
              ))}
            </select>
          </div>
        )}
        <button
          type="submit"
          disabled={busy || !title.trim()}
          className="rounded-lg bg-accent-pri px-2 py-1 text-[11px] font-semibold text-canvas disabled:opacity-40"
        >
          Criar missão
        </button>
      </form>
    </section>
  )
}
