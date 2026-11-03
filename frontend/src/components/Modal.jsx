import { useEffect, useId, useRef, useState } from 'react'
import { Minus } from 'lucide-react'
import { useDialog } from '../hooks/useDialog.js'

// Modal genérico: overlay escuro + painel central; fecha no clique fora ou no ✕.
// minimizable default true em modais grandes (wide). Minimizado = chip fixo no
// canto inferior direito; o painel usa display:none e não desmonta.
// Passe minimized + onMinimizedChange para controlar; senão o estado é interno.
export default function Modal({
  title,
  onClose,
  children,
  wide = false,
  minimizable,
  minimized,
  onMinimizedChange,
}) {
  const canMinimize = minimizable ?? Boolean(wide)
  const [internalMinimized, setInternalMinimized] = useState(false)
  const controlled = minimized !== undefined
  const isMinimized = controlled ? Boolean(minimized) : internalMinimized
  const restoreRef = useRef(null)
  const titleId = useId()
  const dialogRef = useDialog(!isMinimized, onClose)

  const setMinimized = (next) => {
    const value = typeof next === 'function' ? next(isMinimized) : next
    if (!controlled) setInternalMinimized(value)
    onMinimizedChange?.(value)
  }

  useEffect(() => {
    if (isMinimized) restoreRef.current?.focus()
  }, [isMinimized])

  return (
    <>
      <div
        className="fixed inset-0 z-40 flex items-end justify-center bg-canvas/70 p-3 sm:items-center sm:p-4"
        style={isMinimized ? { display: 'none' } : undefined}
        onPointerDown={(e) => {
          if (e.target === e.currentTarget) onClose?.()
        }}
      >
        <div
          ref={dialogRef}
          role="dialog"
          aria-modal="true"
          aria-labelledby={title ? titleId : undefined}
          tabIndex={-1}
          className={`flex max-h-[min(85dvh,var(--app-height,85dvh))] w-full flex-col overflow-hidden rounded-xl border border-subtle bg-elevated shadow-2xl ${
            wide ? 'max-w-2xl' : 'max-w-lg'
          }`}
        >
          <div className="flex items-center justify-between gap-2 border-b border-subtle px-5 py-3">
            <h2 id={titleId} className="min-w-0 flex-1 truncate text-lg font-semibold text-primary">{title}</h2>
            <div className="flex shrink-0 items-center gap-2">
              {canMinimize && (
                <button
                  type="button"
                  onClick={() => setMinimized(true)}
                  className="inline-flex min-h-11 min-w-11 items-center justify-center text-secondary hover:text-primary"
                  aria-label="Minimizar"
                >
                  <Minus className="h-5 w-5" aria-hidden="true" />
                </button>
              )}
              <button
                type="button"
                onClick={onClose}
                className="inline-flex min-h-11 min-w-11 items-center justify-center text-secondary hover:text-primary"
                aria-label="Fechar"
              >
                ✕
              </button>
            </div>
          </div>
          <div className="overflow-y-auto px-5 py-4">{children}</div>
        </div>
      </div>
      {isMinimized && (
        <div className="fixed bottom-[max(1rem,env(safe-area-inset-bottom))] right-4 z-40 flex max-w-[min(20rem,calc(100vw-2rem))] items-center gap-2 rounded-full border border-subtle bg-elevated py-1.5 pl-3 pr-1.5 shadow-2xl">
          <span className="truncate text-sm font-medium text-primary">{title}</span>
          <button
            ref={restoreRef}
            type="button"
            onClick={() => setMinimized(false)}
            className="shrink-0 rounded-full bg-accent-pri px-3 py-1 text-xs font-semibold text-canvas hover:bg-accent-hov"
            aria-label="Restaurar"
          >
            Restaurar
          </button>
        </div>
      )}
    </>
  )
}
