import assert from 'node:assert/strict'
import test from 'node:test'
import { createPcmPlayer, prewarmPcmPlayback, readPcm16Wav, resetPcmPlaybackForTests } from '../src/pcmStream.js'

function encodeWav(samples, sampleRate, dataSize = null) {
  const dataBytes = samples.length * 2
  const sizeField = dataSize == null ? dataBytes : dataSize
  const buf = Buffer.alloc(44 + dataBytes)
  buf.write('RIFF', 0)
  buf.writeUInt32LE(36 + dataBytes, 4)
  buf.write('WAVE', 8)
  buf.write('fmt ', 12)
  buf.writeUInt32LE(16, 16)
  buf.writeUInt16LE(1, 20)
  buf.writeUInt16LE(1, 22)
  buf.writeUInt32LE(sampleRate, 24)
  buf.writeUInt32LE(sampleRate * 2, 28)
  buf.writeUInt16LE(2, 32)
  buf.writeUInt16LE(16, 34)
  buf.write('data', 36)
  buf.writeUInt32LE(sizeField >>> 0, 40)
  for (let i = 0; i < samples.length; i++) buf.writeInt16LE(samples[i], 44 + i * 2)
  return buf
}

function splitAt(bytes, points) {
  const cuts = [0, ...points, bytes.length]
  const chunks = []
  for (let i = 0; i < cuts.length - 1; i++) {
    if (cuts[i] === cuts[i + 1]) continue
    chunks.push(new Uint8Array(bytes.subarray(cuts[i], cuts[i + 1])))
  }
  return chunks
}

function responseFromChunks(chunks) {
  const pending = chunks.slice()
  return new Response(new ReadableStream({
    pull(controller) {
      if (pending.length === 0) {
        controller.close()
        return
      }
      controller.enqueue(pending.shift())
    },
  }))
}

async function collect(chunks) {
  let sampleRate = 0
  const parts = []
  await readPcm16Wav(responseFromChunks(chunks), {
    onFormat(fmt) {
      sampleRate = fmt.sampleRate
    },
    onSamples(buffer) {
      parts.push(new Int16Array(buffer.slice(0)))
    },
  })
  const total = parts.reduce((n, part) => n + part.length, 0)
  const samples = new Int16Array(total)
  let offset = 0
  for (const part of parts) {
    samples.set(part, offset)
    offset += part.length
  }
  return { sampleRate, samples }
}

const SAMPLES = [1, -2, 300, -400, 32767, -32768]

test('WAV partido no header, no fmt e entre dois samples entrega PCM16 a 24 kHz', async () => {
  const wav = encodeWav(SAMPLES, 24000)
  // 5: dentro do header RIFF; 24: dentro do corpo fmt; 46: entre o 1º e o 2º sample.
  const { sampleRate, samples } = await collect(splitAt(wav, [5, 24, 46]))
  assert.equal(sampleRate, 24000)
  assert.deepEqual(Array.from(samples), SAMPLES)
})

test('byte parcial de um sample atravessa reads e os dois samples sobrevivem', async () => {
  const wav = encodeWav(SAMPLES, 24000)
  const { sampleRate, samples } = await collect(splitAt(wav, [45]))
  assert.equal(sampleRate, 24000)
  assert.deepEqual(Array.from(samples), SAMPLES)
})

test('data chunk 0xffffffff lê até EOF sem esperar o tamanho declarado', async () => {
  const wav = encodeWav(SAMPLES, 24000, 0xffffffff)
  const { sampleRate, samples } = await collect([new Uint8Array(wav)])
  assert.equal(sampleRate, 24000)
  assert.deepEqual(Array.from(samples), SAMPLES)
})

test('tamanho conhecido não engole bytes depois do chunk data', async () => {
  const wav = encodeWav([11, -22], 24000)
  const junk = Buffer.concat([wav, Buffer.from([0x7f, 0x00, 0x11, 0x22])])
  const { samples } = await collect(splitAt(junk, [3, 18, 44]))
  assert.deepEqual(Array.from(samples), [11, -22])
})

