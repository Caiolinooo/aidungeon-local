// Voz no mesmo esquema do narrador e da imagem: onde roda e com quais
// modelos. Um servidor só faz fala, clonagem e escuta.
import { useEffect, useState } from 'react'
import { api } from '../../api.js'
import { useToast } from '../Toast.jsx'
import { SettingField } from './Controls.jsx'

const PLACES = [
  { id: 'server', label: 'Servidor configurado' },
  { id: 'local', label: 'Esta máquina' },
  { id: 'colab', label: 'Colab' },
]

const PLACE_NAME = { server: 'no servidor configurado', local: 'nesta máquina' }

const RATING_LABEL = { excelente: 'Excelente', bom: 'Bom', ruim: 'Ruim', pessimo: 'Péssimo' }
const RATING_CLS = {
  excelente: 'text-emerald-400', bom: 'text-emerald-400', ruim: 'text-warn-300', pessimo: 'text-danger-300',
}

function EstimateLine({ estimate, kind }) {
  const item = estimate?.items?.find((row) => row.kind === kind)
  if (!item?.rating) return null
  return (
    <p className="text-[11px] text-secondary">
      Na {estimate.accelerator} do Colab (estimativa): {item.need_gb ? `${item.need_gb} GB · ` : ''}{item.speed}{' · '}
      <strong className={RATING_CLS[item.rating]}>{RATING_LABEL[item.rating]}</strong>
    </p>
  )
}

function gb(value) {
  return String(value).replace('.', ',')
}

function installLine(install) {
  if (!install?.file_bytes) return install?.step || ''
  const done = Math.round(((install.file_bytes_done || 0) / install.file_bytes) * 100)
  return `${install.step} (${Math.min(done, 100)}%)`
}

