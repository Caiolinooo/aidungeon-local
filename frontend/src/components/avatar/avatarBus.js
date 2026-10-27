// Ponte narração -> retrato 2D do dock.
//
// O ChatAvatarDock registra um speaker enquanto há um retrato montado; o
// NarrateButton do FeedItem chama speakNarration() e, se nenhum speaker
// estiver registrado (dock fechado, sem NPC na cena), cai no player PCM.
// Com dock, o Response WAV é consumido só pelo retrato — o botão não toca
// de novo. O retrato mostra que está falando; o texto não anima a boca.
//
// Contrato do speaker (objeto):
//   speak: async ({ response, signal, text, onended, onFirstSample, interrupt }) => undefined
//     - response: Response audio/wav de POST /api/tts/stream (corpo ainda não lido)
//     - signal: AbortSignal da narração; abort para o player
//     - text: texto narrado (o retrato não usa para mover a boca)
//     - onFirstSample: primeiro bloco PCM recebido (estado playing)
//     - onended: callback opcional quando a reprodução termina
//     - interrupt: true (default) corta a fala anterior; false segue
//       depois que o pedaço anterior já acabou
//   stop: () => undefined  (para o player e a fila)

let speaker = null

// Registra o speaker ativo; retorna função de unregister (idempotente).
// Um novo registro substitui o anterior (há no máximo 1 dock visível).
export function registerSpeaker(spk) {
  speaker = spk
  let active = true
  return () => {
    if (active && speaker === spk) {
      speaker = null
      active = false
    }
  }
}

// Com avatar, devolve a Promise da reprodução. Sem avatar, null — o
// chamador usa o player PCM e não há segundo áudio no dock.
export function speakNarration({
  response,
  signal,
  text,
  onended,
  onFirstSample,
  interrupt = true,
} = {}) {
  if (!speaker) return null
  return speaker.speak({ response, signal, text, onended, onFirstSample, interrupt })
}

// Para a fala em andamento no avatar. Retorna true se havia speaker
// (o chamador considera o stop tratado).
export function stopNarration() {
  if (!speaker) return false
  speaker.stop()
  return true
}
