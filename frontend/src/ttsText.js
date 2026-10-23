// O TTS da VM (OmniVoice Q8) carrega o modelo a cada pedido: um trecho curto leva cerca de 5 s e um longo cerca de 11 s.
// A rota /api/tts recusa acima de max_text_chars (4096) e o cliente
// HTTP desiste aos 60s — um parágrafo longo estoura os dois. Pedacos curtos
// cabem no teto, comecam a tocar em poucos segundos, e o proximo e
// sintetizado enquanto o atual ainda toca.
export const TTS_CHUNK_CHARS = 240

// Dica de emoção para o POST /api/tts. O servidor relê a linha e, se ela
// não declarar nada, usa a ficha do NPC. Aqui só mandamos a pista.
const LINE_EMOTIONS = [
  ['fear', ['medo', 'fear', 'assust', 'terror', 'pavor', 'afraid', 'scared']],
  ['anger', ['furios', 'angry', 'raiva', 'grita', 'rage', 'irritad', 'bravo', 'brava']],
  ['sadness', ['triste', 'sad', 'chora', 'cry', 'lágrima', 'lagrima']],
  ['disgust', ['nojo', 'disgust', 'repuls']],
  ['surprise', ['surpres', 'surprise', 'ofeg', 'gasp']],
  ['tenderness', ['amor', 'love', 'beij', 'carinho', 'abraç', 'abrac', 'tender']],
  ['joy', ['feliz', 'happy', 'alegre', 'sorri', 'laugh', 'joy', 'contente', 'smile']],
]

export function emotionForLine(text) {
  const lower = String(text || '').toLowerCase()
  for (const [emotion, words] of LINE_EMOTIONS) {
    if (words.some((word) => lower.includes(word))) return emotion
  }
  return 'neutral'
}

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
