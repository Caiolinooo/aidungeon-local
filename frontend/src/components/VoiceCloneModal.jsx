import { useEffect, useRef, useState } from 'react'
import { api } from '../api.js'

function messageFor(error) {
  const code = error && error.code
  if (code === 'voice_name_taken') return 'Já existe uma voz com esse nome.'
  if (code === 'audio_too_short') return 'O áudio precisa ter pelo menos 1 segundo.'
  if (code === 'audio_too_long') return 'O áudio pode ter no máximo 30 segundos.'
  if (code === 'ref_text_empty') return 'Digite o que foi dito nesse áudio.'
  if (code === 'invalid_audio') return 'Não foi possível ler esse áudio.'
  return (error && error.message) || 'Falha ao clonar a voz.'
}

export default function VoiceCloneModal({ open, onClose, assign }) {
  const [name, setName] = useState('')
  const [refText, setRefText] = useState('')
  const [audioFile, setAudioFile] = useState(null)
  const [audioUrl, setAudioUrl] = useState('')
  const [recording, setRecording] = useState(false)
  const [seconds, setSeconds] = useState(0)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [cloned, setCloned] = useState([])
  const recRef = useRef(null)
  const streamRef = useRef(null)
  const chunksRef = useRef([])
  const timerRef = useRef(null)
  const urlRef = useRef('')
  const requestsRef = useRef(null)
  const activeRef = useRef(false)
  const sessionRef = useRef(0)

  function cleanup() {
    activeRef.current = false
    sessionRef.current += 1
    const controller = requestsRef.current
    requestsRef.current = null
    controller?.abort()
    clearInterval(timerRef.current)

    const rec = recRef.current
    recRef.current = null
    if (rec) {
      rec.ondataavailable = null
      rec.onstop = null
      if (rec.state !== 'inactive') {
        try { rec.stop() } catch { /* recorder já parou */ }
      }
    }
    const stream = streamRef.current || rec?.stream
    streamRef.current = null
    stream?.getTracks().forEach((track) => track.stop())

    if (urlRef.current) {
      URL.revokeObjectURL(urlRef.current)
      urlRef.current = ''
    }
  }

  function close(result) {
    cleanup()
    onClose(result)
  }

  function isCurrent(controller, session) {
    return activeRef.current
      && sessionRef.current === session
      && !controller.signal.aborted
  }

  useEffect(() => {
    if (!open) return undefined
    activeRef.current = true
    sessionRef.current += 1
    setBusy(false)
    setRecording(false)
    const controller = new AbortController()
    requestsRef.current = controller
    let cancelled = false
    api.voice.list({ signal: controller.signal })
      .then((data) => { if (!cancelled && !controller.signal.aborted) setCloned(data.cloned || []) })
      .catch(() => { if (!cancelled && !controller.signal.aborted) setCloned([]) })
    const onKey = (event) => {
      if (event.key === 'Escape' && !busy) close(null)
    }
    window.addEventListener('keydown', onKey)
    return () => {
      cancelled = true
      window.removeEventListener('keydown', onKey)
      cleanup()
    }
  }, [open]) // eslint-disable-line react-hooks/exhaustive-deps

  if (!open) return null

  const target = assign.kind === 'narrator'
    ? 'Narrador'
    : `Personagem: ${assign.cardName || 'Personagem'}`

  function setAudio(file) {
    if (urlRef.current) URL.revokeObjectURL(urlRef.current)
    const url = URL.createObjectURL(file)
    urlRef.current = url
    setAudioFile(file)
    setAudioUrl(url)
    transcribe(file)
  }

  async function transcribe(file) {
    const controller = requestsRef.current
    const session = sessionRef.current
    if (!controller) return
    setError('')
    try {
      const { text } = await api.voice.stt(file, { signal: controller.signal })
      if (!isCurrent(controller, session)) return
      if (text && text.trim()) setRefText(text.trim())
    } catch (err) {
      if (isCurrent(controller, session)) setError(messageFor(err))
    }
  }

  async function startRec() {
    const session = sessionRef.current
    setError('')
    let stream
    try {
      stream = await navigator.mediaDevices.getUserMedia({ audio: true })
    } catch {
      if (activeRef.current && sessionRef.current === session) {
        setError('Não foi possível acessar o microfone.')
      }
      return
    }
    if (!activeRef.current || sessionRef.current !== session) {
      stream.getTracks().forEach((track) => track.stop())
      return
    }
    streamRef.current = stream
    const rec = new MediaRecorder(stream)
    chunksRef.current = []
    rec.ondataavailable = (event) => {
      if (sessionRef.current === session && event.data && event.data.size) {
        chunksRef.current.push(event.data)
      }
    }
    rec.onstop = () => {
      stream.getTracks().forEach((track) => track.stop())
      if (streamRef.current === stream) streamRef.current = null
      if (recRef.current === rec) recRef.current = null
      if (!activeRef.current || sessionRef.current !== session) return
      const type = rec.mimeType || 'audio/webm'
      const blob = new Blob(chunksRef.current, { type })
      setAudio(new File([blob], 'gravacao.webm', { type }))
    }
    rec.start()
    recRef.current = rec
    setRecording(true)
    setSeconds(0)
    clearInterval(timerRef.current)
    timerRef.current = setInterval(() => {
      setSeconds((current) => {
        if (current + 1 >= 30) {
          stopRec()
          return 30
        }
        return current + 1
      })
    }, 1000)
  }

  function stopRec() {
    clearInterval(timerRef.current)
    const rec = recRef.current
    if (rec && rec.state !== 'inactive') rec.stop()
    setRecording(false)
  }

  async function applyVoice(voice) {
    const controller = requestsRef.current
    const session = sessionRef.current
    if (!controller) return
    setBusy(true)
    setError('')
    try {
      if (assign.kind === 'narrator') {
        await api.voice.putSettings({ narrator_voice: voice.id }, { signal: controller.signal })
      } else {
        await api.updateCard(assign.adventureId, assign.cardId, { voice: voice.id }, { signal: controller.signal })
      }
      if (isCurrent(controller, session)) close(voice)
    } catch (err) {
      if (!isCurrent(controller, session)) return
      setError(messageFor(err))
      setBusy(false)
    }
  }

  async function save() {
    const controller = requestsRef.current
    const session = sessionRef.current
    if (!controller) return
    setBusy(true)
    setError('')
    try {
      const saved = await api.voice.clone(audioFile, name.trim(), refText.trim(), { signal: controller.signal })
      if (!isCurrent(controller, session)) return
      await applyVoice(saved)
    } catch (err) {
      if (!isCurrent(controller, session)) return
      setError(messageFor(err))
      setBusy(false)
    }
  }

  async function remove(id) {
    const controller = requestsRef.current
    const session = sessionRef.current
    if (!controller) return
    setBusy(true)
    setError('')
    try {
      await api.voice.remove(id, { signal: controller.signal })
      if (!isCurrent(controller, session)) return
      setCloned((list) => list.filter((item) => item.id !== id))
      close({ deleted: id })
    } catch (err) {
      if (isCurrent(controller, session)) setError(messageFor(err))
    } finally {
      if (isCurrent(controller, session)) setBusy(false)
    }
  }

  const canSave = !busy && !recording && name.trim() && refText.trim() && audioFile

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-4"
      onClick={() => { if (!busy) close(null) }}
    >
      <div
        className="max-h-[90vh] w-full max-w-lg overflow-y-auto rounded-xl border border-subtle bg-surface p-4 text-secondary"
        onClick={(event) => event.stopPropagation()}
      >
        <h2 className="text-base font-semibold">Clonar voz</h2>
        <p className="mt-1 text-xs text-muted">{target}</p>

        <label className="mt-4 block text-xs font-semibold uppercase tracking-wide text-muted">
          Nome
          <input
            value={name}
            maxLength={40}
            onChange={(event) => setName(event.target.value)}
            className="mt-1 w-full rounded-lg border border-subtle bg-surface px-3 py-2 text-sm outline-none"
          />
        </label>

        <div className="mt-3 flex flex-wrap items-center gap-2">
          <label className="cursor-pointer rounded-lg border border-subtle px-3 py-2 text-sm">
            Enviar áudio
            <input
              type="file"
              accept="audio/*"
              className="hidden"
              onChange={(event) => {
                const file = event.target.files && event.target.files[0]
                if (file) setAudio(file)
              }}
            />
          </label>
          {recording ? (
            <button type="button" onClick={stopRec} className="rounded-lg border border-subtle px-3 py-2 text-sm">
              Parar ({seconds}s)
            </button>
          ) : (
            <button type="button" onClick={startRec} className="rounded-lg border border-subtle px-3 py-2 text-sm">
              Gravar
            </button>
          )}
        </div>

        {audioUrl && (
          <audio className="mt-3 w-full" controls src={audioUrl} />
        )}

        <label className="mt-3 block text-xs font-semibold uppercase tracking-wide text-muted">
          O que foi dito
          <textarea
            value={refText}
            rows={3}
            onChange={(event) => setRefText(event.target.value)}
            className="mt-1 w-full resize-y rounded-lg border border-subtle bg-surface px-3 py-2 text-sm outline-none"
          />
        </label>
        <button
          type="button"
          disabled={!audioFile || busy}
          onClick={() => audioFile && transcribe(audioFile)}
          className="mt-2 text-xs text-muted underline disabled:opacity-40"
        >
          Transcrever
        </button>

        {error && <p className="mt-3 text-sm text-red-400">{error}</p>}

        <div className="mt-4 flex justify-end gap-2">
          <button type="button" disabled={busy} onClick={() => close(null)} className="rounded-lg px-3 py-2 text-sm">
            Cancelar
          </button>
          <button
            type="button"
            disabled={!canSave}
            onClick={save}
            className="rounded-lg border border-subtle px-3 py-2 text-sm disabled:opacity-40"
          >
            {busy ? 'Salvando…' : 'Salvar e usar'}
          </button>
        </div>

        <div className="mt-5 border-t border-subtle pt-3">
          <h3 className="text-xs font-semibold uppercase tracking-wide text-muted">Vozes salvas</h3>
          {cloned.length === 0 && <p className="mt-2 text-xs text-muted">Nenhuma voz clonada ainda.</p>}
          <ul className="mt-2 flex flex-col gap-2">
            {cloned.map((item) => (
              <li key={item.id} className="flex items-center justify-between gap-2 text-sm">
                <span>{item.name}</span>
                <span className="flex gap-2">
                  <button type="button" disabled={busy} onClick={() => applyVoice(item)} className="underline">
                    Usar
                  </button>
                  <button type="button" disabled={busy} onClick={() => remove(item.id)} className="underline">
                    Apagar
                  </button>
                </span>
              </li>
            ))}
          </ul>
        </div>
      </div>
    </div>
  )
}
