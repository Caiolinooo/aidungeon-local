// Menção de NPC no texto da cena, para o dock mostrar quem está presente.
// word boundary tolerante a acentos: \b não cobre letras Unicode no JS.

function escapeRegExp(s) {
  return s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')
}

function mentionRegex(title) {
  const t = escapeRegExp(title.trim()).replace(/\s+/g, '\\s+')
  return new RegExp(`(?<![\\p{L}\\p{N}])${t}(?![\\p{L}\\p{N}])`, 'giu')
}

export function mentionsNpc(text, names) {
  if (!text) return false
  return (names || []).some((n) => n && n.trim() && mentionRegex(n).test(text))
}
