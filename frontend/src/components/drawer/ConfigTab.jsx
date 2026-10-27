import { useCallback, useEffect, useRef, useState } from 'react'
import { api } from '../../api.js'
import { getAdventureScripts, putAdventureScripts } from '../../api/adventureScripts.js'
import { useToast } from '../Toast.jsx'
import VoiceCloneModal from '../VoiceCloneModal.jsx'
import { HealthCheck } from '../settings/Controls.jsx'
import ImageAdvancedControls from '../settings/ImageAdvancedControls.jsx'
import LlmSamplingControls, { LLM_DEFAULTS } from '../settings/LlmSamplingControls.jsx'

const HEALTH_STYLE = {
  ok: 'bg-accent-900 text-accent-pri',
  unreachable: 'bg-danger-900 text-danger-300',
  auth_error: 'bg-warn-900 text-warn-300',
  error: 'bg-danger-900 text-danger-300',
}

// Aba Config: GET/PUT /api/settings/llm (campos do contrato — api_key é
// write-only: em branco = mantém; botão Clear envia "" para remover) e
// status ao vivo de GET /api/health/llm (model_discovered etc.).
// Geração reúne presets, tamanho da resposta e todos os samplers de
// settings/llm; as seções Images e Voice usam seus endpoints GET/PUT próprios.
export default function ConfigTab({ adventure = null }) {
  const toast = useToast()
  const [settings, setSettings] = useState(null)
  const [health, setHealth] = useState(null)
  const [form, setForm] = useState(null)
  const [saving, setSaving] = useState(false)

  const load = useCallback(async () => {
    try {
      const [s, h] = await Promise.all([api.getLlmSettings(), api.healthLlm()])
      setSettings(s)
      setHealth(h)
      setForm({
        ...LLM_DEFAULTS,
        base_url: s.base_url ?? '',
        model_alias: s.model_alias ?? 'auto',
        context_size: s.context_size ?? 8192,
        max_tokens: s.max_tokens ?? LLM_DEFAULTS.max_tokens,
        timeout_seconds: s.timeout_seconds ?? 180,
        streaming: !!s.streaming,
        disable_thinking: !!s.disable_thinking,
        temperature: s.temperature ?? LLM_DEFAULTS.temperature,
        top_p: s.top_p ?? LLM_DEFAULTS.top_p,
        min_p: s.min_p ?? LLM_DEFAULTS.min_p,
        repetition_penalty: s.repetition_penalty ?? LLM_DEFAULTS.repetition_penalty,
        presence_penalty: s.presence_penalty ?? LLM_DEFAULTS.presence_penalty,
        frequency_penalty: s.frequency_penalty ?? LLM_DEFAULTS.frequency_penalty,
        dry_multiplier: s.dry_multiplier ?? LLM_DEFAULTS.dry_multiplier,
        dry_base: s.dry_base ?? LLM_DEFAULTS.dry_base,
        dry_allowed_length: s.dry_allowed_length ?? LLM_DEFAULTS.dry_allowed_length,
        dry_penalty_last_n: s.dry_penalty_last_n ?? LLM_DEFAULTS.dry_penalty_last_n,
        retry_temperature_step: s.retry_temperature_step ?? LLM_DEFAULTS.retry_temperature_step,
        max_retry_temperature: s.max_retry_temperature ?? LLM_DEFAULTS.max_retry_temperature,
        api_key: '', // write-only: vazio = manter a chave atual
      })
    } catch (e) {
      toast(e.message)
      setSettings({})
    }
  }, [toast])

  useEffect(() => {
    load()
  }, [load])

  const save = async () => {
    setSaving(true)
    try {
      const payload = {
        base_url: form.base_url,
        model_alias: form.model_alias,
        context_size: Number(form.context_size),
        max_tokens: Number(form.max_tokens),
        timeout_seconds: Number(form.timeout_seconds),
        streaming: form.streaming,
        disable_thinking: form.disable_thinking,
        temperature: Number(form.temperature),
        top_p: Number(form.top_p),
        min_p: Number(form.min_p),
        repetition_penalty: Number(form.repetition_penalty),
        presence_penalty: Number(form.presence_penalty),
        frequency_penalty: Number(form.frequency_penalty),
        dry_multiplier: Number(form.dry_multiplier),
        dry_base: Number(form.dry_base),
        dry_allowed_length: Number(form.dry_allowed_length),
        dry_penalty_last_n: Number(form.dry_penalty_last_n),
        retry_temperature_step: Number(form.retry_temperature_step),
        max_retry_temperature: Number(form.max_retry_temperature),
      }
      if (form.api_key) payload.api_key = form.api_key // só envia se digitado
      const s = await api.putLlmSettings(payload)
      setSettings(s)
      toast('Configurações do modelo salvas.', 'ok')
      const h = await api.healthLlm().catch(() => null)
      if (h) setHealth(h)
    } catch (e) {
      toast(e.message)
    } finally {
      setSaving(false)
    }
  }

  const clearKey = async () => {
    try {
      await api.putLlmSettings({ api_key: '' }) // string vazia remove a chave (contrato)
      toast('API key cleared.', 'ok')
      load()
    } catch (e) {
      toast(e.message)
    }
  }

  if (settings === null || form === null) return <p className="text-sm text-muted">Carregando…</p>

  const contextSizeMax = settings.context_size_max ?? 106496
  const inputCls =
    'w-full rounded-lg border border-subtle bg-surface px-3 py-2 text-sm outline-none focus:border-accent-pri'
  const labelCls = 'mb-1 block text-xs font-semibold uppercase tracking-wide text-muted'

  return (
    <div className="flex flex-col gap-4">
      {health && (
        <div className="rounded-lg border border-subtle bg-surface/40 p-3 text-sm">
          <div className="flex items-center gap-2">
            <span className={`rounded-full px-2 py-0.5 text-xs font-semibold ${HEALTH_STYLE[health.status] ?? HEALTH_STYLE.error}`}>
              {health.status}
            </span>
            <span className="text-secondary">{health.provider}</span>
          </div>
          <div className="mt-2 text-xs text-muted">
            <div>
              Model discovered:{' '}
              <span className="text-secondary">{health.model_discovered ?? settings.model_discovered ?? '—'}</span>
            </div>
            {health.details && <div className="mt-1 text-muted">{health.details}</div>}
          </div>
        </div>
      )}

      <label className="block">
        <span className={labelCls}>URL base (compatível com OpenAI)</span>
        <input value={form.base_url} onChange={(e) => setForm({ ...form, base_url: e.target.value })} className={inputCls} />
      </label>

      <label className="block">
        <span className={labelCls}>Apelido do modelo ("auto" = descobrir em /models)</span>
        <input value={form.model_alias} onChange={(e) => setForm({ ...form, model_alias: e.target.value })} className={inputCls} />
      </label>

      <label className="block">
        <span className={labelCls}>
          API key {settings.api_key ? `(atual: ${settings.api_key})` : '(none set)'} — write-only
        </span>
        <div className="flex gap-2">
          <input
            type="password"
            value={form.api_key}
            onChange={(e) => setForm({ ...form, api_key: e.target.value })}
            placeholder="Leave blank to keep current key"
            className={inputCls}
          />
          {settings.api_key && (
            <button
              onClick={clearKey}
              className="shrink-0 rounded-lg border border-danger-900 px-3 text-xs text-danger-400 hover:bg-danger-950"
            >
              Clear
            </button>
          )}
        </div>
      </label>

      <div className="grid grid-cols-2 gap-3">
        <label className="block">
          <span className={labelCls}>Context size (max {contextSizeMax.toLocaleString()})</span>
          <input
            type="number"
            min={4096}
            max={contextSizeMax}
            step={1024}
            value={form.context_size}
            onChange={(e) => setForm({ ...form, context_size: Math.min(contextSizeMax, Number(e.target.value) || 0) })}
            className={inputCls}
          />

        </label>
        <label className="block">
          <span className={labelCls}>Timeout (s)</span>
          <input
            type="number"
            min={1}
            value={form.timeout_seconds}
            onChange={(e) => setForm({ ...form, timeout_seconds: e.target.value })}
            className={inputCls}
          />
        </label>
      </div>


      <div className="flex gap-5">
        <label className="flex items-center gap-2 text-sm text-secondary">
          <input
            type="checkbox"
            checked={form.streaming}
            onChange={(e) => setForm({ ...form, streaming: e.target.checked })}
            className="accent-accent-600"
          />
          Streaming
        </label>
        <label className="flex items-center gap-2 text-sm text-secondary">
          <input
            type="checkbox"
            checked={form.disable_thinking}
            onChange={(e) => setForm({ ...form, disable_thinking: e.target.checked })}
            className="accent-accent-600"
          />
          Desativar raciocínio (Qwen3.x)
        </label>
      </div>

      {/* Geração: presets e todos os samplers persistidos em settings/llm. */}
      <section className="flex flex-col gap-3 rounded-lg border border-subtle bg-surface/40 p-3" aria-labelledby="drawer-generation-title">
        <h3 id="drawer-generation-title" className="text-xs font-semibold uppercase tracking-wide text-muted">Geração</h3>
        <LlmSamplingControls form={form} onChange={setForm} inputCls={inputCls} labelCls={labelCls} compact />
      </section>

      <div className="flex justify-end">
        <button
          onClick={save}
          disabled={saving}
          className="rounded-lg bg-accent-pri px-4 py-2 text-sm font-semibold text-canvas hover:bg-accent-hov disabled:opacity-50"
        >
          {saving ? 'Salvando…' : 'Salvar configurações'}
        </button>
      </div>

      {/* v2 — seção Images: config do provedor de geração de imagens ("See") */}
      <ImageSettingsSection inputCls={inputCls} labelCls={labelCls} />

      {/* v0.4.0 — seção Voice: TTS (OmniVoice) + STT (Whisper) da stack da VM */}
      <VoiceSettingsSection inputCls={inputCls} labelCls={labelCls} />

      <AdventureScriptsSection adventure={adventure} inputCls={inputCls} labelCls={labelCls} />
    </div>
  )
}

