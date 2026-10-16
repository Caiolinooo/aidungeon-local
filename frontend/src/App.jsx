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
            <Route path="/settings" element={<AuthGate><Settings /></AuthGate>} />
            <Route path="/scenarios/new" element={<AuthGate><ScenarioEditor /></AuthGate>} />
            <Route path="/scenarios/:id/edit" element={<AuthGate><ScenarioEditor /></AuthGate>} />
            <Route path="*" element={<AuthGate><Home /></AuthGate>} />
          </Routes>
        </BrowserRouter>
      </ToastProvider>
    </ThemeProvider>
  )
}
