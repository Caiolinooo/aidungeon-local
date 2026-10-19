// Formatação visual de prosa para leitura fluida na tela.
//
// Regra de ouro (v0.9.0): NUNCA quebrar frases no meio, em vírgulas ou por
// contagem arbitrária de caracteres (o antigo wrapLongChunk de 240 chars
// cortava orações pela metade). Quebras extras só ocorrem em limites reais
// de fim de período (. ! ?) quando um único bloco ultrapassa ~700 caracteres
// ou antes de um turno de diálogo com travessão após ponto final.

const WALL_THRESHOLD = 700

function formatParagraph(paragraph) {
  const trimmed = paragraph.trim()
  if (!trimmed) return ''

  // Separa diálogo com travessão colado logo após fim de frase em parágrafos longos
  let body = trimmed
  if (body.length > 360) {
    body = body.replace(/([.!?…]["”»]?)\s+(?=—\s*[A-ZÀ-Ý])/g, '$1\n\n')
  }

  // Se um único parágrafo ainda for um bloco maciço (> 700 chars), divide a cada
  // 3 frases completas (somente após . ! ? seguidos de espaço e letra maiúscula).
  if (body.length > WALL_THRESHOLD && !body.includes('\n\n')) {
    let sentenceCount = 0
    body = body.replace(/([.!?…]["”»]?)(\s+)(?=[A-ZÀ-Ý“"—])/g, (match, punct, space) => {
      sentenceCount += 1
      return sentenceCount % 3 === 0 ? `${punct}\n\n` : `${punct}${space}`
    })
  }

  return body
}

export function loosenProse(text) {
  if (!text) return text
  // Remove vazamento eventual de marcador de prompt no final e normaliza quebras
  const cleaned = String(text)
    .replace(/\r\n/g, '\n')
    .replace(/\n{3,}/g, '\n\n')

  return cleaned
    .split(/\n{2,}/)
    .map(formatParagraph)
    .filter(Boolean)
    .join('\n\n')
}