export default function VoiceRuntime({ estimate, onSelection, inputCls, labelCls }) {
  const toast = useToast()
  const [data, setData] = useState(null)
  const [busy, setBusy] = useState(false)

  useEffect(() => {
    api.getVoiceRuntime().then(setData).catch(() => setData(null))
  }, [])

  const placement = data?.placement
  const tts = data?.tts
  const stt = data?.stt
  useEffect(() => {
    if (placement) onSelection?.({ placement, tts, stt })
  }, [placement, tts, stt, onSelection])

  const installing = data?.install?.state === 'running'
  useEffect(() => {
    if (!installing) return undefined
    const timer = setInterval(() => {
      api.getVoiceRuntime().then(setData).catch(() => {})
    }, 2000)
    return () => clearInterval(timer)
  }, [installing])

  if (!data) return null

  const save = async (payload) => {
    setBusy(true)
    try {
      setData(await api.putVoiceRuntime(payload))
    } catch (e) {
      toast('Não consegui mudar a voz: ' + e.message)
    } finally {
      setBusy(false)
    }
  }

  const install = async (activate) => {
    setBusy(true)
    try {
      setData(await api.installVoiceRuntime(activate))
    } catch (e) {
      toast('Não consegui instalar a voz: ' + e.message)
    } finally {
      setBusy(false)
    }
  }

  const local = data.local || {}
  const localMatches = local.running && local.tts === tts && local.stt === stt
  const download = (data.tts_models[tts]?.gb || 0) + (data.stt_models[stt]?.gb || 0)
  const colabModels = data.colab?.models
  const colabStale = data.colab?.serving && colabModels
    && (colabModels.tts !== tts || colabModels.stt !== stt)

  return (
    <section className="space-y-3 rounded-2xl border border-subtle bg-surface p-4">
      <div>
        <h3 className="text-xs font-semibold uppercase tracking-wider text-muted">Voz</h3>
        <p className="mt-0.5 text-[11px] text-secondary">
          Fala, clonagem de voz e escuta. Um servidor só faz as três.
        </p>
      </div>

      <div className="flex flex-wrap items-center justify-between gap-2">
        <span className="text-xs font-semibold text-primary">Onde roda</span>
        <div role="radiogroup" aria-label="Onde roda: Voz" className="flex flex-wrap gap-1.5">
          {PLACES.map((place) => {
            const active = placement === place.id
            return (
              <button
                key={place.id}
                type="button"
                role="radio"
                aria-checked={active}
                disabled={busy || installing}
                onClick={() => !active && save({ placement: place.id })}
                className={`min-h-10 rounded-xl border px-4 py-1.5 text-xs font-semibold transition disabled:opacity-50 ${active ? 'border-accent-pri bg-elevated text-primary' : 'border-subtle text-secondary hover:text-primary'}`}
              >
                {place.label}
              </button>
            )
          })}
        </div>
      </div>

      <p role="status" className="text-[11px] text-secondary">
        {placement === 'server' && (
          <>Usa o servidor da aba Voz{data.server_url ? ` (${data.server_url})` : ''}. Os modelos abaixo só valem nesta máquina e no Colab.</>
        )}
        {placement === 'local' && local.running && (
          <>Ligado nesta máquina: {data.tts_models[local.tts]?.label || 'outro modelo'} e {data.stt_models[local.stt]?.label || 'outro Whisper'}.{local.stt_device === 'cpu' ? ' O Whisper não coube na placa e está na CPU, mais lento.' : ''}</>
        )}
        {placement === 'local' && !local.running && 'O servidor de voz desta máquina não está respondendo.'}
        {placement === 'colab' && data.colab?.serving && 'O Colab está atendendo a voz.'}
        {placement === 'colab' && !data.colab?.serving && (
          <>Colab não conectado: a voz segue {PLACE_NAME[data.fallback]} até ele se registrar, e volta para lá se ele cair.</>
        )}
      </p>

      <div className="flex flex-wrap items-center gap-2">
        <span className="text-xs font-semibold text-primary">Pacote pronto</span>
        {Object.entries(data.bundles).map(([id, bundle]) => (
          <button
            key={id}
            type="button"
            disabled={busy || installing}
            aria-pressed={data.bundle === id}
            title={bundle.note}
            onClick={() => save({ bundle: id })}
            className={`min-h-10 rounded-xl border px-3 py-1.5 text-xs font-semibold transition disabled:opacity-50 ${data.bundle === id ? 'border-accent-pri bg-elevated text-primary' : 'border-subtle text-secondary hover:text-primary'}`}
          >
            {bundle.label}
          </button>
        ))}
        {!data.bundle && <span className="text-[11px] text-muted">Escolha própria</span>}
      </div>

      <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
        <div className="space-y-1">
          <SettingField label="Fala e clonagem (TTS)" labelClassName={labelCls}>
            <select value={tts} disabled={busy || installing} onChange={(e) => save({ tts: e.target.value })} className={inputCls}>
              {Object.entries(data.tts_models).map(([id, model]) => (
                <option key={id} value={id}>{model.label} · {gb(model.gb)} GB</option>
              ))}
            </select>
          </SettingField>
          <p className="text-[11px] text-secondary">{data.tts_models[tts]?.note}</p>
          {placement === 'colab' && <EstimateLine estimate={estimate} kind="tts" />}
        </div>
        <div className="space-y-1">
          <SettingField label="Escuta (STT)" labelClassName={labelCls}>
            <select value={stt} disabled={busy || installing} onChange={(e) => save({ stt: e.target.value })} className={inputCls}>
              {Object.entries(data.stt_models).map(([id, model]) => (
                <option key={id} value={id}>{model.label} · {gb(model.gb)} GB</option>
              ))}
            </select>
          </SettingField>
          <p className="text-[11px] text-secondary">{data.stt_models[stt]?.note}</p>
          {placement === 'colab' && <EstimateLine estimate={estimate} kind="stt" />}
        </div>
      </div>

      {colabStale && (
        <p className="text-[11px] text-warn-300">O Colab ligado ainda usa os modelos de antes. Os novos valem na próxima vez que ele ligar.</p>
      )}

      {installing && (
        <p role="status" className="text-[11px] text-secondary">Instalando nesta máquina: {installLine(data.install)}…</p>
      )}
      {data.install?.state === 'error' && (
        <p role="alert" className="text-[11px] text-danger-300">A instalação falhou: {data.install.error}</p>
      )}
      {!installing && !localMatches && local.vram_free_gb != null && (
        <p className={`text-[11px] ${data.need_gb > local.vram_free_gb ? 'text-warn-300' : 'text-secondary'}`}>
          Estes modelos pedem cerca de {gb(data.need_gb)} GB da placa. Esta máquina tem {gb(local.vram_free_gb)} GB livres agora{data.need_gb > local.vram_free_gb ? ': não cabem enquanto o narrador e a imagem ocuparem a placa.' : '.'}
        </p>
      )}
      {!installing && !localMatches && (
        <div className="flex flex-wrap items-center gap-3">
          <button
            type="button"
            disabled={busy}
            onClick={() => install(!local.running)}
            className="min-h-10 rounded-xl border border-subtle bg-elevated px-4 py-2 text-xs font-semibold text-primary disabled:opacity-50"
          >
            {local.running ? 'Aplicar estes modelos nesta máquina' : 'Instalar e usar nesta máquina'}
          </button>
          <p className="min-w-0 flex-1 text-[11px] text-secondary">
            {local.running
              ? 'Baixa o que faltar e reinicia o servidor de voz desta máquina com eles.'
              : `Baixa cerca de ${gb(download.toFixed(1))} GB de modelos, compila o OmniVoice e liga o servidor. Leva alguns minutos.`}
          </p>
        </div>
      )}
    </section>
  )
}
