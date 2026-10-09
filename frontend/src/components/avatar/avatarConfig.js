// Config do avatar 3D (TalkingHead) de um NPC — substitui o antigo contrato
// VRM (tints por cor) desde a migração para Ready Player Me / GLB.
//
// Persistência: card.vrm_id (string livre; nome da coluna mantido por
// compatibilidade de schema) recebe o JSON de serializeAvatarConfig():
//   {"model":"brunette","url":"","body":"F","mood":"neutral"}
// - model: chave em AVATARS, ou "custom" (url GLB própria — Ready Player Me,
//   Avaturn T2, VRoid convertido etc.; precisa de rig Mixamo-compatible +
//   blendshapes ARKit/Oculus, ver README do TalkingHead).
// - url:   só usada quando model === "custom"; GLB pública (https://...).
// - body:  "M" | "F" — forma corporal usada pelo TalkingHead p/ poses.
// - mood:  um dos MOODS (moods nativos do TalkingHead).
//
// Parse tolerante: vazio, legado VRM {"model":"base","hair":...} ou JSON
// inválido -> defaults (avatar padrão, mood neutral).

// Modelos servidos de frontend/public/avatars (vite copia public/ p/ dist).
// Ambos com rig "Armature" + blendshapes ARKit/Oculus Visemes (exigência do
// TalkingHead). Licenças: brunette.glb (Ready Player Me, CC BY-NC 4.0),
// avaturn.glb (Avaturn, uso não-comercial) — ver public/avatars/LICENSE.txt.
// O antigo preset vroid.glb foi removido: GLB cru de VRoid não é compatível
// com o TalkingHead sem conversão em Blender (ver LICENSE.txt).
export const AVATARS = {
  brunette: { url: '/avatars/brunette.glb', body: 'F', label: 'Realista (RPM)' },
  avaturn: { url: '/avatars/avaturn.glb', body: 'F', label: 'Estilizado (Avaturn)' },
}

export const DEFAULT_AVATAR_MODEL = 'brunette'

// Moods nativos do TalkingHead (setMood).
export const MOODS = ['neutral', 'happy', 'angry', 'sad', 'fear', 'disgust', 'love', 'sleep']

export const DEFAULT_AVATAR_CONFIG = {
  model: DEFAULT_AVATAR_MODEL,
  url: '',
  body: AVATARS[DEFAULT_AVATAR_MODEL].body,
  mood: 'neutral',
}

// Interpreta card.vrm_id de forma tolerante: string vazia, JSON legado do
// contrato VRM (cores/emotion) ou JSON inválido/parcial -> defaults com o
// que der para aproveitar. body inválido cai no body do modelo escolhido.
export function parseAvatarConfig(raw) {
  const cfg = { ...DEFAULT_AVATAR_CONFIG }
  if (!raw || typeof raw !== 'string') return cfg
  let obj
  try {
    obj = JSON.parse(raw)
  } catch {
    // Legado pré-JSON: vrm_id era só o nome do modelo.
    if (AVATARS[raw]) cfg.model = raw
    cfg.body = AVATARS[cfg.model].body
    return cfg
  }
  if (!obj || typeof obj !== 'object') return cfg
  if (obj.model === 'custom' && typeof obj.url === 'string' && /^https?:\/\//.test(obj.url)) {
    cfg.model = 'custom'
    cfg.url = obj.url
  } else if (typeof obj.model === 'string' && AVATARS[obj.model]) {
    cfg.model = obj.model
  }
  if (obj.body === 'M' || obj.body === 'F') {
    cfg.body = obj.body
  } else if (cfg.model !== 'custom') {
    cfg.body = AVATARS[cfg.model].body
  }
  if (MOODS.includes(obj.mood)) cfg.mood = obj.mood
  return cfg
}

// Serializa a config completa (sempre o objeto inteiro, formato do contrato).
export function serializeAvatarConfig(cfg) {
  return JSON.stringify({ ...DEFAULT_AVATAR_CONFIG, ...cfg })
}

// Compara duas configs já parseadas (para o botão Save ficar dirty-aware).
export function sameAvatarConfig(a, b) {
  return serializeAvatarConfig(a) === serializeAvatarConfig(b)
}

// URL final do GLB: preset ou custom.
export function avatarUrl(cfg) {
  if (cfg.model === 'custom') return cfg.url
  return AVATARS[cfg.model]?.url || AVATARS[DEFAULT_AVATAR_MODEL].url
}
