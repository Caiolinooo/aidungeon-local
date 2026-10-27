import { DECISION_LABEL, beatHasBody, emotionLabel, formatDelta, targetLabel } from './adjudicationView.js'

export default function TurnBeat({ beat }) {
  if (!beatHasBody(beat)) return null
  const label = DECISION_LABEL[beat.decision] || ''
  return (
    <aside className="turn-beat mb-2 rounded-lg border border-subtle/80 bg-elevated/50 px-2.5 py-1.5 text-[11px] leading-snug text-secondary">
      {(label || beat.outcome) && (
        <p className="text-primary">
          {label ? <span className="mr-1 font-semibold uppercase tracking-wide text-accent-pri">{label}</span> : null}
          {beat.outcome}
        </p>
      )}
      {beat.reactions?.length > 0 && (
        <ul className="mt-1 space-y-0.5">
          {beat.reactions.map((reaction, index) => (
            <li key={`${reaction.card_id || reaction.npc}-${index}`}>
              <span className="font-semibold text-primary">{reaction.npc}</span>
              {reaction.emotion ? <span> · {emotionLabel(reaction.emotion)}</span> : null}
              {reaction.reaction ? <span> — {reaction.reaction}</span> : null}
              {reaction.line ? <span className="italic text-primary"> “{reaction.line}”</span> : null}
            </li>
          ))}
        </ul>
      )}
      {beat.initiative?.line ? (
        <p className="mt-1">
          <span className="font-semibold text-primary">{beat.initiative.npc}</span>
          <span> age: {beat.initiative.line}</span>
        </p>
      ) : null}
      {beat.relation_changes?.length > 0 && (
        <ul className="mt-1 space-y-0.5">
          {beat.relation_changes.map((change, index) => (
            <li key={`${change.card_id}-${change.target}-${index}`}>
              <span className="font-medium text-primary">{change.name}</span>
              <span> → {targetLabel(change)} </span>
              <span className="tabular-nums text-accent-pri">{formatDelta(change.delta)}</span>
              {change.reason ? <span className="text-muted"> · {change.reason}</span> : null}
            </li>
          ))}
        </ul>
      )}
    </aside>
  )
}
