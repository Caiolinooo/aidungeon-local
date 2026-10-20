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

// As seis habilidades com rótulo em pt-BR. Compartilhado pelo modal do
// jogador (criação/level-up) e pelo painel de stats da ficha do NPC.
export const CORE_STATS = [
  ['str', 'Força'],
  ['dex', 'Destreza'],
  ['con', 'Constituição'],
  ['int', 'Inteligência'],
  ['wis', 'Sabedoria'],
  ['cha', 'Carisma'],
]

// Modificador de atributo: floor((valor - 10) / 2).
export function abilityMod(value) {
  const mod = Math.floor((Number(value) - 10) / 2)
  if (!Number.isFinite(mod)) return '+0'
  return mod >= 0 ? `+${mod}` : String(mod)
}

// 4d6 descartando o menor: 3..18.
export function roll4d6DropLowest() {
  const dice = [0, 1, 2, 3].map(() => 1 + Math.floor(Math.random() * 6))
  dice.sort((a, b) => a - b)
  return dice[1] + dice[2] + dice[3]
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
