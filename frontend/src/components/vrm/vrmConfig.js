// Config do avatar VRM de um NPC (v0.4.0).
//
// Persistência: card.vrm_id (string livre) recebe o JSON serializado por
// serializeVrmConfig() — contrato:
//   {"model":"base","hair":"#rrggbb","skin":"#rrggbb","eyes":"#rrggbb",
//    "top":"#rrggbb","bottom":"#rrggbb","emotion":"neutral"}
// As cores são TINTS multiplicativos sobre a cor original do material
// ('#ffffff' = sem alteração). Parse tolerante: vazio/legado/inválido ->
// defaults (ver parseVrmConfig).

// Modelos disponíveis em frontend/public/vrm (vite copia public/ p/ dist).
export const VRM_MODELS = {
  base: '/vrm/base.vrm',
}

// Emoções suportadas (presets de expressão do VRM 1.0).
export const EMOTIONS = ['neutral', 'happy', 'angry', 'sad', 'surprised', 'relaxed']

export const DEFAULT_VRM_CONFIG = {
  model: 'base',
  hair: '#ffffff',
  skin: '#ffffff',
  eyes: '#ffffff',
  top: '#ffffff',
  bottom: '#ffffff',
  emotion: 'neutral',
}

const HEX_RE = /^#[0-9a-fA-F]{6}$/

// Interpreta card.vrm_id de forma tolerante: string vazia, valor legado
// (id simples de modelo, sem JSON) ou JSON inválido/parcial -> defaults com
// o que der para aproveitar.
export function parseVrmConfig(raw) {
  const cfg = { ...DEFAULT_VRM_CONFIG }
  if (!raw || typeof raw !== 'string') return cfg
  let obj
  try {
    obj = JSON.parse(raw)
  } catch {
    // Legado: vrm_id era só o nome do modelo.
    if (VRM_MODELS[raw]) cfg.model = raw
    return cfg
  }
  if (!obj || typeof obj !== 'object') return cfg
  if (typeof obj.model === 'string' && VRM_MODELS[obj.model]) cfg.model = obj.model
  for (const key of ['hair', 'skin', 'eyes', 'top', 'bottom']) {
    if (typeof obj[key] === 'string' && HEX_RE.test(obj[key])) cfg[key] = obj[key].toLowerCase()
  }
  if (EMOTIONS.includes(obj.emotion)) cfg.emotion = obj.emotion
  return cfg
}

// Serializa a config completa (sempre o objeto inteiro, formato do contrato).
export function serializeVrmConfig(cfg) {
  return JSON.stringify({ ...DEFAULT_VRM_CONFIG, ...cfg })
}

// Compara duas configs já parseadas (para o botão Save ficar dirty-aware).
export function sameVrmConfig(a, b) {
  return serializeVrmConfig(a) === serializeVrmConfig(b)
}