const ADVENTURE_SCRIPT_HOOKS = [
  {
    key: 'input_modifier',
    label: 'Entrada',
    hint: 'Roda antes do turno. Variáveis: action, text, type. Pode definir modifier(action).',
  },
  {
    key: 'context_modifier',
    label: 'Contexto',
    hint: 'Roda na lista enviada ao modelo. Variável: context. Pode definir modifier(context).',
  },
  {
    key: 'output_modifier',
    label: 'Saída',
    hint: 'Roda no texto gerado. Variáveis: text, output. Pode definir modifier(text).',
  },
]

const EMPTY_SCRIPTS = { input_modifier: '', context_modifier: '', output_modifier: '' }

function scriptSourceLabel(source) {
  if (source === 'adventure') return 'Vale o script desta aventura.'
  if (source === 'global') return 'Vazio aqui: o turno usa o script global.'
  return 'Nenhum script neste hook.'
}

function AdventureScriptsSection({ adventure, inputCls, labelCls }) {
  const toast = useToast()
  const adventureId = adventure?.id || ''
  const adventureIdRef = useRef(adventureId)
  adventureIdRef.current = adventureId
  const saveGen = useRef(0)
  const [scripts, setScripts] = useState(EMPTY_SCRIPTS)
  const [meta, setMeta] = useState(null)
  const [loading, setLoading] = useState(false)
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState('')

  const applyPayload = (data) => {
    setScripts({
      input_modifier: data?.input_modifier || '',
      context_modifier: data?.context_modifier || '',
      output_modifier: data?.output_modifier || '',
    })
    setMeta({ sources: data?.sources || {}, global: data?.global || {} })
  }

  useEffect(() => {
    if (!adventureId) {
      setScripts(EMPTY_SCRIPTS)
      setMeta(null)
      setError('')
      setLoading(false)
      return undefined
    }
    let cancelled = false
    saveGen.current += 1
    setSaving(false)
    setLoading(true)
    setError('')
    setScripts(EMPTY_SCRIPTS)
    setMeta(null)
    getAdventureScripts(adventureId)
      .then((data) => {
        if (!cancelled) applyPayload(data)
      })
      .catch((e) => {
        if (cancelled) return
        setError(e.message || 'Não foi possível ler os scripts.')
        toast(e.message)
      })
      .finally(() => {
        if (!cancelled) setLoading(false)
      })
    return () => {
      cancelled = true
    }
  }, [adventureId, toast])

  const persist = async (payload, okMessage) => {
    if (!adventureId) return
    const id = adventureId
    const gen = ++saveGen.current
    setSaving(true)
    setError('')
    try {
      const data = await putAdventureScripts(id, payload)
      if (saveGen.current !== gen || adventureIdRef.current !== id) return
      applyPayload(data)
      toast(okMessage, 'ok')
    } catch (e) {
      if (saveGen.current !== gen || adventureIdRef.current !== id) return
      setError(e.message || 'Não foi possível salvar os scripts.')
      toast(e.message)
    } finally {
      if (saveGen.current === gen) setSaving(false)
    }
  }

  const saveAll = () => persist(
    {
      input_modifier: scripts.input_modifier,
      context_modifier: scripts.context_modifier,
      output_modifier: scripts.output_modifier,
    },
    'Scripts da aventura salvos.',
  )

  const clearHook = (key) => persist(
    { [key]: '' },
    'Hook removido. Se existir script global, o turno volta a usá-lo.',
  )

  const busy = loading || saving

  return (
    <section className="flex flex-col gap-3 border-t border-subtle pt-4" aria-labelledby="adventure-scripts-title">
      <div>
        <h3 id="adventure-scripts-title" className="text-xs font-semibold uppercase tracking-wide text-muted">
          Scripts da aventura
        </h3>
        <p className="mt-0.5 text-xs text-muted">
          Cada hook salvo aqui substitui só o da aventura aberta. Campo vazio não grava nada nesta aventura:
          o turno usa <code>script:global:*</code> daquele hook, se existir. Limpar apaga a chave da aventura
          e devolve esse fallback. O global não muda nesta tela. O script é validado ao salvar e só roda nos
          hooks do turno.
        </p>
      </div>

      {!adventureId && (
        <p className="text-sm text-muted">Abra uma aventura para editar os scripts dela.</p>
      )}

      {adventureId && loading && !meta && (
        <p className="text-sm text-muted">Carregando scripts…</p>
      )}

      {adventureId && (meta || error) && (
        <>
          {ADVENTURE_SCRIPT_HOOKS.map((hook) => {
            const source = meta?.sources?.[hook.key] || 'empty'
            const globalText = meta?.global?.[hook.key] || ''
            return (
              <label key={hook.key} className="block">
                <span className={labelCls}>{hook.label}</span>
                <span className="mb-1 block text-xs text-muted">{hook.hint}</span>
                <span className="mb-1 block text-xs text-secondary">{scriptSourceLabel(source)}</span>
                {source === 'global' && globalText && (
                  <pre className="mb-1 max-h-24 overflow-auto whitespace-pre-wrap rounded-lg border border-subtle bg-surface/40 px-3 py-2 text-xs text-muted">
                    {globalText}
                  </pre>
                )}
                <textarea
                  value={scripts[hook.key]}
                  onChange={(e) => setScripts((prev) => ({ ...prev, [hook.key]: e.target.value }))}
                  rows={5}
                  disabled={busy}
                  spellCheck={false}
                  aria-label={`Script de ${hook.label}`}
                  placeholder="Vazio = usar o script global deste hook"
                  className={`${inputCls} resize-y font-mono text-xs disabled:opacity-60`}
                />
                <div className="mt-1 flex justify-end">
                  <button
                    type="button"
                    onClick={() => clearHook(hook.key)}
                    disabled={busy}
                    className="rounded-lg border border-subtle px-3 py-1.5 text-xs text-primary hover:bg-surface disabled:opacity-50"
                  >
                    Limpar
                  </button>
                </div>
              </label>
            )
          })}

          {error && <p className="text-xs text-danger-300">{error}</p>}

          <div className="flex justify-end">
            <button
              type="button"
              onClick={saveAll}
              disabled={busy}
              className="rounded-lg bg-accent-pri px-3 py-1.5 text-xs font-semibold text-canvas hover:bg-accent-hov disabled:opacity-50"
            >
              {saving ? 'Salvando…' : 'Salvar scripts'}
            </button>
          </div>
        </>
      )}
    </section>
  )
}

