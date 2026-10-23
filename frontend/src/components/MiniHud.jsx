import { Activity, Bandage, MapPin, UserRound } from 'lucide-react'

function parseSheet(sheet) {
  if (!sheet) return {}
  if (typeof sheet === 'object') return sheet
  if (typeof sheet !== 'string') return {}
  try {
    const parsed = JSON.parse(sheet)
    return parsed && typeof parsed === 'object' ? parsed : {}
  } catch {
    return {}
  }
}

const PRIORITY = /(hp|health|vida|mana|energy|energia|vigor|stamina|gold|ouro|sanity|sanidade|stress|stress)/i

function statsFrom(profile, state, sheet) {
  const out = []
  const widgets = (profile?.widgets ?? []).filter((w) => w.type === 'stats')
  for (const widget of widgets) {
    const values = state?.[widget.id]
    if (!values || typeof values !== 'object') continue
    for (const stat of widget.stats ?? []) {
      const value = values[stat.name] ?? stat.value
      if (value == null) continue
      out.push({
        name: stat.name,
        value,
        max: stat.max,
        priority: PRIORITY.test(stat.name),
      })
    }
  }
  if (out.length === 0 && sheet.stats && typeof sheet.stats === 'object') {
    for (const [name, raw] of Object.entries(sheet.stats)) {
      const value = Number(raw)
      if (!Number.isFinite(value)) continue
      out.push({ name, value, max: null, priority: PRIORITY.test(name) })
    }
  }
  const chosen = out.filter((stat) => stat.priority)
  for (const stat of out) {
    if (chosen.length >= 4) break
    if (!chosen.includes(stat)) chosen.push(stat)
  }
  return chosen.slice(0, 4)
}

const TONE = {
  neutral: 'border-subtle bg-elevated/80 text-secondary',
  info: 'border-info/30 bg-info/10 text-info',
  good: 'border-accent-pri/35 bg-accent-pri/10 text-accent-pri',
  warn: 'border-warn-600/40 bg-warn-950/40 text-warn-200',
  danger: 'border-danger-500/40 bg-danger-950/50 text-danger-300',
}

export default function MiniHud({
  profile,
  state,
  playerCard,
  location,
  onOpenCharacter,
}) {
  const sheet = parseSheet(playerCard?.sheet)
  const body = sheet.body_state && typeof sheet.body_state === 'object'
    ? sheet.body_state
    : {}
  const pain = Number(body.pain ?? 0)
  const conditions = (Array.isArray(body.conditions) ? body.conditions : [])
    .filter((condition) => condition?.active && condition?.scene_visible !== false)
  const stats = statsFrom(profile, state, sheet)
  const hud = Array.isArray(state?._hud) ? state._hud : []
  const title = playerCard?.title || ''

  const hasAnything = stats.length > 0 || pain > 0 || conditions.length > 0
    || hud.length > 0 || Boolean(location) || Boolean(title)
  if (!hasAnything) return null

  const conditionText = conditions
    .slice(0, 4)
    .map((condition) => {
      const where = condition.location ? ` (${condition.location})` : ''
      return `${condition.description || condition.kind || 'condição'}${where}`
    })
    .join(' · ')

  return (
    <div className="shrink-0 border-b border-subtle/60 bg-surface/75 px-3 py-1.5 backdrop-blur">
      <div className="mx-auto flex max-w-[1680px] items-center gap-1.5 overflow-x-auto [scrollbar-width:none] [&::-webkit-scrollbar]:hidden">
        {title && (
          <button
            type="button"
            onClick={onOpenCharacter}
            title="Abrir a ficha do personagem"
            className="inline-flex min-h-7 shrink-0 items-center gap-1 rounded-full border border-subtle bg-elevated/80 px-2.5 text-[11px] font-semibold text-primary hover:border-accent-pri/60 transition"
          >
            <UserRound className="h-3 w-3 text-accent-pri" />
            <span className="max-w-[10rem] truncate">{title}</span>
          </button>
        )}

        {location && (
          <span
            title={location}
            className={`inline-flex min-h-7 max-w-[14rem] shrink-0 items-center gap-1 rounded-full border px-2.5 text-[11px] ${TONE.info}`}
          >
            <MapPin className="h-3 w-3" />
            <span className="truncate">{location}</span>
          </span>
        )}

        {stats.map((stat) => {
          const max = Number(stat.max)
          const value = Number(stat.value)
          const pct = Number.isFinite(max) && max > 0 ? (value / max) * 100 : null
          const low = pct !== null && pct <= 25
          return (
            <span
              key={stat.name}
              title={pct !== null ? `${stat.name}: ${value}/${max}` : `${stat.name}: ${value}`}
              className={`inline-flex min-h-7 shrink-0 items-center gap-1 rounded-full border px-2.5 text-[11px] ${
                low ? TONE.danger : TONE.neutral
              }`}
            >
              <Activity className="h-3 w-3 opacity-70" />
              <span className="font-semibold uppercase tracking-wide opacity-80">{stat.name}</span>
              <span className="font-bold tabular-nums">{stat.value}</span>
              {Number.isFinite(max) && <span className="opacity-60">/{max}</span>}
            </span>
          )
        })}

        {(pain > 0 || conditions.length > 0) && (
          <span
            title={conditionText || `Dor ${pain}/10`}
            className={`inline-flex min-h-7 shrink-0 items-center gap-1 rounded-full border px-2.5 text-[11px] ${
              pain >= 7 ? TONE.danger : TONE.warn
            }`}
          >
            <Bandage className="h-3 w-3" />
            {pain > 0 && <span>Dor {pain}/10</span>}
            {conditions.length > 0 && (
              <span className={pain > 0 ? 'opacity-80' : ''}>
                {conditions.length} cond.
              </span>
            )}
          </span>
        )}

        {hud.map((item) => (
          <span
            key={`${item.label}-${item.value}`}
            title={`${item.label}: ${item.value}`}
            className={`inline-flex min-h-7 max-w-[16rem] shrink-0 items-center gap-1 rounded-full border px-2.5 text-[11px] ${
              TONE[item.tone] || TONE.neutral
            }`}
          >
            <span className="font-semibold uppercase tracking-wide opacity-75">{item.label}</span>
            <span className="truncate">{item.value}</span>
          </span>
        ))}
      </div>
    </div>
  )
}
