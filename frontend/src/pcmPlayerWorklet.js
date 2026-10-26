// PCM16 mono -> float, fila no AudioWorklet. Sem import: addModule carrega
// este arquivo como worklet clássico.
class PcmPlayerProcessor extends AudioWorkletProcessor {
  constructor(options) {
    super()
    const opt = options?.processorOptions || {}
    this.sourceRate = opt.sourceRate || sampleRate
    this.queue = []
    this.current = null
    this.offset = 0
    this.finished = false
    this.ended = false
    this.stopped = false
    this.lastSample = 0
    this.phase = 1
    this.port.onmessage = (event) => this.onMessage(event.data)
  }

  onMessage(msg) {
    if (!msg || this.stopped) return
    if (msg.type === 'stop') {
      this.stopped = true
      this.queue = []
      this.current = null
      this.offset = 0
      return
    }
    if (msg.type === 'finish') {
      this.finished = true
      return
    }
    if (msg.type === 'audio' && msg.data instanceof ArrayBuffer) {
      const pcm = new Int16Array(msg.data)
      const floats = this.toOutputFloats(pcm)
      if (floats.length) this.queue.push(floats)
    }
  }

  toOutputFloats(pcm) {
    if (this.sourceRate === sampleRate) {
      const out = new Float32Array(pcm.length)
      for (let i = 0; i < pcm.length; i++) out[i] = pcm[i] / 32768
      return out
    }
    const step = this.sourceRate / sampleRate
    const merged = new Float32Array(pcm.length + 1)
    merged[0] = this.lastSample
    for (let i = 0; i < pcm.length; i++) merged[i + 1] = pcm[i] / 32768
    const produced = []
    let pos = this.phase
    while (pos + 1 < merged.length) {
      const i0 = Math.floor(pos)
      const frac = pos - i0
      produced.push(merged[i0] * (1 - frac) + merged[i0 + 1] * frac)
      pos += step
    }
    this.lastSample = merged[merged.length - 1]
    this.phase = pos - (merged.length - 1)
    return Float32Array.from(produced)
  }

  process(_inputs, outputs) {
    const out = outputs[0] && outputs[0][0]
    if (!out) return true
    if (this.stopped) {
      out.fill(0)
      return true
    }
    let i = 0
    while (i < out.length) {
      if (!this.current || this.offset >= this.current.length) {
        this.current = this.queue.shift() || null
        this.offset = 0
      }
      if (!this.current) break
      const n = Math.min(out.length - i, this.current.length - this.offset)
      out.set(this.current.subarray(this.offset, this.offset + n), i)
      this.offset += n
      i += n
    }
    if (i < out.length) out.fill(0, i)
    const drained = this.finished
      && (!this.current || this.offset >= this.current.length)
      && this.queue.length === 0
    if (drained && !this.ended) {
      this.ended = true
      this.current = null
      this.port.postMessage({ type: 'ended' })
    }
    return true
  }
}

registerProcessor('pcm-player', PcmPlayerProcessor)