// Seção "Voice" (v0.4.0): GET/PUT /api/settings/voice. Espelha a seção
// Images: api_key é write-only (em branco mantém; Clear envia "") e
// narrator_voice é um select das vozes da config efetiva.
function VoiceSettingsSection({ inputCls, labelCls }) {
  const toast = useToast()
  const [loaded, setLoaded] = useState(null) // VoiceSettings efetiva (api_key mascarada)
  const [form, setForm] = useState(null)
  const [saving, setSaving] = useState(false)
  const [cloneOpen, setCloneOpen] = useState(false)

  const load = useCallback(async () => {
    try {
      const s = await api.voice.getSettings()
      let cloned = []
      try {
        const listed = await api.voice.list()
        cloned = listed.cloned || []
      } catch { /* a lista de clones é extra */ }
      setLoaded({ ...s, cloned })
      setForm({
        enabled: !!s.enabled,
        base_url: s.base_url ?? '',
        tts_model: s.tts_model ?? '',
        stt_model: s.stt_model ?? '',
        narrator_voice: s.narrator_voice ?? '',
        timeout_seconds: s.timeout_seconds ?? 60,
        api_key: '', // write-only: vazio = manter a chave atual
      })
    } catch (e) {
      toast(e.message)
      setLoaded({})
    }
  }, [toast])

  useEffect(() => {
    load()
  }, [load])

  const save = async () => {
    setSaving(true)
    try {
      const payload = {
        enabled: form.enabled,
        base_url: form.base_url,
        tts_model: form.tts_model,
        stt_model: form.stt_model,
        narrator_voice: form.narrator_voice,
        timeout_seconds: Number(form.timeout_seconds),
      }
      if (form.api_key) payload.api_key = form.api_key // só envia se digitado
      setLoaded(await api.voice.putSettings(payload))
      toast('Configurações de voz salvas.', 'ok')
    } catch (e) {
      toast(e.message)
    } finally {
      setSaving(false)
    }
  }

  const clearKey = async () => {
    try {
      await api.voice.putSettings({ api_key: '' }) // string vazia remove a chave (contrato)
      toast('Voice API key cleared.', 'ok')
      load()
    } catch (e) {
      toast(e.message)
    }
  }

  if (loaded === null || form === null) {
    return (
      <div className="border-t border-subtle pt-4">
        <h3 className="mb-2 text-xs font-semibold uppercase tracking-wide text-muted">Voice</h3>
        <p className="text-sm text-muted">Carregando…</p>
      </div>
    )
  }

  const builtins = loaded.voices?.length ? loaded.voices : ['M1', 'M2', 'M3', 'F1', 'F2', 'F3']
  const voiceOptions = [
    ...builtins.map((v) => ({ value: v, label: v })),
    ...(loaded.cloned || []).map((c) => ({ value: c.id, label: c.name })),
  ]
  if (form.narrator_voice && !voiceOptions.some((o) => o.value === form.narrator_voice)) {
    voiceOptions.push({ value: form.narrator_voice, label: form.narrator_voice })
  }

  return (
    <div className="flex flex-col gap-4 border-t border-subtle pt-4">
      <div>
        <h3 className="text-xs font-semibold uppercase tracking-wide text-muted">Voice</h3>
        <p className="mt-0.5 text-xs text-muted">
          Narração com OmniVoice e ditado por transcrição em um provedor compatível com OpenAI.
        </p>
        {loaded.drama3_enabled ? (
          <p className="mt-1 text-xs text-accent-pri">Drama-3 está ligado. As falas saem com uma direção de emoção.</p>
        ) : (
          <p className="mt-1 text-xs text-muted">
            Drama-3 está desativado nesta máquina (VRAM livre insuficiente). O OmniVoice continua narrando e cada fala envia uma etiqueta de emoção.
          </p>
        )}
      </div>

      <HealthCheck service="voice" compact />

      <label className="flex items-center gap-2 text-sm text-secondary">
        <input
          type="checkbox"
          checked={form.enabled}
          onChange={(e) => setForm({ ...form, enabled: e.target.checked })}
          className="accent-accent-600"
        />
        Enabled
      </label>

      <label className="block">
        <span className={labelCls}>URL base (compatível com OpenAI)</span>
        <input
          value={form.base_url}
          onChange={(e) => setForm({ ...form, base_url: e.target.value })}
          placeholder="https://host/voice/v1"
          className={inputCls}
        />
      </label>

      <label className="block">
        <span className={labelCls}>
          API key {loaded.api_key ? `(current: ${loaded.api_key})` : '(none set)'} — write-only
        </span>
        <div className="flex gap-2">
          <input
            type="password"
            value={form.api_key}
            onChange={(e) => setForm({ ...form, api_key: e.target.value })}
            placeholder={loaded.api_key ? `Current: ${loaded.api_key}` : 'Leave blank to keep current key'}
            className={inputCls}
          />
          {loaded.api_key && (
            <button
              onClick={clearKey}
              className="shrink-0 rounded-lg border border-danger-900 px-3 text-xs text-danger-400 hover:bg-danger-950"
            >
              Clear
            </button>
          )}
        </div>
      </label>

      <div className="grid grid-cols-2 gap-3">
        <label className="block">
          <span className={labelCls}>Modelo de voz (OmniVoice)</span>
          <input
            value={form.tts_model}
            onChange={(e) => setForm({ ...form, tts_model: e.target.value })}
            placeholder="omnivoice"
            className={inputCls}
          />
        </label>
        <label className="block">
          <span className={labelCls}>Modelo de transcrição</span>
          <input
            value={form.stt_model}
            onChange={(e) => setForm({ ...form, stt_model: e.target.value })}
            placeholder="whisper-1"
            className={inputCls}
          />
        </label>
      </div>

      <div className="grid grid-cols-2 gap-3">
        <label className="block">
          <span className={labelCls}>Voz do narrador</span>
          <select
            value={form.narrator_voice}
            onChange={(e) => setForm({ ...form, narrator_voice: e.target.value })}
            className={inputCls}
          >
            {voiceOptions.map((o) => (
              <option key={o.value} value={o.value}>{o.label}</option>
            ))}
          </select>
        </label>
        <button
          type="button"
          onClick={() => setCloneOpen(true)}
          className="rounded-lg border border-subtle px-3 py-2 text-sm text-secondary"
        >
          Clonar voz
        </button>
        {cloneOpen && (
          <VoiceCloneModal
            open
            assign={{ kind: 'narrator' }}
            onClose={(result) => {
              if (result?.deleted) {
                setLoaded((cur) => ({
                  ...cur,
                  cloned: (cur?.cloned || []).filter((c) => c.id !== result.deleted),
                }))
                setForm((f) => (f && f.narrator_voice === result.deleted ? { ...f, narrator_voice: 'M1' } : f))
                return
              }
              setCloneOpen(false)
              if (!result?.id) return
              setForm((f) => ({ ...f, narrator_voice: result.id }))
              setLoaded((cur) => {
                const list = cur?.cloned || []
                const cloned = list.some((c) => c.id === result.id)
                  ? list
                  : [...list, { id: result.id, name: result.name }]
                return { ...cur, cloned }
              })
            }}
          />
        )}
        <label className="block">
          <span className={labelCls}>Timeout (s)</span>
          <input
            type="number"
            min={1}
            value={form.timeout_seconds}
            onChange={(e) => setForm({ ...form, timeout_seconds: e.target.value })}
            className={inputCls}
          />
        </label>
      </div>

      <div className="flex justify-end">
        <button
          onClick={save}
          disabled={saving}
          className="rounded-lg bg-accent-pri px-4 py-2 text-sm font-semibold text-canvas hover:bg-accent-hov disabled:opacity-50"
        >
          {saving ? 'Salvando…' : 'Salvar voz'}
        </button>
      </div>
    </div>
  )
}

