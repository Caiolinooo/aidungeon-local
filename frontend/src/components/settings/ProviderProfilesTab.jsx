import React, { useMemo, useState } from 'react'
import {
  Brain,
  Check,
  Cpu,
  Download,
  Edit3,
  Image as ImageIcon,
  KeyRound,
  Layers,
  Plus,
  Sparkles,
  Trash2,
  Volume2,
  X,
} from 'lucide-react'
import { SettingField } from './Controls.jsx'
import ProviderModelPicker from './ProviderModelPicker.jsx'

const ALL_KINDS = [
  { id: 'llm', label: 'LLM', icon: Cpu },
  { id: 'image', label: 'Imagem', icon: ImageIcon },
  { id: 'voice', label: 'Voz', icon: Volume2 },
  { id: 'embeddings', label: 'Embeddings', icon: Brain },
]

const KIND_LABEL = {
  llm: 'LLM',
  image: 'Imagem',
  voice: 'Voz',
  embeddings: 'Embeddings',
}

function emptyDraft() {
  return {
    id: null,
    name: '',
    provider_id: 'custom',
    base_url: '',
    api_key: '',
    api_key_masked: null,
    clear_api_key: false,
    capabilities: ['llm'],
    llm: { base_url: '', model_alias: '' },
    image: { provider: 'openai_compatible', base_url: '', model: '', size: '1024x1024', sd_model_checkpoint: '' },
    voice: { base_url: '', tts_model: '', stt_model: '', narrator_voice: '', voices: [] },
    embeddings: { base_url: '', model: '' },
  }
}

function draftFromTemplate(tpl) {
  return {
    id: null,
    name: tpl.id === 'custom' ? '' : tpl.name,
    provider_id: tpl.id || 'custom',
    base_url: tpl.base_url || '',
    api_key: '',
    api_key_masked: null,
    clear_api_key: false,
    capabilities: Array.isArray(tpl.capabilities) && tpl.capabilities.length ? [...tpl.capabilities] : ['llm'],
    llm: {
      base_url: tpl.llm?.base_url || '',
      model_alias: tpl.llm?.model_alias || '',
    },
    image: {
      provider: tpl.image?.provider || 'openai_compatible',
      base_url: tpl.image?.base_url || '',
      model: tpl.image?.model || '',
      size: tpl.image?.size || '1024x1024',
      sd_model_checkpoint: tpl.image?.sd_model_checkpoint || '',
    },
    voice: {
      base_url: tpl.voice?.base_url || '',
      tts_model: tpl.voice?.tts_model || '',
      stt_model: tpl.voice?.stt_model || '',
      narrator_voice: tpl.voice?.narrator_voice || '',
      voices: Array.isArray(tpl.voice?.voices) ? [...tpl.voice.voices] : [],
    },
    embeddings: {
      base_url: tpl.embeddings?.base_url || '',
      model: tpl.embeddings?.model || '',
    },
  }
}

function draftFromProfile(prof) {
  return {
    id: prof.id,
    name: prof.name || '',
    provider_id: prof.provider_id || 'custom',
    base_url: prof.base_url || '',
    api_key: '',
    api_key_masked: prof.api_key || null,
    clear_api_key: false,
    capabilities: Array.isArray(prof.capabilities) && prof.capabilities.length ? [...prof.capabilities] : ['llm'],
    llm: {
      base_url: prof.llm?.base_url || '',
      model_alias: prof.llm?.model_alias || '',
    },
    image: {
      provider: prof.image?.provider || 'openai_compatible',
      base_url: prof.image?.base_url || '',
      model: prof.image?.model || '',
      size: prof.image?.size || '1024x1024',
      sd_model_checkpoint: prof.image?.sd_model_checkpoint || '',
    },
    voice: {
      base_url: prof.voice?.base_url || '',
      tts_model: prof.voice?.tts_model || '',
      stt_model: prof.voice?.stt_model || '',
      narrator_voice: prof.voice?.narrator_voice || '',
      voices: Array.isArray(prof.voice?.voices) ? [...prof.voice.voices] : [],
    },
    embeddings: {
      base_url: prof.embeddings?.base_url || '',
      model: prof.embeddings?.model || '',
    },
  }
}

