// Wrapper React do TalkingHead (v0.6.0) — substitui o antigo componente de
// avatar three-vrm. Expõe API imperativa via ref (speak/stop) para o
// avatarBus e para o preview da AvatarsTab; a config visual vem de
// avatarConfig.
//
// Detalhes da integração:
// - lipsyncModules: [] + injeção manual em head.lipsync: o loader dinâmico
//   do TalkingHead faz import() do módulo pelo nome da língua e 404 no vite
//   (dev server não resolve caminho dentro de node_modules empacotado), então
//   registramos pt_br (vendored) e en (node_modules) direto no registry.
// - A narração chega como WAV PCM16 (POST /api/tts/stream). streamStart arma
//   o lipsync; cada frame de 4096 samples vai em streamAudio. O áudio sozinho
//   não gera visemes — as palavras do texto são estimadas, como no MP3.
import {
  forwardRef,
  useEffect,
  useImperativeHandle,
  useRef,
} from 'react'
import { TalkingHead } from '@met4citizen/talkinghead'
import { LipsyncEn } from '@met4citizen/talkinghead/modules/lipsync-en.mjs'
import { LipsyncPt_br } from './vendor/lipsync-pt_br.mjs'
import { avatarUrl } from './avatarConfig.js'
import { PCM_FRAME_SAMPLES, readPcm16Wav } from '../../pcmStream.js'

// Distribui a duração do áudio entre as palavras proporcionalmente ao
// tamanho (len+1 p/ contar o espaço): o TalkingHead precisa de words/wtimes/
// wdurations para gerar os visemes, e o TTS local não devolve timestamps.
function estimateWordTiming(text, durationMs) {
  const words = (text || '').trim().split(/\s+/).filter(Boolean)
  if (words.length === 0 || durationMs <= 0) {
    return { words: [], wtimes: [], wdurations: [] }
  }
  const weights = words.map((w) => w.length + 1)
  const total = weights.reduce((a, b) => a + b, 0)
  const wtimes = []
  const wdurations = []
  let t = 0
  for (const weight of weights) {
    const d = (weight / total) * durationMs
    wtimes.push(t)
    wdurations.push(d)
    t += d
  }
  return { words, wtimes, wdurations }
}

function abortError() {
  const err = new Error('Aborted')
  err.name = 'AbortError'
  return err
}

// Duração total estimada antes de conhecer o WAV: (len+1) * 85 ms por palavra.
// A proporção de áudio já recebido escolhe quantas palavras cabem no frame;
// o frame final leva o que sobrou. Offsets usam o relógio PCM real.
function wordsForFrame({ words, cursor, totalMs, emittedMs, frameLength, sampleRate, isFinal }) {
  const frameMs = (frameLength / sampleRate) * 1000
  let next = cursor
  let selected = []
  if (isFinal) {
    selected = words.slice(cursor)
    next = words.length
  } else if (words.length > 0 && totalMs > 0) {
    const full = estimateWordTiming(words.join(' '), totalMs)
    const ratio = (emittedMs + frameMs) / totalMs
    while (next < full.words.length) {
      const endRatio = (full.wtimes[next] + full.wdurations[next]) / totalMs
      if (endRatio <= ratio + 1e-6) next += 1
      else break
    }
    selected = words.slice(cursor, next)
  }
  const timing = estimateWordTiming(selected.join(' '), frameMs)
  return {
    cursor: next,
    emittedMs: emittedMs + frameMs,
    words: timing.words,
    wtimes: timing.wtimes.map((t) => t + emittedMs),
    wdurations: timing.wdurations,
  }
}