// Seção "Images" (v2): GET/PUT /api/settings/image. Campos por provider
// (sdwebui | openai_compatible); openai_api_key é write-only — em branco
// mantém a chave atual (placeholder mostra o valor mascarado), Clear envia "".
function ImageSettingsSection({ inputCls, labelCls }) {
  const toast = useToast()
  const [loaded, setLoaded] = useState(null) // ImageSettings efetiva (api_key mascarada)
  const [form, setForm] = useState(null)
  const [saving, setSaving] = useState(false)

  const load = useCallback(async () => {
    try {
      const s = await api.getImageSettings()
      setLoaded(s)
      setForm({
        enabled: !!s.enabled,
        provider: s.provider ?? 'sdwebui',
        sdwebui_base_url: s.sdwebui_base_url ?? '',
        sdwebui_steps: s.sdwebui_steps ?? 20,
        sdwebui_width: s.sdwebui_width ?? 512,
        sdwebui_height: s.sdwebui_height ?? 512,
        negative_prompt: s.negative_prompt ?? '',
        openai_base_url: s.openai_base_url ?? '',
        openai_model: s.openai_model ?? '',
        openai_size: s.openai_size ?? '512x512',
        style_suffix: s.style_suffix ?? '',
        seed: s.seed ?? '',
        variation: s.variation ?? 0,
        timeout_seconds: s.timeout_seconds ?? 300,
        cfg_scale: s.cfg_scale ?? 5,
        sampler_name: s.sampler_name ?? 'DPM++ 2M',
        scheduler: s.scheduler ?? 'Karras',
        denoising_strength: s.denoising_strength ?? 0.35,
        sd_model_checkpoint: s.sd_model_checkpoint ?? '',
        openai_api_key: '', // write-only: vazio = manter a chave atual
      })
    } catch (e) {
      toast(e.message)
      setLoaded({})
    }
  }, [toast])

  useEffect(() => {
    load()
  }, [load])

  const save = async () => {
    setSaving(true)
    try {
      const payload = {
        enabled: form.enabled,
        provider: form.provider,
        sdwebui_base_url: form.sdwebui_base_url,
        sdwebui_steps: Number(form.sdwebui_steps),
        sdwebui_width: Number(form.sdwebui_width),
        sdwebui_height: Number(form.sdwebui_height),
        negative_prompt: form.negative_prompt,
        openai_base_url: form.openai_base_url,
        openai_model: form.openai_model,
        openai_size: form.openai_size,
        style_suffix: form.style_suffix,
        seed: form.seed === '' || form.seed == null ? null : Number(form.seed),
        variation: Number(form.variation),
        timeout_seconds: Number(form.timeout_seconds),
        cfg_scale: Number(form.cfg_scale),
        sampler_name: form.sampler_name,
        scheduler: form.scheduler,
        denoising_strength: Number(form.denoising_strength),
        sd_model_checkpoint: form.sd_model_checkpoint,
      }
      if (form.openai_api_key) payload.openai_api_key = form.openai_api_key // só envia se digitado
      setLoaded(await api.putImageSettings(payload))
      toast('Configurações de imagem salvas.', 'ok')
    } catch (e) {
      toast(e.message)
    } finally {
      setSaving(false)
    }
  }

  const clearKey = async () => {
    try {
      await api.putImageSettings({ openai_api_key: '' }) // string vazia remove a chave (contrato)
      toast('OpenAI image API key cleared.', 'ok')
      load()
    } catch (e) {
      toast(e.message)
    }
  }

  if (loaded === null || form === null) {
    return (
      <div className="border-t border-subtle pt-4">
        <h3 className="mb-2 text-xs font-semibold uppercase tracking-wide text-muted">Imagens</h3>
        <p className="text-sm text-muted">Carregando…</p>
      </div>
    )
  }

  return (
    <div className="flex flex-col gap-4 border-t border-subtle pt-4">
      <div>
        <h3 className="text-xs font-semibold uppercase tracking-wide text-muted">Imagens</h3>
        <p className="mt-0.5 text-xs text-muted">
          Provedor usado pela ação “Ver” para ilustrar a cena atual.
        </p>
      </div>

      <HealthCheck service="image" compact />

      <div className="flex gap-5">
        <label className="flex items-center gap-2 text-sm text-secondary">
          <input
            type="checkbox"
            checked={form.enabled}
            onChange={(e) => setForm({ ...form, enabled: e.target.checked })}
            className="accent-accent-600"
          />
          Enabled
        </label>
        <label className="flex flex-1 items-center gap-2 text-sm text-secondary">
          <span className="text-xs font-semibold uppercase tracking-wide text-muted">Provider</span>
          <select
            value={form.provider}
            onChange={(e) => setForm({ ...form, provider: e.target.value })}
            className={inputCls}
          >
            <option value="sdwebui">sdwebui (local/LAN)</option>
            <option value="openai_compatible">openai_compatible</option>
          </select>
        </label>
      </div>

      {form.provider === 'sdwebui' ? (
        <>
          <label className="block">
            <span className={labelCls}>sdwebui base URL</span>
            <input
              value={form.sdwebui_base_url}
              onChange={(e) => setForm({ ...form, sdwebui_base_url: e.target.value })}
              placeholder="http://127.0.0.1:7860"
              className={inputCls}
            />
          </label>
          <div className="grid grid-cols-3 gap-3">
            <label className="block">
              <span className={labelCls}>Passos (20 recomendados)</span>
              <input
                type="number"
                min={1}
                max={150}
                step={1}
                value={form.sdwebui_steps}
                onChange={(e) => setForm({ ...form, sdwebui_steps: e.target.value })}
                className={inputCls}
              />
            </label>
            <label className="block">
              <span className={labelCls}>Width</span>
              <input
                type="number"
                min={64}
                step={64}
                value={form.sdwebui_width}
                onChange={(e) => setForm({ ...form, sdwebui_width: e.target.value })}
                className={inputCls}
              />
            </label>
            <label className="block">
              <span className={labelCls}>Height</span>
              <input
                type="number"
                min={64}
                step={64}
                value={form.sdwebui_height}
                onChange={(e) => setForm({ ...form, sdwebui_height: e.target.value })}
                className={inputCls}
              />
            </label>
          </div>
          <label className="block">
            <span className={labelCls}>Negative prompt</span>
            <textarea
              value={form.negative_prompt}
              onChange={(e) => setForm({ ...form, negative_prompt: e.target.value })}
              rows={2}
              className={`${inputCls} resize-y`}
            />
          </label>
        </>
      ) : (
        <>
          <label className="block">
            <span className={labelCls}>OpenAI-compatible base URL</span>
            <input
              value={form.openai_base_url}
              onChange={(e) => setForm({ ...form, openai_base_url: e.target.value })}
              placeholder="http://127.0.0.1:8000/v1"
              className={inputCls}
            />
          </label>
          <label className="block">
            <span className={labelCls}>
              API key {loaded.openai_api_key ? `(current: ${loaded.openai_api_key})` : '(none set)'} — write-only
            </span>
            <div className="flex gap-2">
              <input
                type="password"
                value={form.openai_api_key}
                onChange={(e) => setForm({ ...form, openai_api_key: e.target.value })}
                placeholder={loaded.openai_api_key ? `Current: ${loaded.openai_api_key}` : 'Leave blank to keep current key'}
                className={inputCls}
              />
              {loaded.openai_api_key && (
                <button
                  onClick={clearKey}
                  className="shrink-0 rounded-lg border border-danger-900 px-3 text-xs text-danger-400 hover:bg-danger-950"
                >
                  Clear
                </button>
              )}
            </div>
          </label>
          <div className="grid grid-cols-2 gap-3">
            <label className="block">
              <span className={labelCls}>Modelo</span>
              <input
                value={form.openai_model}
                onChange={(e) => setForm({ ...form, openai_model: e.target.value })}
                placeholder="dall-e-3, sdxl…"
                className={inputCls}
              />
            </label>
            <label className="block">
              <span className={labelCls}>Tamanho (largura x altura)</span>
              <input
                value={form.openai_size}
                onChange={(e) => setForm({ ...form, openai_size: e.target.value })}
                placeholder="512x512"
                className={inputCls}
              />
            </label>
          </div>
        </>
      )}

      <label className="block">
        <span className={labelCls}>Style suffix</span>
        <input
          value={form.style_suffix}
          onChange={(e) => setForm({ ...form, style_suffix: e.target.value })}
          placeholder="appended to every image prompt (e.g. 'digital painting, cinematic light')"
          className={inputCls}
        />
      </label>

      <ImageAdvancedControls form={form} onChange={setForm} inputCls={inputCls} labelCls={labelCls} />

      <div className="flex justify-end">
        <button
          onClick={save}
          disabled={saving}
          className="rounded-lg bg-accent-pri px-4 py-2 text-sm font-semibold text-canvas hover:bg-accent-hov disabled:opacity-50"
        >
          {saving ? 'Salvando…' : 'Salvar imagem'}
        </button>
      </div>
    </div>
  )
}