export default function ProviderProfilesTab({
  profiles = [],
  active = {},
  templates = [],
  activatingId = null,
  onCreateOrUpdate,
  onDelete,
  onActivate,
  onCaptureCurrent,
  inputCls,
  labelCls,
}) {
  const [editorOpen, setEditorOpen] = useState(false)
  const [draft, setDraft] = useState(() => emptyDraft())
  const [submitting, setSubmitting] = useState(false)
  const [captureOpen, setCaptureOpen] = useState(false)
  const [captureName, setCaptureName] = useState('Local / VM (atual)')
  const [capturing, setCapturing] = useState(false)
  const [showAdvancedUrls, setShowAdvancedUrls] = useState(false)
  const [discoverKind, setDiscoverKind] = useState(null)

  const profileById = useMemo(() => {
    const map = {}
    for (const p of profiles) map[p.id] = p
    return map
  }, [profiles])

  const openNew = (template = null) => {
    const next = template ? draftFromTemplate(template) : emptyDraft()
    setDraft(next)
    setShowAdvancedUrls(
      Boolean(next.llm.base_url || next.image.base_url || next.voice.base_url || next.embeddings.base_url),
    )
    setDiscoverKind(null)
    setEditorOpen(true)
  }

  const openEdit = (prof) => {
    const next = draftFromProfile(prof)
    setDraft(next)
    setShowAdvancedUrls(
      Boolean(next.llm.base_url || next.image.base_url || next.voice.base_url || next.embeddings.base_url),
    )
    setDiscoverKind(null)
    setEditorOpen(true)
  }

  const toggleCapability = (kindId) => {
    setDraft((prev) => {
      const has = prev.capabilities.includes(kindId)
      const nextCaps = has
        ? prev.capabilities.filter((k) => k !== kindId)
        : [...prev.capabilities, kindId]
      return {
        ...prev,
        capabilities: nextCaps.length ? nextCaps : [kindId],
      }
    })
  }

  const handleSaveProfile = async (e) => {
    e.preventDefault()
    if (!draft.name.trim()) return
    setSubmitting(true)
    try {
      const payload = {
        name: draft.name.trim(),
        provider_id: draft.provider_id || 'custom',
        base_url: draft.base_url.trim(),
        capabilities: draft.capabilities,
        sync_active: true,
      }
      if (draft.clear_api_key) {
        payload.api_key = ''
      } else if (draft.api_key.trim()) {
        payload.api_key = draft.api_key.trim()
      }

      if (draft.capabilities.includes('llm')) {
        payload.llm = {
          base_url: draft.llm.base_url.trim(),
          model_alias: draft.llm.model_alias.trim(),
        }
      }
      if (draft.capabilities.includes('image')) {
        payload.image = {
          provider: draft.image.provider || 'openai_compatible',
          base_url: draft.image.base_url.trim(),
          model: draft.image.model.trim(),
          size: draft.image.size.trim() || '1024x1024',
          sd_model_checkpoint: draft.image.sd_model_checkpoint.trim(),
        }
      }
      if (draft.capabilities.includes('voice')) {
        payload.voice = {
          base_url: draft.voice.base_url.trim(),
          tts_model: draft.voice.tts_model.trim(),
          stt_model: draft.voice.stt_model.trim(),
          narrator_voice: draft.voice.narrator_voice.trim(),
          voices: draft.voice.voices,
        }
      }
      if (draft.capabilities.includes('embeddings')) {
        payload.embeddings = {
          base_url: draft.embeddings.base_url.trim(),
          model: draft.embeddings.model.trim(),
        }
      }

      await onCreateOrUpdate(draft.id, payload)
      setEditorOpen(false)
      setDraft(emptyDraft())
      setDiscoverKind(null)
    } finally {
      setSubmitting(false)
    }
  }

  const handleCapture = async (e) => {
    e.preventDefault()
    if (!captureName.trim() || !onCaptureCurrent) return
    setCapturing(true)
    try {
      await onCaptureCurrent(captureName.trim())
      setCaptureOpen(false)
    } finally {
      setCapturing(false)
    }
  }

  const effectiveDraftUrl = (kind) => {
    const specific = draft[kind]?.base_url?.trim()
    return specific || draft.base_url.trim()
  }

  return (
    <div className="space-y-6">
      <div className="border-b border-subtle pb-3 flex flex-wrap items-start justify-between gap-3">
        <div>
          <h2 className="text-sm font-bold uppercase tracking-wider text-primary flex items-center gap-2">
            <Layers className="w-4 h-4 text-accent-pri" />
            <span>Perfis de provedores e APIs</span>
          </h2>
          <p className="text-xs text-secondary mt-0.5">
            Salve suas APIs uma única vez por provedor e alterne entre elas em LLM, Imagem, Voz e Embeddings.
            Provedores com várias soluções (ex.: OpenAI, Together, Fireworks, Groq, OpenRouter) reaproveitam a mesma chave automaticamente.
          </p>
        </div>

        <div className="flex flex-wrap items-center gap-2">
          <button
            type="button"
            onClick={() => setCaptureOpen((v) => !v)}
            className="flex items-center gap-1.5 rounded-xl border border-subtle bg-elevated px-3 py-1.5 text-xs font-semibold text-secondary hover:text-primary transition"
          >
            <Download className="w-3.5 h-3.5 text-accent-pri" />
            <span>Salvar setup atual</span>
          </button>
          <button
            type="button"
            onClick={() => openNew(null)}
            className="flex items-center gap-1.5 rounded-xl bg-accent-pri px-3 py-1.5 text-xs font-semibold text-canvas shadow hover:bg-accent-hov transition"
          >
            <Plus className="w-3.5 h-3.5" />
            <span>Novo perfil</span>
          </button>
        </div>
      </div>

      {/* Capturar configuração atual */}
      {captureOpen && (
        <form
          onSubmit={handleCapture}
          className="rounded-2xl border border-accent-pri/40 bg-accent-pri/5 p-4 space-y-3"
        >
          <div>
            <h3 className="text-xs font-bold uppercase tracking-wider text-primary">
              Capturar configuração atual (LLM + Imagem + Voz + Embeddings)
            </h3>
            <p className="text-[11px] text-secondary mt-0.5">
              Salva todas as URLs, modelos e chaves ativas agora em um único perfil (no servidor, sem precisar redigitar senhas).
              Ideal para guardar seu setup Local / VM antes de testar APIs externas.
            </p>
          </div>
          <div className="flex flex-wrap items-center gap-2">
            <input
              type="text"
              value={captureName}
              onChange={(e) => setCaptureName(e.target.value)}
              placeholder="Ex.: Local / VM (atual)"
              className="flex-1 min-w-[220px] rounded-xl border border-subtle bg-surface px-3 py-2 text-xs text-primary outline-none focus:border-accent-pri"
            />
            <button
              type="submit"
              disabled={capturing || !captureName.trim()}
              className="rounded-xl bg-accent-pri px-4 py-2 text-xs font-semibold text-canvas hover:bg-accent-hov transition disabled:opacity-50"
            >
              {capturing ? 'Capturando…' : 'Capturar agora'}
            </button>
            <button
              type="button"
              onClick={() => setCaptureOpen(false)}
              className="rounded-xl border border-subtle px-3 py-2 text-xs text-secondary hover:text-primary"
            >
              Cancelar
            </button>
          </div>
        </form>
      )}

      {/* Resumo dos perfis ativos agora */}
      <div className="grid grid-cols-2 sm:grid-cols-4 gap-2.5">
        {ALL_KINDS.map(({ id, label, icon: Icon }) => {
          const profId = active?.[id]
          const prof = profId ? profileById[profId] : null
          return (
            <div
              key={id}
              className="rounded-xl border border-subtle bg-surface p-3 space-y-1"
            >
              <div className="flex items-center gap-1.5 text-[10px] font-bold uppercase tracking-wider text-muted">
                <Icon className="w-3.5 h-3.5 text-accent-pri" />
                <span>{label} ativo</span>
              </div>
              <div className="text-xs font-semibold text-primary truncate">
                {prof ? prof.name : 'Configuração manual'}
              </div>
            </div>
          )
        })}
      </div>

      {/* Editor de perfil (Criar / Editar) */}
      {editorOpen && (
        <form
          onSubmit={handleSaveProfile}
          className="rounded-2xl border border-accent-pri/50 bg-surface p-4 space-y-4 shadow-sm"
        >
          <div className="flex items-center justify-between border-b border-subtle pb-2.5">
            <h3 className="text-xs font-bold uppercase tracking-wider text-primary flex items-center gap-1.5">
              <Sparkles className="w-3.5 h-3.5 text-accent-pri" />
              <span>{draft.id ? `Editar perfil: ${draft.name}` : 'Novo perfil de provedor'}</span>
            </h3>
            <button
              type="button"
              onClick={() => { setEditorOpen(false); setDiscoverKind(null) }}
              className="rounded-lg p-1 text-secondary hover:text-primary hover:bg-elevated"
              aria-label="Fechar editor"
            >
              <X className="w-4 h-4" />
            </button>
          </div>

          {/* Templates rápidos */}
          {!draft.id && templates.length > 0 && (
            <div className="space-y-1.5">
              <div className="text-[10px] font-semibold uppercase tracking-wider text-muted">
                Preencher a partir de um provedor (já configura todas as soluções dele)
              </div>
              <div className="flex flex-wrap gap-1.5">
                {templates.map((tpl) => {
                  const selected = draft.provider_id === tpl.id
                  const capsCount = (tpl.capabilities || []).length
                  return (
                    <button
                      key={tpl.id}
                      type="button"
                      onClick={() => openNew(tpl)}
                      className={`flex items-center gap-1 rounded-lg border px-2.5 py-1 text-[11px] font-medium transition ${
                        selected
                          ? 'border-accent-pri bg-accent-pri text-canvas font-semibold'
                          : 'border-subtle bg-elevated/50 text-secondary hover:text-primary hover:border-accent-pri/40'
                      }`}
                    >
                      <span>{tpl.name}</span>
                      {capsCount > 1 && (
                        <span
                          className={`rounded px-1 text-[9px] font-bold ${
                            selected ? 'bg-canvas/20 text-canvas' : 'bg-surface text-muted'
                          }`}
                        >
                          {capsCount}x
                        </span>
                      )}
                    </button>
                  )
                })}
              </div>
            </div>
          )}

          {/* Campos principais compartilhados */}
          <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
            <SettingField label="Nome do perfil" labelClassName={labelCls}>
              <input
                type="text"
                required
                value={draft.name}
                onChange={(e) => setDraft({ ...draft, name: e.target.value })}
                placeholder="Ex.: OpenAI, OpenRouter, Groq, Local VM"
                className={inputCls}
              />
            </SettingField>
            <SettingField label="URL base principal (compartilhada)" labelClassName={labelCls}>
              <input
                type="text"
                value={draft.base_url}
                onChange={(e) => setDraft({ ...draft, base_url: e.target.value })}
                placeholder="https://api.openai.com/v1"
                className={inputCls}
              />
            </SettingField>
          </div>

          <div className="flex flex-col sm:flex-row sm:items-end gap-3">
            <SettingField
              label="Chave da API (única — reaproveitada em todas as modalidades marcadas)"
              labelClassName={labelCls}
              className="flex-1"
            >
              <input
                type="password"
                value={draft.api_key}
                onChange={(e) =>
                  setDraft({ ...draft, api_key: e.target.value, clear_api_key: false })
                }
                placeholder={
                  draft.clear_api_key
                    ? 'Chave será removida ao salvar'
                    : draft.api_key_masked
                      ? `Atual: ${draft.api_key_masked} (em branco mantém)`
                      : 'Cole sua API key (opcional para servidores locais)'
                }
                className={inputCls}
              />
            </SettingField>
            {draft.api_key_masked && !draft.clear_api_key && (
              <button
                type="button"
                onClick={() => setDraft({ ...draft, api_key: '', clear_api_key: true })}
                className="shrink-0 rounded-lg border border-danger-900 px-3 py-2 text-xs text-danger-400 hover:bg-danger-950"
              >
                Remover chave
              </button>
            )}
          </div>

          {/* Modalidades suportadas */}
          <div className="space-y-2">
            <div className="flex flex-wrap items-center justify-between gap-2">
              <span className={labelCls}>Soluções / modalidades atendidas por este perfil</span>
              <button
                type="button"
                onClick={() => setShowAdvancedUrls((v) => !v)}
                className="text-[11px] font-medium text-accent-pri hover:underline"
              >
                {showAdvancedUrls ? 'Ocultar URLs por modalidade' : 'URLs diferentes por modalidade?'}
              </button>
            </div>
            <div className="flex flex-wrap gap-2">
              {ALL_KINDS.map(({ id, label, icon: Icon }) => {
                const checked = draft.capabilities.includes(id)
                return (
                  <button
                    key={id}
                    type="button"
                    onClick={() => toggleCapability(id)}
                    className={`flex items-center gap-1.5 rounded-xl border px-3 py-1.5 text-xs font-semibold transition ${
                      checked
                        ? 'border-accent-pri bg-accent-pri/15 text-accent-pri'
                        : 'border-subtle bg-elevated/40 text-secondary hover:text-primary'
                    }`}
                  >
                    <Icon className="w-3.5 h-3.5" />
                    <span>{label}</span>
                    {checked && <Check className="w-3 h-3" />}
                  </button>
                )
              })}
            </div>
          </div>

          {/* Configurações por modalidade marcada */}
          <div className="space-y-3">
            {draft.capabilities.includes('llm') && (
              <div className="rounded-xl border border-subtle bg-elevated/30 p-3 space-y-3">
                <div className="flex items-center justify-between">
                  <span className="text-[11px] font-bold uppercase tracking-wider text-primary">
                    LLM — Modelo padrão
                  </span>
                  <button
                    type="button"
                    onClick={() => setDiscoverKind(discoverKind === 'llm' ? null : 'llm')}
                    className="text-[11px] font-semibold text-accent-pri hover:underline"
                  >
                    {discoverKind === 'llm' ? 'Fechar catálogo' : 'Puxar modelos da API'}
                  </button>
                </div>
                <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
                  <SettingField label="Modelo LLM" labelClassName={labelCls}>
                    <input
                      type="text"
                      value={draft.llm.model_alias}
                      onChange={(e) =>
                        setDraft({ ...draft, llm: { ...draft.llm, model_alias: e.target.value } })
                      }
                      placeholder="auto / gpt-4o-mini / openai/gpt-4o-mini"
                      className={inputCls}
                    />
                  </SettingField>
                  {showAdvancedUrls && (
                    <SettingField label="URL específica LLM (opcional)" labelClassName={labelCls}>
                      <input
                        type="text"
                        value={draft.llm.base_url}
                        onChange={(e) =>
                          setDraft({ ...draft, llm: { ...draft.llm, base_url: e.target.value } })
                        }
                        placeholder="Em branco usa a URL principal"
                        className={inputCls}
                      />
                    </SettingField>
                  )}
                </div>
                {discoverKind === 'llm' && (
                  <ProviderModelPicker
                    kind="llm"
                    profileId={draft.id}
                    baseUrl={effectiveDraftUrl('llm')}
                    apiKey={draft.api_key}
                    selectedModel={draft.llm.model_alias}
                    onSelectModel={(id) =>
                      setDraft((prev) => ({ ...prev, llm: { ...prev.llm, model_alias: id } }))
                    }
                    title="Catálogo LLM deste perfil"
                    subtitle="Puxa e pesquisa modelos usando a URL e chave deste perfil."
                  />
                )}
              </div>
            )}

            {draft.capabilities.includes('image') && (
              <div className="rounded-xl border border-subtle bg-elevated/30 p-3 space-y-3">
                <div className="flex items-center justify-between">
                  <span className="text-[11px] font-bold uppercase tracking-wider text-primary">
                    Imagem — Provedor e modelo
                  </span>
                  <button
                    type="button"
                    onClick={() => setDiscoverKind(discoverKind === 'image' ? null : 'image')}
                    className="text-[11px] font-semibold text-accent-pri hover:underline"
                  >
                    {discoverKind === 'image' ? 'Fechar catálogo' : 'Puxar modelos da API'}
                  </button>
                </div>
                <div className="grid grid-cols-1 sm:grid-cols-3 gap-3">
                  <SettingField label="Tipo" labelClassName={labelCls}>
                    <select
                      value={draft.image.provider}
                      onChange={(e) =>
                        setDraft({ ...draft, image: { ...draft.image, provider: e.target.value } })
                      }
                      className={inputCls}
                    >
                      <option value="openai_compatible">OpenAI-compatível (API)</option>
                      <option value="comfyui">ComfyUI (Local)</option>
                      <option value="sdwebui">SD WebUI (Local)</option>
                    </select>
                  </SettingField>
                  {draft.image.provider === 'openai_compatible' && (
                    <>
                      <SettingField label="Modelo de imagem" labelClassName={labelCls}>
                        <input
                          type="text"
                          value={draft.image.model}
                          onChange={(e) =>
                            setDraft({ ...draft, image: { ...draft.image, model: e.target.value } })
                          }
                          placeholder="gpt-image-1 / FLUX.1-schnell"
                          className={inputCls}
                        />
                      </SettingField>
                      <SettingField label="Tamanho" labelClassName={labelCls}>
                        <input
                          type="text"
                          value={draft.image.size}
                          onChange={(e) =>
                            setDraft({ ...draft, image: { ...draft.image, size: e.target.value } })
                          }
                          placeholder="1024x1024"
                          className={inputCls}
                        />
                      </SettingField>
                    </>
                  )}
                  {draft.image.provider === 'sdwebui' && (
                    <SettingField label="Checkpoint SD WebUI" labelClassName={labelCls} className="sm:col-span-2">
                      <input
                        type="text"
                        value={draft.image.sd_model_checkpoint}
                        onChange={(e) =>
                          setDraft({
                            ...draft,
                            image: { ...draft.image, sd_model_checkpoint: e.target.value },
                          })
                        }
                        placeholder="Opcional"
                        className={inputCls}
                      />
                    </SettingField>
                  )}
                </div>
                {showAdvancedUrls && (
                  <SettingField label="URL específica de imagem (opcional)" labelClassName={labelCls}>
                    <input
                      type="text"
                      value={draft.image.base_url}
                      onChange={(e) =>
                        setDraft({ ...draft, image: { ...draft.image, base_url: e.target.value } })
                      }
                      placeholder="Em branco usa a URL principal"
                      className={inputCls}
                    />
                  </SettingField>
                )}
                {discoverKind === 'image' && (
                  <ProviderModelPicker
                    kind="image"
                    provider={draft.image.provider}
                    profileId={draft.id}
                    baseUrl={effectiveDraftUrl('image')}
                    apiKey={draft.api_key}
                    selectedModel={
                      draft.image.provider === 'sdwebui'
                        ? draft.image.sd_model_checkpoint
                        : draft.image.model
                    }
                    allowSelect={draft.image.provider !== 'comfyui'}
                    onSelectModel={(id) =>
                      setDraft((prev) => ({
                        ...prev,
                        image:
                          prev.image.provider === 'sdwebui'
                            ? { ...prev.image, sd_model_checkpoint: id }
                            : { ...prev.image, model: id },
                      }))
                    }
                    title="Catálogo de imagem deste perfil"
                    subtitle="Puxa e pesquisa modelos de imagem usando a URL e chave deste perfil."
                  />
                )}
              </div>
            )}

            {draft.capabilities.includes('voice') && (
              <div className="rounded-xl border border-subtle bg-elevated/30 p-3 space-y-3">
                <div className="flex items-center justify-between">
                  <span className="text-[11px] font-bold uppercase tracking-wider text-primary">
                    Voz — TTS, STT e narrador
                  </span>
                  <button
                    type="button"
                    onClick={() => setDiscoverKind(discoverKind === 'voice' ? null : 'voice')}
                    className="text-[11px] font-semibold text-accent-pri hover:underline"
                  >
                    {discoverKind === 'voice' ? 'Fechar catálogo' : 'Puxar modelos/vozes da API'}
                  </button>
                </div>
                <div className="grid grid-cols-1 sm:grid-cols-3 gap-3">
                  <SettingField label="Modelo TTS" labelClassName={labelCls}>
                    <input
                      type="text"
                      value={draft.voice.tts_model}
                      onChange={(e) =>
                        setDraft({ ...draft, voice: { ...draft.voice, tts_model: e.target.value } })
                      }
                      placeholder="gpt-4o-mini-tts / playai-tts"
                      className={inputCls}
                    />
                  </SettingField>
                  <SettingField label="Modelo STT" labelClassName={labelCls}>
                    <input
                      type="text"
                      value={draft.voice.stt_model}
                      onChange={(e) =>
                        setDraft({ ...draft, voice: { ...draft.voice, stt_model: e.target.value } })
                      }
                      placeholder="whisper-1"
                      className={inputCls}
                    />
                  </SettingField>
                  <SettingField label="Voz do narrador" labelClassName={labelCls}>
                    <input
                      type="text"
                      value={draft.voice.narrator_voice}
                      onChange={(e) =>
                        setDraft({ ...draft, voice: { ...draft.voice, narrator_voice: e.target.value } })
                      }
                      placeholder="nova / M1"
                      className={inputCls}
                    />
                  </SettingField>
                </div>
                {showAdvancedUrls && (
                  <SettingField label="URL específica de voz (opcional)" labelClassName={labelCls}>
                    <input
                      type="text"
                      value={draft.voice.base_url}
                      onChange={(e) =>
                        setDraft({ ...draft, voice: { ...draft.voice, base_url: e.target.value } })
                      }
                      placeholder="Em branco usa a URL principal"
                      className={inputCls}
                    />
                  </SettingField>
                )}
                {discoverKind === 'voice' && (
                  <ProviderModelPicker
                    kind="voice"
                    profileId={draft.id}
                    baseUrl={effectiveDraftUrl('voice')}
                    apiKey={draft.api_key}
                    selectedTtsModel={draft.voice.tts_model}
                    selectedSttModel={draft.voice.stt_model}
                    selectedVoice={draft.voice.narrator_voice}
                    onSelectTtsModel={(id) =>
                      setDraft((prev) => ({ ...prev, voice: { ...prev.voice, tts_model: id } }))
                    }
                    onSelectSttModel={(id) =>
                      setDraft((prev) => ({ ...prev, voice: { ...prev.voice, stt_model: id } }))
                    }
                    onApplyVoices={(vs, chosen) =>
                      setDraft((prev) => ({
                        ...prev,
                        voice: {
                          ...prev.voice,
                          voices: vs,
                          narrator_voice: chosen || vs[0] || prev.voice.narrator_voice,
                        },
                      }))
                    }
                    title="Catálogo de voz deste perfil"
                    subtitle="Puxa modelos TTS/STT e vozes usando a URL e chave deste perfil."
                  />
                )}
              </div>
            )}

            {draft.capabilities.includes('embeddings') && (
              <div className="rounded-xl border border-subtle bg-elevated/30 p-3 space-y-3">
                <div className="flex items-center justify-between">
                  <span className="text-[11px] font-bold uppercase tracking-wider text-primary">
                    Embeddings — Modelo de memória vetorial
                  </span>
                  <button
                    type="button"
                    onClick={() => setDiscoverKind(discoverKind === 'embeddings' ? null : 'embeddings')}
                    className="text-[11px] font-semibold text-accent-pri hover:underline"
                  >
                    {discoverKind === 'embeddings' ? 'Fechar catálogo' : 'Puxar modelos da API'}
                  </button>
                </div>
                <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
                  <SettingField label="Modelo de embedding" labelClassName={labelCls}>
                    <input
                      type="text"
                      value={draft.embeddings.model}
                      onChange={(e) =>
                        setDraft({
                          ...draft,
                          embeddings: { ...draft.embeddings, model: e.target.value },
                        })
                      }
                      placeholder="text-embedding-3-small"
                      className={inputCls}
                    />
                  </SettingField>
                  {showAdvancedUrls && (
                    <SettingField label="URL específica de embeddings (opcional)" labelClassName={labelCls}>
                      <input
                        type="text"
                        value={draft.embeddings.base_url}
                        onChange={(e) =>
                          setDraft({
                            ...draft,
                            embeddings: { ...draft.embeddings, base_url: e.target.value },
                          })
                        }
                        placeholder="Em branco usa a URL principal"
                        className={inputCls}
                      />
                    </SettingField>
                  )}
                </div>
                {discoverKind === 'embeddings' && (
                  <ProviderModelPicker
                    kind="embeddings"
                    profileId={draft.id}
                    baseUrl={effectiveDraftUrl('embeddings')}
                    apiKey={draft.api_key}
                    selectedModel={draft.embeddings.model}
                    onSelectModel={(id) =>
                      setDraft((prev) => ({ ...prev, embeddings: { ...prev.embeddings, model: id } }))
                    }
                    title="Catálogo de embeddings deste perfil"
                    subtitle="Puxa e pesquisa modelos de embedding usando a URL e chave deste perfil."
                  />
                )}
              </div>
            )}
          </div>

          <div className="flex items-center justify-end gap-2 pt-2 border-t border-subtle">
            <button
              type="button"
              onClick={() => { setEditorOpen(false); setDiscoverKind(null) }}
              className="rounded-xl border border-subtle px-3.5 py-2 text-xs text-secondary hover:text-primary"
            >
              Cancelar
            </button>
            <button
              type="submit"
              disabled={submitting || !draft.name.trim()}
              className="rounded-xl bg-accent-pri px-4 py-2 text-xs font-semibold text-canvas shadow hover:bg-accent-hov transition disabled:opacity-50"
            >
              {submitting ? 'Salvando…' : draft.id ? 'Salvar alterações' : 'Criar perfil'}
            </button>
          </div>
        </form>
      )}

      {/* Lista de perfis salvos */}
      {profiles.length === 0 ? (
        <div className="rounded-2xl border border-dashed border-subtle bg-surface p-8 text-center space-y-3">
          <Layers className="w-8 h-8 text-accent-pri/70 mx-auto" />
          <div className="space-y-1">
            <h3 className="text-sm font-bold text-primary">Nenhum perfil de provedor salvo ainda</h3>
            <p className="text-xs text-secondary max-w-md mx-auto">
              Capture seu setup atual da VM/local ou crie perfis para OpenAI, OpenRouter, Together, Groq, Fireworks, etc.
              Você salva cada API key uma só vez e reaproveita em todas as soluções do provedor.
            </p>
          </div>
          <div className="flex flex-wrap items-center justify-center gap-2 pt-1">
            <button
              type="button"
              onClick={() => setCaptureOpen(true)}
              className="flex items-center gap-1.5 rounded-xl border border-subtle bg-elevated px-3.5 py-2 text-xs font-semibold text-primary hover:border-accent-pri/50 transition"
            >
              <Download className="w-3.5 h-3.5 text-accent-pri" />
              <span>Salvar setup atual como perfil</span>
            </button>
            <button
              type="button"
              onClick={() => openNew(templates[0] || null)}
              className="flex items-center gap-1.5 rounded-xl bg-accent-pri px-3.5 py-2 text-xs font-semibold text-canvas shadow hover:bg-accent-hov transition"
            >
              <Plus className="w-3.5 h-3.5" />
              <span>Criar primeiro perfil</span>
            </button>
          </div>
        </div>
      ) : (
        <div className="space-y-3">
          {profiles.map((prof) => {
            const caps = prof.capabilities || []
            const activeKinds = caps.filter((k) => active?.[k] === prof.id)
            const isAllActive = caps.length > 0 && activeKinds.length === caps.length
            const isBusy = activatingId === prof.id

            return (
              <div
                key={prof.id}
                className={`rounded-2xl border bg-surface p-4 space-y-3 transition ${
                  activeKinds.length > 0 ? 'border-accent-pri/50' : 'border-subtle'
                }`}
              >
                <div className="flex flex-wrap items-start justify-between gap-3">
                  <div className="min-w-0 flex-1 space-y-1">
                    <div className="flex flex-wrap items-center gap-2">
                      <h3 className="text-sm font-bold text-primary">{prof.name}</h3>
                      {prof.api_key_set ? (
                        <span className="inline-flex items-center gap-1 rounded-full border border-accent-pri/40 bg-accent-pri/10 px-2 py-0.5 text-[10px] font-mono text-accent-pri">
                          <KeyRound className="w-2.5 h-2.5" />
                          <span>{prof.api_key || 'Chave salva'}</span>
                        </span>
                      ) : (
                        <span className="rounded-full border border-subtle bg-elevated px-2 py-0.5 text-[10px] text-muted">
                          Sem chave API
                        </span>
                      )}
                      {activeKinds.length > 0 && (
                        <span className="rounded-full bg-accent-pri text-canvas px-2 py-0.5 text-[10px] font-bold">
                          Ativo: {activeKinds.map((k) => KIND_LABEL[k]).join(', ')}
                        </span>
                      )}
                    </div>
                    {prof.base_url && (
                      <div className="text-[11px] font-mono text-muted truncate" title={prof.base_url}>
                        {prof.base_url}
                      </div>
                    )}
                    <div className="flex flex-wrap gap-x-3 gap-y-1 text-[11px] text-secondary pt-0.5">
                      {caps.includes('llm') && prof.llm?.model_alias && (
                        <span>
                          <strong className="text-muted">LLM:</strong>{' '}
                          <span className="font-mono">{prof.llm.model_alias}</span>
                        </span>
                      )}
                      {caps.includes('image') && (
                        <span>
                          <strong className="text-muted">Imagem:</strong>{' '}
                          <span className="font-mono">
                            {prof.image?.provider === 'comfyui'
                              ? 'ComfyUI'
                              : prof.image?.provider === 'sdwebui'
                                ? (prof.image?.sd_model_checkpoint || 'SD WebUI')
                                : (prof.image?.model || 'OpenAI-compat')}
                          </span>
                        </span>
                      )}
                      {caps.includes('voice') && (prof.voice?.tts_model || prof.voice?.narrator_voice) && (
                        <span>
                          <strong className="text-muted">Voz:</strong>{' '}
                          <span className="font-mono">
                            {[prof.voice?.tts_model, prof.voice?.narrator_voice].filter(Boolean).join(' · ')}
                          </span>
                        </span>
                      )}
                      {caps.includes('embeddings') && prof.embeddings?.model && (
                        <span>
                          <strong className="text-muted">Embeddings:</strong>{' '}
                          <span className="font-mono">{prof.embeddings.model}</span>
                        </span>
                      )}
                    </div>
                  </div>

                  <div className="flex items-center gap-1.5 shrink-0">
                    <button
                      type="button"
                      onClick={() => openEdit(prof)}
                      className="flex items-center gap-1 rounded-lg border border-subtle bg-elevated px-2.5 py-1.5 text-xs font-medium text-secondary hover:text-primary transition"
                    >
                      <Edit3 className="w-3.5 h-3.5" />
                      <span>Editar</span>
                    </button>
                    <button
                      type="button"
                      onClick={() => onDelete && onDelete(prof)}
                      className="flex items-center gap-1 rounded-lg border border-danger-900/70 px-2.5 py-1.5 text-xs text-danger-400 hover:bg-danger-950 transition"
                      aria-label={`Excluir perfil ${prof.name}`}
                    >
                      <Trash2 className="w-3.5 h-3.5" />
                    </button>
                  </div>
                </div>

                {/* Botões de ativação por modalidade ou em todas */}
                <div className="flex flex-wrap items-center gap-1.5 pt-2 border-t border-subtle/60">
                  {caps.length > 1 && (
                    <button
                      type="button"
                      disabled={Boolean(activatingId)}
                      onClick={() => onActivate && onActivate(prof.id, caps)}
                      className={`flex items-center gap-1 rounded-lg border px-2.5 py-1 text-[11px] font-semibold transition disabled:opacity-60 ${
                        isAllActive
                          ? 'border-accent-pri bg-accent-pri text-canvas'
                          : 'border-accent-pri/50 bg-accent-pri/15 text-accent-pri hover:bg-accent-pri/25'
                      }`}
                    >
                      {isAllActive && <Check className="w-3 h-3" />}
                      <span>
                        {isBusy
                          ? 'Aplicando…'
                          : isAllActive
                            ? `Ativo em todas (${caps.length})`
                            : `Aplicar em todas (${caps.length})`}
                      </span>
                    </button>
                  )}

                  {caps.map((k) => {
                    const activeHere = active?.[k] === prof.id
                    return (
                      <button
                        key={k}
                        type="button"
                        disabled={Boolean(activatingId)}
                        onClick={() => onActivate && onActivate(prof.id, [k])}
                        className={`flex items-center gap-1 rounded-lg border px-2.5 py-1 text-[11px] font-medium transition disabled:opacity-60 ${
                          activeHere
                            ? 'border-accent-pri bg-accent-pri text-canvas font-semibold'
                            : 'border-subtle bg-elevated/60 text-secondary hover:text-primary hover:border-accent-pri/40'
                        }`}
                      >
                        {activeHere && <Check className="w-3 h-3" />}
                        <span>{activeHere ? `${KIND_LABEL[k]} ativo` : `Usar em ${KIND_LABEL[k]}`}</span>
                      </button>
                    )
                  })}
                </div>
              </div>
            )
          })}
        </div>
      )}
    </div>
  )
}
