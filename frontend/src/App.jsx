import { BrowserRouter, Route, Routes } from 'react-router-dom'
import AuthGate from './components/AuthGate.jsx'
import { ToastProvider } from './components/Toast.jsx'
import Home from './pages/Home.jsx'
import Login from './pages/Login.jsx'
import Play from './pages/Play.jsx'
import ScenarioEditor from './pages/ScenarioEditor.jsx'

// Roteamento da SPA (PLANO.md §4.5 + §11 auth):
//   "/login"           Login — PÚBLICA (única rota fora do AuthGate)
//   "/"                Home — Suas Aventuras + Nova Aventura + Importar backup
//   "/play/:id"        Tela de Jogo (feed, SSE, toolbar, drawer)
//   "/scenarios/new"   Editor de Scenario (criação)
//   "/scenarios/:id/edit"  Editor em modo somente leitura (v1 não tem PUT — contrato)
export default function App() {
  return (
    <ToastProvider>
      <BrowserRouter>
        <Routes>
          <Route path="/login" element={<Login />} />
          <Route path="/" element={<AuthGate><Home /></AuthGate>} />
          <Route path="/play/:id" element={<AuthGate><Play /></AuthGate>} />
          <Route path="/scenarios/new" element={<AuthGate><ScenarioEditor /></AuthGate>} />
          <Route path="/scenarios/:id/edit" element={<AuthGate><ScenarioEditor /></AuthGate>} />
          <Route path="*" element={<AuthGate><Home /></AuthGate>} />
        </Routes>
      </BrowserRouter>
    </ToastProvider>
  )
}
