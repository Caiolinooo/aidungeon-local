import { useEffect, useState } from 'react'
import { Navigate, useNavigate } from 'react-router-dom'
import { api } from '../api.js'
import { clearToken, getToken } from '../auth.js'

// Guard das rotas protegidas:
//  - sem token → Navigate para /login;
//  - com token → valida UMA vez via GET /api/auth/check:
//      · ok → libera;
//      · 401 → clearToken + /login (token inválido/expirado);
//      · falha de rede/servidor → libera mesmo assim (o banner de "backend
//        fora do ar" das telas já cobre esse caso);
//  - qualquer 401 da API durante a sessão (evento 'aidl:unauthorized'
//    disparado por api.js/sse.js) → volta ao /login.
export default function AuthGate({ children }) {
  const navigate = useNavigate()
  const token = getToken()
  const [checked, setChecked] = useState(false)

  useEffect(() => {
    const onUnauthorized = () => navigate('/login', { replace: true })
    window.addEventListener('aidl:unauthorized', onUnauthorized)
    return () => window.removeEventListener('aidl:unauthorized', onUnauthorized)
  }, [navigate])

  useEffect(() => {
    if (!token) return
    let cancelled = false
    api.checkAuth()
      .then(() => {
        if (!cancelled) setChecked(true)
      })
      .catch((e) => {
        if (cancelled) return
        if (e.status === 401) {
          clearToken()
          navigate('/login', { replace: true })
        } else {
          setChecked(true) // backend fora do ar: deixa seguir (banner cobre)
        }
      })
    return () => {
      cancelled = true
    }
  }, [token, navigate])

  if (!token) return <Navigate to="/login" replace />
  if (!checked) {
    return (
      <div className="flex h-full items-center justify-center text-sm text-zinc-500">
        Checking session…
      </div>
    )
  }
  return children
}
