// Ponte narração -> avatar do dock (TalkingHead).
//
// O ChatAvatarDock registra um speaker enquanto há um avatar montado; o
// NarrateButton do FeedItem chama speakNarration() e, se nenhum speaker
// estiver registrado (dock fechado, sem NPC na cena), cai no fallback de
// Audio() simples.
//
// Contrato do speaker (objeto):
//   speak: async ({ blob, text, onended, interrupt }) => undefined
//     - blob: audio/mpeg retornado pelo POST /api/tts
//     - text: texto narrado (usado p/ gerar visemes via lipsync module)
//     - onended: callback opcional chamado quando a fala termina
//       (TalkingHead: enfileirar speakMarker(onended) após o speakAudio)
//     - interrupt: true (default) para a fala anterior; false enfileira
//       (narração em vários pedaços)
//   stop: () => undefined  (interrompe a fala em andamento)

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

// Tenta falar pelo avatar. Retorna true se um speaker tratou (o chamador
// NÃO deve tocar o áudio de novo), false se não há avatar disponível.
export async function speakNarration({ blob, text, onended, interrupt = true }) {
  if (!speaker) return false
  await speaker.speak({ blob, text, onended, interrupt })
  return true
}

// Para a fala em andamento no avatar. Retorna true se havia speaker
// (o chamador considera o stop tratado).
export function stopNarration() {
  if (!speaker) return false
  speaker.stop()
  return true
}
