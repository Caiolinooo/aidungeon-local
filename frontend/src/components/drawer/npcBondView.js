// Leitura compacta da ficha para a aba NPCs.
// stance segue os limiares de backend/app/npc_bonds.py quando a ficha ainda não gravou posture.

const ALIGNMENTS = ['aliado', 'neutro', 'desconfiado', 'oposto']
const STANCES = ['help', 'resist', 'flirt', 'flee', 'attack']

export const STANCE_LABEL = {
  help: 'ajuda',
  resist: 'recua',
  flirt: 'se aproxima',
  flee: 'foge',
  attack: 'parte pra cima',
}

export const ALIGNMENT_LABEL = {
  aliado: 'aliado',
  neutro: 'neutro',
  desconfiado: 'desconfiado',
  oposto: 'oposto',
}

const BOND_DEFAULTS = {
  relationship: 40,
  trust: 40,
  distrust: 30,
  fear: 15,
  desire: 15,
  boldness: 40,
}

function clamp(value, fallback) {
  const number = Number.parseInt(value, 10)
  if (!Number.isFinite(number)) return fallback
  return Math.max(0, Math.min(100, number))
}

export function readBonds(sheet) {
  const raw = sheet?.bonds && typeof sheet.bonds === 'object' ? sheet.bonds : {}
  const bonds = {}
  for (const key of Object.keys(BOND_DEFAULTS)) {
    bonds[key] = clamp(raw[key], BOND_DEFAULTS[key])
  }
  return bonds
}

export function stanceOf(bonds, obedience = '') {
  if (obedience === 'obedient_compelled') return 'help'
  const trust = bonds.trust
  const fear = bonds.fear
  const desire = bonds.desire
  const boldness = bonds.boldness
  const relationship = bonds.relationship
  if (fear >= 70 && boldness < 55) return 'flee'
  if (fear >= 60 && trust <= 25) return 'flee'
  if (trust <= 25 && boldness >= 60 && relationship <= 35) return 'attack'
  if (bonds.distrust >= 60) return 'resist'
  if (desire >= 65 && trust >= 35 && fear < 50) return 'flirt'
  if (trust >= 55 && relationship >= 50 && fear < 45) return 'help'
  return 'resist'
}

export function relationStage(bonds, alignment) {
  if (alignment === 'oposto' || bonds.distrust >= 70) return 'rompimento'
  if (alignment === 'desconfiado' || bonds.distrust >= 50) return 'cautela'
  if (alignment === 'aliado' && bonds.trust >= 70 && bonds.relationship >= 70) return 'aliança'
  if (alignment === 'aliado') return 'aproximação'
  return 'distância'
}

function clean(value, limit = 160) {
  const text = String(value ?? '').replace(/\s+/g, ' ').trim()
  if (!text || /\[Adjudicação|motivação oculta|\bcue\b/i.test(text)) return ''
  return text.slice(0, limit)
}

function visibleHistory(text) {
  const cleaned = clean(text, 240)
  const parts = cleaned.split(' | ')
  if (parts.length >= 3 && /^seq\s+\d+$/i.test(parts[0])) {
    return parts.slice(2).join(' | ')
  }
  return cleaned
}

function lastReason(relation) {
  if (!relation || typeof relation !== 'object') return ''
  const direct = clean(relation.reason || relation.last_reason)
  if (direct) return direct
  const history = Array.isArray(relation.history) ? relation.history : []
  for (let i = history.length - 1; i >= 0; i -= 1) {
    const item = history[i]
    if (typeof item === 'string') {
      const text = visibleHistory(item)
      if (text) return text
    } else if (item && typeof item === 'object') {
      const text = clean(item.reason || item.evidence || item.text)
      if (text) return text
    }
  }
  return ''
}

export function bondLogEntries(sheet, nameById = {}) {
  const raw = Array.isArray(sheet?.bond_log) ? sheet.bond_log : []
  const out = []
  for (const item of raw) {
    if (!item || typeof item !== 'object') continue
    const reason = clean(item.reason)
    const evidence = clean(item.evidence)
    const delta = item.delta && typeof item.delta === 'object' ? item.delta : {}
    if (!reason && !evidence) continue
    out.push({
      seq: item.seq ?? null,
      target: clean(item.target, 40) || 'player',
      target_name: clean(item.target_name, 80) || nameById?.[item.target] || '',
      delta,
      reason,
      evidence: evidence && evidence !== reason ? evidence : '',
    })
  }
  return out.slice(-4).reverse()
}

export function npcRelations(sheet, nameById) {
  const raw = sheet?.relations && typeof sheet.relations === 'object' ? sheet.relations : {}
  const out = []
  for (const [id, relation] of Object.entries(raw)) {
    if (!relation || typeof relation !== 'object' || Array.isArray(relation)) continue
    const reason = lastReason(relation)
    const trust = Number.isFinite(Number(relation.trust)) ? Number(relation.trust) : null
    const fear = Number.isFinite(Number(relation.fear)) ? Number(relation.fear) : null
    const affinity = Number.isFinite(Number(relation.affinity)) ? Number(relation.affinity) : null
    if (trust == null && fear == null && affinity == null && !reason) continue
    out.push({
      id,
      name: nameById?.[id] || 'NPC',
      trust,
      fear,
      affinity,
      reason,
    })
  }
  return out.slice(0, 4)
}

export function bondGlance(sheet, nameById) {
  const bonds = readBonds(sheet)
  const alignment = ALIGNMENTS.includes(sheet?.alignment) ? sheet.alignment : 'neutro'
  const storedStance = STANCES.includes(sheet?.stance) ? sheet.stance : ''
  const stage = clean(sheet?.stage, 40) || relationStage(bonds, alignment)
  const visual = sheet?.visual_state && typeof sheet.visual_state === 'object' ? sheet.visual_state : {}
  return {
    alignment,
    alignmentLabel: ALIGNMENT_LABEL[alignment],
    stage,
    stance: storedStance || stanceOf(bonds),
    bonds,
    log: bondLogEntries(sheet, nameById),
    relations: npcRelations(sheet, nameById),
    emotion: clean(visual.emotion, 40),
    pose: clean(visual.pose, 40),
  }
}