test('MP3 é rejeitado', async () => {
  const mp3 = new Uint8Array([0x49, 0x44, 0x33, 0x04, 0x00, 0xff, 0xfb, 0x90, 0x00])
  await assert.rejects(() => collect([mp3]), /WAV|PCM/)
})

test('EOF sem samples rejeita a reprodução', async () => {
  const wav = encodeWav([], 24000, 0)
  await assert.rejects(() => collect([new Uint8Array(wav)]), /PCM16|missing|Invalid WAV/)
})

test('duas leituras do mesmo WAV entregam o mesmo PCM', async () => {
  const wav = encodeWav(SAMPLES, 24000)
  const first = await collect(splitAt(wav, [5, 24, 46]))
  const second = await collect([new Uint8Array(wav)])
  assert.equal(first.sampleRate, second.sampleRate)
  assert.deepEqual(Array.from(first.samples), Array.from(second.samples))
  assert.deepEqual(Array.from(first.samples), SAMPLES)
})

function installAudioMocks() {
  resetPcmPlaybackForTests()
  const instances = []
  let modules = 0
  let nodes = 0
  class FakeContext {
    constructor(opts = {}) {
      this.sampleRate = opts.sampleRate || 48000
      this.state = 'suspended'
      this.destination = {}
      this.audioWorklet = {
        addModule: async () => {
          modules += 1
        },
      }
      this.resumes = 0
      this.closed = false
      instances.push(this)
    }

    resume() {
      this.resumes += 1
      this.state = 'running'
      return Promise.resolve()
    }

    close() {
      this.closed = true
      this.state = 'closed'
      return Promise.resolve()
    }
  }
  class FakeNode {
    constructor() {
      nodes += 1
      this.port = { onmessage: null, postMessage() {} }
    }

    connect() {}

    disconnect() {}
  }
  globalThis.AudioContext = FakeContext
  globalThis.AudioWorkletNode = FakeNode
  const listeners = {}
  globalThis.window = {
    addEventListener(type, fn) {
      listeners[type] = fn
    },
    removeEventListener(type, fn) {
      if (listeners[type] === fn) delete listeners[type]
    },
  }
  return {
    instances,
    listeners,
    modules: () => modules,
    nodes: () => nodes,
  }
}

test('prewarm no primeiro gesto não toca; o player reusa contexto e worklet', async () => {
  const fake = installAudioMocks()
  prewarmPcmPlayback()
  assert.equal(fake.instances.length, 0)
  assert.equal(typeof fake.listeners.pointerdown, 'function')
  fake.listeners.pointerdown()
  assert.equal(fake.instances.length, 1)
  assert.equal(fake.instances[0].sampleRate, 24000)
  assert.equal(fake.instances[0].resumes, 1)
  assert.equal(fake.instances[0].state, 'running')
  assert.equal(fake.nodes(), 0)
  const first = createPcmPlayer()
  const second = createPcmPlayer()
  await first.start(24000)
  await second.start(24000)
  assert.equal(fake.instances.length, 1)
  assert.equal(fake.modules(), 1)
  assert.equal(fake.nodes(), 2)
  first.stop()
  second.stop()
  assert.equal(fake.instances[0].closed, false)
  resetPcmPlaybackForTests()
})

test('stop não fecha o AudioContext compartilhado', async () => {
  const fake = installAudioMocks()
  const player = createPcmPlayer()
  await player.start(24000)
  player.stop()
  assert.equal(fake.instances.length, 1)
  assert.equal(fake.instances[0].closed, false)
  const again = createPcmPlayer()
  await again.start(24000)
  assert.equal(fake.instances.length, 1)
  assert.equal(fake.modules(), 1)
  again.stop()
  assert.equal(fake.instances[0].closed, false)
  resetPcmPlaybackForTests()
})
