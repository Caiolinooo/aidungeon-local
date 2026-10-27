import { useState } from 'react'

// Fila do mestre e o aviso de quem só pode mandar intenção.
export default function DmTray({ enabled, isDm, intents, busy, onApprove, onReject }) {
  const [rejecting, setRejecting] = useState('')
  if (!enabled) return null
  const pending = (intents || []).filter((item) => (item?.status || 'pending') === 'pending')
  if (!isDm && pending.length === 0) {
    return (
      <p className="border-t border-subtle bg-surface/95 px-4 py-2 text-xs text-muted">
        Modo DM: sua ação vai para o mestre antes de entrar na história.
      </p>
    )
  }
  if (pending.length === 0) return null

  const reject = async (intent) => {
    if (!intent?.id || rejecting) return
    setRejecting(intent.id)
    try {
      await onReject?.(intent)
    } finally {
      setRejecting('')
    }
  }

  return (
    <div className="border-t border-subtle bg-surface/95 px-4 py-2">
      <div className="mx-auto flex max-w-3xl flex-col gap-2">
        <div className="text-[10px] font-semibold uppercase tracking-wider text-muted">
          {isDm ? 'Fila do mestre' : 'Intenções com o mestre'}
        </div>
        {pending.map((intent) => (
          <div
            key={intent.id}
            className="flex items-start justify-between gap-2 rounded-lg border border-subtle bg-elevated px-3 py-2"
          >
            <div className="min-w-0">
              <div className="text-[10px] font-bold uppercase text-accent-pri">
                {intent.username} · {intent.type}
              </div>
              <p className="break-words text-sm text-primary">{intent.text}</p>
            </div>
            {isDm ? (
              <div className="flex shrink-0 gap-1">
                <button
                  type="button"
                  disabled={!!busy || rejecting === intent.id}
                  onClick={() => onApprove?.(intent)}
                  className="rounded-md bg-emerald-700 px-2 py-1 text-xs font-semibold text-white hover:bg-emerald-600 disabled:opacity-40"
                >
                  Aprovar
                </button>
                <button
                  type="button"
                  disabled={!!busy || rejecting === intent.id}
                  onClick={() => reject(intent)}
                  className="rounded-md border border-subtle px-2 py-1 text-xs text-secondary hover:text-primary disabled:opacity-40"
                >
                  Rejeitar
                </button>
              </div>
            ) : (
              <span className="shrink-0 text-[10px] font-semibold uppercase text-muted">aguardando</span>
            )}
          </div>
        ))}
      </div>
    </div>
  )
}
