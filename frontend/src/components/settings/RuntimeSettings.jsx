import { Fragment, useEffect, useRef, useState } from 'react'
import { Download } from 'lucide-react'
import { api } from '../../api.js'
import { useToast } from '../Toast.jsx'
import { SettingField } from './Controls.jsx'
import ColabPanel from './ColabPanel.jsx'
import ModelPicker from './ModelPicker.jsx'

const PLACES = [
  { id: 'local', label: 'Esta máquina' },
  { id: 'colab', label: 'Colab' },
]

const SERVICES = [
  { key: 'llm', label: 'Narrador' },
  { key: 'comfyui', label: 'Imagem e vídeo' },
]

const KINDS = [
  { kind: 'llm', title: 'Narrador', service: 'llm' },
  { kind: 'image', title: 'Imagem', service: 'comfyui' },
  { kind: 'video', title: 'Vídeo', service: 'comfyui' },
]

const DEFAULT_FAMILIES = {
  llm: ['gguf'],
  image: ['checkpoint', 'flux2_klein', 'z_image_turbo', 'qwen', 'custom'],
  video: ['ltxv', 'wan', 'h3'],
}

const ENDPOINTS = [
  { key: 'local_llm', label: 'Narrador nesta máquina' },
  { key: 'local_comfyui', label: 'ComfyUI nesta máquina' },
  { key: 'colab_llm', label: 'Narrador no Colab (o notebook preenche)' },
  { key: 'colab_comfyui', label: 'ComfyUI no Colab (o notebook preenche)' },
]

const SENSITIVE_KEY = /token|secret|password|api[_-]?key|authorization|auth[_-]?key|credential/i

const HARDWARE_LABELS = {
  gpu: 'GPU',
  gpus: 'GPUs',
  gpu_name: 'GPU',
  name: 'Nome',
  device: 'Dispositivo',
  backend: 'Backend',
  cuda: 'CUDA',
  vram_mib: 'VRAM (MiB)',
  vram_total_mib: 'VRAM total (MiB)',
  vram_free_mib: 'VRAM livre (MiB)',
  ram_mib: 'RAM (MiB)',
  ram_total_mib: 'RAM total (MiB)',
  ram_available_mib: 'RAM disponível (MiB)',
  memory_total_mib: 'RAM total (MiB)',
  memory_available_mib: 'RAM disponível (MiB)',
}

function asString(value) {
  return typeof value === 'string' ? value : ''
}

function modelId(value) {
  if (typeof value === 'string') return value
  if (value && typeof value === 'object' && typeof value.id === 'string') return value.id
  return ''
}

function asPlace(value) {
  return value === 'colab' ? 'colab' : 'local'
}

function asCatalogMap(section) {
  if (!section) return {}
  if (Array.isArray(section)) {
    const out = {}
    for (const item of section) {
      if (typeof item === 'string' && item) {
        out[item] = {}
        continue
      }
      if (!item || typeof item !== 'object') continue
      const id = typeof item.id === 'string' ? item.id : typeof item.model_id === 'string' ? item.model_id : ''
      if (id) out[id] = item
    }
    return out
  }
  if (typeof section === 'object') {
    const out = {}
    for (const [key, value] of Object.entries(section)) {
      if (key === 'quality' || key === 'config' || key === 'artifacts' || key === 'schema') continue
      out[key] = value
    }
    return out
  }
  return {}
}

function isConfigBag(candidate) {
  return Boolean(
    candidate
    && typeof candidate === 'object'
    && !Array.isArray(candidate)
    && ('mode' in candidate || 'placements' in candidate || 'endpoints' in candidate),
  )
}

function configSource(data) {
  if (!data || typeof data !== 'object') return {}
  if (isConfigBag(data.runtime)) return data.runtime
  if (isConfigBag(data.config)) return data.config
  if (isConfigBag(data)) return data
  return data
}

