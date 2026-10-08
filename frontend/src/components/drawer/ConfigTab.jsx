import { useCallback, useEffect, useState } from 'react'
import { api } from '../../api.js'
import { useToast } from '../Toast.jsx'

const HEALTH_STYLE = {
  ok: 'bg-emerald-900 text-emerald-300',
  unreachable: 'bg-red-900 text-red-300',
  auth_error: 'bg-amber-900 text-amber-300',
  error: 'bg-red-900 text-red-300',
}

// Aba Config: GET/PUT /api/settings/llm (campos do contrato — api_key é
// write-only: em branco = mantém; botão Clear envia "" para remover) e
// status ao vivo de GET /api/health/llm (model_discovered etc.).
// v2: subsection "Generation" (temperature/top_p/repetition_penalty, também
// via settings/llm) e seção "Images" (GET/PUT /api/settings/image).
export default function ConfigTab() {
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
        base_url: s.base_url ?? '',
        model_alias: s.model_alias ?? 'auto',
        context_size: s.context_size ?? 8192,
        max_tokens: s.max_tokens ?? 300,
        timeout_seconds: s.timeout_seconds ?? 180,
        streaming: !!s.streaming,
        disable_thinking: !!s.disable_thinking,
        temperature: s.temperature ?? 0.85,
        top_p: s.top_p ?? 0.95,
        repetition_penalty: s.repetition_penalty ?? 1.08,
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
        repetition_penalty: Number(form.repetition_penalty),
      }
      if (form.api_key) payload.api_key = form.api_key // só envia se digitado
      const s = await api.putLlmSettings(payload)
      setSettings(s)
      toast('LLM settings saved.', 'ok')
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

  if (settings === null || form === null) return <p className="text-sm text-zinc-500">Loading…</p>

  const inputCls =
    'w-full rounded-lg border border-zinc-700 bg-zinc-800 px-3 py-2 text-sm outline-none focus:border-emerald-600'
  const labelCls = 'mb-1 block text-xs font-semibold uppercase tracking-wide text-zinc-500'

  return (
    <div className="flex flex-col gap-4">
      {health && (
        <div className="rounded-lg border border-zinc-800 bg-zinc-800/40 p-3 text-sm">
          <div className="flex items-center gap-2">
            <span className={`rounded-full px-2 py-0.5 text-xs font-semibold ${HEALTH_STYLE[health.status] ?? HEALTH_STYLE.error}`}>
              {health.status}
            </span>
            <span className="text-zinc-400">{health.provider}</span>
          </div>
          <div className="mt-2 text-xs text-zinc-500">
            <div>
              Model discovered:{' '}
              <span className="text-zinc-300">{health.model_discovered ?? settings.model_discovered ?? '—'}</span>
            </div>
            {health.details && <div className="mt-1 text-zinc-500">{health.details}</div>}
          </div>
        </div>
      )}

      <label className="block">
        <span className={labelCls}>Base URL (OpenAI-compatible)</span>
        <input value={form.base_url} onChange={(e) => setForm({ ...form, base_url: e.target.value })} className={inputCls} />
      </label>

      <label className="block">
        <span className={labelCls}>Model alias ("auto" = discover via /models)</span>
        <input value={form.model_alias} onChange={(e) => setForm({ ...form, model_alias: e.target.value })} className={inputCls} />
      </label>

      <label className="block">
        <span className={labelCls}>
          API key {settings.api_key ? `(current: ${settings.api_key})` : '(none set)'} — write-only
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
              className="shrink-0 rounded-lg border border-red-900 px-3 text-xs text-red-400 hover:bg-red-950"
            >
              Clear
            </button>
          )}
        </div>
      </label>

      <div className="grid grid-cols-2 gap-3">
        <label className="block">
          <span className={labelCls}>Context size</span>
          <input
            type="number"
            min={4096}
            max={131072}
            value={form.context_size}
            onChange={(e) => setForm({ ...form, context_size: e.target.value })}
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

      <label className="block">
        <span className={labelCls}>Response length (max tokens): {form.max_tokens}</span>
        <input
          type="range"
          min={100}
          max={600}
          step={10}
          value={form.max_tokens}
          onChange={(e) => setForm({ ...form, max_tokens: e.target.value })}
          className="w-full accent-emerald-600"
        />
        <div className="flex justify-between text-[10px] text-zinc-600">
          <span>100</span>
          <span>600</span>
        </div>
      </label>

      <div className="flex gap-5">
        <label className="flex items-center gap-2 text-sm text-zinc-300">
          <input
            type="checkbox"
            checked={form.streaming}
            onChange={(e) => setForm({ ...form, streaming: e.target.checked })}
            className="accent-emerald-600"
          />
          Streaming
        </label>
        <label className="flex items-center gap-2 text-sm text-zinc-300">
          <input
            type="checkbox"
            checked={form.disable_thinking}
            onChange={(e) => setForm({ ...form, disable_thinking: e.target.checked })}
            className="accent-emerald-600"
          />
          Disable thinking (Qwen3.x)
        </label>
      </div>

      {/* v2 — subsection Generation: sampler do LLM (settings/llm) */}
      <div className="flex flex-col gap-3 rounded-lg border border-zinc-800 bg-zinc-800/40 p-3">
        <h3 className="text-xs font-semibold uppercase tracking-wide text-zinc-500">Generation</h3>

        <label className="block">
          <span className={labelCls}>Temperature: {Number(form.temperature).toFixed(2)}</span>
          <input
            type="range"
            min={0}
            max={2}
            step={0.05}
            value={form.temperature}
            onChange={(e) => setForm({ ...form, temperature: e.target.value })}
            className="w-full accent-emerald-600"
          />
          <div className="flex justify-between text-[10px] text-zinc-600">
            <span>0.00 (focused)</span>
            <span>2.00 (wild)</span>
          </div>
        </label>

        <label className="block">
          <span className={labelCls}>Top-p: {Number(form.top_p).toFixed(2)}</span>
          <input
            type="range"
            min={0}
            max={1}
            step={0.05}
            value={form.top_p}
            onChange={(e) => setForm({ ...form, top_p: e.target.value })}
            className="w-full accent-emerald-600"
          />
          <div className="flex justify-between text-[10px] text-zinc-600">
            <span>0.00</span>
            <span>1.00</span>
          </div>
        </label>

        <label className="block">
          <span className={labelCls}>Repetition penalty: {Number(form.repetition_penalty).toFixed(2)}</span>
          <input
            type="range"
            min={1}
            max={2}
            step={0.05}
            value={form.repetition_penalty}
            onChange={(e) => setForm({ ...form, repetition_penalty: e.target.value })}
            className="w-full accent-emerald-600"
          />
          <div className="flex justify-between text-[10px] text-zinc-600">
            <span>1.00 (off)</span>
            <span>2.00</span>
          </div>
        </label>
      </div>

      <div className="flex justify-end">
        <button
          onClick={save}
          disabled={saving}
          className="rounded-lg bg-emerald-700 px-4 py-2 text-sm font-semibold text-white hover:bg-emerald-600 disabled:opacity-50"
        >
          {saving ? 'Saving…' : 'Save settings'}
        </button>
      </div>

      {/* v2 — seção Images: config do provedor de geração de imagens ("See") */}
      <ImageSettingsSection inputCls={inputCls} labelCls={labelCls} />
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
      }
      if (form.openai_api_key) payload.openai_api_key = form.openai_api_key // só envia se digitado
      setLoaded(await api.putImageSettings(payload))
      toast('Image settings saved.', 'ok')
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
      <div className="border-t border-zinc-800 pt-4">
        <h3 className="mb-2 text-xs font-semibold uppercase tracking-wide text-zinc-500">Images</h3>
        <p className="text-sm text-zinc-500">Loading…</p>
      </div>
    )
  }

  return (
    <div className="flex flex-col gap-4 border-t border-zinc-800 pt-4">
      <div>
        <h3 className="text-xs font-semibold uppercase tracking-wide text-zinc-500">Images</h3>
        <p className="mt-0.5 text-xs text-zinc-600">
          Provider used by the "See" action to illustrate the current scene.
        </p>
      </div>

      <div className="flex gap-5">
        <label className="flex items-center gap-2 text-sm text-zinc-300">
          <input
            type="checkbox"
            checked={form.enabled}
            onChange={(e) => setForm({ ...form, enabled: e.target.checked })}
            className="accent-emerald-600"
          />
          Enabled
        </label>
        <label className="flex flex-1 items-center gap-2 text-sm text-zinc-300">
          <span className="text-xs font-semibold uppercase tracking-wide text-zinc-500">Provider</span>
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
              <span className={labelCls}>Steps</span>
              <input
                type="number"
                min={1}
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
                  className="shrink-0 rounded-lg border border-red-900 px-3 text-xs text-red-400 hover:bg-red-950"
                >
                  Clear
                </button>
              )}
            </div>
          </label>
          <div className="grid grid-cols-2 gap-3">
            <label className="block">
              <span className={labelCls}>Model</span>
              <input
                value={form.openai_model}
                onChange={(e) => setForm({ ...form, openai_model: e.target.value })}
                placeholder="dall-e-3, sdxl…"
                className={inputCls}
              />
            </label>
            <label className="block">
              <span className={labelCls}>Size</span>
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

      <div className="flex justify-end">
        <button
          onClick={save}
          disabled={saving}
          className="rounded-lg bg-emerald-700 px-4 py-2 text-sm font-semibold text-white hover:bg-emerald-600 disabled:opacity-50"
        >
          {saving ? 'Saving…' : 'Save image settings'}
        </button>
      </div>
    </div>
  )
}
