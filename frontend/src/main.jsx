import React from 'react'
import ReactDOM from 'react-dom/client'
import App from './App.jsx'
import './index.css'

// 100dvh does not shrink when virtual keyboard opens.
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

// Register PWA service worker
if ('serviceWorker' in navigator && !window.location.hostname.includes('localhost.disabled')) {
  window.addEventListener('load', () => {
    navigator.serviceWorker
      .register('/sw.js')
      .then((reg) => {
        // SW registered
      })
      .catch((err) => {
        console.warn('SW registration skipped:', err)
      })
  })
}

ReactDOM.createRoot(document.getElementById('root')).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>,
)
