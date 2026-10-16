import { useState } from 'react'
import { Navigate, useNavigate } from 'react-router-dom'
import { api } from '../api.js'
import { getToken, setToken } from '../auth.js'
import { Lock, User, Eye, EyeOff, ShieldCheck } from 'lucide-react'

// Login Page (public route "/login" — SPA statics do not require a token).
// Success: POST /api/auth/login → setToken → redirect to "/".
export default function Login() {
  const navigate = useNavigate()
  const [username, setUsername] = useState('')
  const [password, setPassword] = useState('')
  const [showPassword, setShowPassword] = useState(false)
  const [error, setError] = useState(null)
  const [loading, setLoading] = useState(false)

  // Already authenticated → go directly to Home
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
      if (err.status === 401) setError('Invalid username or password.')
      else if (err.status === 429) setError('Too many attempts. Please wait 1 minute.')
      else if (err.status === 503) setError('Authentication is not configured on the server.')
      else if (err.status === 0) setError('Cannot reach the backend server.')
      else setError(err.message || 'Error signing in.')
      setLoading(false)
    }
  }

  return (
    <div className="flex h-full items-center justify-center overflow-y-auto px-4 py-10 bg-canvas text-primary">
      <div className="w-full max-w-sm rounded-2xl border border-subtle bg-surface p-8 shadow-2xl">
        <header className="mb-6 text-center">
          <div className="mx-auto mb-3 flex h-12 w-12 items-center justify-center rounded-2xl bg-accent-pri/10 border border-accent-pri/20 text-accent-pri">
            <ShieldCheck className="h-6 w-6" />
          </div>
          <h1 className="text-2xl font-bold tracking-tight text-primary">
            AI Dungeon <span className="text-accent-pri">Local</span>
          </h1>
          <p className="mt-1 text-xs text-muted">Sign in to continue your adventure.</p>
        </header>

        <form onSubmit={submit} className="flex flex-col gap-4">
          <label className="flex flex-col gap-1.5">
            <span className="text-xs font-semibold uppercase tracking-wider text-muted">Username</span>
            <div className="relative">
              <User className="absolute left-3 top-2.5 h-4 w-4 text-muted pointer-events-none" />
              <input
                type="text"
                value={username}
                onChange={(e) => setUsername(e.target.value)}
                autoComplete="username"
                autoFocus
                required
                placeholder="Enter your username"
                className="w-full rounded-xl border border-subtle bg-surface-input pl-9 pr-3 py-2 text-xs text-primary placeholder-muted outline-none focus:border-accent-pri transition"
              />
            </div>
          </label>

          <label className="flex flex-col gap-1.5">
            <span className="text-xs font-semibold uppercase tracking-wider text-muted">Password</span>
            <div className="relative">
              <Lock className="absolute left-3 top-2.5 h-4 w-4 text-muted pointer-events-none" />
              <input
                type={showPassword ? 'text' : 'password'}
                value={password}
                onChange={(e) => setPassword(e.target.value)}
                autoComplete="current-password"
                required
                placeholder="Enter your password"
                className="w-full rounded-xl border border-subtle bg-surface-input pl-9 pr-9 py-2 text-xs text-primary placeholder-muted outline-none focus:border-accent-pri transition"
              />
              <button
                type="button"
                onClick={() => setShowPassword(!showPassword)}
                aria-label={showPassword ? 'Hide password' : 'Show password'}
                className="absolute right-3 top-2.5 text-muted hover:text-primary transition"
              >
                {showPassword ? <EyeOff className="h-4 w-4" /> : <Eye className="h-4 w-4" />}
              </button>
            </div>
          </label>

          {error && (
            <p className="rounded-xl border border-red-900/80 bg-red-950/50 px-3.5 py-2.5 text-xs text-red-200">
              {error}
            </p>
          )}

          <button
            type="submit"
            disabled={loading || !username || !password}
            className="mt-2 min-h-11 rounded-xl bg-accent-pri px-4 py-2.5 text-xs font-semibold text-white shadow-md hover:bg-accent-hov disabled:cursor-not-allowed disabled:opacity-50 transition"
          >
            {loading ? 'Signing in…' : 'Sign In'}
          </button>
        </form>
      </div>
    </div>
  )
}
