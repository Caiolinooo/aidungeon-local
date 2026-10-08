// Formata timestamps ISO-8601 (UTC) de forma amigável para a UI.
export function fmtDate(iso) {
  if (!iso) return ''
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return iso
  return new Intl.DateTimeFormat('en-US', {
    month: 'short',
    day: 'numeric',
    year: 'numeric',
    hour: 'numeric',
    minute: '2-digit',
  }).format(d)
}

// Ordena actions por seq (ordem absoluta da história — nunca pelo id).
export function bySeq(a, b) {
  return a.seq - b.seq
}

// Ações do jogador (do/say) são persistidas JÁ FORMATADAS pelo backend
// (ex.: "\n> You play a song\n" / '\n> You say "hi"\n' — mesmo formato do
// backup oficial). Para exibir/edição na UI (rótulo "You do/say" + citação),
// removemos o wrapper "> You ..." sem tocar no conteúdo.
export function playerDisplayText(type, text) {
  let t = (text ?? '').trim()
  if (type === 'do') {
    const m = t.match(/^>\s*You\s+([\s\S]*)$/i)
    if (m) t = m[1]
  } else if (type === 'say') {
    const m = t.match(/^>\s*You\s+say\s+"([\s\S]*)"\s*$/i)
    if (m) t = m[1]
    else {
      const m2 = t.match(/^>\s*You\s+say\s+([\s\S]*)$/i)
      if (m2) t = m2[1]
    }
  }
  return t.trim()
}
