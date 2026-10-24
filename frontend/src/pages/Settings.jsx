import React, { useEffect, useState } from 'react'
import { Link, useNavigate } from 'react-router-dom'
import { api } from '../api.js'
import { useToast } from '../components/Toast.jsx'
import { useTheme } from '../themeContext.jsx'
import ReaderPreferencesModal from '../components/ReaderPreferencesModal.jsx'
import VoiceCloneModal from '../components/VoiceCloneModal.jsx'
import { HealthCheck, SettingField } from '../components/settings/Controls.jsx'
import ImageAdvancedControls from '../components/settings/ImageAdvancedControls.jsx'
import LlmSamplingControls, { LLM_DEFAULTS } from '../components/settings/LlmSamplingControls.jsx'
import {
  ArrowLeft,
  Cpu,
  Image as ImageIcon,
  Volume2,
  Brain,
  Code2,
  Save,
  Play,
  Sliders,
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
    streaming: true,
    disable_thinking: true,
    timeout_seconds: 180,
    ...LLM_DEFAULTS,
  })
  const [llmHealth, setLlmHealth] = useState(null)

  // Image Settings State
  const [imageForm, setImageForm] = useState({
    enabled: true,
    provider: 'sdwebui',
    sdwebui_base_url: 'http://127.0.0.1:7860',
    sdwebui_steps: 20,
    sdwebui_width: 1024,
    sdwebui_height: 1024,
    negative_prompt: '',
    style_suffix: '',
    openai_base_url: 'https://api.openai.com/v1',
    openai_api_key: '',
    openai_model: 'dall-e-3',
    openai_size: '1024x1024',
    seed: '',
    variation: 0,
    timeout_seconds: 300,
    cfg_scale: 5,
    sampler_name: 'DPM++ 2M',
    scheduler: 'Karras',
    denoising_strength: 0.35,
    sd_model_checkpoint: '',
  })

  // Voice Settings State
  const [voiceForm, setVoiceForm] = useState({
    enabled: true,
    base_url: 'http://127.0.0.1:8000',
    tts_model: 'omnivoice',
    stt_model: 'whisper-large-v3',
    narrator_voice: 'M1',
    voices: ['M1', 'M2', 'M3', 'F1', 'F2', 'F3'],
    cloned: [],
    timeout_seconds: 30,
    api_key: '',
  })
  const [voiceSettings, setVoiceSettings] = useState(null)
  const [previewingVoice, setPreviewingVoice] = useState(null)
  const [cloneOpen, setCloneOpen] = useState(false)
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

  const voiceOptions = [
    ...(voiceForm.voices || ['M1', 'M2', 'M3', 'F1', 'F2', 'F3']).map((voice) => ({
      value: voice,
      label: `${voice} (${voice.startsWith('M') ? 'masculina' : 'feminina'})`,
    })),
    ...(voiceForm.cloned || []).map((clone) => ({ value: clone.id, label: `${clone.name} (clone)` })),
  ]
  if (voiceForm.narrator_voice && !voiceOptions.some((option) => option.value === voiceForm.narrator_voice)) {
    voiceOptions.push({ value: voiceForm.narrator_voice, label: voiceForm.narrator_voice })
  }

  useEffect(() => {
    async function loadAllSettings() {
      setLoading(true)
      try {
        const [llm, img, vce, voiceList, emb, scr, health] = await Promise.all([
          api.getLlmSettings().catch(() => ({})),
          api.getImageSettings().catch(() => ({})),
          api.getVoiceSettings().catch(() => ({})),
          api.voice.list().catch(() => ({})),
          api.getEmbeddingsSettings().catch(() => ({})),
          api.getScriptingSettings().catch(() => ({})),
          api.healthLlm().catch(() => null),
        ])

        if (llm && Object.keys(llm).length) {
          setLlmForm((prev) => ({
            ...prev,
            ...llm,
            api_key: '',
            context_size: Math.min(llm.context_size_max ?? 106496, llm.context_size || 65536),
          }))
        }
        if (img && Object.keys(img).length) {
          setImageForm((prev) => ({ ...prev, ...img, openai_api_key: '' }))
        }
        if (vce && Object.keys(vce).length) {
          const cloned = voiceList.cloned || []
          setVoiceSettings({ ...vce, cloned })
          setVoiceForm((prev) => ({
            ...prev,
            ...vce,
            voices: voiceList.voices || vce.voices || prev.voices,
            cloned,
            api_key: '',
          }))
        }
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
        if (!llmForm.api_key) delete payload.api_key
        const updated = await api.putLlmSettings(payload)
        setLlmForm((prev) => ({
          ...prev,
          ...updated,
          api_key: '',
          context_size: Math.min(updated.context_size_max ?? contextSizeMax, updated.context_size),
        }))
        toast('Configurações do modelo salvas.', 'ok')
      } else if (activeTab === 'image') {
        const payload = {
          enabled: imageForm.enabled,
          provider: imageForm.provider,
          sdwebui_base_url: imageForm.sdwebui_base_url,
          sdwebui_steps: Number(imageForm.sdwebui_steps),
          sdwebui_width: Number(imageForm.sdwebui_width),
          sdwebui_height: Number(imageForm.sdwebui_height),
          negative_prompt: imageForm.negative_prompt,
          style_suffix: imageForm.style_suffix,
          openai_base_url: imageForm.openai_base_url,
          openai_model: imageForm.openai_model,
          openai_size: imageForm.openai_size,
          seed: imageForm.seed === '' || imageForm.seed == null ? null : Number(imageForm.seed),
          variation: Number(imageForm.variation),
          timeout_seconds: Number(imageForm.timeout_seconds),
          cfg_scale: Number(imageForm.cfg_scale),
          sampler_name: imageForm.sampler_name,
          scheduler: imageForm.scheduler,
          denoising_strength: Number(imageForm.denoising_strength),
          sd_model_checkpoint: imageForm.sd_model_checkpoint,
        }
        if (imageForm.openai_api_key) payload.openai_api_key = imageForm.openai_api_key
        const updated = await api.putImageSettings(payload)
        setImageForm((prev) => ({ ...prev, ...updated, openai_api_key: '' }))
        toast('Configurações de imagem salvas.', 'ok')
      } else if (activeTab === 'voice') {
        const payload = {
          enabled: voiceForm.enabled,
          base_url: voiceForm.base_url,
          tts_model: voiceForm.tts_model,
          stt_model: voiceForm.stt_model,
          narrator_voice: voiceForm.narrator_voice,
          timeout_seconds: Number(voiceForm.timeout_seconds),
        }
        if (voiceForm.api_key) payload.api_key = voiceForm.api_key
        const updated = await api.putVoiceSettings(payload)
        setVoiceSettings((prev) => ({ ...prev, ...updated }))
        setVoiceForm((prev) => ({ ...prev, ...updated, api_key: '' }))
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

  const clearVoiceKey = async () => {
    try {
      const updated = await api.putVoiceSettings({ api_key: '' })
      setVoiceSettings((prev) => ({ ...prev, ...updated }))
      setVoiceForm((prev) => ({ ...prev, api_key: '' }))
      toast('Chave da API de voz removida.', 'ok')
    } catch (e) {
      toast('Falha ao remover a chave: ' + e.message)
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
            className="flex items-center gap-1.5 rounded-xl bg-accent-pri px-4 py-1.5 text-xs font-semibold text-canvas shadow hover:bg-accent-hov transition disabled:opacity-50"
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
                    ? 'bg-accent-pri text-canvas font-semibold shadow-sm'
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
                      <SettingField label="URL base" labelClassName={labelCls}>
                        <input
                          type="text"
                          value={llmForm.base_url}
                          onChange={(e) => setLlmForm({ ...llmForm, base_url: e.target.value })}
                          placeholder="http://127.0.0.1:1234/v1"
                          className={inputCls}
                        />
                      </SettingField>
                      <SettingField label="Apelido do modelo" labelClassName={labelCls}>
                        <input
                          type="text"
                          value={llmForm.model_alias}
                          onChange={(e) => setLlmForm({ ...llmForm, model_alias: e.target.value })}
                          placeholder="auto (ou o nome exato do modelo)"
                          className={inputCls}
                        />
                      </SettingField>
                    </div>

                    <SettingField label="Chave da API (opcional / oculta)" labelClassName={labelCls}>
                      <input
                        type="password"
                        value={llmForm.api_key || ''}
                        onChange={(e) => setLlmForm({ ...llmForm, api_key: e.target.value })}
                        placeholder="Deixe em branco para usar a chave atual ou o .env"
                        className={inputCls}
                      />
                    </SettingField>
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
                                ? 'bg-accent-pri text-canvas font-bold'
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
                        aria-label="Tamanho da janela de contexto"
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

                    <div className="grid grid-cols-1 sm:grid-cols-2 gap-4 pt-2">
                      <SettingField label="Tempo limite (segundos)" labelClassName={labelCls}>
                        <input
                          type="number"
                          min="10"
                          max="600"
                          value={llmForm.timeout_seconds}
                          onChange={(e) => setLlmForm({ ...llmForm, timeout_seconds: Number(e.target.value) })}
                          className={inputCls}
                        />
                      </SettingField>
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
                  <section className="rounded-2xl border border-subtle bg-surface p-4" aria-labelledby="llm-sampling-title">
                    <h3 id="llm-sampling-title" className="mb-4 text-xs font-semibold uppercase tracking-wider text-muted">
                      Amostragem e penalidades
                    </h3>
                    <LlmSamplingControls form={llmForm} onChange={setLlmForm} inputCls={inputCls} labelCls={labelCls} />
                  </section>
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

                  <HealthCheck service="image" />

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

                      <div className="flex rounded-lg border border-subtle overflow-hidden p-0.5 bg-elevated" role="group" aria-label="Provedor de imagem">
                        <button
                          type="button"
                          onClick={() => setImageForm({ ...imageForm, provider: 'sdwebui' })}
                          aria-pressed={imageForm.provider === 'sdwebui'}
                          className={`px-3 py-1 text-xs font-medium rounded-md transition ${
                            imageForm.provider === 'sdwebui' ? 'bg-surface text-primary shadow font-semibold' : 'text-secondary'
                          }`}
                        >
                          SD WebUI (Local)
                        </button>
                        <button
                          type="button"
                          onClick={() => setImageForm({ ...imageForm, provider: 'openai_compatible' })}
                          aria-pressed={imageForm.provider === 'openai_compatible'}
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
                          <SettingField label="URL base do SD WebUI" labelClassName={labelCls}>
                            <input
                              type="text"
                              value={imageForm.sdwebui_base_url}
                              onChange={(e) => setImageForm({ ...imageForm, sdwebui_base_url: e.target.value })}
                              placeholder="http://127.0.0.1:7860"
                              className={inputCls}
                            />
                          </SettingField>
                          <SettingField label="Passos de inferência (20 recomendados)" labelClassName={labelCls}>
                            <input
                              type="number"
                              min="1"
                              max="150"
                              step="1"
                              value={imageForm.sdwebui_steps}
                              onChange={(e) => setImageForm({ ...imageForm, sdwebui_steps: e.target.value })}
                              className={inputCls}
                            />
                          </SettingField>
                        </div>

                        <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
                          <SettingField label="Largura (1024)" labelClassName={labelCls}>
                            <input
                              type="number"
                              min="64"
                              max="4096"
                              step="64"
                              value={imageForm.sdwebui_width}
                              onChange={(e) => setImageForm({ ...imageForm, sdwebui_width: e.target.value })}
                              className={inputCls}
                            />
                          </SettingField>
                          <SettingField label="Altura (1024)" labelClassName={labelCls}>
                            <input
                              type="number"
                              min="64"
                              max="4096"
                              step="64"
                              value={imageForm.sdwebui_height}
                              onChange={(e) => setImageForm({ ...imageForm, sdwebui_height: e.target.value })}
                              className={inputCls}
                            />
                          </SettingField>
                        </div>

                        <SettingField label="Prompt negativo padrão" labelClassName={labelCls}>
                          <textarea
                            rows={3}
                            value={imageForm.negative_prompt}
                            onChange={(e) => setImageForm({ ...imageForm, negative_prompt: e.target.value })}
                            placeholder="text, watermark, signature, low quality, deformed, blurry..."
                            className={inputCls}
                          />
                        </SettingField>

                        <SettingField label="Sufixo de estilo" labelClassName={labelCls}>
                          <input
                            type="text"
                            value={imageForm.style_suffix || ''}
                            onChange={(e) => setImageForm({ ...imageForm, style_suffix: e.target.value })}
                            placeholder="ex.: pintura a óleo de fantasia sombria, luz cinematográfica"
                            className={inputCls}
                          />
                        </SettingField>
                      </div>
                    ) : (
                      <div className="space-y-4 pt-2">
                        <SettingField label="Endpoint OpenAI" labelClassName={labelCls}>
                          <input
                            type="text"
                            value={imageForm.openai_base_url}
                            onChange={(e) => setImageForm({ ...imageForm, openai_base_url: e.target.value })}
                            placeholder="https://api.openai.com/v1"
                            className={inputCls}
                          />
                        </SettingField>
                        <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
                          <SettingField label="Modelo" labelClassName={labelCls}>
                            <input
                              type="text"
                              value={imageForm.openai_model}
                              onChange={(e) => setImageForm({ ...imageForm, openai_model: e.target.value })}
                              placeholder="dall-e-3"
                              className={inputCls}
                            />
                          </SettingField>
                          <SettingField label="Tamanho da imagem (largura x altura)" labelClassName={labelCls}>
                            <input
                              type="text"
                              value={imageForm.openai_size}
                              onChange={(e) => setImageForm({ ...imageForm, openai_size: e.target.value })}
                              placeholder="1024x1024"
                              className={inputCls}
                            />
                          </SettingField>
                        </div>
                        <SettingField label="Chave da API OpenAI" labelClassName={labelCls}>
                          <input
                            type="password"
                            value={imageForm.openai_api_key}
                            onChange={(e) => setImageForm({ ...imageForm, openai_api_key: e.target.value })}
                            placeholder="Em branco mantém a chave atual"
                            className={inputCls}
                          />
                        </SettingField>
                      </div>
                    )}

                    <ImageAdvancedControls
                      form={imageForm}
                      onChange={setImageForm}
                      inputCls={inputCls}
                      labelCls={labelCls}
                    />
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
                      Narração com OmniVoice e transcrição com Whisper.
                    </p>
                  </div>

                  <HealthCheck service="voice" />

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
                      <SettingField label="URL base do serviço de voz" labelClassName={labelCls}>
                        <input
                          type="text"
                          value={voiceForm.base_url}
                          onChange={(e) => setVoiceForm({ ...voiceForm, base_url: e.target.value })}
                          placeholder="https://host/voice/v1"
                          className={inputCls}
                        />
                      </SettingField>
                      <SettingField label="Voz padrão do narrador" labelClassName={labelCls}>
                        <select
                          value={voiceForm.narrator_voice}
                          onChange={(e) => setVoiceForm({ ...voiceForm, narrator_voice: e.target.value })}
                          className={inputCls}
                        >
                          {voiceOptions.map((option) => (
                            <option key={option.value} value={option.value}>{option.label}</option>
                          ))}
                        </select>
                      </SettingField>
                    </div>

                    <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
                      <SettingField label="Modelo de voz (OmniVoice)" labelClassName={labelCls}>
                        <input
                          type="text"
                          value={voiceForm.tts_model}
                          onChange={(e) => setVoiceForm({ ...voiceForm, tts_model: e.target.value })}
                          placeholder="omnivoice"
                          className={inputCls}
                        />
                      </SettingField>
                      <SettingField label="Modelo de transcrição (Whisper)" labelClassName={labelCls}>
                        <input
                          type="text"
                          value={voiceForm.stt_model}
                          onChange={(e) => setVoiceForm({ ...voiceForm, stt_model: e.target.value })}
                          placeholder="whisper-1"
                          className={inputCls}
                        />
                      </SettingField>
                    </div>

                    <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
                      <SettingField label="Chave da API de voz (opcional / oculta)" labelClassName={labelCls}>
                        <input
                          type="password"
                          value={voiceForm.api_key}
                          onChange={(e) => setVoiceForm({ ...voiceForm, api_key: e.target.value })}
                          placeholder={voiceSettings?.api_key ? `Atual: ${voiceSettings.api_key}` : 'Em branco mantém a chave atual'}
                          className={inputCls}
                        />
                      </SettingField>
                      <SettingField label="Tempo limite (segundos)" labelClassName={labelCls}>
                        <input
                          type="number"
                          min="1"
                          max="600"
                          step="1"
                          value={voiceForm.timeout_seconds}
                          onChange={(e) => setVoiceForm({ ...voiceForm, timeout_seconds: e.target.value })}
                          className={inputCls}
                        />
                      </SettingField>
                    </div>

                    <div className="flex flex-wrap justify-end gap-2">
                      {voiceSettings?.api_key && (
                        <button type="button" onClick={clearVoiceKey} className="rounded-lg border border-danger-900 px-3 py-2 text-xs text-danger-400 hover:bg-danger-950">
                          Remover chave
                        </button>
                      )}
                      <button type="button" onClick={() => setCloneOpen(true)} className="rounded-lg border border-subtle px-3 py-2 text-xs text-secondary hover:text-primary">
                        Clonar ou gerenciar vozes
                      </button>
                    </div>

                    <div className="pt-3 border-t border-subtle">
                      <h3 className={labelCls}>Prévia das vozes</h3>
                      <div className="grid grid-cols-2 sm:grid-cols-3 md:grid-cols-6 gap-2 mt-2">
                        {voiceOptions.map((option) => {
                          const voiceKey = option.value
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
                              <span className="max-w-full truncate text-xs font-bold text-primary" title={option.label}>{option.label}</span>
                              <button
                                type="button"
                                onClick={() => handlePreviewVoice(voiceKey)}
                                disabled={isPreviewing}
                                aria-label={`Ouvir prévia de ${option.label}`}
                                className="mt-2 flex items-center gap-1 rounded-lg bg-accent-pri/20 border border-accent-pri/40 px-2 py-1 text-[10px] font-semibold text-accent-pri hover:bg-accent-pri/30 transition disabled:opacity-50"
                              >
                                <Play className={`w-2.5 h-2.5 ${isPreviewing ? 'animate-spin' : ''}`} />
                                <span>{isPreviewing ? 'Reproduzindo…' : 'Ouvir'}</span>
                              </button>
                            </div>
                          )
                        })}
                      </div>
                    </div>
                  </div>

                  {cloneOpen && (
                    <VoiceCloneModal
                      open
                      assign={{ kind: 'narrator' }}
                      onClose={(result) => {
                        if (result?.deleted) {
                          setVoiceForm((current) => ({
                            ...current,
                            cloned: (current.cloned || []).filter((clone) => clone.id !== result.deleted),
                            narrator_voice: current.narrator_voice === result.deleted ? 'M1' : current.narrator_voice,
                          }))
                          setVoiceSettings((current) => ({
                            ...current,
                            cloned: (current?.cloned || []).filter((clone) => clone.id !== result.deleted),
                          }))
                          return
                        }
                        setCloneOpen(false)
                        if (!result?.id) return
                        setVoiceForm((current) => ({
                          ...current,
                          narrator_voice: result.id,
                          cloned: (current.cloned || []).some((clone) => clone.id === result.id)
                            ? current.cloned
                            : [...(current.cloned || []), { id: result.id, name: result.name }],
                        }))
                      }}
                    />
                  )}
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
                      <SettingField label="URL base dos embeddings" labelClassName={labelCls}>
                        <input
                          type="text"
                          value={embeddingsForm.base_url}
                          onChange={(e) => setEmbeddingsForm({ ...embeddingsForm, base_url: e.target.value })}
                          placeholder="http://127.0.0.1:8081/v1"
                          className={inputCls}
                        />
                      </SettingField>
                      <SettingField label="Modelo de embedding" labelClassName={labelCls}>
                        <input
                          type="text"
                          value={embeddingsForm.model}
                          onChange={(e) => setEmbeddingsForm({ ...embeddingsForm, model: e.target.value })}
                          placeholder="Qwen3-Embedding-0.6B"
                          className={inputCls}
                        />
                      </SettingField>
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
                      <SettingField label="Modificador de entrada (altera a ação do jogador antes do motor)" labelClassName={labelCls}>
                        <textarea
                          rows={4}
                          value={scriptingForm.input_modifier}
                          onChange={(e) =>
                            setScriptingForm({ ...scriptingForm, input_modifier: e.target.value })
                          }
                          placeholder="# Example:&#10;def modify(action):&#10;    action['text'] = action['text'].strip()&#10;    return action"
                          className={`${inputCls} font-mono`}
                        />
                      </SettingField>

                      <SettingField label="Modificador de contexto (altera as mensagens antes de enviar ao modelo)" labelClassName={labelCls}>
                        <textarea
                          rows={4}
                          value={scriptingForm.context_modifier}
                          onChange={(e) =>
                            setScriptingForm({ ...scriptingForm, context_modifier: e.target.value })
                          }
                          placeholder="# Example:&#10;def modify(context):&#10;    # context is a list of {role, content}&#10;    return context"
                          className={`${inputCls} font-mono`}
                        />
                      </SettingField>

                      <SettingField label="Modificador de saída (altera a geração antes de gravar o turno)" labelClassName={labelCls}>
                        <textarea
                          rows={4}
                          value={scriptingForm.output_modifier}
                          onChange={(e) =>
                            setScriptingForm({ ...scriptingForm, output_modifier: e.target.value })
                          }
                          placeholder="# Example:&#10;def modify(text):&#10;    return text.replace('  ', ' ')"
                          className={`${inputCls} font-mono`}
                        />
                      </SettingField>
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
