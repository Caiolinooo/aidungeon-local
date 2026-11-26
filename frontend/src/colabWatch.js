// Acompanha a subida do Colab fora da tela de configurações: quem liga e vai
// jogar é avisado quando a GPU chega, quando fica pronta e se a subida falha.
import { api } from './api.js'

let timer = null
let last = null
let toast = null
const listeners = new Set()

const WORKING = new Set(['creating', 'starting'])

function tell(message, kind) {
  toast?.(message, kind)
  try {
    if (typeof Notification !== 'undefined' && Notification.permission === 'granted') {
      new Notification('AI Dungeon · Colab', { body: message })
    }
  } catch {
    // navegador sem notificação: o aviso na tela já foi
  }
}

// O que avisar quando o estado muda. Exportado para o teste.
export function messageFor(before, now) {
  const gpu = now?.spec?.accelerator ? ` ${now.spec.accelerator}` : ''
  if (now?.state === 'starting' && before === 'creating') {
    return [`O Colab entregou a GPU${gpu}. Instalando e baixando os modelos.`, 'ok']
  }
  if (now?.state === 'on' && before !== 'on') {
    return [`Colab ligado e pronto${gpu ? ` (${gpu.trim()})` : ''}. O jogo já usa essa GPU.`, 'ok']
  }
  if (now?.state === 'error' && before !== 'error') {
    return [`O Colab não ligou: ${now.error || 'erro desconhecido'}`, 'error']
  }
  return null
}

export function askToNotify() {
  try {
    if (typeof Notification !== 'undefined' && Notification.permission === 'default') {
      Notification.requestPermission()
    }
  } catch {
    // sem suporte: fica só o aviso na tela
  }
}

export function onColabStatus(listener) {
  listeners.add(listener)
  return () => listeners.delete(listener)
}

export function watchColab(session, toastFn) {
  toast = toastFn || toast
  if (session) last = session.state
  if (timer || !WORKING.has(last)) return
  timer = setInterval(async () => {
    let now
    try {
      now = await api.getColabStatus()
    } catch {
      return // a próxima conferência tenta de novo
    }
    const message = messageFor(last, now)
    last = now.state
    for (const listener of listeners) listener(now)
    if (message) tell(...message)
    if (!WORKING.has(now.state)) {
      clearInterval(timer)
      timer = null
    }
  }, 4000)
}
