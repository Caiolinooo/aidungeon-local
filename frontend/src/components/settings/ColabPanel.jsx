// Colab ligado pelo próprio jogo, pela API do Google: conta, escolha da GPU,
// ligar, acompanhar as células e desligar.
import { useEffect, useState } from 'react'
import { Power } from 'lucide-react'
import { api } from '../../api.js'
import { askToNotify, onColabStatus, watchColab } from '../../colabWatch.js'
import { useToast } from '../Toast.jsx'
import { SettingField } from './Controls.jsx'

const TIER = {
  SUBSCRIPTION_TIER_FREE: 'gratuito',
  SUBSCRIPTION_TIER_PRO: 'Pro',
  SUBSCRIPTION_TIER_PRO_PLUS: 'Pro+',
}

const CELL = {
  variables: 'preparando',
  secrets: 'preparando',
  helpers: 'preparando',
  hardware: 'conferindo a GPU',
  provision: 'instalando o ComfyUI e baixando os modelos',
  health: 'esperando os serviços subirem',
  tunnel: 'abrindo o túnel',
  register: 'registrando no jogo',
  keepalive: 'ligado',
}

const RATING = {
  excelente: { label: 'Excelente', cls: 'text-emerald-400' },
  bom: { label: 'Bom', cls: 'text-emerald-400' },
  ruim: { label: 'Ruim', cls: 'text-warn-300' },
  pessimo: { label: 'Péssimo', cls: 'text-danger-300' },
}

const KIND = { llm: 'Narrador', image: 'Imagem', video: 'Vídeo' }

function specLabel(spec, estimate) {
  const memory = spec.shape === 'SHAPE_HIGHMEM' ? ' · mais memória RAM' : ''
  const vram = estimate?.vram_gb ? ` · ${estimate.vram_gb} GB` : ''
  const rating = RATING[estimate?.rating]?.label
  return `${spec.accelerator}${vram}${memory}${rating ? ` · ${rating}` : ''}`
}

function Estimate({ estimate }) {
  if (!estimate) return null
  if (!estimate.items?.length) {
    return estimate.note ? <p className="text-[11px] text-muted">{estimate.note}</p> : null
  }
  return (
    <div className="space-y-1 rounded-lg border border-subtle bg-surface p-2.5">
      <p className="text-[11px] text-secondary">
        Estimativa para a {estimate.accelerator} ({estimate.vram_gb} GB{estimate.vram_measured ? ', medidos' : ', de tabela'}):{' '}
        os modelos escolhidos pedem cerca de {estimate.total_gb} GB.{' '}
        <strong className={estimate.fits ? 'text-emerald-400' : 'text-danger-300'}>
          {estimate.fits ? 'Cabe.' : 'Não cabe nesta placa.'}
        </strong>
      </p>
      <ul className="space-y-0.5">
        {estimate.items.map((item) => (
          <li key={item.kind} className="flex flex-wrap items-baseline gap-x-2 text-[11px] text-secondary">
            <span className="w-16 font-semibold text-primary">{KIND[item.kind]}</span>
            {item.need_gb ? <span>{item.need_gb} GB</span> : null}
            {item.speed ? <span>{item.speed}</span> : null}
            {item.rating ? <strong className={RATING[item.rating].cls}>{RATING[item.rating].label}</strong> : null}
            {item.note ? <span className="text-muted">{item.note}</span> : null}
          </li>
        ))}
      </ul>
      <p className="text-[10px] text-muted">São contas aproximadas, não medições. A memória da placa passa a ser a real depois da primeira vez que ela liga.</p>
    </div>
  )
}

function specKey(spec) {
  return `${spec.variant}|${spec.accelerator}|${spec.shape}`
}