export function normalizeRuntimeConfig(data) {
  const src = configSource(data)
  const placementsIn = src.placements && typeof src.placements === 'object' ? src.placements : {}
  const modelsIn = src.models && typeof src.models === 'object' && !Array.isArray(src.models) ? src.models : {}
  const endpointsIn = src.endpoints && typeof src.endpoints === 'object' ? src.endpoints : {}
  let llm = asPlace(placementsIn.llm)
  let comfyui = asPlace(placementsIn.comfyui)
  let mode = src.mode
  if (mode === 'local') {
    llm = 'local'
    comfyui = 'local'
  } else if (mode === 'colab') {
    llm = 'colab'
    comfyui = 'colab'
  } else if (mode === 'hybrid') {
    if (llm === comfyui) comfyui = llm === 'local' ? 'colab' : 'local'
  } else if (llm === 'local' && comfyui === 'local') {
    mode = 'local'
  } else if (llm === 'colab' && comfyui === 'colab') {
    mode = 'colab'
  } else {
    mode = 'hybrid'
  }
  return {
    mode,
    placements: { llm, comfyui },
    models: {
      llm: modelId(modelsIn.llm),
      image: modelId(modelsIn.image),
      video: modelId(modelsIn.video),
    },
    endpoints: {
      local_llm: asString(endpointsIn.local_llm),
      local_comfyui: asString(endpointsIn.local_comfyui),
      colab_llm: asString(endpointsIn.colab_llm),
      colab_comfyui: asString(endpointsIn.colab_comfyui),
    },
    project_url: asString(src.project_url),
  }
}

export function applyRuntimePlacement(form, key, place) {
  const base = normalizeRuntimeConfig(form)
  if (key !== 'llm' && key !== 'comfyui') return base
  const placements = { ...base.placements, [key]: asPlace(place) }
  let mode = 'hybrid'
  if (placements.llm === 'local' && placements.comfyui === 'local') mode = 'local'
  else if (placements.llm === 'colab' && placements.comfyui === 'colab') mode = 'colab'
  return { ...base, mode, placements }
}

export function runtimePayload(form) {
  const config = normalizeRuntimeConfig(form)
  const trim = (value) => value.trim()
  return {
    mode: config.mode,
    placements: { ...config.placements },
    models: {
      llm: config.models.llm.trim(),
      image: config.models.image.trim(),
      video: config.models.video.trim(),
    },
    endpoints: {
      local_llm: trim(config.endpoints.local_llm),
      local_comfyui: trim(config.endpoints.local_comfyui),
      colab_llm: trim(config.endpoints.colab_llm),
      colab_comfyui: trim(config.endpoints.colab_comfyui),
    },
    project_url: trim(config.project_url),
  }
}

export function isAllLocalRuntime(form) {
  const config = normalizeRuntimeConfig(form)
  return config.mode === 'local'
    || (config.placements.llm === 'local' && config.placements.comfyui === 'local')
}

function sideBag(data) {
  if (!data || typeof data !== 'object') return {}
  const nested = configSource(data)
  return nested === data ? data : nested
}

export function extractCatalog(data) {
  const bag = data && typeof data === 'object' ? data : {}
  const catalog = bag.catalog || sideBag(bag).catalog
  const models = catalog?.models && typeof catalog.models === 'object' && !Array.isArray(catalog.models)
    ? catalog.models
    : catalog
  return {
    llm: asCatalogMap(models?.llm),
    image: asCatalogMap(models?.image),
    video: asCatalogMap(models?.video),
  }
}

export function extractHardware(data) {
  if (!data || typeof data !== 'object') return null
  const nested = sideBag(data)
  const hardware = data.hardware ?? data.local_hardware ?? nested.hardware ?? nested.local_hardware
  if (!hardware || typeof hardware !== 'object' || Array.isArray(hardware)) return null
  return hardware
}

export function extractRegistration(data) {
  if (!data || typeof data !== 'object') return null
  const nested = sideBag(data)
  const registration = data.registration
    ?? data.last_registration
    ?? data.colab_registration
    ?? nested.registration
    ?? nested.last_registration
    ?? nested.colab_registration
  if (!registration || typeof registration !== 'object' || Array.isArray(registration)) return null
  return registration
}

export function configFromRuntimeResponse(sent, updated) {
  if (!updated || typeof updated !== 'object') return normalizeRuntimeConfig(sent)
  const src = configSource(updated)
  const hasConfig = ['mode', 'placements', 'models', 'endpoints', 'project_url'].some((key) => key in src)
  if (!hasConfig) return normalizeRuntimeConfig(sent)
  return normalizeRuntimeConfig(updated)
}

