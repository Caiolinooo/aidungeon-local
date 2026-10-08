import { BrowserRouter, Route, Routes } from 'react-router-dom'
import { ToastProvider } from './components/Toast.jsx'
import Home from './pages/Home.jsx'
import Play from './pages/Play.jsx'
import ScenarioEditor from './pages/ScenarioEditor.jsx'

// Roteamento da SPA (PLANO.md §4.5):
//   "/"                Home — Suas Aventuras + Nova Aventura + Importar backup
//   "/play/:id"        Tela de Jogo (feed, SSE, toolbar, drawer)
//   "/scenarios/new"   Editor de Scenario (criação)
//   "/scenarios/:id/edit"  Editor em modo somente leitura (v1 não tem PUT — contrato)
export default function App() {
  return (
    <ToastProvider>
      <BrowserRouter>
        <Routes>
          <Route path="/" element={<Home />} />
          <Route path="/play/:id" element={<Play />} />
          <Route path="/scenarios/new" element={<ScenarioEditor />} />
          <Route path="/scenarios/:id/edit" element={<ScenarioEditor />} />
          <Route path="*" element={<Home />} />
        </Routes>
      </BrowserRouter>
    </ToastProvider>
  )
}