const TalkingAvatar = forwardRef(function TalkingAvatar(
  { cfg, className, view = 'upper' },
  ref,
) {
  const containerRef = useRef(null)
  const headRef = useRef(null)
  const speakGenRef = useRef(0)
  // Promise do showAvatar corrente — resolve true/false conforme o GLB
  // carregou; speak() e setMood() aguardam o avatar estar carregado e
  // verificam o flag para não animarem no vazio (GLB incompatível, ex.
  // sem rig "Armature"/visemes, faz o TalkingHead rejeitar o modelo).
  const headReadyRef = useRef(Promise.resolve(false))

  // Monta o TalkingHead uma única vez (o div é o canvas host dele).
  useEffect(() => {
    const head = new TalkingHead(containerRef.current, {
      cameraView: view,
      cameraRotateEnable: false,
      lipsyncModules: [],
      modelFPS: 30,
      lipsyncLang: 'pt_br',
      mixerGainSpeech: 3,
    })
    head.lipsync['pt_br'] = new LipsyncPt_br()
    head.lipsync['en'] = new LipsyncEn()
    headRef.current = head
    return () => {
      try { head.streamStop() } catch { /* stream ainda não armado */ }
      head.stopSpeaking()
      head.stop()
      headRef.current = null
    }
    // view só vale na construção (cameraView é lida no init da cena).
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  // Carrega/troca o avatar quando url ou body mudam.
  const url = avatarUrl(cfg)
  const body = cfg.body
  const mood = cfg.mood
  useEffect(() => {
    const head = headRef.current
    if (!head) return
    headReadyRef.current = head
      .showAvatar({
        url,
        body,
        lipsyncLang: 'pt_br',
        avatarMood: mood,
      })
      .then(() => true)
      .catch((err) => {
        // GLB inacessível/incompatível (sem Armature/visemes): loga e
        // resolve false p/ setMood/speak não operarem em avatar vazio.
        console.warn('[TalkingAvatar] showAvatar falhou para', url, err)
        return false
      })
  }, [url, body]) // mood inicial entra no showAvatar; mudanças vão pelo efeito abaixo

  // Aplica o mood persistido quando muda (após o avatar estar pronto).
  useEffect(() => {
    let cancelled = false
    headReadyRef.current.then((ok) => {
      if (!cancelled && ok && headRef.current) headRef.current.setMood(mood)
    })
    return () => {
      cancelled = true
    }
  }, [mood])

  useImperativeHandle(ref, () => ({
    // Fala um WAV PCM16 com lipsync por palavras. O frame mais recente fica
    // retido; o anterior sai quando o próximo chega, e o EOF manda o retido
    // como frame final — assim o último bloco é conhecido sem o áudio inteiro.
    async speak({ response, signal, text, onended, onFirstSample, interrupt = true }) {
      const head = headRef.current
      if (!head) return
      const gen = ++speakGenRef.current
      const mine = () => speakGenRef.current === gen && headRef.current === head
      const ok = await headReadyRef.current
      if (!ok || !mine()) {
        try { await response?.body?.cancel() } catch { /* corpo já fechado */ }
        return
      }
      if (signal?.aborted) {
        try { await response?.body?.cancel() } catch { /* corpo já fechado */ }
        throw abortError()
      }
      if (interrupt) {
        try { head.streamStop() } catch { /* sem stream anterior */ }
        head.stopSpeaking()
      }
      try {
        await head.audioCtx.resume()
      } catch {
        // Autoplay policy: sem gesto do usuário o resume falha — os visemes
        // ainda podem ser enfileirados quando o streamStart segue.
      }

      const words = String(text || '').trim().split(/\s+/).filter(Boolean)
      const totalMs = words.reduce((sum, word) => sum + (word.length + 1) * 85, 0)
      let cursor = 0
      let emittedMs = 0
      let sampleRate = 24000
      let started = false
      let noted = false
      let held = null
      let stash = new Int16Array(0)
      let failed = false
      let settled = false
      let resolveDone
      const done = new Promise((resolve) => {
        resolveDone = () => {
          if (settled) return
          settled = true
          resolve()
        }
      })

      const dispatch = (frame, isFinal) => {
        if (!mine()) return
        const timing = wordsForFrame({
          words,
          cursor,
          totalMs,
          emittedMs,
          frameLength: frame.length,
          sampleRate,
          isFinal,
        })
        cursor = timing.cursor
        emittedMs = timing.emittedMs
        head.streamAudio({
          audio: frame,
          words: timing.words,
          wtimes: timing.wtimes,
          wdurations: timing.wdurations,
        })
      }

      const pushSamples = (buffer) => {
        const incoming = new Int16Array(buffer)
        if (incoming.length === 0) return
        if (!stash.length) stash = incoming
        else {
          const merged = new Int16Array(stash.length + incoming.length)
          merged.set(stash, 0)
          merged.set(incoming, stash.length)
          stash = merged
        }
        while (stash.length >= PCM_FRAME_SAMPLES) {
          const frame = stash.slice(0, PCM_FRAME_SAMPLES)
          stash = stash.slice(PCM_FRAME_SAMPLES)
          if (held) dispatch(held, false)
          held = frame
        }
      }

      const onAbort = () => {
        if (!mine()) return
        try { head.streamStop() } catch { /* stream já parado */ }
        resolveDone()
      }
      signal?.addEventListener('abort', onAbort, { once: true })

      try {
        await readPcm16Wav(response, {
          onFormat: async (fmt) => {
            if (!mine()) return
            sampleRate = fmt.sampleRate
            await head.streamStart({
              sampleRate,
              lipsyncLang: navigator.language.toLowerCase().startsWith('pt') ? 'pt_br' : 'en',
              lipsyncType: 'words',
            }, null, () => {
              if (mine() && !failed && !signal?.aborted && typeof onended === 'function') {
                try { onended() } catch { /* o botão trata o fim */ }
              }
              resolveDone()
            })
            started = true
          },
          onSamples: (buffer) => {
            if (!mine() || signal?.aborted) return
            if (!noted) {
              noted = true
              try { onFirstSample?.() } catch { /* estado do botão */ }
            }
            pushSamples(buffer)
          },
        })
        if (!mine() || signal?.aborted) throw abortError()
        if (stash.length) {
          if (held) dispatch(held, false)
          held = stash
          stash = new Int16Array(0)
        }
        if (held) dispatch(held, true)
        held = null
        if (started && mine()) head.streamNotifyEnd()
        await done
        if (!mine() || signal?.aborted) throw abortError()
      } catch (err) {
        failed = true
        if (mine()) {
          try { head.streamStop() } catch { /* stream já parado */ }
        }
        resolveDone()
        throw err
      } finally {
        signal?.removeEventListener('abort', onAbort)
      }
    },
    stop() {
      speakGenRef.current += 1
      const head = headRef.current
      if (!head) return
      try { head.streamStop() } catch { /* sem stream */ }
      try { head.stopSpeaking() } catch { /* fila já vazia */ }
    },
  }))

  // Tamanho vem do className do chamador (dock passa via pai h-32 w-24,
  // preview h-80 w-full); sem className, preenche o pai. NÃO fixar
  // width/height inline aqui — style inline sobrescreve a classe (bug: o
  // preview da AvatarsTab ficava com altura 0).
  return (
    <div
      ref={containerRef}
      className={className || 'h-full w-full'}
      style={{ position: 'relative', overflow: 'hidden' }}
    />
  )
})

export default TalkingAvatar
