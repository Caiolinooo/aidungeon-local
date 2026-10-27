import { useEffect, useRef, useState } from 'react'
import { api } from '../api.js'
import {
  clipEventMatches,
  clipImageFromJob,
  clipJobTerminal,
  motionPrompt,
} from '../sceneClip.js'

function isAbort(error) {
  return error?.name === 'AbortError'
}

export function useSceneClip({ adventureId, sourceImageId, prompt, toast }) {
  const [pending, setPending] = useState(false)
  const [jobId, setJobId] = useState('')
  const [clip, setClip] = useState(null)
  const [showVideo, setShowVideo] = useState(false)
  const clipRef = useRef(null)
  const jobRef = useRef('')
  const pendingRef = useRef(false)
  const genRef = useRef(0)
  const fetchSeq = useRef(0)
  const ignoredRef = useRef(new Set())
  const handledRef = useRef(new Set())
  const mountedRef = useRef(true)
  const promptRef = useRef(prompt)
  const toastRef = useRef(toast)
  const abortRef = useRef(null)
  promptRef.current = prompt
  toastRef.current = toast

  const notify = (message) => {
    toastRef.current?.(message)
  }

  const revoke = (url) => {
    if (url) URL.revokeObjectURL(url)
  }

  useEffect(() => {
    mountedRef.current = true
    return () => {
      mountedRef.current = false
    }
  }, [])

  useEffect(() => {
    setPending(false)
    setShowVideo(false)
    setClip(null)
    setJobId('')
    pendingRef.current = false
    jobRef.current = ''
    return () => {
      genRef.current += 1
      abortRef.current?.abort()
      abortRef.current = null
      jobRef.current = ''
      pendingRef.current = false
      const url = clipRef.current?.url
      clipRef.current = null
      revoke(url)
    }
  }, [adventureId, sourceImageId])

  const applyBlob = async (image, generation) => {
    const seq = ++fetchSeq.current
    let url = ''
    try {
      const blob = await api.fetchImageBlob(image.id)
      if (!mountedRef.current || generation !== genRef.current || seq !== fetchSeq.current) return
      const type = String(blob?.type || image.media_type || '').toLowerCase()
      if (type.startsWith('image/')) {
        throw new Error('O clipe não veio como vídeo.')
      }
      url = URL.createObjectURL(blob)
      if (!mountedRef.current || generation !== genRef.current || seq !== fetchSeq.current) {
        revoke(url)
        return
      }
      const prev = clipRef.current?.url
      const next = {
        url,
        id: image.id,
        mediaType: image.media_type || blob.type || 'video/mp4',
        durationMs: image.duration_ms,
      }
      clipRef.current = next
      setClip(next)
      setShowVideo(true)
      setPending(false)
      pendingRef.current = false
      jobRef.current = ''
      setJobId('')
      if (prev && prev !== url) revoke(prev)
    } catch (error) {
      if (url) revoke(url)
      if (!mountedRef.current || generation !== genRef.current || seq !== fetchSeq.current) return
      setPending(false)
      pendingRef.current = false
      jobRef.current = ''
      setJobId('')
      notify(error?.message || 'Não foi possível carregar o clipe.')
    }
  }

  useEffect(() => {
    if (!adventureId || !sourceImageId) return undefined
    const onLive = (event) => {
      const detail = event.detail || {}
      if (clipEventMatches(detail, { adventureId, sourceImageId })) {
        const data = detail.data || {}
        const readyJob = data.job_id == null ? '' : String(data.job_id)
        if (readyJob && ignoredRef.current.has(readyJob)) return
        applyBlob(data.image, genRef.current)
        return
      }
      const currentJob = jobRef.current
      const terminal = clipJobTerminal(detail, { adventureId, jobId: currentJob })
      if (!terminal) return
      const key = `${currentJob}:${terminal}`
      if (handledRef.current.has(key)) return
      handledRef.current.add(key)
      if (terminal === 'canceled') {
        ignoredRef.current.add(String(currentJob))
        jobRef.current = ''
        pendingRef.current = false
        if (!mountedRef.current) return
        setJobId('')
        setPending(false)
        return
      }
      if (terminal === 'error') {
        const data = detail.data || {}
        jobRef.current = ''
        pendingRef.current = false
        if (!mountedRef.current) return
        setJobId('')
        setPending(false)
        notify(data.message || data.error || 'Não foi possível animar a cena.')
        return
      }
      const generation = genRef.current
      api.getAdventureJob(adventureId, currentJob).then((job) => {
        if (!mountedRef.current || generation !== genRef.current) return
        const image = clipImageFromJob(job, sourceImageId)
        if (image) applyBlob(image, generation)
      }).catch(() => {})
    }
    window.addEventListener('aidungeon-live', onLive)
    return () => window.removeEventListener('aidungeon-live', onLive)
  }, [adventureId, sourceImageId])

  const start = async () => {
    if (!adventureId || !sourceImageId || pendingRef.current) return
    pendingRef.current = true
    setPending(true)
    setShowVideo(false)
    const generation = genRef.current
    abortRef.current?.abort()
    const ctrl = new AbortController()
    abortRef.current = ctrl
    try {
      const res = await api.requestClip(adventureId, {
        image_id: sourceImageId,
        prompt: promptRef.current || motionPrompt({}),
      }, { signal: ctrl.signal })
      if (!mountedRef.current || generation !== genRef.current) return
      if (!res?.job_id) {
        pendingRef.current = false
        setPending(false)
        notify('Não foi possível iniciar a animação.')
        return
      }
      const id = String(res.job_id)
      jobRef.current = id
      setJobId(id)
    } catch (error) {
      if (!mountedRef.current || generation !== genRef.current) return
      pendingRef.current = false
      setPending(false)
      jobRef.current = ''
      setJobId('')
      if (isAbort(error)) return
      notify(error?.message || 'Não foi possível iniciar a animação.')
    }
  }

  const cancel = async () => {
    const id = jobRef.current
    if (!id) {
      abortRef.current?.abort()
      pendingRef.current = false
      setPending(false)
      return
    }
    try {
      await api.cancelJob(id)
      ignoredRef.current.add(id)
      if (jobRef.current === id) {
        jobRef.current = ''
        pendingRef.current = false
        setJobId('')
        setPending(false)
      }
    } catch (error) {
      if (isAbort(error)) return
      if (error?.status === 409) {
        const generation = genRef.current
        api.getAdventureJob(adventureId, id).then((job) => {
          if (!mountedRef.current || generation !== genRef.current) return
          const image = clipImageFromJob(job, sourceImageId)
          if (image) {
            applyBlob(image, generation)
            return
          }
          if (jobRef.current === id) {
            jobRef.current = ''
            pendingRef.current = false
            setJobId('')
            setPending(false)
          }
          if (job?.status === 'error') notify(job.error || 'Não foi possível animar a cena.')
        }).catch(() => {
          if (!mountedRef.current || generation !== genRef.current) return
          if (jobRef.current === id) {
            jobRef.current = ''
            pendingRef.current = false
            setJobId('')
            setPending(false)
          }
        })
        return
      }
      notify(error?.message || 'Não foi possível cancelar a animação.')
    }
  }

  const failPlayback = () => {
    if (!mountedRef.current || !clipRef.current?.url) return
    const url = clipRef.current.url
    clipRef.current = null
    revoke(url)
    setClip(null)
    setShowVideo(false)
    setPending(false)
    pendingRef.current = false
    notify('Não foi possível reproduzir o clipe.')
  }

  return {
    pending,
    jobId,
    clip,
    showVideo,
    showImage: () => setShowVideo(false),
    showClip: () => {
      if (clipRef.current?.url) setShowVideo(true)
    },
    failPlayback,
    start,
    cancel,
  }
}
