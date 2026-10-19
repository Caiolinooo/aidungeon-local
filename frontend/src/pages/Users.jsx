import { useCallback, useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { api } from '../api.js'
import { useToast } from '../components/Toast.jsx'

// Caio administra as contas. Cada pessoa troca a própria senha aqui.
export default function Users() {
  const toast = useToast()
  const [me, setMe] = useState(null)
  const [users, setUsers] = useState(null)
  const [current, setCurrent] = useState('')
  const [next, setNext] = useState('')
  const [saving, setSaving] = useState(false)
  const [name, setName] = useState('')
  const [password, setPassword] = useState('')
  const [creating, setCreating] = useState(false)
  const [resetFor, setResetFor] = useState(null)
  const [resetPassword, setResetPassword] = useState('')
  const [removing, setRemoving] = useState(null)

  const load = useCallback(async () => {
    const session = await api.checkAuth()
    setMe(session)
    if (session.admin) setUsers(await api.listUsers())
  }, [])

  useEffect(() => {
    load().catch((e) => toast(e.message))
  }, [load, toast])

  const changeOwn = async () => {
    setSaving(true)
    try {
      await api.changePassword(current, next)
      setCurrent('')
      setNext('')
      toast('Password updated.')
    } catch (e) {
      toast(e.message)
    } finally {
      setSaving(false)
    }
  }

  const create = async () => {
    setCreating(true)
    try {
      await api.createUser(name.trim(), password)
      setName('')
      setPassword('')
      setUsers(await api.listUsers())
    } catch (e) {
      toast(e.message)
    } finally {
      setCreating(false)
    }
  }

  const reset = async () => {
    if (!resetFor) return
    try {
      await api.resetUserPassword(resetFor, resetPassword)
      setResetFor(null)
      setResetPassword('')
      toast('Password updated.')
    } catch (e) {
      toast(e.message)
    }
  }

  const remove = async () => {
    if (!removing) return
    try {
      await api.deleteUser(removing)
      setRemoving(null)
      setUsers(await api.listUsers())
    } catch (e) {
      toast(e.message)
    }
  }

  return (
    <div className="mx-auto flex h-full max-w-2xl flex-col gap-6 overflow-y-auto px-4 py-8 text-zinc-100">
      <div>
        <Link to="/" className="text-sm text-emerald-400 hover:underline">← Adventures</Link>
        <h1 className="mt-2 text-2xl font-bold">Account</h1>
        {me && <p className="text-sm text-zinc-500">Signed in as {me.user}{me.admin ? ' · admin' : ''}</p>}
      </div>

      <section className="rounded-xl border border-zinc-800 bg-zinc-900 p-4">
        <h2 className="mb-3 text-sm font-semibold uppercase tracking-wide text-zinc-500">Change your password</h2>
        <div className="flex flex-col gap-2">
          <input
            type="password"
            value={current}
            onChange={(e) => setCurrent(e.target.value)}
            placeholder="Current password"
            autoComplete="current-password"
            className="rounded-lg border border-zinc-700 bg-zinc-800 px-3 py-2 text-sm outline-none focus:border-emerald-600"
          />
          <input
            type="password"
            value={next}
            onChange={(e) => setNext(e.target.value)}
            placeholder="New password (8 or more characters)"
            autoComplete="new-password"
            className="rounded-lg border border-zinc-700 bg-zinc-800 px-3 py-2 text-sm outline-none focus:border-emerald-600"
          />
          <button
            onClick={changeOwn}
            disabled={saving || current.length < 1 || next.length < 8}
            className="self-end rounded-lg bg-emerald-700 px-4 py-2 text-sm font-semibold text-white hover:bg-emerald-600 disabled:opacity-50"
          >
            {saving ? 'Saving…' : 'Update password'}
          </button>
        </div>
      </section>

      {me?.admin && (
        <section className="rounded-xl border border-zinc-800 bg-zinc-900 p-4">
          <h2 className="mb-1 text-sm font-semibold uppercase tracking-wide text-zinc-500">People</h2>
          <p className="mb-3 text-sm text-zinc-500">
            Each person sees only their own stories. You see every story.
          </p>
          <div className="mb-4 flex flex-col gap-2 sm:flex-row">
            <input
              value={name}
              onChange={(e) => setName(e.target.value)}
              placeholder="Username"
              className="flex-1 rounded-lg border border-zinc-700 bg-zinc-800 px-3 py-2 text-sm outline-none focus:border-emerald-600"
            />
            <input
              type="password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              placeholder="Password"
              className="flex-1 rounded-lg border border-zinc-700 bg-zinc-800 px-3 py-2 text-sm outline-none focus:border-emerald-600"
            />
            <button
              onClick={create}
              disabled={creating || name.trim().length < 2 || password.length < 8}
              className="rounded-lg bg-emerald-700 px-4 py-2 text-sm font-semibold text-white hover:bg-emerald-600 disabled:opacity-50"
            >
              {creating ? 'Creating…' : 'Create'}
            </button>
          </div>
          {users === null ? (
            <p className="text-sm text-zinc-500">Loading…</p>
          ) : (
            <ul className="divide-y divide-zinc-800">
              {users.map((user) => (
                <li key={user.username} className="flex items-center justify-between gap-2 py-2">
                  <div>
                    <div className="font-medium">{user.username}{user.role === 'admin' ? ' · admin' : ''}</div>
                    <div className="text-xs text-zinc-500">{user.adventure_count} stories</div>
                  </div>
                  <div className="flex gap-2">
                    <button
                      onClick={() => { setResetFor(user.username); setResetPassword('') }}
                      className="rounded-lg border border-zinc-700 px-3 py-1.5 text-xs text-zinc-300 hover:bg-zinc-800"
                    >
                      Set password
                    </button>
                    {user.role !== 'admin' && (
                      <button
                        onClick={() => setRemoving(user.username)}
                        className="rounded-lg border border-red-900 px-3 py-1.5 text-xs text-red-300 hover:bg-red-950"
                      >
                        Delete
                      </button>
                    )}
                  </div>
                </li>
              ))}
            </ul>
          )}
        </section>
      )}

      {resetFor && (
        <div className="rounded-xl border border-zinc-700 bg-zinc-900 p-4">
          <p className="mb-2 text-sm">New password for {resetFor}</p>
          <div className="flex gap-2">
            <input
              type="password"
              value={resetPassword}
              onChange={(e) => setResetPassword(e.target.value)}
              className="flex-1 rounded-lg border border-zinc-700 bg-zinc-800 px-3 py-2 text-sm outline-none focus:border-emerald-600"
            />
            <button onClick={() => setResetFor(null)} className="rounded-lg border border-zinc-700 px-3 py-2 text-sm">Cancel</button>
            <button
              onClick={reset}
              disabled={resetPassword.length < 8}
              className="rounded-lg bg-emerald-700 px-3 py-2 text-sm font-semibold text-white disabled:opacity-50"
            >
              Save
            </button>
          </div>
        </div>
      )}

      {removing && (
        <div className="rounded-xl border border-red-900 bg-red-950/40 p-4">
          <p className="text-sm text-red-100">
            Delete {removing} and every story that belongs to them? This cannot be undone.
          </p>
          <div className="mt-3 flex justify-end gap-2">
            <button onClick={() => setRemoving(null)} className="rounded-lg border border-zinc-700 px-3 py-2 text-sm">Cancel</button>
            <button onClick={remove} className="rounded-lg bg-red-700 px-3 py-2 text-sm font-semibold text-white">
              Delete user
            </button>
          </div>
        </div>
      )}
    </div>
  )
}
