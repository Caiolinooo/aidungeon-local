import React, { useEffect, useState } from 'react'
import { Link, useNavigate } from 'react-router-dom'
import { api } from '../api.js'
import { useToast } from '../components/Toast.jsx'
import { useTheme } from '../themeContext.jsx'
import ReaderPreferencesModal from '../components/ReaderPreferencesModal.jsx'
import {
  ArrowLeft,
  Cpu,
  Image as ImageIcon,
  Volume2,
  Brain,
  Code2,
  Save,
  RefreshCw,
  Play,
  Check,
  AlertCircle,
  Sliders,
  Sparkles,
  BookOpen,
} from 'lucide-react'

const TABS = [
  { id: 'llm', label: 'Modelo e amostragem', icon: Cpu },
  { id: 'image', label: 'Geração de imagem', icon: ImageIcon },
  { id: 'voice', label: 'Voz e fala', icon: Volume2 },
  { id: 'memory', label: 'Memória e scripts', icon: Brain },
]

export default function Settings() {
  const navigate = useNavigate()
  const toast = useToast()
  const { theme } = useTheme()
  const [activeTab, setActiveTab] = useState('llm')
  const [showReaderModal, setShowReaderModal] = useState(false)
  const [loading, setLoading] = useState(true)
  const [saving, setSaving] = useState(false)

  // LLM Settings State
  const [llmForm, setLlmForm] = useState({
    provider: 'llamacpp',
    base_url: '',
    api_key: '',
    model_alias: 'auto',
    context_size: 65536,
    max_tokens: 300,
    streaming: true,
    disable_thinking: true,
    timeout_seconds: 180,
    temperature: 0.85,
    top_p: 1.0,
    min_p: 0.08,
    repetition_penalty: 1.02,
    presence_penalty: 0.15,
    frequency_penalty: 0.10,
    dry_multiplier: 0.8,
    dry_base: 1.75,
    dry_allowed_length: 2,
    dry_penalty_last_n: -1,
    retry_temperature_step: 0.05,
    max_retry_temperature: 1.15,
  })
  const [llmHealth, setLlmHealth] = useState(null)

  // Image Settings State
  const [imageForm, setImageForm] = useState({
    enabled: true,
    provider: 'sdwebui',
    sdwebui_base_url: 'http://127.0.0.1:7860',
    sdwebui_steps: 30,
    sdwebui_width: 1024,
    sdwebui_height: 1024,
    negative_prompt: '',
    style_suffix: '',
    openai_base_url: 'https://api.openai.com/v1',
    openai_api_key: '',
    openai_model: 'dall-e-3',
    openai_size: '1024x1024',
  })

  // Voice Settings State
  const [voiceForm, setVoiceForm] = useState({
    enabled: true,
    base_url: 'http://127.0.0.1:8000',
    tts_model: 'supertonic',
    stt_model: 'whisper-large-v3',
    narrator_voice: 'M1',
    voices: ['M1', 'M2', 'M3', 'F1', 'F2', 'F3'],
    timeout_seconds: 30,
    api_key: '',
  })
  const [previewingVoice, setPreviewingVoice] = useState(null)

  // Embeddings & Scripting State
  const [embeddingsForm, setEmbeddingsForm] = useState({
    enabled: true,
    base_url: 'http://127.0.0.1:8081/v1',
    model: 'Qwen3-Embedding-0.6B',
    timeout_seconds: 30,
  })
  const [scriptingForm, setScriptingForm] = useState({
    input_modifier: '',
    context_modifier: '',
    output_modifier: '',
  })
  const contextSizeMax = llmForm.context_size_max ?? 106496
  const contextPresets = [
    { label: '32k', val: 32768 },
    { label: '64k', val: 65536 },
    { label: '128k', val: 131072 },
  ]
    .filter((preset) => preset.val <= contextSizeMax)
    .concat([{ label: `${Math.round(contextSizeMax / 1024)}k Max`, val: contextSizeMax }])

  useEffect(() => {
    async function loadAllSettings() {
      setLoading(true)
      try {
        const [llm, img, vce, emb, scr, health] = await Promise.all([
          api.getLlmSettings().catch(() => ({})),
          api.getImageSettings().catch(() => ({})),
          api.getVoiceSettings().catch(() => ({})),
          api.getEmbeddingsSettings().catch(() => ({})),
          api.getScriptingSettings().catch(() => ({})),
          api.healthLlm().catch(() => null),
        ])

        if (llm && Object.keys(llm).length) {
          setLlmForm((prev) => ({
            ...prev,
            ...llm,
            context_size: Math.min(llm.context_size_max ?? 106496, llm.context_size || 65536),
          }))
        }
        if (img && Object.keys(img).length) setImageForm((prev) => ({ ...prev, ...img }))
        if (vce && Object.keys(vce).length) setVoiceForm((prev) => ({ ...prev, ...vce }))
        if (emb && Object.keys(emb).length) setEmbeddingsForm((prev) => ({ ...prev, ...emb }))
        if (scr && Object.keys(scr).length) setScriptingForm((prev) => ({ ...prev, ...scr }))
        if (health) setLlmHealth(health)
      } catch (e) {
        toast('Falha ao carregar algumas configurações: ' + e.message)
      } finally {
        setLoading(false)
      }
    }
    loadAllSettings()
  }, [toast])

  // Save current active tab settings
  const handleSave = async () => {
    setSaving(true)
    try {
      if (activeTab === 'llm') {
        const payload = {
          ...llmForm,
          context_size: Math.min(contextSizeMax, Number(llmForm.context_size) || 65536),
          max_tokens: Number(llmForm.max_tokens),
          temperature: Number(llmForm.temperature),
          top_p: Number(llmForm.top_p),
          min_p: Number(llmForm.min_p),
          repetition_penalty: Number(llmForm.repetition_penalty),
          presence_penalty: Number(llmForm.presence_penalty),
          frequency_penalty: Number(llmForm.frequency_penalty),
          dry_multiplier: Number(llmForm.dry_multiplier),
          dry_base: Number(llmForm.dry_base),
          dry_allowed_length: Number(llmForm.dry_allowed_length),
          dry_penalty_last_n: Number(llmForm.dry_penalty_last_n),
          retry_temperature_step: Number(llmForm.retry_temperature_step),
          max_retry_temperature: Number(llmForm.max_retry_temperature),
          timeout_seconds: Number(llmForm.timeout_seconds),
        }
        const updated = await api.putLlmSettings(payload)
        setLlmForm((prev) => ({
          ...prev,
          ...updated,
          context_size: Math.min(updated.context_size_max ?? contextSizeMax, updated.context_size),
        }))
        toast('Configurações do modelo salvas.', 'ok')
      } else if (activeTab === 'image') {
        const payload = {
          ...imageForm,
          sdwebui_steps: Number(imageForm.sdwebui_steps),
          sdwebui_width: Number(imageForm.sdwebui_width),
          sdwebui_height: Number(imageForm.sdwebui_height),
        }
        const updated = await api.putImageSettings(payload)
        setImageForm((prev) => ({ ...prev, ...updated }))
        toast('Configurações de imagem salvas.', 'ok')
      } else if (activeTab === 'voice') {
        const payload = {
          ...voiceForm,
          timeout_seconds: Number(voiceForm.timeout_seconds),
        }
        const updated = await api.putVoiceSettings(payload)
        setVoiceForm((prev) => ({ ...prev, ...updated }))
        toast('Configurações de voz salvas.', 'ok')
      } else if (activeTab === 'memory') {
        const embPayload = {
          ...embeddingsForm,
          timeout_seconds: Number(embeddingsForm.timeout_seconds),
        }
        await Promise.all([
          api.putEmbeddingsSettings(embPayload),
          api.putScriptingSettings(scriptingForm),
        ])
        toast('Configurações de memória salvas.', 'ok')
      }
    } catch (e) {
      toast('Falha ao salvar: ' + e.message)
    } finally {
      setSaving(false)
    }
  }

  // Voice Preview test
  const handlePreviewVoice = async (voiceKey) => {
    setPreviewingVoice(voiceKey)
    try {
      const sampleText = `Olá, aventureiro! Esta é a prévia da voz ${voiceKey}. Pronto para explorar?`
      const blob = await api.voice.tts(sampleText, voiceKey)
      const audioUrl = URL.createObjectURL(blob)
      const audio = new Audio(audioUrl)
      audio.onended = () => {
        setPreviewingVoice(null)
        URL.revokeObjectURL(audioUrl)
      }
      audio.onerror = () => {
        setPreviewingVoice(null)
        URL.revokeObjectURL(audioUrl)
        toast('Não foi possível ouvir a prévia da voz.')
      }
      await audio.play()
    } catch (e) {
      setPreviewingVoice(null)
      toast('Erro na prévia da voz: ' + e.message)
    }
  }

  const inputCls =
    'w-full rounded-xl border border-subtle bg-surface px-3 py-2 text-xs text-primary outline-none focus:border-accent-pri transition'
  const labelCls = 'mb-1 block text-xs font-semibold uppercase tracking-wider text-muted'

  return (
    <div className="flex h-full flex-col bg-canvas text-primary">
      {/* Top Header */}
      <header className="flex h-14 shrink-0 items-center justify-between border-b border-subtle bg-surface px-4 sm:px-6">
        <div className="flex items-center gap-3">
          <button
            onClick={() => navigate(-1)}
            aria-label="Voltar"
            className="rounded-xl border border-subtle p-2 text-secondary hover:text-primary hover:bg-elevated transition"
          >
            <ArrowLeft className="w-4 h-4" />
          </button>
          <div>
            <h1 className="text-base font-bold tracking-tight text-primary flex items-center gap-2">
              <Sliders className="w-4 h-4 text-accent-pri" />
              <span>Configurações</span>
            </h1>
          </div>
        </div>

        <div className="flex items-center gap-2">
          <button
            onClick={() => setShowReaderModal(true)}
            title="Preferências de leitura e tema"
            className="flex items-center gap-1.5 rounded-xl border border-subtle bg-elevated px-3 py-1.5 text-xs text-secondary hover:text-primary transition"
          >
            <BookOpen className="w-3.5 h-3.5 text-accent-pri" />
            <span className="hidden sm:inline">Tema / tipografia</span>
          </button>

          <button
            onClick={handleSave}
            disabled={saving || loading}
            className="flex items-center gap-1.5 rounded-xl bg-accent-pri px-4 py-1.5 text-xs font-semibold text-white shadow hover:bg-accent-hov transition disabled:opacity-50"
          >
            <Save className="w-3.5 h-3.5" />
            <span>{saving ? 'Salvando…' : 'Salvar'}</span>
          </button>
        </div>
      </header>

      {/* Main Content Area */}
      <div className="flex flex-1 overflow-hidden flex-col md:flex-row">
        {/* Sidebar Tabs */}
        <nav className="w-full md:w-64 border-b md:border-b-0 md:border-r border-subtle bg-surface/50 p-2 sm:p-3 flex md:flex-col gap-1 overflow-x-auto md:overflow-x-visible shrink-0">
          {TABS.map((tab) => {
            const Icon = tab.icon
            const active = activeTab === tab.id
            return (
              <button
                key={tab.id}
                onClick={() => setActiveTab(tab.id)}
                className={`flex items-center gap-2.5 rounded-xl px-3 py-2.5 text-xs font-medium transition text-left whitespace-nowrap shrink-0 ${
                  active
                    ? 'bg-accent-pri text-white font-semibold shadow-sm'
                    : 'text-secondary hover:text-primary hover:bg-elevated/60'
                }`}
              >
                <Icon className="w-4 h-4 shrink-0" />
                <span>{tab.label}</span>
              </button>
            )
          })}
        </nav>

        {/* Tab Body */}
        <main className="flex-1 overflow-y-auto p-4 sm:p-6 max-w-4xl">
          {loading ? (
            <div className="flex h-64 items-center justify-center text-xs text-muted">
              Carregando configurações…
            </div>
          ) : (
            <>
              {/* TAB 1: LLM & Samplers */}
              {activeTab === 'llm' && (
                <div className="space-y-6">
                  <div className="border-b border-subtle pb-3 flex items-center justify-between">
                    <div>
                      <h2 className="text-sm font-bold uppercase tracking-wider text-primary">
                        Modelo e amostragem
                      </h2>
                      <p className="text-xs text-secondary mt-0.5">
                        Servidor do modelo, orçamento de contexto e parâmetros da geração.
                      </p>
                    </div>
                    {llmHealth && (
                      <span className="flex items-center gap-1 rounded-full bg-accent-950/60 border border-accent-800 px-2.5 py-1 text-[11px] font-mono text-accent-pri">
                        <span className="w-1.5 h-1.5 rounded-full bg-accent-500 animate-pulse" />
                        {llmHealth.status === 'ok' ? 'Online' : 'Warning'}
                      </span>
                    )}
                  </div>

                  {/* Server Connection */}
                  <div className="rounded-2xl border border-subtle bg-surface p-4 space-y-4">
                    <h3 className="text-xs font-semibold uppercase tracking-wider text-muted">
                      Endpoint & Model
                    </h3>
                    <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
                      <div>
                        <label className={labelCls}>Base URL</label>
                        <input
                          type="text"
                          value={llmForm.base_url}
                          onChange={(e) => setLlmForm({ ...llmForm, base_url: e.target.value })}
                          placeholder="http://127.0.0.1:1234/v1"
                          className={inputCls}
                        />
                      </div>
                      <div>
                        <label className={labelCls}>Apelido do modelo</label>
                        <input
                          type="text"
                          value={llmForm.model_alias}
                          onChange={(e) => setLlmForm({ ...llmForm, model_alias: e.target.value })}
                          placeholder="auto (ou o nome exato do modelo)"
                          className={inputCls}
                        />
                      </div>
                    </div>

                    <div>
                      <label className={labelCls}>Chave da API (opcional / oculta)</label>
                      <input
                        type="password"
                        value={llmForm.api_key || ''}
                        onChange={(e) => setLlmForm({ ...llmForm, api_key: e.target.value })}
                        placeholder="Deixe em branco para usar a chave atual ou o .env"
                        className={inputCls}
                      />
                    </div>
                  </div>

                  {/* Context Size & Long-Horizon Durability */}
                  <div className="rounded-2xl border border-subtle bg-surface p-4 space-y-4">
                    <div className="flex flex-wrap items-center justify-between gap-2">
                      <div>
                        <h3 className="text-xs font-semibold uppercase tracking-wider text-muted">
                          Janela de contexto (até {contextSizeMax.toLocaleString()} tokens)
                        </h3>
                        <p className="text-[11px] text-secondary mt-0.5">
                          A escala do cânone e a âncora de continuidade mantêm campanhas longas coerentes.
                        </p>
                      </div>
                      <div className="flex items-center gap-1.5">
                        {contextPresets.map((preset) => (
                          <button
                            key={preset.val}
                            type="button"
                            onClick={() => setLlmForm({ ...llmForm, context_size: preset.val })}
                            className={`rounded-lg px-2 py-1 text-[10px] font-mono transition ${
                              llmForm.context_size === preset.val
                                ? 'bg-accent-pri text-white font-bold'
                                : 'border border-subtle bg-elevated text-secondary hover:text-primary'
                            }`}
                          >
                            {preset.label}
                          </button>
                        ))}
                        <span className="ml-1 font-mono text-xs font-bold text-accent-pri">
                          {llmForm.context_size.toLocaleString()} tk
                        </span>
                      </div>
                    </div>

                    <div>
                      <input
                        type="range"
                        min="4096"
                        max={contextSizeMax}
                        step="4096"
                        value={llmForm.context_size}
                        onChange={(e) =>
                          setLlmForm({ ...llmForm, context_size: Math.min(contextSizeMax, Number(e.target.value)) })
                        }
                        className="w-full accent-accent-500 h-1.5 bg-elevated rounded-lg appearance-none cursor-pointer"
                      />
                      <div className="flex justify-between text-[10px] text-muted font-mono mt-1">
                        <span>4.096 mín.</span>
                        <span>65.536 (padrão 64k)</span>
                        <span className="font-semibold text-accent-pri">{contextSizeMax.toLocaleString()} máx. por slot</span>
                      </div>
                    </div>

                    <div className="grid grid-cols-1 sm:grid-cols-3 gap-4 pt-2">
                      <div>
                        <label className={labelCls}>Máximo de tokens por turno ({llmForm.max_tokens})</label>
                        <input
                          type="range"
                          min="100"
                          max="2048"
                          step="16"
                          value={llmForm.max_tokens}
                          onChange={(e) => setLlmForm({ ...llmForm, max_tokens: Number(e.target.value) })}
                          className="w-full accent-accent-500 h-1.5 bg-elevated rounded-lg appearance-none cursor-pointer"
                        />
                      </div>
                      <div>
                        <label className={labelCls}>Tempo limite (segundos)</label>
                        <input
                          type="number"
                          min="10"
                          max="600"
                          value={llmForm.timeout_seconds}
                          onChange={(e) => setLlmForm({ ...llmForm, timeout_seconds: Number(e.target.value) })}
                          className={inputCls}
                        />
                      </div>
                      <div className="flex flex-col justify-end gap-2 pb-1">
                        <label className="flex items-center gap-2 text-xs text-secondary cursor-pointer">
                          <input
                            type="checkbox"
                            checked={llmForm.streaming}
                            onChange={(e) => setLlmForm({ ...llmForm, streaming: e.target.checked })}
                            className="accent-accent-600 rounded"
                          />
                          <span>Transmitir tokens</span>
                        </label>
                        <label className="flex items-center gap-2 text-xs text-secondary cursor-pointer">
                          <input
                            type="checkbox"
                            checked={llmForm.disable_thinking}
                            onChange={(e) => setLlmForm({ ...llmForm, disable_thinking: e.target.checked })}
                            className="accent-accent-600 rounded"
                          />
                          <span>Desligar o raciocínio (Qwen3)</span>
                        </label>
                      </div>
                    </div>
                  </div>

                  {/* Samplers & Penalties */}
                  <div className="rounded-2xl border border-subtle bg-surface p-4 space-y-4">
                    <h3 className="text-xs font-semibold uppercase tracking-wider text-muted">
                      Sampler Tuning & DRY Repetition
                    </h3>
                    <div className="grid grid-cols-1 sm:grid-cols-3 gap-4">
                      <div>
                        <label className={labelCls}>Temperature ({llmForm.temperature})</label>
                        <input
                          type="range"
                          min="0.1"
                          max="1.5"
                          step="0.05"
                          value={llmForm.temperature}
                          onChange={(e) => setLlmForm({ ...llmForm, temperature: Number(e.target.value) })}
                          className="w-full accent-accent-500 h-1.5 bg-elevated rounded-lg appearance-none cursor-pointer"
                        />
                      </div>
                      <div>
                        <label className={labelCls}>Top P ({llmForm.top_p})</label>
                        <input
                          type="range"
                          min="0.1"
                          max="1.0"
                          step="0.05"
                          value={llmForm.top_p}
                          onChange={(e) => setLlmForm({ ...llmForm, top_p: Number(e.target.value) })}
                          className="w-full accent-accent-500 h-1.5 bg-elevated rounded-lg appearance-none cursor-pointer"
                        />
                      </div>
                      <div>
                        <label className={labelCls}>Min P ({llmForm.min_p})</label>
                        <input
                          type="range"
                          min="0.01"
                          max="0.3"
                          step="0.01"
                          value={llmForm.min_p}
                          onChange={(e) => setLlmForm({ ...llmForm, min_p: Number(e.target.value) })}
                          className="w-full accent-accent-500 h-1.5 bg-elevated rounded-lg appearance-none cursor-pointer"
                        />
                      </div>
                    </div>

                    <div className="grid grid-cols-1 sm:grid-cols-3 gap-4 pt-2">
                      <div>
                        <label className={labelCls}>Presence Penalty ({llmForm.presence_penalty})</label>
                        <input
                          type="range"
                          min="0.0"
                          max="1.0"
                          step="0.05"
                          value={llmForm.presence_penalty}
                          onChange={(e) => setLlmForm({ ...llmForm, presence_penalty: Number(e.target.value) })}
                          className="w-full accent-accent-500 h-1.5 bg-elevated rounded-lg appearance-none cursor-pointer"
                        />
                      </div>
                      <div>
                        <label className={labelCls}>Frequency Penalty ({llmForm.frequency_penalty})</label>
                        <input
                          type="range"
                          min="0.0"
                          max="1.0"
                          step="0.05"
                          value={llmForm.frequency_penalty}
                          onChange={(e) => setLlmForm({ ...llmForm, frequency_penalty: Number(e.target.value) })}
                          className="w-full accent-accent-500 h-1.5 bg-elevated rounded-lg appearance-none cursor-pointer"
                        />
                      </div>
                      <div>
                        <label className={labelCls}>Repetition Penalty ({llmForm.repetition_penalty})</label>
                        <input
                          type="range"
                          min="1.0"
                          max="1.3"
                          step="0.01"
                          value={llmForm.repetition_penalty}
                          onChange={(e) => setLlmForm({ ...llmForm, repetition_penalty: Number(e.target.value) })}
                          className="w-full accent-accent-500 h-1.5 bg-elevated rounded-lg appearance-none cursor-pointer"
                        />
                      </div>
                    </div>

                    <div className="grid grid-cols-1 sm:grid-cols-3 gap-4 pt-2 border-t border-subtle">
                      <div>
                        <label className={labelCls}>DRY Multiplier ({llmForm.dry_multiplier})</label>
                        <input
                          type="number"
                          step="0.1"
                          value={llmForm.dry_multiplier}
                          onChange={(e) => setLlmForm({ ...llmForm, dry_multiplier: Number(e.target.value) })}
                          className={inputCls}
                        />
                      </div>
                      <div>
                        <label className={labelCls}>DRY Base ({llmForm.dry_base})</label>
                        <input
                          type="number"
                          step="0.05"
                          value={llmForm.dry_base}
                          onChange={(e) => setLlmForm({ ...llmForm, dry_base: Number(e.target.value) })}
                          className={inputCls}
                        />
                      </div>
                      <div>
                        <label className={labelCls}>DRY Allowed Length</label>
                        <input
                          type="number"
                          value={llmForm.dry_allowed_length}
                          onChange={(e) => setLlmForm({ ...llmForm, dry_allowed_length: Number(e.target.value) })}
                          className={inputCls}
                        />
                      </div>
                    </div>
                  </div>
                </div>
              )}

              {/* TAB 2: Image Generation */}
              {activeTab === 'image' && (
                <div className="space-y-6">
                  <div className="border-b border-subtle pb-3">
                    <h2 className="text-sm font-bold uppercase tracking-wider text-primary">
                      Geração de imagem
                    </h2>
                    <p className="text-xs text-secondary mt-0.5">
                      SDXL WebUI ou um endpoint compatível com OpenAI, para pinturas de cena e retratos.
                    </p>
                  </div>

                  <div className="rounded-2xl border border-subtle bg-surface p-4 space-y-4">
                    <div className="flex items-center justify-between">
                      <label className="flex items-center gap-2 text-xs font-semibold text-primary cursor-pointer">
                        <input
                          type="checkbox"
                          checked={imageForm.enabled}
                          onChange={(e) => setImageForm({ ...imageForm, enabled: e.target.checked })}
                          className="accent-accent-600 rounded"
                        />
                        <span>Ativar geração de imagem (cena e retratos)</span>
                      </label>

                      <div className="flex rounded-lg border border-subtle overflow-hidden p-0.5 bg-elevated">
                        <button
                          type="button"
                          onClick={() => setImageForm({ ...imageForm, provider: 'sdwebui' })}
                          className={`px-3 py-1 text-xs font-medium rounded-md transition ${
                            imageForm.provider === 'sdwebui' ? 'bg-surface text-primary shadow font-semibold' : 'text-secondary'
                          }`}
                        >
                          SD WebUI (Local)
                        </button>
                        <button
                          type="button"
                          onClick={() => setImageForm({ ...imageForm, provider: 'openai_compatible' })}
                          className={`px-3 py-1 text-xs font-medium rounded-md transition ${
                            imageForm.provider === 'openai_compatible' ? 'bg-surface text-primary shadow font-semibold' : 'text-secondary'
                          }`}
                        >
                          OpenAI Compatible
                        </button>
                      </div>
                    </div>

                    {imageForm.provider === 'sdwebui' ? (
                      <div className="space-y-4 pt-2">
                        <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
                          <div>
                            <label className={labelCls}>URL base do SD WebUI</label>
                            <input
                              type="text"
                              value={imageForm.sdwebui_base_url}
                              onChange={(e) => setImageForm({ ...imageForm, sdwebui_base_url: e.target.value })}
                              placeholder="http://127.0.0.1:7860"
                              className={inputCls}
                            />
                          </div>
                          <div>
                            <label className={labelCls}>Passos de inferência (30 recomendado)</label>
                            <input
                              type="number"
                              min="10"
                              max="100"
                              value={imageForm.sdwebui_steps}
                              onChange={(e) => setImageForm({ ...imageForm, sdwebui_steps: Number(e.target.value) })}
                              className={inputCls}
                            />
                          </div>
                        </div>

                        <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
                          <div>
                            <label className={labelCls}>Largura (1024)</label>
                            <input
                              type="number"
                              value={imageForm.sdwebui_width}
                              onChange={(e) => setImageForm({ ...imageForm, sdwebui_width: Number(e.target.value) })}
                              className={inputCls}
                            />
                          </div>
                          <div>
                            <label className={labelCls}>Altura (1024)</label>
                            <input
                              type="number"
                              value={imageForm.sdwebui_height}
                              onChange={(e) => setImageForm({ ...imageForm, sdwebui_height: Number(e.target.value) })}
                              className={inputCls}
                            />
                          </div>
                        </div>

                        <div>
                          <label className={labelCls}>Prompt negativo padrão</label>
                          <textarea
                            rows={3}
                            value={imageForm.negative_prompt}
                            onChange={(e) => setImageForm({ ...imageForm, negative_prompt: e.target.value })}
                            placeholder="text, watermark, signature, low quality, deformed, blurry..."
                            className={inputCls}
                          />
                        </div>

                        <div>
                          <label className={labelCls}>Sufixo de estilo</label>
                          <input
                            type="text"
                            value={imageForm.style_suffix || ''}
                            onChange={(e) => setImageForm({ ...imageForm, style_suffix: e.target.value })}
                            placeholder="e.g. masterpiece, grim fantasy oil painting, cinematic lighting"
                            className={inputCls}
                          />
                        </div>
                      </div>
                    ) : (
                      <div className="space-y-4 pt-2">
                        <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
                          <div>
                            <label className={labelCls}>Endpoint OpenAI</label>
                            <input
                              type="text"
                              value={imageForm.openai_base_url}
                              onChange={(e) => setImageForm({ ...imageForm, openai_base_url: e.target.value })}
                              placeholder="https://api.openai.com/v1"
                              className={inputCls}
                            />
                          </div>
                          <div>
                            <label className={labelCls}>Modelo</label>
                            <input
                              type="text"
                              value={imageForm.openai_model}
                              onChange={(e) => setImageForm({ ...imageForm, openai_model: e.target.value })}
                              placeholder="dall-e-3"
                              className={inputCls}
                            />
                          </div>
                        </div>
                        <div>
                          <label className={labelCls}>Chave da API OpenAI</label>
                          <input
                            type="password"
                            value={imageForm.openai_api_key}
                            onChange={(e) => setImageForm({ ...imageForm, openai_api_key: e.target.value })}
                            placeholder="sk-..."
                            className={inputCls}
                          />
                        </div>
                      </div>
                    )}
                  </div>
                </div>
              )}

              {/* TAB 3: Voice & Speech */}
              {activeTab === 'voice' && (
                <div className="space-y-6">
                  <div className="border-b border-subtle pb-3">
                    <h2 className="text-sm font-bold uppercase tracking-wider text-primary">
                      Voz e fala
                    </h2>
                    <p className="text-xs text-secondary mt-0.5">
                      Fala (Supertonic / TTS) e transcrição (Whisper).
                    </p>
                  </div>

                  <div className="rounded-2xl border border-subtle bg-surface p-4 space-y-4">
                    <label className="flex items-center gap-2 text-xs font-semibold text-primary cursor-pointer">
                      <input
                        type="checkbox"
                        checked={voiceForm.enabled}
                        onChange={(e) => setVoiceForm({ ...voiceForm, enabled: e.target.checked })}
                        className="accent-accent-600 rounded"
                      />
                      <span>Ativar o motor de voz</span>
                    </label>

                    <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
                      <div>
                        <label className={labelCls}>URL base do serviço de voz</label>
                        <input
                          type="text"
                          value={voiceForm.base_url}
                          onChange={(e) => setVoiceForm({ ...voiceForm, base_url: e.target.value })}
                          placeholder="http://127.0.0.1:8000"
                          className={inputCls}
                        />
                      </div>
                      <div>
                        <label className={labelCls}>Voz padrão do narrador</label>
                        <select
                          value={voiceForm.narrator_voice}
                          onChange={(e) => setVoiceForm({ ...voiceForm, narrator_voice: e.target.value })}
                          className={inputCls}
                        >
                          {(voiceForm.voices || ['M1', 'M2', 'M3', 'F1', 'F2', 'F3']).map((v) => (
                            <option key={v} value={v}>
                              {v} ({v.startsWith('M') ? 'Male' : 'Female'})
                            </option>
                          ))}
                        </select>
                      </div>
                    </div>

                    <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
                      <div>
                        <label className={labelCls}>Modelo de voz</label>
                        <input
                          type="text"
                          value={voiceForm.tts_model}
                          onChange={(e) => setVoiceForm({ ...voiceForm, tts_model: e.target.value })}
                          className={inputCls}
                        />
                      </div>
                      <div>
                        <label className={labelCls}>Modelo de transcrição (Whisper)</label>
                        <input
                          type="text"
                          value={voiceForm.stt_model}
                          onChange={(e) => setVoiceForm({ ...voiceForm, stt_model: e.target.value })}
                          className={inputCls}
                        />
                      </div>
                    </div>

                    {/* Interactive Voice Audition Picker */}
                    <div className="pt-3 border-t border-subtle">
                      <label className={labelCls}>Prévia da voz</label>
                      <div className="grid grid-cols-2 sm:grid-cols-3 md:grid-cols-6 gap-2 mt-2">
                        {(voiceForm.voices || ['M1', 'M2', 'M3', 'F1', 'F2', 'F3']).map((voiceKey) => {
                          const isPreviewing = previewingVoice === voiceKey
                          const isSelected = voiceForm.narrator_voice === voiceKey
                          return (
                            <div
                              key={voiceKey}
                              className={`flex flex-col items-center justify-center p-3 rounded-xl border transition ${
                                isSelected
                                  ? 'border-accent-pri bg-elevated ring-1 ring-accent-pri'
                                  : 'border-subtle bg-surface hover:bg-elevated/50'
                              }`}
                            >
                              <span className="text-xs font-bold text-primary">{voiceKey}</span>
                              <span className="text-[10px] text-muted">
                                {voiceKey.startsWith('M') ? 'Male' : 'Female'}
                              </span>
                              <button
                                type="button"
                                onClick={() => handlePreviewVoice(voiceKey)}
                                disabled={isPreviewing}
                                className="mt-2 flex items-center gap-1 rounded-lg bg-accent-pri/20 border border-accent-pri/40 px-2 py-1 text-[10px] font-semibold text-accent-pri hover:bg-accent-pri/30 transition disabled:opacity-50"
                              >
                                <Play className={`w-2.5 h-2.5 ${isPreviewing ? 'animate-spin' : ''}`} />
                                <span>{isPreviewing ? 'Playing…' : 'Sample'}</span>
                              </button>
                            </div>
                          )
                        })}
                      </div>
                    </div>
                  </div>
                </div>
              )}

              {/* TAB 4: Memory & Scripting */}
              {activeTab === 'memory' && (
                <div className="space-y-6">
                  <div className="border-b border-subtle pb-3">
                    <h2 className="text-sm font-bold uppercase tracking-wider text-primary">
                      Embeddings de memória e scripts isolados
                    </h2>
                    <p className="text-xs text-secondary mt-0.5">
                      Busca vetorial e ganchos de script Python isolados.
                    </p>
                  </div>

                  {/* Embeddings Vector Engine */}
                  <div className="rounded-2xl border border-subtle bg-surface p-4 space-y-4">
                    <h3 className="text-xs font-semibold uppercase tracking-wider text-muted">
                      Semantic Embeddings (Retrieval-Augmented Context)
                    </h3>
                    <label className="flex items-center gap-2 text-xs font-semibold text-primary cursor-pointer">
                      <input
                        type="checkbox"
                        checked={embeddingsForm.enabled}
                        onChange={(e) => setEmbeddingsForm({ ...embeddingsForm, enabled: e.target.checked })}
                        className="accent-accent-600 rounded"
                      />
                      <span>Ativar embeddings vetoriais</span>
                    </label>

                    <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
                      <div>
                        <label className={labelCls}>URL base dos embeddings</label>
                        <input
                          type="text"
                          value={embeddingsForm.base_url}
                          onChange={(e) => setEmbeddingsForm({ ...embeddingsForm, base_url: e.target.value })}
                          placeholder="http://127.0.0.1:8081/v1"
                          className={inputCls}
                        />
                      </div>
                      <div>
                        <label className={labelCls}>Modelo de embedding</label>
                        <input
                          type="text"
                          value={embeddingsForm.model}
                          onChange={(e) => setEmbeddingsForm({ ...embeddingsForm, model: e.target.value })}
                          placeholder="Qwen3-Embedding-0.6B"
                          className={inputCls}
                        />
                      </div>
                    </div>
                  </div>

                  {/* Sandboxed Scripting Hooks */}
                  <div className="rounded-2xl border border-subtle bg-surface p-4 space-y-4">
                    <div>
                      <h3 className="text-xs font-semibold uppercase tracking-wider text-muted flex items-center gap-2">
                        <Code2 className="w-4 h-4 text-accent-pri" />
                        <span>Ganchos de script Python isolados</span>
                      </h3>
                      <p className="text-[11px] text-secondary mt-1">
                        Roda num processo isolado, com inspeção da árvore, limite de 1 segundo e recuo seguro se falhar.
                      </p>
                    </div>

                    <div className="space-y-4">
                      <div>
                        <label className={labelCls}>
                          Modificador de entrada (altera a ação do jogador antes do motor)
                        </label>
                        <textarea
                          rows={4}
                          value={scriptingForm.input_modifier}
                          onChange={(e) =>
                            setScriptingForm({ ...scriptingForm, input_modifier: e.target.value })
                          }
                          placeholder="# Example:&#10;def modify(action):&#10;    action['text'] = action['text'].strip()&#10;    return action"
                          className={`${inputCls} font-mono`}
                        />
                      </div>

                      <div>
                        <label className={labelCls}>
                          Modificador de contexto (altera as mensagens antes de enviar ao modelo)
                        </label>
                        <textarea
                          rows={4}
                          value={scriptingForm.context_modifier}
                          onChange={(e) =>
                            setScriptingForm({ ...scriptingForm, context_modifier: e.target.value })
                          }
                          placeholder="# Example:&#10;def modify(context):&#10;    # context is a list of {role, content}&#10;    return context"
                          className={`${inputCls} font-mono`}
                        />
                      </div>

                      <div>
                        <label className={labelCls}>
                          Modificador de saída (altera a geração antes de gravar o turno)
                        </label>
                        <textarea
                          rows={4}
                          value={scriptingForm.output_modifier}
                          onChange={(e) =>
                            setScriptingForm({ ...scriptingForm, output_modifier: e.target.value })
                          }
                          placeholder="# Example:&#10;def modify(text):&#10;    return text.replace('  ', ' ')"
                          className={`${inputCls} font-mono`}
                        />
                      </div>
                    </div>
                  </div>
                </div>
              )}
            </>
          )}
        </main>
      </div>

      <ReaderPreferencesModal
        isOpen={showReaderModal}
        onClose={() => setShowReaderModal(false)}
      />
    </div>
  )
}
