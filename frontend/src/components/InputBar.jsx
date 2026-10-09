import { useEffect, useRef, useState } from 'react'
import { api } from '../api.js'
import { useToast } from './Toast.jsx'

const TABS = [
  { id: 'do', label: 'Do', placeholder: 'What do you do?' },
  { id: 'say', label: 'Say', placeholder: 'What do you say?' },
  { id: 'story', label: 'Story', placeholder: 'What happens next?' },
]

// Mime da gravação. 'audio/webm' pelado faz o Chrome lançar NotSupportedError
// em alguns builds mesmo com isTypeSupported true — por isso a ordem tenta
// opus explícito e, se o construtor falhar, o default do browser.
function preferredMicMime() {
  if (typeof MediaRecorder === 'undefined' || !MediaRecorder.isTypeSupported) return ''
  for (const mime of ['audio/webm;codecs=opus', 'audio/webm', 'audio/mp4']) {
    if (MediaRecorder.isTypeSupported(mime)) return mime
  }
  return ''
}

// Barra de entrada fixa no rodapé: tabs [Do | Say | Story], textarea
// (Enter envia, Shift+Enter quebra linha) e botão Send.
// Desabilitada durante a geração (backend também responde 409).
// onTextChange(text) (opcional): avisa o pai quando o usuário digita/limpa —
// usado pelo Play p/ esconder as "choices" enquanto há texto no input.
// v0.4.0: botão 🎙 dita o texto (MediaRecorder → POST /api/stt) e ANEXA a
// transcrição ao conteúdo atual do input.
export default function InputBar({ disabled, onSend, onTextChange }) {
  const toast = useToast()
  const [tab, setTab] = useState('do')
  const [text, setText] = useState('')
  const [recording, setRecording] = useState(false)
  const [transcribing, setTranscribing] = useState(false)
  const [micHint, setMicHint] = useState('')
  const areaRef = useRef(null)
  const recorderRef = useRef(null)
  const chunksRef = useRef([])

  const active = TABS.find((t) => t.id === tab)

  const submit = () => {
    const t = text.trim()
    if (!t || disabled) return
    onSend(tab, t)
    setText('')
    onTextChange?.('')
  }

  // Para a gravação e limpa stream/recorder (unmount no meio da captura).
  useEffect(() => {
    return () => {
      if (recorderRef.current) {
        recorderRef.current.onstop = null
        if (recorderRef.current.state !== 'inactive') recorderRef.current.stop()
        recorderRef.current.stream?.getTracks().forEach((t) => t.stop())
      }
    }
  }, [])

  const stopRecording = () => {
    // onstop do MediaRecorder faz o envio (o último chunk chega antes dele)
    if (recorderRef.current && recorderRef.current.state !== 'inactive') {
      recorderRef.current.stop()
    }
  }

  const startRecording = async () => {
    setMicHint('')
    // getUserMedia só existe em contexto seguro (https ou localhost).
    // http://host:2122 não pede permissão — a gravação morre aqui e o
    // /api/stt nunca é chamado.
    if (!window.isSecureContext) {
      const msg = 'Microphone needs HTTPS. Open the game with https://, not http://…:2122.'
      setMicHint(msg)
      toast(msg)
      return
    }
    if (!navigator.mediaDevices?.getUserMedia || typeof MediaRecorder === 'undefined') {
      const msg = 'Voice recording is not supported in this browser.'
      setMicHint(msg)
      toast(msg)
      return
    }
    let stream
    try {
      stream = await navigator.mediaDevices.getUserMedia({ audio: true })
    } catch (err) {
      const msg = err?.name === 'NotFoundError'
        ? 'No microphone found on this device.'
        : 'Microphone access denied — allow the mic for this site and try again.'
      setMicHint(msg)
      toast(msg)
      return
    }
    const mime = preferredMicMime()
    let rec
    try {
      rec = new MediaRecorder(stream, mime ? { mimeType: mime } : undefined)
    } catch {
      try {
        rec = new MediaRecorder(stream)
      } catch (err) {
        stream.getTracks().forEach((t) => t.stop())
        const msg = err?.message || 'Could not start the recorder.'
        setMicHint(msg)
        toast(msg)
        return
      }
    }
    recorderRef.current = rec
    chunksRef.current = []
    rec.ondataavailable = (e) => {
      if (e.data && e.data.size > 0) chunksRef.current.push(e.data)
    }
    rec.onstop = async () => {
      stream.getTracks().forEach((t) => t.stop()) // solta o mic (some o indicador do SO)
      recorderRef.current = null
      setRecording(false)
      const blob = new Blob(chunksRef.current, { type: rec.mimeType || mime || 'audio/webm' })
      chunksRef.current = []
      if (blob.size < 200) {
        const msg = 'Recording was empty. Speak, then tap the mic again to stop.'
        setMicHint(msg)
        toast(msg)
        return
      }
      setMicHint('Transcribing…')
      setTranscribing(true)
      try {
        const { text: spoken } = await api.voice.stt(blob)
        const t = (spoken || '').trim()
        if (t) {
          setMicHint('')
          setText((prev) => {
            const next = prev.trim() ? `${prev.trimEnd()} ${t}` : t
            onTextChange?.(next)
            return next
          })
        } else {
          const msg = 'Nothing recognized — try speaking closer to the mic.'
          setMicHint(msg)
          toast(msg)
        }
      } catch (e) {
        setMicHint(e.message)
        toast(e.message)
      } finally {
        setTranscribing(false)
      }
    }
    try {
      // timeslice: sem ele alguns browsers só entregam o áudio no stop e
      // o blob sai vazio se o clique de parar chega cedo demais.
      rec.start(250)
    } catch (err) {
      stream.getTracks().forEach((t) => t.stop())
      recorderRef.current = null
      const msg = err?.message || 'Could not start the recorder.'
      setMicHint(msg)
      toast(msg)
      return
    }
    setRecording(true)
    setMicHint('Recording… tap the mic again to transcribe.')
  }

  // auto-resize da textarea (2..10 linhas)
  useEffect(() => {
    const el = areaRef.current
    if (!el) return
    el.style.height = 'auto'
    el.style.height = `${Math.min(el.scrollHeight, 240)}px`
  }, [text])

  return (
    <div className="border-t border-zinc-800 bg-zinc-950/95 px-3 pb-3 pt-2 backdrop-blur">
      <div className="mx-auto max-w-3xl">
        <div className="mb-2 flex gap-1">
          {TABS.map((t) => (
            <button
              key={t.id}
              onClick={() => setTab(t.id)}
              disabled={disabled}
              className={`rounded-md px-4 py-1.5 text-sm font-medium transition-colors ${
                tab === t.id
                  ? 'bg-emerald-700 text-white'
                  : 'bg-zinc-800 text-zinc-400 hover:bg-zinc-700 hover:text-zinc-200'
              } disabled:opacity-50`}
            >
              {t.label}
            </button>
          ))}
        </div>
        <div className="flex items-end gap-2">
          <textarea
            ref={areaRef}
            value={text}
            onChange={(e) => {
              setText(e.target.value)
              onTextChange?.(e.target.value)
            }}
            onKeyDown={(e) => {
              if (e.key === 'Enter' && !e.shiftKey) {
                e.preventDefault() // Enter envia; Shift+Enter quebra linha
                submit()
              }
            }}
            disabled={disabled}
            rows={2}
            placeholder={disabled ? 'AI is writing…' : active.placeholder}
            className="max-h-60 flex-1 resize-none rounded-lg border border-zinc-700 bg-zinc-800 px-3 py-2 font-prose text-zinc-100 outline-none placeholder:text-zinc-500 focus:border-emerald-600 disabled:opacity-60"
          />
          <button
            onClick={recording ? stopRecording : startRecording}
            disabled={disabled || transcribing}
            title={
              transcribing
                ? 'Transcribing…'
                : recording
                  ? 'Stop recording'
                  : 'Dictate (speech-to-text)'
            }
            className={`rounded-lg px-3 py-2.5 font-semibold text-white disabled:cursor-not-allowed disabled:opacity-40 ${
              recording
                ? 'animate-pulse bg-red-700 hover:bg-red-600'
                : 'bg-zinc-700 hover:bg-zinc-600'
            }`}
          >
            {transcribing ? '⏳' : recording ? '⏹' : '🎙'}
          </button>
          <button
            onClick={submit}
            disabled={disabled || !text.trim()}
            className="rounded-lg bg-emerald-700 px-5 py-2.5 font-semibold text-white hover:bg-emerald-600 disabled:cursor-not-allowed disabled:opacity-40"
          >
            Send
          </button>
        </div>
        {micHint && (
          <p className="mt-1 text-xs text-amber-300/90">{micHint}</p>
        )}
      </div>
    </div>
  )
}
