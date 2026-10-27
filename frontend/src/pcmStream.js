// Parser incremental de WAV PCM16 mono e player Web Audio.
// O retrato do dock e o botão de narração usam este player, um de cada vez:
// se o dock está registrado, o botão entrega o Response só para ele.

export const PCM_FRAME_SAMPLES = 4096

const UNKNOWN_CHUNK = 0xffffffff

function ascii(bytes, start, len) {
  let s = ''
  for (let i = 0; i < len; i++) s += String.fromCharCode(bytes[start + i])
  return s
}

function createQueue() {
  const chunks = []
  let head = 0
  let length = 0

  function push(u8) {
    if (!u8?.byteLength) return
    const bytes = u8 instanceof Uint8Array ? u8 : new Uint8Array(u8)
    chunks.push(bytes)
    length += bytes.byteLength
  }

  function take(n) {
    if (n > length) return null
    const out = new Uint8Array(n)
    let filled = 0
    while (filled < n) {
      const current = chunks[0]
      const avail = current.byteLength - head
      const nTake = Math.min(avail, n - filled)
      out.set(current.subarray(head, head + nTake), filled)
      filled += nTake
      head += nTake
      length -= nTake
      if (head >= current.byteLength) {
        chunks.shift()
        head = 0
      }
    }
    return out
  }

  return {
    get length() {
      return length
    },
    push,
    take,
    skip(n) {
      if (take(n) == null) throw new Error('Invalid WAV: truncated chunk.')
    },
  }
}

function parseFmt(bytes) {
  if (bytes.byteLength < 16) throw new Error('Invalid WAV: truncated fmt.')
  const view = new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength)
  const audioFormat = view.getUint16(0, true)
  const channels = view.getUint16(2, true)
  const sampleRate = view.getUint32(4, true)
  const bits = view.getUint16(14, true)
  if (audioFormat !== 1 || channels !== 1 || bits !== 16 || !(sampleRate > 0)) {
    throw new Error('Invalid WAV: expected PCM16 mono.')
  }
  return { sampleRate, channels, bitsPerSample: bits }
}

function createPcm16Parser({ onFormat, onSamples }) {
  const queue = createQueue()
  let state = 'riff'
  let need = 0
  let fmtBytes = 16
  let dataLeft = 0
  let gotFormat = false
  let gotSamples = false
  let closed = false

  async function emit(bytes) {
    if (!bytes || bytes.byteLength < 2) return
    gotSamples = true
    if (onSamples) await onSamples(bytes.buffer)
  }

  async function pump() {
    while (!closed) {
      if (state === 'riff') {
        if (queue.length < 12) return
        const hdr = queue.take(12)
        if (ascii(hdr, 0, 4) !== 'RIFF' || ascii(hdr, 8, 4) !== 'WAVE') {
          throw new Error('Invalid WAV: expected RIFF/WAVE PCM16 mono.')
        }
        state = 'chunk'
      } else if (state === 'chunk') {
        if (queue.length < 8) return
        const hdr = queue.take(8)
        const id = ascii(hdr, 0, 4)
        const size = new DataView(hdr.buffer, hdr.byteOffset, hdr.byteLength).getUint32(4, true)
        if (id === 'fmt ') {
          fmtBytes = size
          need = size + (size & 1)
          state = 'fmt'
        } else if (id === 'data') {
          if (!gotFormat) throw new Error('Invalid WAV: data chunk before fmt.')
          dataLeft = size === UNKNOWN_CHUNK ? Infinity : size
          state = 'data'
        } else {
          need = size + (size & 1)
          state = 'skip'
        }
      } else if (state === 'fmt') {
        if (queue.length < need) return
        const body = queue.take(need)
        const format = parseFmt(body.subarray(0, Math.min(fmtBytes, body.byteLength)))
        gotFormat = true
        if (onFormat) {
          await onFormat({
            sampleRate: format.sampleRate,
            channels: format.channels,
            bitsPerSample: format.bitsPerSample,
          })
        }
        state = 'chunk'
      } else if (state === 'skip') {
        if (need === 0) {
          state = 'chunk'
          continue
        }
        if (queue.length < need) return
        queue.skip(need)
        state = 'chunk'
      } else if (state === 'data') {
        if (dataLeft <= 0) {
          closed = true
          return
        }
        const available = Math.min(queue.length, dataLeft)
        const takeN = available - (available & 1)
        if (takeN <= 0) return
        const raw = queue.take(takeN)
        dataLeft -= takeN
        await emit(raw)
        if (dataLeft <= 0) {
          closed = true
          return
        }
      } else {
        return
      }
    }
  }

  return {
    async push(chunk) {
      if (closed) return
      queue.push(chunk)
      await pump()
    },
    async finish() {
      if (!closed && state === 'data' && queue.length >= 2 && dataLeft > 0) {
        const available = Math.min(queue.length, dataLeft)
        const takeN = available - (available & 1)
        if (takeN > 0) await emit(queue.take(takeN))
      }
      if (!gotFormat || !gotSamples) {
        throw new Error('Invalid WAV: PCM16 mono data missing.')
      }
    },
  }
}

export async function readPcm16Wav(response, { onFormat, onSamples } = {}) {
  if (!response?.body || typeof response.body.getReader !== 'function') {
    throw new Error('Invalid WAV: response has no body.')
  }
  const parser = createPcm16Parser({ onFormat, onSamples })
  const reader = response.body.getReader()
  try {
    while (true) {
      const { done, value } = await reader.read()
      if (done) break
      if (value) await parser.push(value)
    }
    await parser.finish()
  } finally {
    try { reader.releaseLock() } catch { /* lock já solto */ }
  }
}

