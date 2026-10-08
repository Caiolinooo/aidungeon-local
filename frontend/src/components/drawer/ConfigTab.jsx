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
// Nota: o contrato v1 não expõe temperature/top_p/repetition_penalty —
// sampler fixo do config/settings.json (0.85 / 0.95 / 1.08).
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

      <p className="rounded-lg border border-zinc-800 bg-zinc-800/40 p-2 text-xs text-zinc-500">
        Sampler (temperature 0.85 · top_p 0.95 · repetition penalty 1.08) is fixed in
        config/settings.json for v1 — not exposed by the API contract.
      </p>

      <div className="flex justify-end">
        <button
          onClick={save}
          disabled={saving}
          className="rounded-lg bg-emerald-700 px-4 py-2 text-sm font-semibold text-white hover:bg-emerald-600 disabled:opacity-50"
        >
          {saving ? 'Saving…' : 'Save settings'}
        </button>
      </div>
    </div>
  )
}
