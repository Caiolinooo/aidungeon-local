import assert from 'node:assert/strict'
import test from 'node:test'
import { api } from '../src/api.js'

function json(data, status = 200) {
  return new Response(JSON.stringify(data), {
    status,
    headers: { 'content-type': 'application/json' },
  })
}

test('narrator_voice fica em cache e muda no PUT ou no delete, sem GET extra', async () => {
  const calls = []
  let narrator = 'F2'
  globalThis.localStorage = {
    getItem: () => null,
    setItem() {},
    removeItem() {},
  }
  globalThis.fetch = async (url, opts = {}) => {
    const method = opts.method || 'GET'
    calls.push({ url: String(url), method })
    const path = String(url)
    if (path.endsWith('/api/voices') && method === 'GET') {
      return json({ voices: ['F2', 'M1'], narrator_voice: narrator, cloned: [] })
    }
    if (path.endsWith('/api/settings/voice') && method === 'PUT') {
      const body = JSON.parse(opts.body)
      if (body.narrator_voice) narrator = body.narrator_voice
      return json({ narrator_voice: narrator, voices: ['F2', 'M1'] })
    }
    if (path.includes('/api/voices/') && method === 'DELETE') {
      return json({ id: path.split('/').pop() })
    }
    return json({})
  }

  const gets = () => calls.filter((call) => call.url.endsWith('/api/voices') && call.method === 'GET')

  assert.equal(await api.voice.narratorVoice(), 'F2')
  assert.equal(await api.voice.narratorVoice(), 'F2')
  assert.equal(gets().length, 1)

  await api.voice.putSettings({ narrator_voice: 'cv_abc' })
  assert.equal(await api.voice.narratorVoice(), 'cv_abc')
  assert.equal(gets().length, 1)

  await api.putVoiceSettings({ narrator_voice: 'M3' })
  assert.equal(await api.voice.narratorVoice(), 'M3')
  assert.equal(gets().length, 1)

  await api.voice.remove('M3')
  assert.equal(await api.voice.narratorVoice(), 'M1')
  assert.equal(gets().length, 1)
})
