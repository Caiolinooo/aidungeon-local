const RELOAD_AT_KEY = 'aidungeon-preload-reload-at'

export function installPreloadRecovery(target = window, now = () => Date.now()) {
  target.addEventListener('vite:preloadError', (event) => {
    event.preventDefault()
    const lastReload = Number(target.sessionStorage.getItem(RELOAD_AT_KEY) || 0)
    if (now() - lastReload < 30000) return
    target.sessionStorage.setItem(RELOAD_AT_KEY, String(now()))
    target.location.reload()
  })
}
