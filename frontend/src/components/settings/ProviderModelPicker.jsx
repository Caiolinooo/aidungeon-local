import React, { useMemo, useState } from 'react'
import { api } from '../../api.js'
import { Check, RefreshCw, Search, Sparkles, Volume2, Mic } from 'lucide-react'

const KIND_BADGE = {
  llm: 'LLM',
  image: 'Imagem',
  tts: 'TTS',
  stt: 'STT',
  embeddings: 'Embedding',
  other: 'Modelo',
}

function formatContext(ctx) {
  const n = Number(ctx)
  if (!Number.isFinite(n) || n <= 0) return ''
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1).replace(/\.0$/, '')}M ctx`
  if (n >= 1000) return `${Math.round(n / 1024)}k ctx`
  return `${n} ctx`
}

function normalizeUrl(url) {
  return String(url || '').trim().replace(/\/+$/, '').toLowerCase()
}

export default function ProviderModelPicker({
  kind,
  provider = null,
  profileId = null,
  presets = [],
  baseUrl = '',
  apiKey = '',
  selectedModel = '',
  selectedTtsModel = '',
  selectedSttModel = '',
  selectedVoice = '',
  onSelectPreset,
  onSelectModel,
  onSelectTtsModel,
  onSelectSttModel,
  onApplyVoices,
  allowSelect = true,
  title = 'Provedores e catálogo de modelos',
  subtitle = 'Selecione um provedor ou puxe e pesquise os modelos direto da API.',
}) {
  const [models, setModels] = useState([])
  const [voices, setVoices] = useState([])
  const [query, setQuery] = useState('')
  const [onlyMatching, setOnlyMatching] = useState(true)
  const [loading, setLoading] = useState(false)
  const [fetched, setFetched] = useState(false)
  const [error, setError] = useState('')
  const [totalAvailable, setTotalAvailable] = useState(0)

  const activePresetId = useMemo(() => {
    const norm = normalizeUrl(baseUrl)
    if (!norm) return ''
    const found = presets.find((p) => normalizeUrl(p.base_url) === norm)
    return found?.id || ''
  }, [baseUrl, presets])

  const handleFetch = async (overrideQuery = '') => {
    setLoading(true)
    setError('')
    try {
      const res = await api.discoverModels({
        kind,
        provider: provider || undefined,
        profile_id: profileId || undefined,
        base_url: (baseUrl || '').trim() || undefined,
        api_key: (apiKey || '').trim() || undefined,
        query: (overrideQuery || '').trim(),
        only_matching: false,
        limit: 500,
      })
      setFetched(true)
      if (res.status !== 'ok') {
        setModels([])
        setVoices(res.voices || [])
        setTotalAvailable(0)
        setError(res.detail || `Não foi possível consultar o provedor (${res.status}).`)
        return
      }
      setModels(res.models || [])
      setVoices(res.voices || [])
      setTotalAvailable(res.total_available ?? (res.models || []).length)
    } catch (e) {
      setFetched(true)
      setModels([])
      setError(e.message || 'Falha ao consultar modelos no provedor.')
    } finally {
      setLoading(false)
    }
  }

  const handlePresetClick = (preset) => {
    setModels([])
    setVoices(preset.default_voices || [])
    setFetched(false)
    setError('')
    setQuery('')
    if (onSelectPreset) onSelectPreset(preset)
  }

  const recommendedCount = useMemo(
    () => models.filter((m) => m.recommended).length,
    [models],
  )

  const filteredModels = useMemo(() => {
    const tokens = query.trim().toLowerCase().split(/\s+/).filter(Boolean)
    const basePool = onlyMatching && recommendedCount > 0
      ? models.filter((m) => m.recommended)
      : models
    if (!tokens.length) return basePool
    return basePool.filter((item) => {
      const hay = [
        item.id,
        item.name,
        item.owned_by,
        item.modality,
        item.description,
        item.kind_hint,
      ]
        .filter(Boolean)
        .join(' ')
        .toLowerCase()
      return tokens.every((tok) => hay.includes(tok))
    })
  }, [models, query, onlyMatching, recommendedCount])

  const isVoice = kind === 'voice'

  return (
    <div className="rounded-xl border border-subtle bg-elevated/40 p-3.5 space-y-3">
      <div className="flex flex-wrap items-start justify-between gap-2">
        <div>
          <h4 className="text-[11px] font-bold uppercase tracking-wider text-primary flex items-center gap-1.5">
            <Sparkles className="w-3.5 h-3.5 text-accent-pri" />
            <span>{title}</span>
          </h4>
          <p className="text-[11px] text-secondary mt-0.5">{subtitle}</p>
        </div>

        <button
          type="button"
          onClick={() => handleFetch('')}
          disabled={loading}
          className="flex items-center gap-1.5 rounded-lg border border-accent-pri/50 bg-accent-pri/15 px-3 py-1.5 text-xs font-semibold text-accent-pri hover:bg-accent-pri/25 transition disabled:opacity-50"
        >
          <RefreshCw className={`w-3.5 h-3.5 ${loading ? 'animate-spin' : ''}`} />
          <span>{loading ? 'Puxando modelos…' : 'Puxar modelos da API'}</span>
        </button>
      </div>

      {/* Presets de provedores */}
      {presets.length > 0 && (
        <div className="space-y-1.5">
          <div className="text-[10px] font-semibold uppercase tracking-wider text-muted">
            Atalhos de provedores
          </div>
          <div className="flex flex-wrap gap-1.5">
            {presets.map((preset) => {
              const active = activePresetId === preset.id
              return (
                <button
                  key={preset.id}
                  type="button"
                  onClick={() => handlePresetClick(preset)}
                  className={`rounded-lg border px-2.5 py-1 text-[11px] font-medium transition ${
                    active
                      ? 'border-accent-pri bg-accent-pri text-canvas font-semibold shadow-sm'
                      : 'border-subtle bg-surface text-secondary hover:text-primary hover:border-accent-pri/40'
                  }`}
                >
                  {preset.label}
                </button>
              )
            })}
          </div>
        </div>
      )}

      {/* Erro */}
      {error && (
        <div className="rounded-lg border border-danger-800 bg-danger-950/50 px-3 py-2 text-xs text-danger-300">
          {error}
        </div>
      )}

      {/* Barra de pesquisa e filtros */}
      {fetched && !error && (
        <div className="space-y-2">
          <div className="flex flex-wrap items-center gap-2">
            <div className="relative flex-1 min-w-[200px]">
              <Search className="pointer-events-none absolute left-2.5 top-1/2 -translate-y-1/2 w-3.5 h-3.5 text-muted" />
              <input
                type="text"
                value={query}
                onChange={(e) => setQuery(e.target.value)}
                placeholder="Pesquisar modelos por nome, provedor ou ID…"
                aria-label="Pesquisar modelos"
                className="w-full rounded-lg border border-subtle bg-surface pl-8 pr-3 py-1.5 text-xs text-primary outline-none focus:border-accent-pri transition"
              />
            </div>

            {recommendedCount > 0 && recommendedCount < models.length && (
              <div className="flex rounded-lg border border-subtle bg-surface p-0.5 text-[11px]">
                <button
                  type="button"
                  onClick={() => setOnlyMatching(true)}
                  className={`rounded-md px-2 py-1 font-medium transition ${
                    onlyMatching ? 'bg-accent-pri text-canvas font-semibold' : 'text-secondary hover:text-primary'
                  }`}
                >
                  Compatíveis ({recommendedCount})
                </button>
                <button
                  type="button"
                  onClick={() => setOnlyMatching(false)}
                  className={`rounded-md px-2 py-1 font-medium transition ${
                    !onlyMatching ? 'bg-accent-pri text-canvas font-semibold' : 'text-secondary hover:text-primary'
                  }`}
                >
                  Todos ({models.length})
                </button>
              </div>
            )}

            <span className="text-[11px] font-mono text-muted">
              {filteredModels.length} / {totalAvailable}
            </span>
          </div>

          {filteredModels.length === 0 ? (
            <div className="rounded-lg border border-subtle bg-surface px-3 py-3 text-center text-xs text-muted">
              {models.length === 0
                ? 'Nenhum modelo retornado por este endpoint.'
                : 'Nenhum modelo corresponde à pesquisa.'}
            </div>
          ) : (
            <div className="max-h-60 overflow-y-auto rounded-lg border border-subtle bg-surface divide-y divide-subtle/60">
              {filteredModels.slice(0, 120).map((model) => {
                const ctx = formatContext(model.context_length)
                const isSelected = !isVoice && selectedModel === model.id
                const isTtsSelected = isVoice && selectedTtsModel === model.id
                const isSttSelected = isVoice && selectedSttModel === model.id
                const badge = KIND_BADGE[model.kind_hint] || 'Modelo'

                return (
                  <div
                    key={model.id}
                    className={`flex flex-wrap items-center justify-between gap-2 px-3 py-2 text-xs transition ${
                      isSelected || isTtsSelected || isSttSelected
                        ? 'bg-accent-pri/10'
                        : 'hover:bg-elevated/60'
                    }`}
                  >
                    <div className="min-w-0 flex-1">
                      <div className="flex flex-wrap items-center gap-1.5">
                        <span className="font-mono text-xs font-semibold text-primary break-all">
                          {model.id}
                        </span>
                        <span className="rounded bg-elevated px-1.5 py-0.5 text-[10px] font-semibold text-secondary border border-subtle">
                          {badge}
                        </span>
                        {ctx && (
                          <span className="rounded bg-elevated px-1.5 py-0.5 text-[10px] font-mono text-muted">
                            {ctx}
                          </span>
                        )}
                      </div>
                      {(model.name && model.name !== model.id) || model.owned_by ? (
                        <div className="text-[11px] text-muted truncate mt-0.5">
                          {model.name && model.name !== model.id ? model.name : ''}
                          {model.name && model.name !== model.id && model.owned_by ? ' · ' : ''}
                          {model.owned_by || ''}
                        </div>
                      ) : null}
                    </div>

                    {allowSelect && (
                      <div className="flex items-center gap-1.5 shrink-0">
                        {isVoice ? (
                          <>
                            <button
                              type="button"
                              onClick={() => onSelectTtsModel && onSelectTtsModel(model.id)}
                              className={`flex items-center gap-1 rounded-md border px-2 py-1 text-[11px] font-semibold transition ${
                                isTtsSelected
                                  ? 'border-accent-pri bg-accent-pri text-canvas'
                                  : 'border-subtle bg-elevated text-secondary hover:text-primary'
                              }`}
                            >
                              <Volume2 className="w-3 h-3" />
                              <span>{isTtsSelected ? 'TTS ativo' : 'Usar TTS'}</span>
                            </button>
                            <button
                              type="button"
                              onClick={() => onSelectSttModel && onSelectSttModel(model.id)}
                              className={`flex items-center gap-1 rounded-md border px-2 py-1 text-[11px] font-semibold transition ${
                                isSttSelected
                                  ? 'border-accent-pri bg-accent-pri text-canvas'
                                  : 'border-subtle bg-elevated text-secondary hover:text-primary'
                              }`}
                            >
                              <Mic className="w-3 h-3" />
                              <span>{isSttSelected ? 'STT ativo' : 'Usar STT'}</span>
                            </button>
                          </>
                        ) : (
                          <button
                            type="button"
                            onClick={() => onSelectModel && onSelectModel(model.id)}
                            className={`flex items-center gap-1 rounded-md border px-2.5 py-1 text-[11px] font-semibold transition ${
                              isSelected
                                ? 'border-accent-pri bg-accent-pri text-canvas'
                                : 'border-subtle bg-elevated text-secondary hover:text-primary'
                            }`}
                          >
                            {isSelected && <Check className="w-3 h-3" />}
                            <span>{isSelected ? 'Selecionado' : 'Usar'}</span>
                          </button>
                        )}
                      </div>
                    )}
                  </div>
                )
              })}
              {filteredModels.length > 120 && (
                <div className="px-3 py-2 text-center text-[11px] text-muted">
                  Mostrando os primeiros 120 de {filteredModels.length} modelos. Refine a pesquisa acima.
                </div>
              )}
            </div>
          )}
        </div>
      )}

      {/* Vozes descobertas / do provedor (modalidade voice) */}
      {isVoice && voices.length > 0 && (
        <div className="space-y-1.5 pt-1 border-t border-subtle/60">
          <div className="flex flex-wrap items-center justify-between gap-2">
            <span className="text-[10px] font-semibold uppercase tracking-wider text-muted">
              Vozes disponíveis neste provedor ({voices.length})
            </span>
            {onApplyVoices && (
              <button
                type="button"
                onClick={() => onApplyVoices(voices, voices.includes(selectedVoice) ? selectedVoice : voices[0])}
                className="text-[11px] font-semibold text-accent-pri hover:underline"
              >
                Aplicar lista de vozes ao formulário
              </button>
            )}
          </div>
          <div className="flex flex-wrap gap-1.5">
            {voices.slice(0, 32).map((voice) => {
              const active = selectedVoice === voice
              return (
                <button
                  key={voice}
                  type="button"
                  onClick={() => onApplyVoices && onApplyVoices(voices, voice)}
                  className={`rounded-md border px-2 py-0.5 text-[11px] font-mono transition ${
                    active
                      ? 'border-accent-pri bg-accent-pri text-canvas font-semibold'
                      : 'border-subtle bg-surface text-secondary hover:text-primary'
                  }`}
                >
                  {voice}
                </button>
              )
            })}
          </div>
        </div>
      )}
    </div>
  )
}
