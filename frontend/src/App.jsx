import { lazy, Suspense } from 'react'
import { BrowserRouter, Route, Routes } from 'react-router-dom'
import AuthGate from './components/AuthGate.jsx'
import OfflineBanner from './components/OfflineBanner.jsx'
import { ToastProvider } from './components/Toast.jsx'
import { ThemeProvider } from './themeContext.jsx'
import Login from './pages/Login.jsx'

const Home = lazy(() => import('./pages/Home.jsx'))
const Play = lazy(() => import('./pages/Play.jsx'))
const ScenarioEditor = lazy(() => import('./pages/ScenarioEditor.jsx'))
const Settings = lazy(() => import('./pages/Settings.jsx'))
const Users = lazy(() => import('./pages/Users.jsx'))
const Catalog = lazy(() => import('./pages/Catalog.jsx'))
const Rulesets = lazy(() => import('./pages/Rulesets.jsx'))
const VisualNovel = lazy(() => import('./pages/VisualNovel.jsx'))

const routeFallback = (
  <div className="flex h-full items-center justify-center text-sm text-secondary">Carregando…</div>
)

function Page({ children }) {
  return <Suspense fallback={routeFallback}>{children}</Suspense>
}

export default function App() {
  return (
    <ThemeProvider>
      <ToastProvider>
        <div className="flex h-full flex-col">
          <OfflineBanner />
          <div className="relative flex min-h-0 flex-1 flex-col overflow-hidden">
            <div className="absolute inset-0">
              <BrowserRouter>
                <Routes>
                  <Route path="/login" element={<Login />} />
                  <Route path="/" element={<AuthGate><Page><Home /></Page></AuthGate>} />
                  <Route path="/play/:id" element={<AuthGate><Page><Play /></Page></AuthGate>} />
                  <Route path="/adventures/:id/novel" element={<AuthGate><Page><VisualNovel /></Page></AuthGate>} />
                  <Route path="/catalog" element={<AuthGate><Page><Catalog /></Page></AuthGate>} />
                  <Route path="/settings" element={<AuthGate><Page><Settings /></Page></AuthGate>} />
                  <Route path="/users" element={<AuthGate><Page><Users /></Page></AuthGate>} />
                  <Route path="/rulesets" element={<AuthGate><Page><Rulesets /></Page></AuthGate>} />
                  <Route path="/scenarios/new" element={<AuthGate><Page><ScenarioEditor /></Page></AuthGate>} />
                  <Route path="/scenarios/:id/edit" element={<AuthGate><Page><ScenarioEditor /></Page></AuthGate>} />
                  <Route path="*" element={<AuthGate><Page><Home /></Page></AuthGate>} />
                </Routes>
              </BrowserRouter>
            </div>
          </div>
        </div>
      </ToastProvider>
    </ThemeProvider>
  )
}
