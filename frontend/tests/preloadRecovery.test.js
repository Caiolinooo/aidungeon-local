import test from 'node:test'
import assert from 'node:assert/strict'
import { installPreloadRecovery } from '../src/preloadRecovery.js'

function fakeWindow() {
  const listeners = {}
  return {
    listeners,
    reloads: 0,
    sessionStorage: {
      values: new Map(),
      getItem(key) { return this.values.get(key) ?? null },
      setItem(key, value) { this.values.set(key, value) },
    },
    addEventListener(type, handler) { listeners[type] = handler },
    location: {
      reload() { this.owner.reloads++ },
      owner: null,
    },
  }
}

test('vite:preloadError recarrega uma vez a cada 30 segundos', () => {
  const target = fakeWindow()
  target.location.owner = target
  let now = 100000
  installPreloadRecovery(target, () => now)
  let prevented = false
  const event = { preventDefault() { prevented = true } }

  target.listeners['vite:preloadError'](event)
  assert.equal(prevented, true)
  assert.equal(target.reloads, 1)

  target.listeners['vite:preloadError'](event)
  assert.equal(target.reloads, 1)
  now += 30001
  target.listeners['vite:preloadError'](event)
  assert.equal(target.reloads, 2)
})
