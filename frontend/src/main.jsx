import React from 'react'
import ReactDOM from 'react-dom/client'
import App from './App.jsx'
import './index.css'

// 100dvh does not shrink when the virtual keyboard opens. On iOS the
// layout viewport also shifts; follow visualViewport.offsetTop.
const iosDevice = (() => {
  const ua = navigator.userAgent || ''
  if (/iPad|iPhone|iPod/.test(ua)) return true
  return navigator.platform === 'MacIntel' && navigator.maxTouchPoints > 1
})()

function lockAppHeight() {
  const vv = window.visualViewport
  const height = vv?.height ?? window.innerHeight
  const root = document.documentElement
  root.style.setProperty('--app-height', `${Math.round(height)}px`)
  if (iosDevice) {
    const offsetTop = vv?.offsetTop || 0
    root.style.transform = offsetTop ? `translateY(${offsetTop}px)` : ''
  }
  const keyboardOpen = !!vv && vv.height < window.innerHeight * 0.75
  root.classList.toggle('kb-open', keyboardOpen)
}
lockAppHeight()
window.visualViewport?.addEventListener('resize', lockAppHeight)
window.addEventListener('resize', lockAppHeight)
window.addEventListener('orientationchange', lockAppHeight)
document.addEventListener('focusin', (event) => {
  const field = event.target
  if (!(field instanceof HTMLElement)) return
  if (!field.matches('input, textarea, select')) return
  window.setTimeout(() => field.scrollIntoView({ block: 'nearest' }), 80)
})

// Register PWA service worker
if ('serviceWorker' in navigator && !window.location.hostname.includes('localhost.disabled')) {
  let refreshing = false
  // Primeira visita: o controllerchange do claim inicial é ignorado, mas o
  // flag vira true para que updates FUTUROS recarreguem a aba (PWA fica dias aberta).
  let hadController = !!navigator.serviceWorker.controller
  window.addEventListener('load', () => {
    navigator.serviceWorker
      .register('/sw.js')
      .then((reg) => {
        // SW registered
      })
      .catch((err) => {
        console.warn('SW registration skipped:', err)
      })
    navigator.serviceWorker.addEventListener('controllerchange', () => {
      if (!hadController) {
        hadController = true
        return
      }
      if (refreshing) return
      refreshing = true
      window.location.reload()
    })
  })
}

ReactDOM.createRoot(document.getElementById('root')).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>,
)