// OmniVoice entrega PCM a 24 kHz. O contexto criado no
// gesto usa essa taxa; se o browser recusar, o worklet reamostra.
const PREWARM_SAMPLE_RATE = 24000

let sharedCtx = null
let moduleReady = null
let moduleContext = null
let prewarmInstalled = false
let prewarmHandler = null

function workletHref() {
  return new URL('./pcmPlayerWorklet.js', import.meta.url).href
}

function createContext(sampleRate) {
  const Ctx = globalThis.AudioContext
  try {
    return sampleRate > 0 ? new Ctx({ sampleRate }) : new Ctx()
  } catch {
    return new Ctx()
  }
}

function sharedContextFor(sampleRate) {
  if (sharedCtx && sharedCtx.state !== 'closed') return sharedCtx
  moduleReady = null
  moduleContext = null
  sharedCtx = createContext(sampleRate)
  return sharedCtx
}

function loadWorklet(context) {
  if (moduleContext !== context) {
    moduleContext = context
    moduleReady = null
  }
  if (!moduleReady) {
    const loading = context.audioWorklet.addModule(workletHref())
    const tracked = loading.catch((err) => {
      if (moduleReady === tracked) {
        moduleReady = null
        moduleContext = null
      }
      throw err
    })
    moduleReady = tracked
  }
  return moduleReady
}

function detachPrewarm() {
  if (typeof window === 'undefined' || !prewarmHandler) return
  window.removeEventListener('pointerdown', prewarmHandler, true)
  window.removeEventListener('keydown', prewarmHandler, true)
  prewarmHandler = null
  prewarmInstalled = false
}

// Primeiro gesto: cria o AudioContext e carrega o worklet. Não toca áudio.
export function prewarmPcmPlayback() {
  if (prewarmInstalled || typeof window === 'undefined') return
  prewarmInstalled = true
  prewarmHandler = () => {
    detachPrewarm()
    const context = sharedContextFor(PREWARM_SAMPLE_RATE)
    loadWorklet(context).catch(() => { /* o start seguinte tenta de novo */ })
    if (context.state === 'suspended') {
      context.resume().catch(() => { /* gesto já passou */ })
    }
  }
  window.addEventListener('pointerdown', prewarmHandler, true)
  window.addEventListener('keydown', prewarmHandler, true)
}

export function resetPcmPlaybackForTests() {
  detachPrewarm()
  try { sharedCtx?.close() } catch { /* contexto já fechado */ }
  sharedCtx = null
  moduleReady = null
  moduleContext = null
  prewarmInstalled = false
}

if (typeof window !== 'undefined') prewarmPcmPlayback()

export function createPcmPlayer({ onEnded } = {}) {
  let ctx = null
  let node = null
  let stopped = false
  let ended = false
  let pending = []
  let pendingFinish = false
  let session = 0

  const emitEnded = () => {
    if (ended) return
    ended = true
    try { onEnded?.() } catch { /* callback do chamador */ }
  }

  const dropNode = () => {
    try { node?.disconnect() } catch { /* nó já desligado */ }
    node = null
    if (ctx && sharedCtx !== ctx) {
      try { ctx.close() } catch { /* contexto já fechado */ }
    }
    ctx = null
  }

  return {
    async start(sampleRate) {
      if (stopped) return
      if (!(sampleRate > 0)) throw new Error('Invalid sample rate.')
      const id = ++session
      const context = sharedContextFor(sampleRate)
      ctx = context
      try {
        await loadWorklet(context)
      } catch (err) {
        if (ctx === context) ctx = null
        throw err
      }
      if (stopped || id !== session) {
        if (ctx === context) ctx = null
        return
      }
      const WorkletNode = globalThis.AudioWorkletNode
      node = new WorkletNode(context, 'pcm-player', {
        numberOfInputs: 0,
        numberOfOutputs: 1,
        outputChannelCount: [1],
        processorOptions: { sourceRate: sampleRate },
      })
      node.port.onmessage = (event) => {
        if (event.data?.type === 'ended') emitEnded()
      }
      node.connect(context.destination)
      if (context.state === 'suspended') {
        try { await context.resume() } catch { /* autoplay: sem gesto, fica mudo */ }
      }
      if (stopped || id !== session) {
        dropNode()
        return
      }
      for (const buf of pending) {
        node.port.postMessage({ type: 'audio', data: buf }, [buf])
      }
      pending = []
      if (pendingFinish) node.port.postMessage({ type: 'finish' })
    },

    write(arrayBuffer) {
      if (stopped || !arrayBuffer || arrayBuffer.byteLength === 0) return
      if (!node) {
        pending.push(arrayBuffer)
        return
      }
      node.port.postMessage({ type: 'audio', data: arrayBuffer }, [arrayBuffer])
    },

    finish() {
      if (stopped) return
      if (!node) {
        pendingFinish = true
        return
      }
      node.port.postMessage({ type: 'finish' })
    },

    stop() {
      if (stopped) return
      stopped = true
      session += 1
      pending = []
      pendingFinish = false
      try { node?.port.postMessage({ type: 'stop' }) } catch { /* porta já fechada */ }
      try { node?.disconnect() } catch { /* nó já desligado */ }
      node = null
      ctx = null
      emitEnded()
    },
  }
}
