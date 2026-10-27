import assert from 'node:assert/strict'
import test from 'node:test'
import {
  commandForKey,
  initialCursor,
  isEnd,
  loadVisualNovel,
  mapNovel,
  positionLabel,
  realImageRef,
  reduceNovel,
  selectChoice,
} from '../src/api/visualNovel.js'

function action(partial) {
  return {
    id: partial.seq,
    adventure_id: 'adv-1',
    text: '',
    created_at: '',
    actor: '',
    image: null,
    ...partial,
  }
}

test('ações vazias não inventam beats', () => {
  const novel = mapNovel({ adventure: { id: 'adv-1', title: 'Vazio', actions: [] } })
  assert.deepEqual(novel.beats, [])
  assert.equal(novel.title, 'Vazio')
  assert.equal(positionLabel(0, 0), '0 / 0')
})

test('ordena por seq, propaga see e troca pelo cenário quando ele é posterior', () => {
  const novel = mapNovel({
    adventure: {
      id: 'adv-1',
      title: 'Porto',
      actions: [
        action({ seq: 2, type: 'see', text: 'O cais', created_at: '2026-01-01T00:00:03Z', image: { id: 'see1', url: '/api/images/see1' } }),
        action({ seq: 0, type: 'start', text: 'A Taverna abre.', created_at: '2026-01-01T00:00:01Z' }),
        action({ seq: 1, type: 'continue', text: 'A névoa cobre a Taverna.', created_at: '2026-01-01T00:00:02Z' }),
        action({ seq: 3, type: 'continue', text: 'A noite cai na Taverna.', created_at: '2026-01-01T00:00:05Z' }),
      ],
    },
    cards: [{ id: 'loc', type: 'location', title: 'Taverna' }],
    map: { locations: [{ id: 'loc', title: 'Taverna', image: { id: 'loc1', url: '/api/images/loc1' } }] },
    jobs: [
      {
        type: 'scene_backdrop',
        status: 'done',
        finished_at: '2026-01-01T00:00:04Z',
        result: { image: { id: 'bg1', url: '/api/images/bg1' } },
      },
      {
        type: 'see_image',
        status: 'done',
        finished_at: '2026-01-01T00:00:03Z',
        result: { image: { id: 'ignore', url: '/api/images/ignore' } },
      },
      {
        type: 'scene_backdrop',
        status: 'done',
        finished_at: '',
        result: { image: { id: 'untimed', url: '/api/images/untimed' } },
      },
    ],
  })
  assert.deepEqual(novel.beats.map((beat) => beat.seq), [0, 1, 2, 3])
  assert.equal(novel.beats[0].background.url, '/api/images/loc1')
  assert.equal(novel.beats[0].locationTitle, 'Taverna')
  assert.equal(novel.beats[1].background.url, '/api/images/loc1')
  assert.equal(novel.beats[2].background.url, '/api/images/see1')
  assert.equal(novel.beats[2].kind, 'scene')
  assert.equal(novel.beats[3].background.url, '/api/images/bg1')
})

test('retrato real entra no elenco e mídia falsa fica de fora', () => {
  const novel = mapNovel({
    adventure: {
      id: 'adv-1',
      actions: [
        action({
          seq: 1,
          type: 'say',
          actor: 'Mira',
          text: '\n> You say "para a porta"\n',
          created_at: '2026-01-01T00:00:02Z',
        }),
      ],
    },
    cards: [
      { id: 'c1', type: 'character', title: 'Mira', portrait: { id: 'p1', url: '/api/images/p1' } },
      { id: 'c2', type: 'character', title: 'Sem Foto', portrait: null, portrait_image_id: '' },
      { id: 'c3', type: 'character', title: 'Sprite', portrait: { id: '', url: '/sprites/hero.png' } },
    ],
  })
  const beat = novel.beats[0]
  assert.equal(beat.kind, 'dialogue')
  assert.equal(beat.speaker, 'Mira')
  assert.equal(beat.text, 'para a porta')
  assert.equal(beat.portrait.url, '/api/images/p1')
  assert.equal(beat.background, null)
  assert.equal(realImageRef({ url: '/sprites/hero.png' }), null)
  assert.equal(realImageRef({ id: 'port-1' }).url, '/api/images/port-1')
})

test('sugestões persistidas marcam só o caminho gravado', () => {
  const novel = mapNovel({
    adventure: {
      id: 'adv-1',
      actions: [
        action({ seq: 0, type: 'start', text: 'Abertura', created_at: '2026-01-01T00:00:01Z' }),
        action({ seq: 1, type: 'do', text: '\n> You open the door\n', created_at: '2026-01-01T00:00:02Z' }),
        action({ seq: 2, type: 'continue', text: 'A porta cede.', created_at: '2026-01-01T00:00:03Z' }),
        action({ seq: 3, type: 'say', text: '\n> You say "quem está aí"\n', created_at: '2026-01-01T00:00:04Z' }),
      ],
    },
    events: [
      { type: 'choices_ready', event_id: 1, payload: { for_seq: 0, choices: [
        { type: 'do', text: 'wait' },
        { type: 'do', text: 'open the door' },
      ] } },
      { type: 'choices_ready', event_id: 2, payload: { for_seq: 0, choices: [
        { type: 'do', text: 'open the door' },
        { type: 'say', text: 'hello' },
      ] } },
      { type: 'token', event_id: 3, payload: { for_seq: 0, choices: [{ type: 'do', text: 'ignored' }] } },
    ],
  })
  assert.equal(novel.beats[0].choices[0].taken, true)
  assert.equal(novel.beats[0].choices[0].text, 'open the door')
  assert.equal(novel.beats[0].choices[0].actionSeq, 1)
  assert.equal(novel.beats[0].choices[1].taken, false)
  assert.equal(novel.beats[0].choices[1].text, 'hello')
  assert.equal(novel.beats[0].choices[1].actionSeq, null)
  assert.deepEqual(novel.beats[1].choices, [])
  assert.equal(novel.beats[2].choices[0].text, 'quem está aí')
  assert.equal(novel.beats[2].choices[0].taken, true)
  assert.equal(novel.beats[2].choices.length, 1)
  assert.deepEqual(novel.beats[3].choices, [])
})

