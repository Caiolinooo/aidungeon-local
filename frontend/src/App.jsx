import { BrowserRouter, Route, Routes } from 'react-router-dom'
import AuthGate from './components/AuthGate.jsx'
import OfflineBanner from './components/OfflineBanner.jsx'
import { ToastProvider } from './components/Toast.jsx'
import { ThemeProvider } from './themeContext.jsx'
import Home from './pages/Home.jsx'
import Login from './pages/Login.jsx'
import Play from './pages/Play.jsx'
import ScenarioEditor from './pages/ScenarioEditor.jsx'
import Settings from './pages/Settings.jsx'
import Users from './pages/Users.jsx'
import Catalog from './pages/Catalog.jsx'
import Rulesets from './pages/Rulesets.jsx'
import VisualNovel from './pages/VisualNovel.jsx'

export default function App() {
  return (
    <ThemeProvider>
      <ToastProvider>
        <OfflineBanner />
        <BrowserRouter>
          <Routes>
            <Route path="/login" element={<Login />} />
            <Route path="/" element={<AuthGate><Home /></AuthGate>} />
            <Route path="/play/:id" element={<AuthGate><Play /></AuthGate>} />
            <Route path="/adventures/:id/novel" element={<AuthGate><VisualNovel /></AuthGate>} />
            <Route path="/catalog" element={<AuthGate><Catalog /></AuthGate>} />
            <Route path="/settings" element={<AuthGate><Settings /></AuthGate>} />
            <Route path="/users" element={<AuthGate><Users /></AuthGate>} />
            <Route path="/rulesets" element={<AuthGate><Rulesets /></AuthGate>} />
            <Route path="/scenarios/new" element={<AuthGate><ScenarioEditor /></AuthGate>} />
            <Route path="/scenarios/:id/edit" element={<AuthGate><ScenarioEditor /></AuthGate>} />
            <Route path="*" element={<AuthGate><Home /></AuthGate>} />
          </Routes>
        </BrowserRouter>
      </ToastProvider>
    </ThemeProvider>
  )
}
