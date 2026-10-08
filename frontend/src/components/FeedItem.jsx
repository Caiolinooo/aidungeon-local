// Bloco individual do feed da história.
//
// Tipos (contrato Action.type):
//   start / continue / story → prosa corrida (story = narração injetada, itálico sutil)
//   do                     → "**You do**" + blockquote (destaque do jogador)
//   say                    → "**You say**" + blockquote com aspas
// Hover revela o botão Edit (qualquer bloco, inclusive respostas da IA).
import { playerDisplayText } from '../utils.js'

export default function FeedItem({ action, onEdit, disabled }) {
  const { type, text, edited_at } = action
  const isPlayer = type === 'do' || type === 'say'
  // do/say vêm formatados do backend ("\n> You ...\n") — exibe sem o wrapper
  const display = isPlayer ? playerDisplayText(type, text) : text

  return (
    <div className="group relative rounded-lg px-3 py-2 transition-colors hover:bg-zinc-900/70">
      <button
        onClick={() => onEdit(action)}
        disabled={disabled}
        title="Edit"
        className="absolute right-2 top-2 hidden rounded-md border border-zinc-700 bg-zinc-800 px-2 py-1 text-xs text-zinc-300 hover:bg-zinc-700 disabled:opacity-40 group-hover:block"
      >
        ✏ Edit
      </button>

      {isPlayer ? (
        <blockquote className="border-l-4 border-emerald-700/70 pl-4">
          <div className="mb-0.5 text-xs font-bold uppercase tracking-wide text-emerald-400">
            You {type}
          </div>
          <p className="whitespace-pre-wrap font-prose text-zinc-100">
            {type === 'say' ? `“${display}”` : display}
          </p>
        </blockquote>
      ) : (
        <p
          className={`whitespace-pre-wrap font-prose leading-relaxed ${
            type === 'story' ? 'italic text-zinc-300' : 'text-zinc-200'
          }`}
        >
          {text}
        </p>
      )}

      {edited_at && (
        <div className="mt-1 text-[10px] uppercase tracking-wide text-zinc-600">edited</div>
      )}
    </div>
  )
}
