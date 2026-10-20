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

// Arquétipos do wizard. Espelham backend/app/stats.py (_ARCHETYPES):
// base 10, +4/+3 nas habilidades-chave; o resto cabe no point-buy
// (8..15, custo ≤ 27) para o modo Pontos não reduzir o preset.
export const CHARACTER_PRESETS = [
  {
    id: 'guerreiro',
    name: 'Guerreiro',
    classHint: 'Guerreiro',
    blurb: 'Linha de frente, arma e escudo.',
    stats: { str: 14, dex: 13, con: 13, int: 8, wis: 12, cha: 12 },
    appearanceHints: [
      'cicatriz atravessando a sobrancelha e ombros largos',
      'cabelo curto, queixo quadrado e olhar que não recua',
      'pele curtida de sol, mãos calejadas e postura de guarda',
    ],
  },
  {
    id: 'ladino',
    name: 'Ladino',
    classHint: 'Ladino',
    blurb: 'Furtividade, truques e dedos leves.',
    stats: { str: 8, dex: 14, con: 12, int: 14, wis: 13, cha: 12 },
    appearanceHints: [
      'olhos atentos e um sorriso de canto que some rápido',
      'corpo magro, dedos ágeis e um capuz gasto',
      'mecha caída no rosto e uma marca fina no pulso',
    ],
  },
  {
    id: 'mago',
    name: 'Mago',
    classHint: 'Mago',
    blurb: 'Estudo, foco e magia.',
    stats: { str: 8, dex: 13, con: 12, int: 14, wis: 14, cha: 10 },
    appearanceHints: [
      'olheiras de quem lê até tarde e mãos manchadas de tinta',
      'cabelo comprido preso de qualquer jeito e olhar distante',
      'rosto estreito, um anel gasto e cheiro de ervas queimadas',
    ],
  },
  {
    id: 'clerigo',
    name: 'Clérigo',
    classHint: 'Clérigo',
    blurb: 'Fé, cura e presença.',
    stats: { str: 12, dex: 10, con: 14, int: 10, wis: 14, cha: 13 },
    appearanceHints: [
      'expressão serena e mãos limpas de quem trata feridas',
      'cabelo preso e um símbolo sagrado discreto na testa',
      'olhos pacientes e um manto simples de tecido grosso',
    ],
  },
  {
    id: 'bardo',
    name: 'Bardo',
    classHint: 'Bardo',
    blurb: 'Música, lábia e presença de palco.',
    stats: { str: 8, dex: 14, con: 12, int: 12, wis: 10, cha: 14 },
    appearanceHints: [
      'sorriso fácil e cabelo arrumado para o palco',
      'olhos brilhantes e um brinco que pega a luz',
      'postura leve, roupa colorida e mãos de quem toca',
    ],
  },
]

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
