import { useState } from 'react'

// Confirmação em dois cliques. O primeiro arma a ação e mostra a frase;
// o segundo, no mesmo botão, executa.
export function useConfirm() {
  const [pending, setPending] = useState(null)
  const ask = (id, message) => {
    if (pending?.id === id) {
      setPending(null)
      return true
    }
    setPending({ id, message })
    return false
  }
  const bar = pending ? (
    <div className="mb-2 rounded-lg border border-warn-800 bg-warn-950/80 p-2 text-xs text-warn-200">
      {pending.message} Clique de novo para confirmar.{' '}
      <button type="button" className="underline" onClick={() => setPending(null)}>
        Cancelar
      </button>
    </div>
  ) : null
  return { ask, bar }
}
