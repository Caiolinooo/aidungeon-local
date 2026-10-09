import React from 'react'
import ReactDOM from 'react-dom/client'
import App from './App.jsx'
import './index.css'

// 100dvh não encolhe quando o teclado abre. A altura visível segura o app.
function lockAppHeight() {
  const height = window.visualViewport?.height ?? window.innerHeight
  document.documentElement.style.setProperty('--app-height', `${Math.round(height)}px`)
}
lockAppHeight()
window.visualViewport?.addEventListener('resize', lockAppHeight)
window.visualViewport?.addEventListener('scroll', lockAppHeight)
window.addEventListener('orientationchange', lockAppHeight)
document.addEventListener('focusin', (event) => {
  const field = event.target
  if (!(field instanceof HTMLElement)) return
  if (!field.matches('input, textarea, select')) return
  window.setTimeout(() => field.scrollIntoView({ block: 'nearest' }), 80)
})

ReactDOM.createRoot(document.getElementById('root')).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>,
)
