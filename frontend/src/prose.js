// Só na tela. O texto gravado e a narração ficam como a IA escreveu.
//
// Respostas neste modelo saem numa frase só, sem ponto, emendadas com
// ponto-e-vírgula e reticências. Quebrar nessas pausas deixa o parágrafo
// legível sem reescrever as palavras.

const LONG = 240

function endings(text) {
  return text.match(/[.!?](?!\.)(?:\s|$)/g) || []
}

function wrapLongChunk(chunk) {
  if (chunk.length <= LONG) return chunk
  const parts = []
  let rest = chunk
  while (rest.length > LONG) {
    const window = rest.slice(0, 200)
    const atPunct = Math.max(window.lastIndexOf(', '), window.lastIndexOf('; '))
    const atWord = window.lastIndexOf(' ')
    const cut = atPunct >= 80 ? atPunct : atWord
    if (cut < 40) break
    const keepPunct = cut === atPunct
    parts.push(rest.slice(0, cut + (keepPunct ? 1 : 0)).trim())
    rest = rest.slice(cut + 1).trim()
  }
  if (rest) parts.push(rest)
  return parts.join('\n\n')
}

function loosenParagraph(paragraph) {
  const trimmed = paragraph.trim()
  if (trimmed.length < LONG) return trimmed
  const telegraphic = endings(trimmed).length < Math.floor(trimmed.length / 220)
  let body = trimmed
  if (telegraphic) {
    body = body.replace(/;\s+/g, '.\n\n').replace(/\.{3,}\s+/g, '.\n\n')
  } else if (trimmed.length > 500) {
    let n = 0
    body = trimmed.replace(/([.!?])(\s+)(?=\S)/g, (match, punct, space) => {
      n += 1
      return n % 3 === 0 ? `${punct}\n\n` : `${punct}${space}`
    })
  }
  return body
    .split(/\n{2,}/)
    .map((chunk) => wrapLongChunk(chunk.trim()))
    .filter(Boolean)
    .join('\n\n')
}

export function loosenProse(text) {
  if (!text || text.length < LONG) return text
  return text.split(/\n{2,}/).map(loosenParagraph).join('\n\n')
}