function redact(value) {
  return String(value)
    .replace(/[\u0000-\u001f\u007f]/g, ' ')
    .replace(/\b(?:bearer|basic)\s+\S+/gi, '[credencial]')
    .replace(/\bsk-[a-z0-9_-]+\b/gi, '[credencial]')
    .trim()
}

function formatValue(value) {
  if (typeof value === 'boolean') return value ? 'sim' : 'não'
  if (typeof value === 'number') return Number.isFinite(value) ? String(value) : ''
  if (typeof value === 'string') return redact(value).slice(0, 300)
  try {
    return redact(JSON.stringify(value)).slice(0, 400)
  } catch {
    return ''
  }
}

function hardwareRows(hardware) {
  return Object.entries(hardware)
    .filter(([key, value]) => value != null && typeof value !== 'function' && !SENSITIVE_KEY.test(key))
    .slice(0, 24)
    .map(([key, value]) => ({
      key,
      label: HARDWARE_LABELS[key] || key,
      value: formatValue(value),
    }))
    .filter((row) => row.value)
}

function formatWhen(value) {
  if (typeof value === 'number' && Number.isFinite(value) && value > 0) {
    const parsed = new Date(value < 1e12 ? value * 1000 : value)
    if (!Number.isNaN(parsed.getTime())) return parsed.toLocaleString('pt-BR')
  }
  if (typeof value !== 'string' || !value.trim()) return ''
  const parsed = new Date(value.trim())
  if (Number.isNaN(parsed.getTime())) return value.trim()
  return parsed.toLocaleString('pt-BR')
}

function publicEntries(value) {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return []
  return Object.entries(value)
    .filter(([key, item]) => item != null && item !== '' && typeof item !== 'function' && !SENSITIVE_KEY.test(key))
    .slice(0, 12)
    .map(([key, item]) => ({ key, value: formatValue(item) }))
    .filter((row) => row.value)
}

function registrationView(registration) {
  const when = formatWhen(registration.last_seen)
  return {
    when,
    models: publicEntries(registration.models),
    endpoints: publicEntries(registration.endpoints),
    hardware: publicEntries(registration.hardware),
    hasRecord: Boolean(when),
  }
}

