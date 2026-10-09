import { useState } from 'react'
import { Navigate, useNavigate } from 'react-router-dom'
import { api } from '../api.js'
import { getToken, setToken } from '../auth.js'

// Página de Login (rota "/login", PÚBLICA — estáticos da SPA não exigem token).
// Sucesso: POST /api/auth/login → setToken → redirect para "/".
// Erros amigáveis: 401 "Usuário ou senha inválidos" · 429 "Muitas tentativas…".
export default function Login() {
  const navigate = useNavigate()
  const [username, setUsername] = useState('')
  const [password, setPassword] = useState('')
  const [error, setError] = useState(null)
  const [loading, setLoading] = useState(false)

  // já autenticado → vai direto para a Home (o AuthGate valida o token lá)
  if (getToken()) return <Navigate to="/" replace />

  const submit = async (e) => {
    e.preventDefault()
    if (loading) return
    setError(null)
    setLoading(true)
    try {
      const res = await api.login(username.trim(), password)
      setToken(res.token)
      navigate('/', { replace: true })
    } catch (err) {
      if (err.status === 401) setError('Usuário ou senha inválidos.')
      else if (err.status === 429) setError('Muitas tentativas, aguarde 1 minuto.')
      else if (err.status === 503) setError('Autenticação não configurada no servidor.')
      else if (err.status === 0) setError('Não foi possível alcançar o servidor.')
      else setError(err.message || 'Erro ao entrar.')
      setLoading(false)
    }
  }

  return (
    <div className="flex h-full items-center justify-center overflow-y-auto px-4 py-10">
      <div className="w-full max-w-sm rounded-2xl border border-zinc-800 bg-zinc-900 p-8 shadow-2xl">
        <header className="mb-6 text-center">
          <h1 className="text-2xl font-bold tracking-tight text-zinc-100">
            AI Dungeon <span className="text-emerald-500">Local</span>
          </h1>
          <p className="mt-1 text-sm text-zinc-500">Entre para continuar sua história.</p>
        </header>

        <form onSubmit={submit} className="flex flex-col gap-4">
          <label className="flex flex-col gap-1">
            <span className="text-xs font-semibold uppercase tracking-wide text-zinc-500">Usuário</span>
            <input
              type="text"
              value={username}
              onChange={(e) => setUsername(e.target.value)}
              autoComplete="username"
              autoFocus
              required
              className="rounded-lg border border-zinc-700 bg-zinc-800 px-3 py-2 text-sm text-zinc-100 outline-none focus:border-emerald-600"
            />
          </label>

          <label className="flex flex-col gap-1">
            <span className="text-xs font-semibold uppercase tracking-wide text-zinc-500">Senha</span>
            <input
              type="password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              autoComplete="current-password"
              required
              className="rounded-lg border border-zinc-700 bg-zinc-800 px-3 py-2 text-sm text-zinc-100 outline-none focus:border-emerald-600"
            />
          </label>

          {error && (
            <p className="rounded-lg border border-red-900 bg-red-950/60 px-3 py-2 text-sm text-red-200">
              {error}
            </p>
          )}

          <button
            type="submit"
            disabled={loading || !username || !password}
            className="mt-1 rounded-lg bg-emerald-700 px-4 py-2.5 text-sm font-semibold text-white hover:bg-emerald-600 disabled:cursor-not-allowed disabled:opacity-50"
          >
            {loading ? 'Entrando…' : 'Entrar'}
          </button>
        </form>
      </div>
    </div>
  )
}
