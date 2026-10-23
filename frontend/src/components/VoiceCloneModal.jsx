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
  const chunksRef = useRef([])
  const timerRef = useRef(null)
  const urlRef = useRef('')

  useEffect(() => {
    if (!open) return undefined
    let cancelled = false
    api.voice.list()
      .then((data) => { if (!cancelled) setCloned(data.cloned || []) })
      .catch(() => { if (!cancelled) setCloned([]) })
    const onKey = (event) => {
      if (event.key === 'Escape' && !busy) onClose(null)
    }
    window.addEventListener('keydown', onKey)
    return () => {
      cancelled = true
      window.removeEventListener('keydown', onKey)
      clearInterval(timerRef.current)
      if (urlRef.current) URL.revokeObjectURL(urlRef.current)
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
    setError('')
    try {
      const { text } = await api.voice.stt(file)
      if (text && text.trim()) setRefText(text.trim())
    } catch (err) {
      setError(messageFor(err))
    }
  }

  async function startRec() {
    setError('')
    let stream
    try {
      stream = await navigator.mediaDevices.getUserMedia({ audio: true })
    } catch {
      setError('Não foi possível acessar o microfone.')
      return
    }
    const rec = new MediaRecorder(stream)
    chunksRef.current = []
    rec.ondataavailable = (event) => {
      if (event.data && event.data.size) chunksRef.current.push(event.data)
    }
    rec.onstop = () => {
      stream.getTracks().forEach((track) => track.stop())
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
    setBusy(true)
    setError('')
    try {
      if (assign.kind === 'narrator') {
        await api.voice.putSettings({ narrator_voice: voice.id })
      } else {
        await api.updateCard(assign.adventureId, assign.cardId, { voice: voice.id })
      }
      onClose(voice)
    } catch (err) {
      setError(messageFor(err))
      setBusy(false)
    }
  }

  async function save() {
    setBusy(true)
    setError('')
    try {
      const saved = await api.voice.clone(audioFile, name.trim(), refText.trim())
      await applyVoice(saved)
    } catch (err) {
      setError(messageFor(err))
      setBusy(false)
    }
  }

  async function remove(id) {
    setBusy(true)
    setError('')
    try {
      await api.voice.remove(id)
      setCloned((list) => list.filter((item) => item.id !== id))
      onClose({ deleted: id })
    } catch (err) {
      setError(messageFor(err))
    } finally {
      setBusy(false)
    }
  }

  const canSave = !busy && !recording && name.trim() && refText.trim() && audioFile

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-4"
      onClick={() => { if (!busy) onClose(null) }}
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
          <button type="button" disabled={busy} onClick={() => onClose(null)} className="rounded-lg px-3 py-2 text-sm">
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