export default function RuntimeSettings({
  form,
  catalog,
  hardware,
  registration,
  loaded,
  loadError,
  dirty,
  downloading,
  onChange,
  onDownload,
  onLibraryChange,
  onRefresh,
  imageProvider,
  onUseComfy,
  llmContext,
  llmContextMax,
  onLlmContext,
  inputCls,
  labelCls,
}) {
  const toast = useToast()
  const [library, setLibrary] = useState(null)
  const [keys, setKeys] = useState({ civitai: '', huggingface: '' })
  const [savingKeys, setSavingKeys] = useState(false)
  const refresh = useRef(onRefresh)
  refresh.current = onRefresh

  useEffect(() => {
    api.getModelLibrary().then(setLibrary).catch(() => setLibrary(null))
  }, [])

  // O Colab bate a cada 60 s; a tela confere a cada 20 s quem está atendendo.
  useEffect(() => {
    const timer = setInterval(() => refresh.current?.(), 20000)
    return () => clearInterval(timer)
  }, [])

  const installing = library?.install?.state === 'running'
  useEffect(() => {
    if (!installing) return undefined
    const timer = setInterval(async () => {
      try {
        const install = await api.getModelInstall()
        setLibrary((prev) => (prev ? { ...prev, install } : prev))
        if (install.state === 'done' && install.narrator !== undefined) {
          // O narrador desta máquina mudou: recarrega qual está em uso e o modelo salvo.
          setLibrary({ ...(await api.getModelLibrary()), install })
          await onLibraryChange?.('llm', install.model)
        }
      } catch {
        // a próxima batida tenta de novo
      }
    }, 2000)
    return () => clearInterval(timer)
  }, [installing])

  const allLocal = isAllLocalRuntime(form)
  const notebookDisabled = !loaded || allLocal || dirty || downloading
  let notebookHint = 'Abra o arquivo no Colab, escolha uma GPU e clique em Executar tudo. Ele baixa os modelos e se liga a este jogo sozinho.'
  if (allLocal) notebookHint = 'Ponha o narrador ou a imagem no Colab, acima, para gerar o notebook.'
  else if (dirty) notebookHint = 'Salve antes de gerar o notebook.'

  const hardwareUnavailable = hardware?.status === 'unavailable'
  const rows = hardware ? hardwareRows(hardware).filter((row) => row.key !== 'status') : []
  const reg = registration ? registrationView(registration) : null
  const gpu = registration?.hardware?.gpu_name || registration?.hardware?.gpu

  const pick = (kind, id) => onChange({ ...form, models: { ...form.models, [kind]: id } })

  const installHere = async (kind, id) => {
    try {
      const install = await api.installModel(kind, id)
      setLibrary((prev) => (prev ? { ...prev, install } : prev))
      if (install.state === 'done') toast('Os arquivos deste modelo já estão nesta máquina.', 'ok')
    } catch (e) {
      toast('Não consegui baixar: ' + e.message)
    }
  }

  const switchNarrator = async (id) => {
    try {
      const install = await api.useNarrator(id)
      setLibrary((prev) => (prev ? { ...prev, install } : prev))
    } catch (e) {
      toast('Não consegui trocar o narrador: ' + e.message)
    }
  }

  const saveKeys = async (payload) => {
    setSavingKeys(true)
    try {
      setLibrary(await api.putModelKeys(payload))
      setKeys({ civitai: '', huggingface: '' })
      toast('Chaves de download atualizadas.', 'ok')
    } catch (e) {
      toast('Não consegui salvar as chaves: ' + e.message)
    } finally {
      setSavingKeys(false)
    }
  }

  // SettingField aceita um único filho (ele recebe o id do rótulo): o botão fica fora.
  const keyField = (name, label, hint) => (
    <div>
      <SettingField label={label} labelClassName={labelCls}>
        <input
          type="password"
          autoComplete="off"
          value={keys[name]}
          onChange={(e) => setKeys({ ...keys, [name]: e.target.value })}
          placeholder={library?.keys?.[name] ? 'Salva. Digite outra para trocar.' : hint}
          className={inputCls}
        />
      </SettingField>
      {library?.keys?.[name] && (
        <button type="button" disabled={savingKeys} onClick={() => saveKeys({ [name]: '' })} className="mt-1 text-[11px] text-danger-300 underline disabled:opacity-50">
          Apagar a chave salva
        </button>
      )}
    </div>
  )

  return (
    <div className="space-y-6">
      <div className="border-b border-subtle pb-3">
        <h2 className="text-sm font-bold uppercase tracking-wider text-primary">
          Runtime e modelos
        </h2>
        <p className="text-xs text-secondary mt-0.5">
          Onde cada parte roda e qual modelo ela usa. As aventuras, o banco e as imagens ficam sempre neste servidor.
        </p>
      </div>

      {loadError && (
        <p role="alert" className="rounded-xl border border-danger-800 bg-danger-950/60 px-3 py-2 text-xs text-danger-300">
          Não foi possível carregar o runtime: {loadError}
        </p>
      )}

      {loaded && (
        <>
          <p role="status" className="rounded-2xl border border-subtle bg-surface px-4 py-3 text-xs text-secondary">
            {allLocal && 'Tudo roda nesta máquina.'}
            {!allLocal && registration?.online && (
              <>Colab conectado{gpu ? ` (${gpu})` : ''}. Última batida: {reg?.when || 'agora'}.</>
            )}
            {!allLocal && !registration?.online && (
              <>Colab não conectado. Esta máquina atende até o notebook se registrar, e volta a atender se ele cair.</>
            )}
          </p>

          {imageProvider && imageProvider !== 'comfyui' && (
            <div role="alert" className="flex flex-wrap items-center justify-between gap-3 rounded-2xl border border-danger-800 bg-danger-950/60 px-4 py-3 text-xs text-danger-300">
              <p className="min-w-0 flex-1">
                Hoje as imagens saem por outro provedor ({imageProvider}), escolhido na aba Geração de imagem. Os modelos de imagem e vídeo desta tela só valem com o ComfyUI.
              </p>
              <button type="button" onClick={onUseComfy} className="min-h-10 rounded-xl border border-danger-800 px-3 py-1.5 font-semibold">
                Usar o ComfyUI
              </button>
            </div>
          )}

          <section className="rounded-2xl border border-subtle bg-surface p-4 space-y-3">
            <h3 className="text-xs font-semibold uppercase tracking-wider text-muted">Onde roda</h3>
            {SERVICES.map((service) => (
              <div key={service.key} className="flex flex-wrap items-center justify-between gap-2">
                <span className="text-xs font-semibold text-primary">{service.label}</span>
                <div role="radiogroup" aria-label={`Onde roda: ${service.label}`} className="flex gap-1.5">
                  {PLACES.map((place) => {
                    const active = form.placements[service.key] === place.id
                    return (
                      <button
                        key={place.id}
                        type="button"
                        role="radio"
                        aria-checked={active}
                        onClick={() => onChange(applyRuntimePlacement(form, service.key, place.id))}
                        className={`min-h-10 rounded-xl border px-4 py-1.5 text-xs font-semibold transition ${active ? 'border-accent-pri bg-elevated text-primary' : 'border-subtle text-secondary hover:text-primary'}`}
                      >
                        {place.label}
                      </button>
                    )
                  })}
                </div>
              </div>
            ))}
          </section>

          {KINDS.map((item) => (
            <Fragment key={item.kind}>
            <ModelPicker
              kind={item.kind}
              title={item.title}
              value={form.models[item.kind]}
              catalog={catalog?.[item.kind]}
              families={library?.families?.[item.kind] || DEFAULT_FAMILIES[item.kind]}
              place={form.placements[item.service]}
              library={library}
              onPick={pick}
              onLibraryChange={onLibraryChange}
              onInstall={installHere}
              onNarrator={switchNarrator}
              inputCls={inputCls}
              labelCls={labelCls}
            />
            {item.kind === 'llm' && onLlmContext && (
              <section className="space-y-2 rounded-2xl border border-subtle bg-surface p-4">
                <h3 className="text-xs font-semibold uppercase tracking-wider text-muted">Parâmetros do narrador</h3>
                <SettingField label="Contexto que o jogo usa (tokens)" labelClassName={labelCls}>
                  <select value={llmContext} onChange={(e) => onLlmContext(Number(e.target.value))} className={inputCls}>
                    {[...new Set([16384, 32768, 65536, llmContextMax, llmContext])]
                      .filter((value) => value && value <= Math.max(llmContextMax, llmContext))
                      .sort((a, b) => a - b)
                      .map((value) => <option key={value} value={value}>{value.toLocaleString('pt-BR')}</option>)}
                  </select>
                </SettingField>
                <p className="text-[11px] text-secondary">
                  No Colab o narrador sobe com o dobro disso ({Math.max(32768, Math.min(262144, 2 * llmContext)).toLocaleString('pt-BR')} tokens), dividido entre 5 conversas ao mesmo tempo. Mais contexto pede mais memória da placa: a estimativa no bloco do Colab já considera este valor. Com o Colab ligado, a mudança só vale depois de ligar de novo.
                </p>
              </section>
            )}
            </Fragment>
          ))}

          <section className="rounded-2xl border border-subtle bg-surface p-4 space-y-3">
            <h3 className="text-xs font-semibold uppercase tracking-wider text-muted">Chaves de download</h3>
            <p className="text-[11px] text-secondary">
              Ficam salvas neste servidor e vão escritas no notebook, para o Colab baixar sem você colar nada. O CivitAI pede chave na maioria dos downloads; o Hugging Face só em repositório fechado.
            </p>
            <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
              {keyField('civitai', 'CivitAI e CivitAI Red', 'Chave da sua conta')}
              {keyField('huggingface', 'Hugging Face', 'Token (opcional)')}
            </div>
            <button
              type="button"
              disabled={savingKeys || (!keys.civitai.trim() && !keys.huggingface.trim())}
              onClick={() => saveKeys({
                ...(keys.civitai.trim() ? { civitai: keys.civitai.trim() } : {}),
                ...(keys.huggingface.trim() ? { huggingface: keys.huggingface.trim() } : {}),
              })}
              className="min-h-10 rounded-xl border border-subtle bg-elevated px-4 py-2 text-xs font-semibold text-primary disabled:opacity-50"
            >
              {savingKeys ? 'Salvando…' : 'Salvar chaves'}
            </button>
          </section>

          <section className="rounded-2xl border border-subtle bg-surface p-4 space-y-3">
            <h3 className="text-xs font-semibold uppercase tracking-wider text-muted">Colab</h3>
            <ColabPanel
              refreshKey={`${llmContext}|${dirty}|${JSON.stringify(form.models)}|${JSON.stringify(form.placements)}`}
              disabledReason={allLocal ? 'Ponha o narrador ou a imagem no Colab, acima, e salve.' : dirty ? 'Salve antes de ligar.' : ''}
              inputCls={inputCls}
              labelCls={labelCls}
              onChanged={onRefresh}
            />
            <p className="text-[11px] font-semibold text-muted">Ou rode você mesmo no site do Colab:</p>
            <div className="flex flex-wrap items-center gap-3">
              <button
                type="button"
                onClick={onDownload}
                disabled={notebookDisabled}
                className="flex min-h-11 items-center gap-1.5 rounded-xl border border-subtle bg-elevated px-4 py-2 text-xs font-semibold text-secondary transition hover:text-primary disabled:opacity-50"
              >
                <Download className="w-3.5 h-3.5" />
                <span>{downloading ? 'Gerando notebook…' : 'Gerar notebook (.ipynb)'}</span>
              </button>
              <p className="min-w-0 flex-1 text-[11px] text-secondary">{notebookHint}</p>
            </div>
            <p className="text-[11px] text-muted">
              O arquivo leva um token que só registra o runtime (vale 30 dias) e as chaves de download acima. Não compartilhe o arquivo. Trocou de modelo? Gere o notebook de novo.
            </p>
            {reg?.hasRecord && (
              <details className="text-xs text-secondary">
                <summary className="cursor-pointer font-semibold text-primary">Último registro: {reg.when}</summary>
                <div className="mt-2 space-y-1">
                  {reg.endpoints.length > 0 && (
                    <p className="break-all">Endereços: {reg.endpoints.map((row) => `${row.key}=${row.value}`).join(', ')}</p>
                  )}
                  {reg.models.length > 0 && (
                    <p className="break-words">Modelos: {reg.models.map((row) => `${row.key}=${row.value}`).join(', ')}</p>
                  )}
                  {reg.hardware.length > 0 && (
                    <p className="break-words">Hardware: {reg.hardware.map((row) => `${row.key}=${row.value}`).join(', ')}</p>
                  )}
                </div>
              </details>
            )}
          </section>

          <details className="rounded-2xl border border-subtle bg-surface p-4 text-xs text-secondary">
            <summary className="cursor-pointer text-xs font-semibold uppercase tracking-wider text-muted">
              Avançado: endereços e hardware
            </summary>
            <div className="mt-3 space-y-3">
              <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
                {ENDPOINTS.map((endpoint) => (
                  <SettingField key={endpoint.key} label={endpoint.label} labelClassName={labelCls}>
                    <input
                      type="url"
                      value={form.endpoints[endpoint.key]}
                      onChange={(e) => onChange({ ...form, endpoints: { ...form.endpoints, [endpoint.key]: e.target.value } })}
                      className={inputCls}
                    />
                  </SettingField>
                ))}
              </div>
              <SettingField label="Endereço público deste jogo (o Colab liga de volta para ele)" labelClassName={labelCls}>
                <input
                  type="url"
                  value={form.project_url}
                  onChange={(e) => onChange({ ...form, project_url: e.target.value })}
                  placeholder="Em branco: o endereço em que você está agora"
                  className={inputCls}
                />
              </SettingField>
              <div>
                <span className={labelCls}>Hardware desta máquina</span>
                {hardwareUnavailable || rows.length === 0 ? (
                  <p>Não detectado.</p>
                ) : (
                  <p className="break-words">{rows.map((row) => `${row.label}: ${row.value}`).join(' · ')}</p>
                )}
              </div>
            </div>
          </details>
        </>
      )}
    </div>
  )
}
