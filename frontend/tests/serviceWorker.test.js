import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import vm from 'node:vm'

test('service worker não guarda o HTML da SPA como se fosse JavaScript', async () => {
  const listeners = {}
  const writes = []
  const cache = {
    addAll: async () => {},
    put: async (request, response) => { writes.push([request, response]) },
  }
  const html = {
    status: 200,
    type: 'basic',
    headers: { get: (name) => name.toLowerCase() === 'content-type' ? 'text/html' : null },
    clone() { return this },
  }
  const context = {
    URL,
    Response,
    self: {
      location: { origin: 'https://game.example' },
      clients: { claim: async () => {} },
      addEventListener(type, handler) { listeners[type] = handler },
    },
    caches: {
      open: async () => cache,
      keys: async () => [],
      delete: async () => true,
      match: async () => undefined,
    },
    fetch: async () => html,
  }
  vm.runInNewContext(readFileSync(new URL('../public/sw.js', import.meta.url), 'utf8'), context)

  let responsePromise
  const request = { method: 'GET', url: 'https://game.example/assets/Settings-expired.js' }
  listeners.fetch({ request, respondWith(value) { responsePromise = value } })
  await responsePromise
  assert.equal(writes.length, 0)
})
