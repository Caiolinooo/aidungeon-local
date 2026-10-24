import { cloneElement, useId, useState } from 'react'
import { api } from '../../api.js'

export function SettingField({ label, children, className = '', labelClassName = '' }) {
  const id = useId()
  return (
    <div className={className}>
      <label htmlFor={id} className={labelClassName}>{label}</label>
      {cloneElement(children, { id })}
    </div>
  )
}

const HEALTH_STATUS = {
  ok: { label: 'Online', style: 'border-accent-800 bg-accent-950/60 text-accent-pri' },
  unreachable: { label: 'Inacessível', style: 'border-danger-800 bg-danger-950/60 text-danger-300' },
  auth_error: { label: 'Falha de autenticação', style: 'border-warn-800 bg-warn-950/60 text-warn-300' },
  error: { label: 'Erro no serviço', style: 'border-danger-800 bg-danger-950/60 text-danger-300' },
  disabled: { label: 'Desativado', style: 'border-subtle bg-elevated text-muted' },
  config_error: { label: 'Configuração inválida', style: 'border-warn-800 bg-warn-950/60 text-warn-300' },
}

// O backend já sanitiza as sondas; esta camada também limita o texto e evita
// ecoar URLs ou credenciais caso um provedor/dev proxy devolva detalhes extras.
function safeHealthText(value) {
  if (typeof value !== 'string') return ''
  return value
    .replace(/[\u0000-\u001f\u007f]/g, ' ')
    .replace(/\b(?:bearer|basic)\s+[a-z0-9._~+/=-]+/gi, '[credencial]')
    .replace(/\bsk-[a-z0-9_-]+\b/gi, '[credencial]')
    .replace(/\b[a-z][a-z0-9+.-]*:\/\/[^\s]+/gi, '[endpoint]')
    .trim()
    .slice(0, 240)
}

export function HealthCheck({ service, compact = false }) {
  const [checking, setChecking] = useState(false)
  const [result, setResult] = useState(null)
  const isVoice = service === 'voice'
  const serviceName = isVoice ? 'voz' : 'imagem'

  const check = async () => {
    setChecking(true)
    try {
      const health = await (isVoice ? api.healthVoice() : api.healthImage())
      setResult({ ...health, failed: false })
    } catch {
      setResult({ failed: true })
    } finally {
      setChecking(false)
    }
  }

  const status = HEALTH_STATUS[result?.status] ?? HEALTH_STATUS.error
  const detail = safeHealthText(result?.details ?? result?.detail)

  return (
    <div className="flex flex-wrap items-center gap-2">
      <button
        type="button"
        onClick={check}
        disabled={checking}
        className={`rounded-lg border border-subtle bg-elevated font-semibold text-secondary transition hover:text-primary disabled:opacity-50 ${compact ? 'px-2.5 py-1.5 text-xs' : 'px-3 py-1.5 text-xs'}`}
      >
        {checking ? 'Verificando…' : `Verificar ${serviceName}`}
      </button>
      {result && (
        <div aria-live="polite" className="min-w-0 text-xs">
          {result.failed ? (
            <span className={`inline-block rounded-full border px-2.5 py-1 ${HEALTH_STATUS.error.style}`}>
              Não foi possível verificar o serviço.
            </span>
          ) : (
            <>
              <span className={`inline-block rounded-full border px-2.5 py-1 font-semibold ${status.style}`}>
                {status.label}
              </span>
              {detail && <span className="ml-2 break-words text-muted">{detail}</span>}
            </>
          )}
        </div>
      )}
    </div>
  )
}
