import { useEffect, useState } from 'react'
import { api } from '../api.js'
import Modal from './Modal.jsx'
import { useToast } from './Toast.jsx'

const SHARE_ERRORS = {
  user_not_found: 'Usuário não encontrado',
  already_shared: 'Este usuário já participa da aventura',
  cannot_share_owner: 'Não é possível compartilhar com o dono',
}

function shareErrorText(e) {
  const fallback = SHARE_ERRORS[e?.code]
  const msg = e?.message || ''
  if (fallback && (!msg || msg === e.code || msg.startsWith('Request failed'))) return fallback
  return msg || fallback || 'Não foi possível compartilhar'
}

// Modal de compartilhamento: participantes, adicionar/remover e jogo por turno.
export default function ShareModal({ adventureId, owner, onClose, onChanged }) {
  const toast = useToast()
  const [sharing, setSharing] = useState(null)
  const [username, setUsername] = useState('')
  const [busy, setBusy] = useState(false)
  const [savingTurn, setSavingTurn] = useState(false)

  useEffect(() => {
    let cancelled = false
    api.getSharing(adventureId)
      .then((dict) => { if (!cancelled) setSharing(dict) })
      .catch((e) => { if (!cancelled) toast(shareErrorText(e)) })
    return () => { cancelled = true }
  }, [adventureId, toast])

  const apply = (dict) => {
    setSharing(dict)
    onChanged?.(dict)
  }

  const add = async () => {
    const name = username.trim()
    if (!name || busy) return
    setBusy(true)
    try {
      const dict = await api.shareAdventure(adventureId, name)
      setUsername('')
      apply(dict)
    } catch (e) {
      toast(shareErrorText(e))
    } finally {
      setBusy(false)
    }
  }

  const remove = async (name) => {
    if (busy) return
    setBusy(true)
    try {
      apply(await api.unshareAdventure(adventureId, name))
    } catch (e) {
      toast(shareErrorText(e))
    } finally {
      setBusy(false)
    }
  }

  const toggleTurn = async () => {
    if (!sharing || savingTurn) return
    const next = !sharing.turn_mode
    setSavingTurn(true)
    try {
      const updated = await api.updateAdventure(adventureId, { turn_mode: next })
      let dict = {
        ...sharing,
        owner: updated?.owner ?? sharing.owner,
        turn_mode: updated?.turn_mode ?? next,
        turn_user: updated?.turn_user ?? sharing.turn_user,
        shares: updated?.shares ?? sharing.shares,
      }
      try {
        dict = await api.getSharing(adventureId)
      } catch { /* o PATCH já trouxe turn_mode / turn_user */ }
      apply(dict)
    } catch (e) {
      toast(e.message)
    } finally {
      setSavingTurn(false)
    }
  }

  const ownerName = sharing?.owner || owner || ''
  const seen = new Set()
  const listed = []
  if (ownerName) {
    seen.add(ownerName)
    listed.push({ name: ownerName, role: 'owner' })
  }
  for (const name of sharing?.shares ?? []) {
    if (!name || seen.has(name)) continue
    seen.add(name)
    listed.push({ name, role: 'share' })
  }
  for (const name of sharing?.participants ?? []) {
    if (!name || seen.has(name)) continue
    seen.add(name)
    listed.push({ name, role: 'member' })
  }

  return (
    <Modal title="👥 Compartilhar aventura" onClose={onClose} minimizable>
      <div className="flex flex-col gap-4">
        <ul className="flex max-h-52 flex-col gap-1.5 overflow-y-auto">
          {!sharing && listed.length === 0 ? (
            <li className="text-sm text-zinc-500">Carregando…</li>
          ) : listed.length === 0 ? (
            <li className="text-sm text-zinc-500">Nenhum participante.</li>
          ) : listed.map((p) => (
            <li
              key={`${p.role}-${p.name}`}
              className="flex items-center justify-between gap-2 rounded-lg border border-zinc-800 bg-zinc-800/50 px-3 py-2"
            >
              <span className="truncate text-sm text-zinc-100">{p.name}</span>
              {p.role === 'owner' ? (
                <span className="shrink-0 rounded bg-zinc-700 px-1.5 py-0.5 text-[10px] font-semibold uppercase tracking-wide text-zinc-300">
                  dono
                </span>
              ) : p.role === 'share' ? (
                <button
                  type="button"
                  onClick={() => remove(p.name)}
                  disabled={busy}
                  aria-label={`Remover ${p.name}`}
                  title={`Remover ${p.name}`}
                  className="shrink-0 rounded px-2 py-1 text-sm text-zinc-400 hover:bg-zinc-700 hover:text-zinc-100 disabled:opacity-40"
                >
                  ✕
                </button>
              ) : null}
            </li>
          ))}
        </ul>

        <form
          onSubmit={(e) => {
            e.preventDefault()
            add()
          }}
          className="flex gap-2"
        >
          <input
            value={username}
            onChange={(e) => setUsername(e.target.value)}
            placeholder="nome de usuário"
            autoComplete="off"
            aria-label="Usuário para compartilhar"
            className="min-w-0 flex-1 rounded-lg border border-zinc-700 bg-zinc-800 px-3 py-2 text-sm text-zinc-100 outline-none focus:border-emerald-600"
          />
          <button
            type="submit"
            disabled={busy || !username.trim()}
            className="rounded-lg bg-emerald-700 px-4 py-2 text-sm font-semibold text-white hover:bg-emerald-600 disabled:opacity-50"
          >
            Adicionar
          </button>
        </form>

        <div className="rounded-lg border border-zinc-800 bg-zinc-950/40 p-3">
          <div className="flex items-center justify-between gap-3">
            <div className="min-w-0">
              <div className="text-sm font-medium text-zinc-100">🎲 Jogo por turno</div>
              <p className="mt-0.5 text-xs text-zinc-500">os jogadores alternam a vez a cada ação</p>
            </div>
            <button
              type="button"
              role="switch"
              aria-checked={!!sharing?.turn_mode}
              aria-label="Jogo por turno"
              disabled={!sharing || savingTurn}
              onClick={toggleTurn}
              className={`relative h-6 w-11 shrink-0 rounded-full transition-colors disabled:opacity-50 ${
                sharing?.turn_mode ? 'bg-accent-pri' : 'bg-zinc-700'
              }`}
            >
              <span
                className={`absolute top-0.5 left-0.5 h-5 w-5 rounded-full bg-white transition-transform ${
                  sharing?.turn_mode ? 'translate-x-5' : 'translate-x-0'
                }`}
              />
            </button>
          </div>
          {sharing?.turn_mode ? (
            <p className="mt-2 text-xs font-medium text-accent-pri">
              Vez de {sharing.turn_user || '—'}
            </p>
          ) : null}
        </div>
      </div>
    </Modal>
  )
}
