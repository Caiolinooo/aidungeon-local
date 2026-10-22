import { createContext, useCallback, useContext, useState } from 'react'

// Toasts globais (erros de API/SSE e confirmações). Auto-dismiss em 6s.
const ToastCtx = createContext(null)
export const useToast = () => useContext(ToastCtx)

let nextId = 1

export function ToastProvider({ children }) {
  const [toasts, setToasts] = useState([])

  const dismiss = useCallback((id) => {
    setToasts((list) => list.filter((t) => t.id !== id))
  }, [])

  const push = useCallback(
    (message, kind = 'error') => {
      const id = nextId++
      setToasts((list) => [...list, { id, message, kind }])
      setTimeout(() => dismiss(id), 6000)
    },
    [dismiss],
  )

  return (
    <ToastCtx.Provider value={push}>
      {children}
      <div role="status" aria-live="polite" className="pointer-events-none fixed inset-x-3 bottom-[max(0.75rem,env(safe-area-inset-bottom))] z-50 flex flex-col gap-2 sm:inset-x-auto sm:right-4 sm:w-80">
        {toasts.map((t) => (
          <div
            key={t.id}
            className={`pointer-events-auto rounded-lg border px-4 py-3 text-sm shadow-lg ${
              t.kind === 'error'
                ? 'border-red-800 bg-red-950/95 text-red-200'
                : 'border-emerald-800 bg-emerald-950/95 text-emerald-200'
            }`}
          >
            <div className="flex items-start justify-between gap-2">
              <span className="break-words">{t.message}</span>
              <button
                onClick={() => dismiss(t.id)}
                className="shrink-0 text-zinc-400 hover:text-zinc-100"
                aria-label="Dispensar"
              >
                ✕
              </button>
            </div>
          </div>
        ))}
      </div>
    </ToastCtx.Provider>
  )
}
