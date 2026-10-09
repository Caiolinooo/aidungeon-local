// O TTS da VM (Supertonic 3) roda na CPU: ~25 caracteres por segundo de
// parede. A rota /api/tts recusa acima de max_text_chars (2000) e o cliente
// HTTP desiste aos 60s — um parágrafo longo estoura os dois. Pedacos curtos
// cabem no teto, comecam a tocar em poucos segundos, e o proximo e
// sintetizado enquanto o atual ainda toca.
export const TTS_CHUNK_CHARS = 240

export function splitNarration(text, max = TTS_CHUNK_CHARS) {
  const clean = String(text || '').replace(/\s+/g, ' ').trim()
  if (!clean) return []
  if (clean.length <= max) return [clean]

  const sentences = clean.match(/[^.!?…]+[.!?…]+(?:["”']+)?|[^.!?…]+$/g) || [clean]
  const chunks = []
  let buf = ''

  const flush = () => {
    const t = buf.trim()
    if (t) chunks.push(t)
    buf = ''
  }

  const pushLong = (sentence) => {
    const words = sentence.split(' ')
    let line = ''
    for (const word of words) {
      if (word.length > max) {
        if (line) chunks.push(line)
        line = ''
        for (let i = 0; i < word.length; i += max) chunks.push(word.slice(i, i + max))
        continue
      }
      const next = line ? `${line} ${word}` : word
      if (next.length > max) {
        if (line) chunks.push(line)
        line = word
      } else {
        line = next
      }
    }
    buf = line
  }

  for (const raw of sentences) {
    const sentence = raw.trim()
    if (!sentence) continue
    if (sentence.length > max) {
      flush()
      pushLong(sentence)
      flush()
      continue
    }
    const next = buf ? `${buf} ${sentence}` : sentence
    if (next.length > max) {
      flush()
      buf = sentence
    } else {
      buf = next
    }
  }
  flush()
  return chunks
}
