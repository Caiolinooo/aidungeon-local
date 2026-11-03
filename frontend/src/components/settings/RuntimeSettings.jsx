import { Download } from 'lucide-react'
import { SettingField } from './Controls.jsx'

const MODES = [
  { id: 'local', label: 'Local' },
  { id: 'hybrid', label: 'Híbrido' },
  { id: 'colab', label: 'Colab' },
]

const PLACES = [
  { id: 'local', label: 'Neste projeto' },
  { id: 'colab', label: 'Colab' },
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

export function applyRuntimeMode(form, mode) {
  const base = normalizeRuntimeConfig(form)
  if (mode === 'local' || mode === 'colab') {
    return {
      ...base,
      mode,
      placements: { llm: mode, comfyui: mode },
    }
  }
  const placements = { ...base.placements }
  if (placements.llm === placements.comfyui) {
    placements.comfyui = placements.llm === 'local' ? 'colab' : 'local'
  }
  return { ...base, mode: 'hybrid', placements }
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

function optionLabel(id, meta) {
  if (!meta || typeof meta !== 'object' || Array.isArray(meta)) return id
  const role = meta.role === 'narrator' ? 'narrador' : meta.role === 'fallback' ? 'reserva' : ''
  const engine = typeof meta.engine === 'string' && meta.engine !== id ? meta.engine : ''
  const hint = [role, engine].filter(Boolean).join(' · ')
  return hint ? `${id} · ${hint}` : id
}

function ModelSelect({ label, value, catalog, excludeRole, onChange, inputCls, labelCls }) {
  const ids = Object.keys(catalog || {}).filter(
    (id) => !excludeRole || catalog?.[id]?.role !== excludeRole,
  )
  const options = !value || ids.includes(value) ? ids : [value, ...ids]
  return (
    <SettingField label={label} labelClassName={labelCls}>
      <select value={value} onChange={(e) => onChange(e.target.value)} className={inputCls}>
        <option value="">{ids.length ? 'Selecione um modelo' : 'Catálogo vazio'}</option>
        {options.map((id) => (
          <option key={id} value={id}>{optionLabel(id, catalog?.[id])}</option>
        ))}
      </select>
    </SettingField>
  )
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

function placeLabel(place) {
  return place === 'colab' ? 'Colab' : 'neste projeto'
}

function isFineView(modelId, meta) {
  if (String(modelId || '').toLowerCase().includes('fineview')) return true
  if (!meta || typeof meta !== 'object') return false
  return /fineview/i.test(`${meta.label || ''} ${meta.name || ''} ${meta.engine || ''}`)
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
  inputCls,
  labelCls,
}) {
  const allLocal = isAllLocalRuntime(form)
  const notebookDisabled = !loaded || allLocal || dirty || downloading
  let notebookHint = 'Gera o .ipynb da configuração salva. Os segredos continuam só no Colab.'
  if (!loaded) notebookHint = 'O notebook fica indisponível até a configuração carregar.'
  else if (allLocal) notebookHint = 'Notebook desativado: narrador e ComfyUI estão neste projeto.'
  else if (dirty) notebookHint = 'Salve a configuração antes de gerar o notebook.'

  const hardwareUnavailable = hardware?.status === 'unavailable'
  const rows = hardware ? hardwareRows(hardware).filter((row) => row.key !== 'status') : []
  const reg = registration ? registrationView(registration) : null

  return (
    <div className="space-y-6">
      <div className="border-b border-subtle pb-3">
        <h2 className="text-sm font-bold uppercase tracking-wider text-primary">
          Runtime e Colab
        </h2>
        <p className="text-xs text-secondary mt-0.5">
          Escolhe onde o narrador e o ComfyUI rodam. O banco e os arquivos finais permanecem neste projeto.
        </p>
      </div>

      <div className="rounded-2xl border border-subtle bg-surface p-4 space-y-2 text-xs text-secondary">
        <p>O Colab é computação sem estado. A aventura, o SQLite e os arquivos finais não saem daqui.</p>
        <p>
          No Colab, informe você mesmo os segredos <span className="font-mono text-primary">AIDUNGEON_TOKEN</span> (token da conta admin) e <span className="font-mono text-primary">TAILSCALE_AUTH_KEY</span>.
          Este aplicativo não grava esses segredos.
          {isFineView(form?.models?.image, catalog?.image?.[form?.models?.image]) && (
            <>
              {' '}FineView também pede <span className="font-mono text-primary">CIVITAI_API_KEY</span> só no Colab. Os outros modelos de imagem não usam essa chave.
            </>
          )}
        </p>
        <p>Trocar o narrador, a imagem ou o vídeo exige reiniciar ou reconectar o runtime correspondente. Imagem e vídeo usam o mesmo ComfyUI.</p>
        <p>Gravar esta aba exige administrador. A resposta não inclui chave de API.</p>
      </div>

      {loadError && (
        <p role="alert" className="rounded-xl border border-danger-800 bg-danger-950/60 px-3 py-2 text-xs text-danger-300">
          Não foi possível carregar o runtime: {loadError}
        </p>
      )}

      {loaded && (
        <>
          <div className="rounded-2xl border border-subtle bg-surface p-4 space-y-4">
            <h3 className="text-xs font-semibold uppercase tracking-wider text-muted">
              Onde a computação roda
            </h3>
            <fieldset className="space-y-2">
              <legend className={labelCls}>Modo</legend>
              <div role="radiogroup" aria-label="Modo do runtime" className="flex flex-wrap gap-1.5">
                {MODES.map((mode) => {
                  const active = form.mode === mode.id
                  return (
                    <button
                      key={mode.id}
                      type="button"
                      role="radio"
                      aria-checked={active}
                      onClick={() => onChange(applyRuntimeMode(form, mode.id))}
                      className={`min-h-11 rounded-lg px-3.5 py-2 text-xs font-semibold transition ${
                        active
                          ? 'bg-accent-pri text-canvas'
                          : 'border border-subtle bg-elevated text-secondary hover:text-primary'
                      }`}
                    >
                      {mode.label}
                    </button>
                  )
                })}
              </div>
            </fieldset>
            <p className="text-[11px] text-secondary">
              Narrador: {placeLabel(form.placements.llm)}. ComfyUI (imagem e vídeo): {placeLabel(form.placements.comfyui)}.
              Local mantém os dois aqui. Colab envia os dois ao notebook. Híbrido deixa um em cada lado.
            </p>
            <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
              <SettingField label="Onde o narrador roda" labelClassName={labelCls}>
                <select
                  value={form.placements.llm}
                  onChange={(e) => onChange(applyRuntimePlacement(form, 'llm', e.target.value))}
                  className={inputCls}
                >
                  {PLACES.map((place) => (
                    <option key={place.id} value={place.id}>{place.label}</option>
                  ))}
                </select>
              </SettingField>
              <SettingField label="Onde o ComfyUI roda" labelClassName={labelCls}>
                <select
                  value={form.placements.comfyui}
                  onChange={(e) => onChange(applyRuntimePlacement(form, 'comfyui', e.target.value))}
                  className={inputCls}
                >
                  {PLACES.map((place) => (
                    <option key={place.id} value={place.id}>{place.label}</option>
                  ))}
                </select>
              </SettingField>
            </div>
          </div>

          <div className="rounded-2xl border border-subtle bg-surface p-4 space-y-4">
            <div>
              <h3 className="text-xs font-semibold uppercase tracking-wider text-muted">Modelos</h3>
              <p className="text-[11px] text-secondary mt-0.5">
                Identificadores do catálogo do servidor. A lista não é preenchida com modelos inventados.
                O narrador não traz URL nem SHA: isso fica nas variáveis do notebook.
              </p>
            </div>
            <div className="grid grid-cols-1 sm:grid-cols-3 gap-4">
              <ModelSelect
                label="Narrador"
                value={form.models.llm}
                catalog={catalog?.llm}
                excludeRole={form.placements.llm === 'local' ? 'fallback' : undefined}
                onChange={(llm) => onChange({ ...form, models: { ...form.models, llm } })}
                inputCls={inputCls}
                labelCls={labelCls}
              />
              <ModelSelect
                label="Imagem"
                value={form.models.image}
                catalog={catalog?.image}
                onChange={(image) => onChange({ ...form, models: { ...form.models, image } })}
                inputCls={inputCls}
                labelCls={labelCls}
              />
              <ModelSelect
                label="Vídeo"
                value={form.models.video}
                catalog={catalog?.video}
                onChange={(video) => onChange({ ...form, models: { ...form.models, video } })}
                inputCls={inputCls}
                labelCls={labelCls}
              />
            </div>
          </div>

          <div className="rounded-2xl border border-subtle bg-surface p-4 space-y-4">
            <h3 className="text-xs font-semibold uppercase tracking-wider text-muted">Endpoints</h3>
            <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
              <SettingField label="URL local do narrador" labelClassName={labelCls}>
                <input
                  type="text"
                  value={form.endpoints.local_llm}
                  onChange={(e) => onChange({
                    ...form,
                    endpoints: { ...form.endpoints, local_llm: e.target.value },
                  })}
                  placeholder="http://127.0.0.1:1234/v1"
                  className={inputCls}
                  autoComplete="off"
                />
              </SettingField>
              <SettingField label="URL local do ComfyUI" labelClassName={labelCls}>
                <input
                  type="text"
                  value={form.endpoints.local_comfyui}
                  onChange={(e) => onChange({
                    ...form,
                    endpoints: { ...form.endpoints, local_comfyui: e.target.value },
                  })}
                  placeholder="http://127.0.0.1:8188"
                  className={inputCls}
                  autoComplete="off"
                />
              </SettingField>
              <SettingField label="URL do narrador no Colab" labelClassName={labelCls}>
                <input
                  type="text"
                  value={form.endpoints.colab_llm}
                  onChange={(e) => onChange({
                    ...form,
                    endpoints: { ...form.endpoints, colab_llm: e.target.value },
                  })}
                  placeholder="http://host-tailscale:1234/v1"
                  className={inputCls}
                  autoComplete="off"
                />
              </SettingField>
              <SettingField label="URL do ComfyUI no Colab" labelClassName={labelCls}>
                <input
                  type="text"
                  value={form.endpoints.colab_comfyui}
                  onChange={(e) => onChange({
                    ...form,
                    endpoints: { ...form.endpoints, colab_comfyui: e.target.value },
                  })}
                  placeholder="http://host-tailscale:8188"
                  className={inputCls}
                  autoComplete="off"
                />
              </SettingField>
            </div>
            <p className="text-[11px] text-secondary">
              Uma URL de Colab vazia também é gravada. Com essa colocação no Colab, ela passa a ser a URL do narrador ou do ComfyUI.
            </p>
            <SettingField label="URL de retorno do projeto" labelClassName={labelCls}>
              <input
                type="text"
                value={form.project_url}
                onChange={(e) => onChange({ ...form, project_url: e.target.value })}
                placeholder="https://projeto.exemplo"
                className={inputCls}
                autoComplete="off"
              />
            </SettingField>
          </div>

          <div className="rounded-2xl border border-subtle bg-surface p-4 space-y-3">
            <div>
              <h3 className="text-xs font-semibold uppercase tracking-wider text-muted">Hardware local</h3>
              <p className="text-[11px] text-secondary mt-0.5">
                Leitura enviada pelo servidor deste projeto. Não é uma sonda de saúde do Colab.
              </p>
            </div>
            {hardwareUnavailable && (
              <p className="text-xs text-secondary">Sonda do hardware local indisponível.</p>
            )}
            {rows.length > 0 && (
              <dl className="grid grid-cols-1 sm:grid-cols-2 gap-x-4 gap-y-2">
                {rows.map((row) => (
                  <div key={row.key} className="min-w-0">
                    <dt className="text-[10px] font-semibold uppercase tracking-wider text-muted">{row.label}</dt>
                    <dd className="break-words font-mono text-xs text-primary">{row.value}</dd>
                  </div>
                ))}
              </dl>
            )}
            {!hardwareUnavailable && rows.length === 0 && (
              <p className="text-xs text-secondary">O servidor não informou o hardware local.</p>
            )}
          </div>

          <div className="rounded-2xl border border-subtle bg-surface p-4 space-y-2">
            <h3 className="text-xs font-semibold uppercase tracking-wider text-muted">Último registro do Colab</h3>
            <p className="text-[11px] text-secondary">
              Estado gravado na última vez que um notebook se registrou. Não é uma verificação ao vivo.
            </p>
            {(!reg || !reg.hasRecord) && (
              <p className="text-xs text-secondary">Nenhum registro do Colab recebido.</p>
            )}
            {reg?.hasRecord && (
              <div className="space-y-2 text-xs text-secondary">
                <p>
                  Último contato: <span className="font-mono text-primary">{reg.when}</span>
                </p>
                {reg.models.length > 0 && (
                  <p className="break-words">
                    Modelos no registro: {reg.models.map((row) => `${row.key}=${row.value}`).join(', ')}
                  </p>
                )}
                {reg.endpoints.length > 0 && (
                  <p className="break-words">
                    Endpoints no registro: {reg.endpoints.map((row) => `${row.key}=${row.value}`).join(', ')}
                  </p>
                )}
                {reg.hardware.length > 0 && (
                  <p className="break-words">
                    Hardware no registro: {reg.hardware.map((row) => `${row.key}=${row.value}`).join(', ')}
                  </p>
                )}
              </div>
            )}
          </div>

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
            <p className="text-[11px] text-secondary">{notebookHint}</p>
          </div>
        </>
      )}
    </div>
  )
}
