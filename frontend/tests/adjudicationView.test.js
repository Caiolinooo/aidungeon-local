import assert from 'node:assert/strict'
import test from 'node:test'
import {
  adjudicationMode,
  mergeBeat,
  presentAdjudication,
  storeBeat,
} from '../src/components/adjudicationView.js'
import {
  gestureFromVisual,
  moodFromEmotion,
  poseFromVisual,
  resolveAvatarMotion,
} from '../src/components/avatar/avatarMotion.js'
import { bondGlance, stanceOf } from '../src/components/drawer/npcBondView.js'

test('adjudicação pública omite cue e motivo oculto', () => {
  const view = presentAdjudication({
    decision: 'refuse',
    outcome: 'Ela recua e não entrega a chave.',
    cue: 'Não deixe o jogador vencer.',
    hidden: 'quer a coroa',
    reactions: [{
      npc: 'Mara',
      card_id: 'c1',
      reaction: 'segura a bolsa',
      line: 'Não.',
      emotion: 'fear',
      pose: 'back',
      motive: 'esconder o mapa',
    }],
    initiative: null,
    relation_changes: [{
      card_id: 'c1',
      name: 'Mara',
      target: 'player',
      delta: { distrust: 8, trust: -4, affinity: 9 },
      reason: 'recusou entregar a chave',
      evidence: 'segurou a bolsa contra o peito',
    }],
  })
  assert.equal(view.decision, 'refuse')
  assert.equal('cue' in view, false)
  assert.equal(view.reactions[0].motive, undefined)
  assert.deepEqual(view.relation_changes[0].delta, { distrust: 8, trust: -4 })
  assert.equal(view.relation_changes[0].evidence, 'segurou a bolsa contra o peito')
})

test('delta repetido não entra duas vezes e retry/story não aplicam', () => {
  assert.equal(adjudicationMode('retry'), 'ignore')
  assert.equal(adjudicationMode('story'), 'ignore')
  assert.equal(adjudicationMode('do'), 'apply')
  const early = presentAdjudication({
    decision: 'partial',
    outcome: 'Ela cede só o nome.',
    reactions: [{ npc: 'Lia', card_id: 'c2', reaction: 'sussurra o nome', line: '' }],
  }, { includeDeltas: false })
  const done = presentAdjudication({
    decision: 'partial',
    outcome: 'Ela cede só o nome.',
    reactions: [{ npc: 'Lia', card_id: 'c2', reaction: 'sussurra o nome', line: '' }],
    relation_changes: [
      { card_id: 'c2', name: 'Lia', target: 'n9', delta: { affinity: 5, trust: 0 }, reason: 'disse o nome', evidence: 'disse o nome' },
      { card_id: 'c2', name: 'Lia', target: 'n9', delta: { affinity: 5 }, reason: 'disse o nome', evidence: '' },
    ],
  })
  const merged = mergeBeat(early, done)
  assert.equal(merged.relation_changes.length, 1)
  assert.equal(merged.reactions.length, 1)
  const stored = storeBeat(storeBeat({}, 4, merged), 4, merged)
  assert.equal(Object.keys(stored).length, 1)
})

test('emoção vira mood do TalkingHead e pose solta não vira URL', () => {
  assert.equal(moodFromEmotion('tenderness'), 'love')
  assert.equal(moodFromEmotion('euforia'), null)
  assert.equal(poseFromVisual('sitting'), 'sitting')
  assert.equal(poseFromVisual('de braços cruzados'), null)
  assert.equal(gestureFromVisual('shrug'), 'shrug')
  assert.equal(gestureFromVisual('recua'), null)
  const motion = resolveAvatarMotion({ emotion: 'medo', pose: 'kneel', action: 'wave' }, 'neutral')
  assert.deepEqual(motion, { mood: 'fear', pose: 'kneel', gesture: 'handup' })
})

test('ficha mostra desconfiança, estágio, histórico e motivo NPC', () => {
  const bonds = {
    relationship: 20, trust: 20, distrust: 64, fear: 10, desire: 10, boldness: 30,
  }
  assert.equal(stanceOf(bonds), 'resist')
  const glance = bondGlance({
    alignment: 'desconfiado',
    bonds,
    visual_state: { emotion: 'fear', pose: 'back', action: 'recua', seq: 3 },
    bond_log: [
      { seq: 3, target: 'player', delta: { distrust: 8 }, reason: 'recusou a ordem', evidence: 'deu um passo atrás' },
      { seq: 3, target: 'player', delta: { distrust: 1 }, reason: 'motivação oculta: quer fugir', evidence: '' },
    ],
    relations: {
      lia: {
        trust: 22,
        fear: 40,
        affinity: 15,
        history: ['seq 3 | {"affinity": 5} | dividiram o turno', 'cue secreto'],
      },
    },
  }, { lia: 'Lia' })
  assert.equal(glance.stance, 'resist')
  assert.equal(glance.stage, 'cautela')
  assert.equal(glance.bonds.distrust, 64)
  assert.equal(glance.log.length, 1)
  assert.equal(glance.log[0].reason, 'recusou a ordem')
  assert.equal(glance.relations[0].name, 'Lia')
  assert.equal(glance.relations[0].reason, 'dividiram o turno')
  assert.equal(glance.emotion, 'fear')
})
