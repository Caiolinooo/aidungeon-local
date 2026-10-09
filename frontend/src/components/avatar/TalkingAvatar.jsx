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
// - O AudioContext interno (head.audioCtx) é reutilizado para decodificar o
//   blob de TTS; browsers suspendem AudioContext criado fora de gesto do
//   usuário, então speak() tenta resume() (clique no NarrateButton é gesto).
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

const TalkingAvatar = forwardRef(function TalkingAvatar(
  { cfg, className, view = 'upper' },
  ref,
) {
  const containerRef = useRef(null)
  const headRef = useRef(null)
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
    // Fala o blob de TTS com lipsync pt-BR. Interrompe a fala anterior.
    // onended (opcional): enfileirado via speakMarker p/ o chamador saber
    // quando a fala terminou (NarrateButton volta ao estado idle).
    async speak({ blob, text, onended }) {
      const head = headRef.current
      if (!head) return
      const ok = await headReadyRef.current
      if (!ok || headRef.current !== head) return // GLB não carregou / avatar trocado
      head.stopSpeaking()
      try {
        await head.audioCtx.resume()
      } catch {
        // Autoplay policy: sem gesto do usuário o resume falha — os visemes
        // ainda animam mesmo sem o áudio sair (speakAudio usa o timing).
      }
      const buf = await blob.arrayBuffer()
      const audioBuffer = await head.audioCtx.decodeAudioData(buf)
      const { words, wtimes, wdurations } = estimateWordTiming(
        text,
        audioBuffer.duration * 1000,
      )
      head.speakAudio(
        { audio: audioBuffer, words, wtimes, wdurations },
        { lipsyncLang: 'pt_br' },
      )
      if (typeof onended === 'function') head.speakMarker(onended)
    },
    stop() {
      headRef.current?.stopSpeaking()
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