test('navegação, fim, recomeço e escolha fora do registro', () => {
  const beats = [{ seq: 1, type: 'do' }, { seq: 2, type: 'say' }, { seq: 3, type: 'continue' }]
  let cursor = initialCursor()
  cursor = reduceNovel(cursor, { type: 'next' }, beats.length)
  assert.equal(cursor.index, 1)
  cursor = reduceNovel(cursor, { type: 'end' }, beats.length)
  assert.equal(isEnd(cursor, beats.length), true)
  assert.equal(positionLabel(cursor.index, beats.length), 'fim · 3')
  cursor = reduceNovel(cursor, { type: 'prev' }, beats.length)
  assert.equal(cursor.index, 2)
  assert.equal(isEnd(cursor, beats.length), false)
  cursor = reduceNovel(cursor, { type: 'restart' }, beats.length)
  assert.deepEqual(cursor, { index: 0, rejected: '' })

  const stayed = selectChoice({ index: 0, rejected: '' }, { type: 'say', text: 'hello', taken: false, actionSeq: null }, beats)
  assert.equal(stayed.index, 0)
  assert.equal(stayed.rejected, 'hello')
  const jumped = selectChoice(stayed, { type: 'say', text: 'oi', taken: true, actionSeq: 2 }, beats)
  assert.equal(jumped.index, 1)
  assert.equal(jumped.rejected, '')

  assert.deepEqual(commandForKey('ArrowRight', {}), { type: 'next' })
  assert.deepEqual(commandForKey('Home', { tag: 'BUTTON' }), { type: 'restart' })
  assert.equal(commandForKey(' ', { tag: 'BUTTON' }), null)
  assert.equal(commandForKey('ArrowLeft', { ctrlKey: true }), null)
  assert.equal(commandForKey('ArrowLeft', { tag: 'TEXTAREA' }), null)
  assert.equal(reduceNovel({ index: 0, rejected: '' }, { type: 'next' }, 0).index, 0)
})

test('loadVisualNovel só usa GET autenticado e sobrevive sem mapa', async () => {
  const calls = []
  globalThis.localStorage = {
    getItem: (key) => (key === 'aidl_token' ? 'token-1' : null),
    setItem() {},
    removeItem() {},
  }
  globalThis.fetch = async (url, opts = {}) => {
    const method = opts.method || 'GET'
    calls.push({ url: String(url), method, authorization: opts.headers?.Authorization })
    const path = String(url)
    if (method !== 'GET') return json({ error: { message: 'no' } }, 500)
    if (path.endsWith('/api/adventures/adv%2F1')) {
      return json({
        id: 'adv/1',
        title: 'Real',
        actions: [action({ seq: 0, type: 'start', text: 'Era uma vez.', created_at: '2026-01-01T00:00:01Z' })],
      })
    }
    if (path.includes('/events/history?after=0&limit=500')) {
      return json({
        events: [{ type: 'choices_ready', event_id: 4, payload: { for_seq: 0, choices: [{ type: 'do', text: 'seguir' }] } }],
        next_cursor: 4,
        has_more: true,
      })
    }
    if (path.includes('/events/history?after=4&limit=500')) {
      return json({ events: [], next_cursor: 4, has_more: false })
    }
    if (path.endsWith('/cards')) {
      return json([{ id: 'c', type: 'character', title: 'Lia', portrait_image_id: 'lia' }])
    }
    if (path.includes('/jobs?status=done')) return json([])
    if (path.endsWith('/map')) return json({ error: { code: 'not_found', message: 'missing' } }, 404)
    return json({ error: { message: 'unexpected' } }, 500)
  }

  const novel = await loadVisualNovel('adv/1')
  assert.equal(novel.title, 'Real')
  assert.equal(novel.beats.length, 1)
  assert.equal(novel.beats[0].text, 'Era uma vez.')
  assert.equal(novel.beats[0].choices.length, 1)
  assert.equal(novel.beats[0].choices[0].taken, false)
  assert.equal(novel.beats[0].choices[0].text, 'seguir')
  assert.ok(calls.every((call) => call.method === 'GET'))
  assert.ok(calls.every((call) => call.authorization === 'Bearer token-1'))
  assert.ok(calls.some((call) => call.url.includes('/api/adventures/adv%2F1/cards')))
  assert.ok(calls.some((call) => call.url.includes('/jobs?status=done')))
  assert.ok(calls.some((call) => call.url.includes('/events/history?after=4')))
  assert.equal(calls.some((call) => call.url.includes('/scene') || call.url.includes('/choices')), false)
})

function json(data, status = 200) {
  return new Response(JSON.stringify(data), {
    status,
    headers: { 'content-type': 'application/json' },
  })
}
