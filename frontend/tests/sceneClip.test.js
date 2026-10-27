import assert from 'node:assert/strict'
import test from 'node:test'
import {
  SCENE_CLIP_LABEL,
  clipCaption,
  clipEventMatches,
  clipImageFromJob,
  clipJobTerminal,
  clipVideoProps,
  isVideoMedia,
  jobTypeLabel,
  mediaDownloadName,
  mediaFileExtension,
  motionPrompt,
  prefersReducedMotion,
} from '../src/sceneClip.js'

test('scene_clip tem rótulo humano', () => {
  assert.equal(jobTypeLabel('scene_clip'), SCENE_CLIP_LABEL)
  assert.equal(SCENE_CLIP_LABEL, 'Animação de 5 s')
  assert.equal(jobTypeLabel('see_image'), '')
  assert.notEqual(jobTypeLabel('scene_clip'), 'scene_clip')
})

test('mídia de vídeo usa extensão mp4', () => {
  assert.equal(isVideoMedia('video/mp4'), true)
  assert.equal(isVideoMedia('VIDEO/MP4'), true)
  assert.equal(isVideoMedia('image/png'), false)
  assert.equal(isVideoMedia(''), false)
  assert.equal(mediaFileExtension('video/mp4'), 'mp4')
  assert.equal(mediaFileExtension('video/webm'), 'mp4')
  assert.equal(mediaFileExtension('image/png'), 'png')
  assert.equal(mediaFileExtension(undefined), 'png')
  assert.equal(mediaDownloadName('abc', 'video/mp4'), 'scene-abc.mp4')
  assert.equal(mediaDownloadName('abc', 'image/png', { prefix: 'clip' }), 'clip-abc.png')
})

test('prompt de movimento usa a cena e tira metadado de See', () => {
  const prompt = motionPrompt({
    place: 'Cais',
    atmosphere: 'rain+night',
    caption: 'o barco RANGE, people present: Mara, exact likeness, wide shot of that action',
    story: 'A chuva bate no convés. O farol apaga.',
  })
  assert.match(prompt, /5 segundos/)
  assert.match(prompt, /Cais/)
  assert.match(prompt, /chuva/)
  assert.match(prompt, /noite/)
  assert.match(prompt, /o barco/)
  assert.match(prompt, /A chuva bate no convés/)
  assert.doesNotMatch(prompt, /people present/i)
  assert.doesNotMatch(prompt, /exact likeness/i)
  assert.equal(clipCaption('> You open the door, people present: Ana'), 'open the door')
})

test('clip_ready casa aventura e imagem de origem', () => {
  const detail = {
    type: 'clip_ready',
    adventureId: 'adv-1',
    data: {
      adventure_id: 'adv-1',
      job_id: 'job-9',
      source_image_id: 'img-2',
      image: { id: 'vid-3', url: '/api/images/vid-3', media_type: 'video/mp4', duration_ms: 5000 },
    },
  }
  assert.equal(clipEventMatches(detail, { adventureId: 'adv-1', sourceImageId: 'img-2' }), true)
  assert.equal(clipEventMatches(detail, { adventureId: 'adv-9', sourceImageId: 'img-2' }), false)
  assert.equal(clipEventMatches(detail, { adventureId: 'adv-1', sourceImageId: 'img-8' }), false)
  assert.equal(clipEventMatches({ ...detail, type: 'see_ready' }, { adventureId: 'adv-1', sourceImageId: 'img-2' }), false)
  const noImage = { ...detail, data: { ...detail.data, image: {} } }
  assert.equal(clipEventMatches(noImage, { adventureId: 'adv-1', sourceImageId: 'img-2' }), false)
  const wrappedOnly = {
    type: 'clip_ready',
    adventureId: 'adv-1',
    data: { source_image_id: 'img-2', image: { id: 'vid-3' } },
  }
  assert.equal(clipEventMatches(wrappedOnly, { adventureId: 'adv-1', sourceImageId: 'img-2' }), true)
})

test('job terminal só fecha o job desta cena', () => {
  const error = {
    type: 'job_progress',
    adventureId: 'adv-1',
    data: { job_id: 'job-9', type: 'scene_clip', status: 'error', message: 'vram' },
  }
  assert.equal(clipJobTerminal(error, { adventureId: 'adv-1', jobId: 'job-9' }), 'error')
  assert.equal(clipJobTerminal(error, { adventureId: 'adv-1', jobId: 'other' }), '')
  const canceled = { ...error, data: { ...error.data, status: 'cancelled' } }
  assert.equal(clipJobTerminal(canceled, { adventureId: 'adv-1', jobId: 'job-9' }), 'canceled')
  const done = { type: 'job_changed', adventureId: 'adv-1', data: { job_id: 'job-9', status: 'done' } }
  assert.equal(clipJobTerminal(done, { adventureId: 'adv-1', jobId: 'job-9' }), 'done')
})

test('resultado do job só vale se a origem bater', () => {
  const job = {
    payload: { image_id: 'img-2' },
    result: {
      source_image_id: 'img-2',
      image: { id: 'vid-3', media_type: 'video/mp4', duration_ms: 5040 },
    },
  }
  assert.equal(clipImageFromJob(job, 'img-2')?.id, 'vid-3')
  assert.equal(clipImageFromJob(job, 'img-8'), null)
  assert.equal(clipImageFromJob({ result: { image: { id: 'img-2' } } }, 'img-2'), null)
})

test('vídeo não força autoplay, com ou sem reduced motion', () => {
  const still = clipVideoProps({ reducedMotion: false })
  const reduced = clipVideoProps({ reducedMotion: prefersReducedMotion(() => ({ matches: true })) })
  assert.equal(still.controls, true)
  assert.equal(still.playsInline, true)
  assert.equal(still.autoPlay, false)
  assert.equal(still.loop, false)
  assert.equal(reduced.autoPlay, false)
  assert.equal(reduced.reducedMotion, true)
  assert.equal(prefersReducedMotion(() => ({ matches: false })), false)
  assert.equal(prefersReducedMotion(undefined), true)
})
