import { useEffect, useRef, useState } from 'react'
import { api } from '../api.js'
import { useToast } from './Toast.jsx'

const TABS = [
  { id: 'do', label: 'Ação', placeholder: 'O que você faz?' },
  { id: 'say', label: 'Fala', placeholder: 'O que você diz?' },
  { id: 'story', label: 'História', placeholder: 'O que acontece em seguida?' },
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
// v0.7.0: `tabs` dinâmicas do ui_profile da IA (labels/placeholders por
// gênero; ids continuam do/say/story), botão 🎲 abre o GamePanel quando há
// widgets, e `inject` ({text, nonce}) anexa texto vindo dos widgets.
const DEFAULT_TABS = TABS

export default function InputBar({
  disabled, disabledTurn = false, onSend, onTextChange,
  tabs = DEFAULT_TABS, inject = null,
  panelAvailable = false, panelOpen = false, onTogglePanel,
}) {
  const toast = useToast()
  const normTabs = (tabs && tabs.length ? tabs : DEFAULT_TABS).map((t) => ({
    ...t,
    id: t.id || t.type || 'do',
  }))
  const [tab, setTab] = useState(normTabs[0]?.id ?? 'do')
  const [text, setText] = useState('')
  const [recording, setRecording] = useState(false)
  const [transcribing, setTranscribing] = useState(false)
  const [micHint, setMicHint] = useState('')
  const [webOn, setWebOn] = useState(false)
  const areaRef = useRef(null)
  const recorderRef = useRef(null)
  const chunksRef = useRef([])

  const active = normTabs.find((t) => t.id === tab) ?? normTabs[0] ?? DEFAULT_TABS[0]

  // Tabs dinâmicas: se a tab atual sumiu do manifesto, volta p/ a primeira.
  useEffect(() => {
    if (!normTabs.some((t) => t.id === tab)) setTab(normTabs[0]?.id ?? 'do')
  }, [normTabs, tab])

  // Texto injetado pelos widgets do GamePanel (dado, "use item"…): ANEXA,
  // nunca sobrescreve o que o jogador já digitou. nonce = nova injeção.
  const lastInjectNonce = useRef(null)
  useEffect(() => {
    if (!inject || inject.nonce === lastInjectNonce.current) return
    lastInjectNonce.current = inject.nonce
    const t = (inject.text || '').trim()
    if (!t) return
    setText((prev) => {
      const next = prev.trim() ? `${prev.trimEnd()} ${t}` : t
      onTextChange?.(next)
      return next
    })
    areaRef.current?.focus()
  }, [inject, onTextChange])

  const inputLocked = disabled || disabledTurn

  const submit = () => {
    const t = text.trim()
    if (!t || inputLocked) return
    onSend(tab, t, { webSearch: webOn })
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
      const msg = 'O microfone precisa de HTTPS. Abra o jogo com https://, não com http://…:2122.'
      setMicHint(msg)
      toast(msg)
      return
    }
    if (!navigator.mediaDevices?.getUserMedia || typeof MediaRecorder === 'undefined') {
      const msg = 'Este navegador não grava áudio.'
      setMicHint(msg)
      toast(msg)
      return
    }
    let stream
    try {
      stream = await navigator.mediaDevices.getUserMedia({ audio: true })
    } catch (err) {
      const msg = err?.name === 'NotFoundError'
        ? 'Nenhum microfone encontrado neste aparelho.'
        : 'Acesso ao microfone negado — permita o microfone neste site e tente de novo.'
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
        const msg = err?.message || 'Não foi possível iniciar a gravação.'
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
        const msg = 'A gravação ficou vazia. Fale e toque no microfone de novo para parar.'
        setMicHint(msg)
        toast(msg)
        return
      }
      setMicHint('Transcrevendo…')
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
          const msg = 'Nada foi reconhecido — fale mais perto do microfone.'
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
      const msg = err?.message || 'Não foi possível iniciar a gravação.'
      setMicHint(msg)
      toast(msg)
      return
    }
    setRecording(true)
    setMicHint('Gravando… toque no microfone de novo para transcrever.')
  }

  // auto-resize da textarea (2..10 linhas)
  useEffect(() => {
    const el = areaRef.current
    if (!el) return
    el.style.height = 'auto'
    el.style.height = `${Math.min(el.scrollHeight, 240)}px`
  }, [text])

  return (
    <div className="w-full border-t border-subtle bg-surface/95 px-4 pb-[max(0.75rem,env(safe-area-inset-bottom))] pt-2.5 backdrop-blur">
      <div className="mx-auto w-full max-w-4xl">
        <div className="mb-2.5 flex w-full items-center gap-1 rounded-full border border-subtle bg-elevated p-1 sm:w-auto sm:self-start">
          {normTabs.map((t) => (
            <button
              key={t.id}
              onClick={() => setTab(t.id)}
              disabled={inputLocked}
              title={t.placeholder || t.label}
              className={`min-h-9 flex-1 whitespace-nowrap rounded-full px-4 py-1.5 text-[11px] font-semibold uppercase tracking-[0.12em] transition-all sm:flex-none ${
                tab === t.id
                  ? 'bg-accent-pri text-canvas shadow-sm'
                  : 'text-muted hover:bg-surface hover:text-primary'
              } disabled:opacity-50`}
            >
              {t.label}
            </button>
          ))}
        </div>
        <div className="flex w-full flex-col gap-2 sm:flex-row sm:items-end">
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
            disabled={inputLocked}
            rows={2}
            placeholder={disabled ? 'A IA está escrevendo…' : active.placeholder}
            aria-label={active.label || 'Ação da história'}
            enterKeyHint="send"
            className="max-h-60 min-w-0 w-full flex-1 resize-none rounded-2xl border border-subtle bg-elevated px-4 py-3 font-prose text-[15px] leading-relaxed text-primary outline-none placeholder:text-muted focus:border-accent-pri disabled:opacity-60"
          />
          <div className="flex items-center gap-2 sm:contents">
          {panelAvailable && (
            <button
              onClick={onTogglePanel}
              disabled={inputLocked}
              title={panelOpen ? 'Esconder controles' : 'Mostrar controles (dados, atributos, inventário…)'}
              aria-label="Alternar controles do jogo"
              aria-pressed={panelOpen}
              className={`h-11 w-11 shrink-0 rounded-full border font-semibold transition-colors disabled:cursor-not-allowed disabled:opacity-40 ${
                panelOpen
                  ? 'border-accent-pri bg-accent-pri text-canvas hover:bg-accent-hov'
                  : 'border-subtle bg-elevated text-secondary hover:text-primary'
              }`}
            >
              🎲
            </button>
          )}
          <button
            onClick={recording ? stopRecording : startRecording}
            disabled={inputLocked || transcribing}
            title={
              transcribing
                ? 'Transcrevendo…'
                : recording
                  ? 'Parar gravação'
                  : 'Ditar (fala para texto)'
            }
            aria-label={recording ? 'Parar gravação' : 'Ditar a ação com o microfone'}
            className={`h-11 w-11 shrink-0 rounded-full border font-semibold text-primary transition disabled:cursor-not-allowed disabled:opacity-40 ${
              recording
                ? 'animate-pulse border-danger-500 bg-danger-600 text-white hover:bg-danger-500'
                : 'border-subtle bg-elevated hover:bg-surface'
            }`}
          >
            {transcribing ? '⏳' : recording ? '⏹' : '🎙'}
          </button>
          <button
            type="button"
            onClick={() => setWebOn((v) => !v)}
            disabled={inputLocked}
            title="Buscar na web para ancorar a cena (Wikipedia/DuckDuckGo)"
            aria-label="Buscar na web"
            aria-pressed={webOn}
            className={`h-11 w-11 shrink-0 rounded-full border font-semibold transition-colors disabled:cursor-not-allowed disabled:opacity-40 ${
              webOn
                ? 'border-accent-pri bg-accent-pri text-canvas hover:bg-accent-hov'
                : 'border-subtle bg-elevated text-secondary hover:text-primary'
            }`}
          >
            🌐
          </button>
          <button
            onClick={submit}
            disabled={inputLocked || !text.trim()}
            title={disabledTurn ? 'Aguarde a sua vez' : undefined}
            className="h-11 flex-1 rounded-full bg-accent-pri px-5 text-sm font-semibold text-canvas shadow-sm hover:bg-accent-hov disabled:cursor-not-allowed disabled:opacity-40 sm:flex-none"
          >
            Enviar
          </button>
          </div>
        </div>
        {micHint && (
          <p className="mt-1.5 text-xs text-warn-300/90">{micHint}</p>
        )}
      </div>
    </div>
  )
}
