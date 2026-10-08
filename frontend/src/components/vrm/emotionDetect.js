// Detecção de emoção "seguindo a história" (v0.4.0).
//
// Dado o texto narrativo mais recente da aventura e o título do NPC,
// detectEmotion() procura menções do NPC (case-insensitive, word boundary)
// e, num raio de WINDOW_CHARS caracteres em volta de cada menção, procura
// palavras de emoção (mapa PT+EN). Retorna a emoção da menção mais recente
// do texto (a última menção costuma refletir o estado atual da cena).

// Raio em caracteres ao redor da menção do NPC onde vale procurar emoção.
const WINDOW_CHARS = 200

// Mapa emoção -> fragmentos de palavra (PT + EN). Usa-se prefixo (sem \b no
// fim) para pegar flexões: "sorri" pega sorri/sorriu/sorrindo/smile não —
// por isso verbos EN entram como palavras inteiras comuns.
const EMOTION_KEYWORDS = {
  happy: ['feliz', 'happy', 'alegre', 'alegria', 'sorri', 'smil', 'laugh', 'joy', 'contente'],
  sad: ['triste', 'sad', 'chora', 'choro', 'cry', 'tear', 'lágrima', 'melancol', 'deprimid'],
  angry: ['furios', 'angry', 'raiva', 'grita', 'grito', 'yell', 'rage', 'irritad', 'bravo', 'brava'],
  surprised: ['surpres', 'surprised', 'assust', 'shock', 'espant', 'chocad', 'atônit'],
  relaxed: ['calm', 'calmo', 'calma', 'relax', 'tranquil', 'seren', 'alívio', 'aliviad', 'relieved'],
}

// Escapa caracteres especiais de regex no título do NPC.
function escapeRegExp(s) {
  return s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')
}

// Regex case-insensitive com "word boundary" tolerante a acentos: como \b
// não funciona com Unicode em JS, usamos lookarounds de classe de letras.
function mentionRegex(title) {
  const t = escapeRegExp(title.trim()).replace(/\s+/g, '\\s+')
  return new RegExp(`(?<![\\p{L}\\p{N}])${t}(?![\\p{L}\\p{N}])`, 'giu')
}

// Procura palavra de emoção no trecho; retorna a emoção (chave do mapa) ou null.
function emotionInWindow(windowText) {
  const lower = windowText.toLowerCase()
  for (const [emotion, keywords] of Object.entries(EMOTION_KEYWORDS)) {
    for (const kw of keywords) {
      if (lower.includes(kw)) return emotion
    }
  }
  return null
}

// Detecta a emoção associada ao NPC no texto narrativo.
// Retorna { emotion, mention } ou null se não houver menção/emoção.
export function detectEmotion(text, npcTitle) {
  if (!text || !npcTitle || !npcTitle.trim()) return null
  const re = mentionRegex(npcTitle)
  let m
  let found = null
  while ((m = re.exec(text)) !== null) {
    const start = Math.max(0, m.index - WINDOW_CHARS)
    const end = Math.min(text.length, m.index + m[0].length + WINDOW_CHARS)
    const emotion = emotionInWindow(text.slice(start, end))
    if (emotion) found = { emotion, mention: m[0] }
    // Proteção contra loop infinito em matches vazios (não deve ocorrer).
    if (m[0].length === 0) re.lastIndex++
  }
  return found
}