export default function ColabPanel({ disabledReason, inputCls, labelCls, onChanged, refreshKey }) {
  const toast = useToast()
  const [colab, setColab] = useState(null)
  const [client, setClient] = useState({ client_id: '', client_secret: '' })
  const [pasted, setPasted] = useState('')
  const [picked, setPicked] = useState('')
  const [busy, setBusy] = useState(false)

  // refreshKey muda quando o que está salvo muda (modelos, onde roda, contexto):
  // as estimativas por GPU são calculadas no servidor a partir disso.
  useEffect(() => {
    api.getColab().then(setColab).catch(() => setColab(null))
  }, [refreshKey])

  const session = colab?.session || { state: 'off' }
  const working = session.state === 'creating' || session.state === 'starting'
  // O acompanhamento mora fora deste componente: continua avisando se o admin
  // sair desta tela para jogar enquanto o Colab sobe.
  useEffect(() => onColabStatus((next) => {
    setColab((prev) => (prev ? { ...prev, session: next } : prev))
    if (next.state === 'on') onChanged?.()
  }), [onChanged])
  useEffect(() => {
    if (working) watchColab(session, toast)
  }, [working, session, toast])

  const act = async (work, okMessage) => {
    setBusy(true)
    try {
      const result = await work()
      if (result) setColab(result)
      if (okMessage) toast(okMessage, 'ok')
    } catch (e) {
      toast(e.message)
    } finally {
      setBusy(false)
    }
  }

  if (!colab) return null

  const setTopic = (payload, okMessage) => act(async () => {
    const res = await api.putColabNotify(payload)
    return { ...colab, notify_topic: res.notify_topic }
  }, okMessage)
  const phone = (
    <div className="space-y-1.5 rounded-lg border border-subtle bg-surface p-2.5 text-[11px] text-secondary">
      <p className="font-semibold text-primary">Avisos no celular</p>
      {colab.notify_topic ? (
        <>
          <p>
            Ligados. No iPhone ou Android, instale o app <strong>ntfy</strong>, toque em + e assine o tópico{' '}
            <code className="select-all break-all rounded bg-elevated px-1 py-0.5 font-mono text-primary">{colab.notify_topic}</code>.
            O servidor avisa quando o Colab entrega a GPU, quando fica pronto e se falhar, mesmo com o jogo fechado.
          </p>
          <p className="text-muted">Quem souber o tópico lê os avisos; eles só falam do Colab, nunca da aventura.</p>
          <div className="flex flex-wrap gap-2">
            <button type="button" disabled={busy} onClick={() => act(async () => { await api.testColabNotify(); return null }, 'Aviso de teste enviado.')} className="min-h-9 rounded-lg border border-subtle px-3 font-semibold text-primary disabled:opacity-50">
              Enviar aviso de teste
            </button>
            <button type="button" disabled={busy} onClick={() => setTopic({ generate: true }, 'Tópico novo. Assine-o no app.')} className="min-h-9 rounded-lg border border-subtle px-3 font-semibold text-secondary disabled:opacity-50">
              Trocar o tópico
            </button>
            <button type="button" disabled={busy} onClick={() => setTopic({ topic: '' }, 'Avisos no celular desligados.')} className="min-h-9 rounded-lg border border-subtle px-3 font-semibold text-danger-300 disabled:opacity-50">
              Desligar
            </button>
          </div>
        </>
      ) : (
        <>
          <p>Para ser avisado no telefone com o jogo fechado (o aviso do navegador só chega com a página aberta), o servidor usa o serviço ntfy.sh.</p>
          <button type="button" disabled={busy} onClick={() => setTopic({ generate: true }, 'Avisos ligados. Assine o tópico no app ntfy.')} className="min-h-9 rounded-lg border border-subtle px-3 font-semibold text-primary disabled:opacity-50">
            Ligar avisos no celular
          </button>
        </>
      )}
    </div>
  )

  const gpus = (colab.specs || []).filter((spec) => spec.variant === 'VARIANT_GPU' && spec.eligible)
  const chosen = gpus.find((spec) => specKey(spec) === picked) || gpus[0]

  if (!colab.connected) {
    return (
      <div className="space-y-3 rounded-xl border border-subtle bg-elevated/40 p-3 text-xs text-secondary">
        <p className="font-semibold text-primary">Ligar o Colab por aqui</p>
        <p>
          Conecte uma vez a sua conta Google e o jogo passa a ligar e desligar o Colab sozinho. Precisa de um cliente OAuth do tipo "App para computador" num projeto do Google Cloud com a API do Colab ativada.
        </p>
        {!colab.client_saved && (
          <div className="grid grid-cols-1 gap-2 sm:grid-cols-[1fr_1fr_auto]">
            <SettingField label="Client ID" labelClassName={labelCls}>
              <input value={client.client_id} onChange={(e) => setClient({ ...client, client_id: e.target.value })} className={inputCls} autoComplete="off" />
            </SettingField>
            <SettingField label="Client secret" labelClassName={labelCls}>
              <input type="password" value={client.client_secret} onChange={(e) => setClient({ ...client, client_secret: e.target.value })} className={inputCls} autoComplete="off" />
            </SettingField>
            <button
              type="button"
              disabled={busy || !client.client_id.trim() || !client.client_secret.trim()}
              onClick={() => act(() => api.putColabClient({ client_id: client.client_id.trim(), client_secret: client.client_secret.trim() }), 'Cliente salvo.')}
              className="min-h-10 self-end rounded-xl border border-subtle bg-surface px-3 text-xs font-semibold text-primary disabled:opacity-50"
            >
              Salvar
            </button>
          </div>
        )}
        {colab.client_saved && (
          <>
            <ol className="list-decimal space-y-1 pl-4">
              <li>Abra a autorização e aceite com a conta que você usa no Colab.</li>
              <li>No fim o navegador vai para um endereço que começa com {colab.redirect} e mostra erro de conexão. É esperado.</li>
              <li>Copie esse endereço inteiro da barra do navegador e cole abaixo.</li>
            </ol>
            <div className="flex flex-wrap gap-2">
              <button
                type="button"
                disabled={busy}
                onClick={() => act(async () => { window.open((await api.getColabAuthUrl()).url, '_blank', 'noopener'); return null })}
                className="min-h-10 rounded-xl bg-accent-pri px-4 py-2 text-xs font-semibold text-canvas hover:bg-accent-hov disabled:opacity-50"
              >
                Abrir a autorização do Google
              </button>
              <button type="button" disabled={busy} onClick={() => act(() => api.putColabClient({ client_id: '', client_secret: '' }))} className="min-h-10 rounded-xl border border-subtle px-3 text-xs font-semibold text-secondary">
                Trocar o cliente
              </button>
            </div>
            <div className="flex gap-2">
              <input value={pasted} onChange={(e) => setPasted(e.target.value)} placeholder={`${colab.redirect}/?state=…&code=…`} className={inputCls} aria-label="Endereço em que o navegador parou" />
              <button
                type="button"
                disabled={busy || pasted.trim().length < 4}
                onClick={() => act(async () => { const next = await api.finishColabAuth(pasted.trim()); setPasted(''); return next }, 'Conta Google conectada.')}
                className="min-h-10 shrink-0 rounded-xl border border-subtle bg-surface px-3 text-xs font-semibold text-primary disabled:opacity-50"
              >
                Concluir
              </button>
            </div>
          </>
        )}
        {phone}
      </div>
    )
  }

  const others = (colab.runtimes || []).filter((item) => item.name && item.name !== session.runtime)
  return (
    <div className="space-y-3 rounded-xl border border-subtle bg-elevated/40 p-3 text-xs text-secondary">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <p className="font-semibold text-primary">
          Ligar o Colab por aqui{colab.tier ? ` · plano ${TIER[colab.tier] || colab.tier}` : ''}
        </p>
        <button type="button" disabled={busy} onClick={() => act(() => api.disconnectColab(), 'Conta Google desconectada.')} className="text-[11px] text-danger-300 underline disabled:opacity-50">
          Desconectar a conta Google
        </button>
      </div>
      {colab.error && <p role="alert" className="text-danger-300">{colab.error}</p>}

      {session.state === 'off' || session.state === 'error' ? (
        <div className="flex flex-wrap items-end gap-2">
          <SettingField label="GPU" labelClassName={labelCls}>
            <select value={chosen ? specKey(chosen) : ''} onChange={(e) => setPicked(e.target.value)} className={inputCls} disabled={gpus.length === 0}>
              {gpus.length === 0 && <option value="">Nenhuma GPU liberada para esta conta</option>}
              {gpus.map((spec) => <option key={specKey(spec)} value={specKey(spec)}>{specLabel(spec, colab.estimates?.[spec.accelerator])}</option>)}
            </select>
          </SettingField>
          <button
            type="button"
            disabled={busy || !chosen || Boolean(disabledReason)}
            onClick={() => act(async () => {
              askToNotify()
              const next = await api.startColab({ variant: chosen.variant, accelerator: chosen.accelerator, shape: chosen.shape })
              return { ...colab, session: next }
            })}
            className="flex min-h-11 items-center gap-1.5 rounded-xl bg-accent-pri px-4 py-2 text-xs font-semibold text-canvas shadow hover:bg-accent-hov disabled:opacity-50"
          >
            <Power className="h-3.5 w-3.5" />
            Ligar o Colab
          </button>
          {disabledReason && <p className="min-w-0 flex-1 text-[11px]">{disabledReason}</p>}
          <div className="basis-full">
            <Estimate estimate={chosen ? colab.estimates?.[chosen.accelerator] : null} />
          </div>
        </div>
      ) : (
        <div className="flex flex-wrap items-center gap-3">
          <p role="status" className="min-w-0 flex-1 text-primary">
            {session.state === 'creating' && (session.attempt ? `O Colab está sem essa GPU livre. O jogo segue pedindo (tentativa ${session.attempt}) e avisa quando conseguir.` : 'Pedindo a máquina ao Colab…')}
            {session.state === 'starting' && (session.resumed ? 'O Colab está subindo (o jogo reiniciou e retomou esta sessão)…' : `Passo ${session.step || 1} de ${session.steps || '?'}: ${CELL[session.cell] || 'preparando'}…`)}
            {session.state === 'on' && `Colab ligado${session.spec?.accelerator ? ` (${session.spec.accelerator})` : ''}.`}
          </p>
          <button
            type="button"
            disabled={busy}
            onClick={() => act(async () => { await api.stopColab(); onChanged?.(); return api.getColab() }, 'Colab desligado.')}
            className="min-h-10 rounded-xl border border-danger-800 px-4 py-1.5 text-xs font-semibold text-danger-300 disabled:opacity-50"
          >
            Desligar o Colab
          </button>
        </div>
      )}
      {session.state === 'error' && <p role="alert" className="text-danger-300">Não ligou: {session.error}</p>}
      {Array.isArray(session.log) && session.log.length > 0 && (
        <details open={working || session.state === 'error'}>
          <summary className="cursor-pointer text-[11px] font-semibold text-muted">O que o Colab está dizendo</summary>
          <pre className="mt-1 max-h-48 overflow-auto whitespace-pre-wrap rounded-lg bg-surface p-2 font-mono text-[11px] text-secondary">{session.log.slice(-20).join('\n')}</pre>
        </details>
      )}
      {others.length > 0 && (
        <div className="space-y-1">
          <p className="text-[11px] text-muted">Sessões ligadas na sua conta que este jogo não está acompanhando (consomem créditos):</p>
          {others.map((item) => (
            <div key={item.name} className="flex items-center justify-between gap-2 rounded-lg border border-subtle bg-surface px-2.5 py-1.5">
              <span className="truncate font-mono text-[11px] text-primary">{item.accelerator || 'CPU'} · {item.name}</span>
              <button type="button" disabled={busy} onClick={() => act(async () => { await api.stopColab(item.name); return api.getColab() }, 'Sessão desligada.')} className="shrink-0 text-[11px] font-semibold text-danger-300 disabled:opacity-50">
                Desligar
              </button>
            </div>
          ))}
        </div>
      )}
      {phone}
      <p className="text-[11px] text-muted">
        Desligue quando terminar: sessão ligada gasta créditos do Colab. Sem batida por 3 minutos, o jogo volta a usar esta máquina sozinho.
      </p>
    </div>
  )
}
