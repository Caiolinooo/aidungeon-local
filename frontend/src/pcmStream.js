// Parser incremental de WAV PCM16 mono e player Web Audio para o fallback
// sem avatar. O dock usa o stream do TalkingHead; este player não entra
// junto, senão a narração toca duas vezes.

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

  return {
    async start(sampleRate) {
      if (stopped) return
      if (!(sampleRate > 0)) throw new Error('Invalid sample rate.')
      const id = ++session
      let context
      try {
        context = new AudioContext({ sampleRate })
      } catch {
        context = new AudioContext()
      }
      ctx = context
      const workletUrl = new URL('./pcmPlayerWorklet.js', import.meta.url)
      await context.audioWorklet.addModule(workletUrl.href)
      if (stopped || id !== session) {
        try { await context.close() } catch { /* contexto já fechado */ }
        if (ctx === context) ctx = null
        return
      }
      node = new AudioWorkletNode(context, 'pcm-player', {
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
        try { await context.resume() } catch { /* autoplay */ }
      }
      if (stopped || id !== session) return
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
      const closing = ctx
      node = null
      ctx = null
      if (closing) {
        try { closing.close() } catch { /* contexto já fechado */ }
      }
      emitEnded()
    },
  }
}
