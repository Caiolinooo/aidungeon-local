// Escolha de um modelo do runtime: o que está em uso, a busca nos catálogos
// (CivitAI, CivitAI Red, Hugging Face), LoRA, amostragem do checkpoint e o
// download para o ComfyUI desta máquina.
import { useState } from 'react'
import { Download, Search, Trash2 } from 'lucide-react'
import { api } from '../../api.js'
import { useToast } from '../Toast.jsx'

const SOURCES = [
  { id: 'civitai', label: 'CivitAI' },
  { id: 'civitai_red', label: 'CivitAI Red' },
  { id: 'huggingface', label: 'Hugging Face' },
]

const FAMILY_LABEL = {
  checkpoint: 'Arquivo completo (SDXL, Pony, Illustrious, SD 1.5)',
  flux2_klein: 'Flux.2 Klein 4B (só o modelo de difusão)',
  z_image_turbo: 'Z-Image Turbo (só o modelo de difusão)',
  qwen: 'Qwen Image 2.1 (só o modelo de difusão)',
  ltxv: 'LTXV (arquivo completo)',
  wan: 'Wan 2.2 TI2V 5B (só o modelo de difusão)',
  h3: 'MiniMax H3 (só o modelo de difusão)',
  gguf: 'GGUF para o llama-server',
  lora: 'LoRA',
}

const CUSTOM_FOLDERS = [
  'checkpoints', 'diffusion_models', 'text_encoders', 'vae', 'loras',
  'clip_vision', 'controlnet', 'upscale_models',
]

const CUSTOM_HELP = 'Cole o fluxo exportado em Arquivo → Exportar (API). Use {{prompt}} no texto positivo e, se quiser, {{negative}}, {{width}}, {{height}}, {{seed}} e {{reference_1}}.'

function sizeLabel(bytes) {
  if (!bytes) return ''
  const gb = bytes / 1024 ** 3
  return gb >= 1 ? `${gb.toFixed(1)} GB` : `${Math.max(1, Math.round(bytes / 1024 ** 2))} MB`
}

export function modelLabel(id, entry) {
  if (!entry || typeof entry !== 'object') return id
  return entry.label || entry.model_alias || id
}

function entrySize(entry) {
  return (entry?.artifacts || []).reduce((sum, art) => sum + (art.bytes || 0), 0)
}

function ResultRow({ row, kind, families, onAdd, onCollect, busy, inputCls }) {
  const toast = useToast()
  const [files, setFiles] = useState(row.files)
  const [fileIndex, setFileIndex] = useState(0)
  const [family, setFamily] = useState(row.family || families[0])
  const [folder, setFolder] = useState('checkpoints')
  const [loadingFiles, setLoadingFiles] = useState(false)

  const loadFiles = async () => {
    setLoadingFiles(true)
    try {
      const res = await api.listRepoFiles(row.id)
      setFiles(res.files || [])
    } catch (e) {
      toast('Não consegui listar os arquivos: ' + e.message)
    } finally {
      setLoadingFiles(false)
    }
  }

  const file = files?.[fileIndex]
  return (
    <li className="rounded-xl border border-subtle bg-surface p-3 space-y-2 text-xs">
      <div className="flex flex-wrap items-baseline gap-x-2 gap-y-1">
        <a href={row.page_url} target="_blank" rel="noreferrer" className="font-semibold text-primary hover:underline break-all">
          {row.name}
        </a>
        {row.base_model && <span className="rounded bg-elevated px-1.5 py-0.5 text-[10px] text-muted">{row.base_model}</span>}
        {row.nsfw && <span className="rounded bg-elevated px-1.5 py-0.5 text-[10px] text-muted">adulto</span>}
        {row.gated && <span className="rounded bg-elevated px-1.5 py-0.5 text-[10px] text-muted">pede token</span>}
      </div>
      {!row.family && kind !== 'lora' && row.source !== 'huggingface' && (
        <p className="text-[11px] text-muted">
          O jogo não tem fluxo pronto para esta família. Escolha abaixo como carregar, ou use num fluxo próprio.
        </p>
      )}
      {files === null ? (
        <button type="button" onClick={loadFiles} disabled={loadingFiles} className="rounded-lg border border-subtle px-3 py-1.5 font-semibold text-secondary hover:text-primary disabled:opacity-50">
          {loadingFiles ? 'Lendo…' : 'Ver arquivos'}
        </button>
      ) : files.length === 0 ? (
        <p className="text-muted">Nenhum arquivo .safetensors ou .gguf com SHA256 neste repositório.</p>
      ) : (
        <div className="grid grid-cols-1 gap-2 sm:grid-cols-[1fr_1fr_auto]">
          <select aria-label="Arquivo" value={fileIndex} onChange={(e) => setFileIndex(Number(e.target.value))} className={inputCls}>
            {files.map((item, index) => (
              <option key={item.url + item.name} value={index}>
                {item.path || item.name}{item.bytes ? ` · ${sizeLabel(item.bytes)}` : ''}
              </option>
            ))}
          </select>
          {onCollect ? (
            <select aria-label="Pasta no ComfyUI" value={folder} onChange={(e) => setFolder(e.target.value)} className={inputCls}>
              {CUSTOM_FOLDERS.map((name) => <option key={name} value={name}>models/{name}</option>)}
            </select>
          ) : families.length > 1 ? (
            <select aria-label="Usar como" value={family} onChange={(e) => setFamily(e.target.value)} className={inputCls}>
              {families.map((id) => <option key={id} value={id}>{FAMILY_LABEL[id] || id}</option>)}
            </select>
          ) : <span />}
          <button
            type="button"
            disabled={busy || !file}
            onClick={() => (onCollect ? onCollect({ ...file, folder }) : onAdd(row, file, family))}
            className="min-h-10 rounded-lg bg-accent-pri px-3 py-1.5 font-semibold text-canvas hover:bg-accent-hov disabled:opacity-50"
          >
            {onCollect ? 'Juntar ao fluxo' : kind === 'lora' ? 'Adicionar LoRA' : 'Usar este'}
          </button>
        </div>
      )}
    </li>
  )
}

function ModelSearch({ kind, families, onAdd, onCollect, busy, inputCls }) {
  const toast = useToast()
  const [source, setSource] = useState('civitai')
  const [query, setQuery] = useState('')
  const [rows, setRows] = useState(null)
  const [searching, setSearching] = useState(false)

  const run = async (event) => {
    event?.preventDefault()
    if (query.trim().length < 2) return
    setSearching(true)
    try {
      const res = await api.searchModels(source, kind, query.trim())
      setRows(res.results || [])
    } catch (e) {
      toast('Busca falhou: ' + e.message)
    } finally {
      setSearching(false)
    }
  }

  return (
    <div className="space-y-2 rounded-xl border border-subtle bg-elevated/40 p-3">
      <div role="radiogroup" aria-label="Catálogo" className="flex flex-wrap gap-1.5">
        {SOURCES.map((item) => (
          <button
            key={item.id}
            type="button"
            role="radio"
            aria-checked={source === item.id}
            onClick={() => { setSource(item.id); setRows(null) }}
            className={`rounded-lg border px-3 py-1.5 text-xs font-semibold transition ${source === item.id ? 'border-accent-pri bg-surface text-primary' : 'border-subtle text-secondary hover:text-primary'}`}
          >
            {item.label}
          </button>
        ))}
      </div>
      <form onSubmit={run} className="flex gap-2">
        <input
          type="search"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          placeholder={kind === 'llm' ? 'Nome do modelo GGUF' : kind === 'lora' ? 'Nome do LoRA' : 'Nome do modelo'}
          className={inputCls}
        />
        <button type="submit" disabled={searching || query.trim().length < 2} className="flex min-h-10 shrink-0 items-center gap-1.5 rounded-xl border border-subtle bg-surface px-3 text-xs font-semibold text-primary disabled:opacity-50">
          <Search className="h-3.5 w-3.5" />
          {searching ? 'Buscando…' : 'Buscar'}
        </button>
      </form>
      {rows && rows.length === 0 && <p className="text-xs text-muted">Nada encontrado com arquivo .safetensors ou .gguf.</p>}
      {rows && rows.length > 0 && (
        <ul className="max-h-96 space-y-2 overflow-y-auto pr-1">
          {rows.map((row) => (
            <ResultRow key={`${row.source}-${row.id}`} row={row} kind={kind} families={families} onAdd={onAdd} onCollect={onCollect} busy={busy} inputCls={inputCls} />
          ))}
        </ul>
      )}
    </div>
  )
}

function CustomWorkflow({ kind, onCreated, inputCls, labelCls }) {
  const toast = useToast()
  const [label, setLabel] = useState('')
  const [graphText, setGraphText] = useState('')
  const [nodesText, setNodesText] = useState('')
  const [files, setFiles] = useState([])
  const [saving, setSaving] = useState(false)

  const save = async () => {
    let graph
    try {
      graph = JSON.parse(graphText)
    } catch {
      toast('O fluxo não é um JSON válido.')
      return
    }
    const nodes = nodesText.split('\n').map((line) => line.trim()).filter(Boolean).map((line) => {
      const [git_url, git_ref] = line.split(/\s+/)
      return { git_url, git_ref: git_ref || '' }
    })
    setSaving(true)
    try {
      const res = await api.addLibraryModel({ kind, family: 'custom', label, graph, files, nodes })
      onCreated(res.id)
    } catch (e) {
      toast('Não consegui salvar o fluxo: ' + e.message)
    } finally {
      setSaving(false)
    }
  }

  return (
    <div className="space-y-3 rounded-xl border border-subtle bg-elevated/40 p-3 text-xs text-secondary">
      <p>{CUSTOM_HELP}</p>
      {kind === 'video' && <p>Use {"{{reference_1}}"} na imagem inicial e salve o resultado em MP4. Os marcadores {"{{frames}}"}, {"{{fps}}"}, {"{{steps}}"} e {"{{cfg}}"} recebem os valores configurados.</p>}
      <label className="block">
        <span className={labelCls}>Nome</span>
        <input value={label} onChange={(e) => setLabel(e.target.value)} className={inputCls} placeholder="Meu fluxo" />
      </label>
      <label className="block">
        <span className={labelCls}>Fluxo (JSON no formato API)</span>
        <textarea value={graphText} onChange={(e) => setGraphText(e.target.value)} rows={6} spellCheck={false} className={`${inputCls} font-mono`} />
      </label>
      <label className="block">
        <span className={labelCls}>Nodes extras, um por linha: link do GitHub e commit ou tag</span>
        <textarea value={nodesText} onChange={(e) => setNodesText(e.target.value)} rows={2} spellCheck={false} className={`${inputCls} font-mono`} placeholder="https://github.com/autor/ComfyUI-Node.git abc1234" />
      </label>
      <div>
        <span className={labelCls}>Arquivos que o fluxo usa</span>
        {files.length === 0 && <p className="text-muted">Busque abaixo e junte cada arquivo, escolhendo a pasta.</p>}
        <ul className="space-y-1">
          {files.map((file, index) => (
            <li key={file.url + file.name} className="flex items-center justify-between gap-2 rounded-lg border border-subtle bg-surface px-2.5 py-1.5">
              <span className="truncate font-mono text-[11px] text-primary">models/{file.folder}/{file.name}</span>
              <button type="button" aria-label={`Tirar ${file.name}`} onClick={() => setFiles((prev) => prev.filter((_, i) => i !== index))} className="text-danger-400">
                <Trash2 className="h-3.5 w-3.5" />
              </button>
            </li>
          ))}
        </ul>
      </div>
      <ModelSearch kind={kind} families={[]} onCollect={(file) => setFiles((prev) => [...prev, file])} inputCls={inputCls} />
      <button type="button" onClick={save} disabled={saving || !graphText.trim() || files.length === 0} className="min-h-10 rounded-xl bg-accent-pri px-4 py-2 text-xs font-semibold text-canvas hover:bg-accent-hov disabled:opacity-50">
        {saving ? 'Salvando…' : 'Salvar e usar este fluxo'}
      </button>
    </div>
  )
}

function CheckpointSampling({ block, onApply, inputCls, labelCls }) {
  const [draft, setDraft] = useState(() => ({
    steps: block.steps ?? 28, cfg: block.cfg ?? 6, sampler: block.sampler || '', scheduler: block.scheduler || '',
  }))
  const field = (key, label, type = 'text', step) => (
    <label className="block">
      <span className={labelCls}>{label}</span>
      <input type={type} step={step} value={draft[key]} onChange={(e) => setDraft({ ...draft, [key]: e.target.value })} className={inputCls} />
    </label>
  )
  return (
    <div className="grid grid-cols-2 gap-2 sm:grid-cols-5">
      {field('steps', 'Passos', 'number', 1)}
      {field('cfg', 'CFG', 'number', 0.5)}
      {field('sampler', 'Sampler')}
      {field('scheduler', 'Scheduler')}
      <button type="button" onClick={() => onApply({ ...draft, steps: Number(draft.steps), cfg: Number(draft.cfg) })} className="min-h-10 self-end rounded-xl border border-subtle bg-surface px-3 text-xs font-semibold text-primary">
        Aplicar
      </button>
    </div>
  )
}

export default function ModelPicker({
  kind, title, value, catalog, families, place, library, onPick, onLibraryChange, onInstall, inputCls, labelCls,
}) {
  const toast = useToast()
  const [panel, setPanel] = useState('')
  const [busy, setBusy] = useState(false)
  const entry = catalog?.[value]
  const ids = Object.keys(catalog || {})
  const loras = Array.isArray(entry?.loras) ? entry.loras : []
  const install = library?.install
  const installing = install?.state === 'running'

  const act = async (work, okMessage) => {
    setBusy(true)
    try {
      const id = await work()
      await onLibraryChange(kind, id)
      if (okMessage) toast(okMessage, 'ok')
      return true
    } catch (e) {
      toast(e.message)
      return false
    } finally {
      setBusy(false)
    }
  }

  const addFromSearch = (row, file, family) => act(async () => {
    const res = await api.addLibraryModel({
      kind, family, label: row.name, base_model: row.base_model, page_url: row.page_url, file,
    })
    setPanel('')
    return res.id
  }, 'Modelo adicionado. Salve para aplicar.')

  const addLora = (row, file) => act(async () => {
    const res = await api.addModelLora(value, { file, strength: 0.8 })
    setPanel('')
    return res.id
  }, 'LoRA adicionado.')

  const setLoras = (next) => act(async () => (await api.updateLibraryModel('image', value, { loras: next })).id)

  const remove = () => act(async () => {
    await api.deleteLibraryModel(kind, value)
    return ids.find((id) => id !== value && !catalog[id]?.user) || ''
  }, 'Modelo removido da biblioteca.')

  return (
    <section className="space-y-3 rounded-2xl border border-subtle bg-surface p-4">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h3 className="text-xs font-semibold uppercase tracking-wider text-muted">{title}</h3>
        <div className="flex flex-wrap gap-1.5">
          <button type="button" onClick={() => setPanel(panel === 'search' ? '' : 'search')} className="rounded-lg border border-subtle px-3 py-1.5 text-xs font-semibold text-secondary hover:text-primary">
            {panel === 'search' ? 'Fechar busca' : 'Buscar outro modelo'}
          </button>
          {kind !== 'llm' && (
            <button type="button" onClick={() => setPanel(panel === 'custom' ? '' : 'custom')} className="rounded-lg border border-subtle px-3 py-1.5 text-xs font-semibold text-secondary hover:text-primary">
              {panel === 'custom' ? 'Fechar fluxo' : 'Colar fluxo próprio'}
            </button>
          )}
        </div>
      </div>

      <select aria-label={`${title}: modelo em uso`} value={value} onChange={(e) => onPick(kind, e.target.value)} className={inputCls}>
        {!ids.includes(value) && <option value={value}>{value || 'Selecione um modelo'}</option>}
        {ids.map((id) => (
          <option key={id} value={id}>
            {modelLabel(id, catalog[id])}{catalog[id]?.user ? ' · sua biblioteca' : ''}
          </option>
        ))}
      </select>

      {entry && (
        <p className="text-[11px] text-secondary">
          {FAMILY_LABEL[entry.engine] || entry.engine || FAMILY_LABEL.gguf}
          {entrySize(entry) > 0 && <> · {sizeLabel(entrySize(entry))} para baixar</>}
          {entry.user && typeof entry.source === 'string' && entry.source.startsWith('https://') && (
            <> · <a href={entry.source} target="_blank" rel="noreferrer" className="underline">página do modelo</a></>
          )}
        </p>
      )}
      {kind === 'llm' && entry?.role === 'fallback' && place === 'local' && (
        <p role="alert" className="text-[11px] text-danger-300">
          Este narrador só roda no Colab. Nesta máquina o llama-server usa o modelo do serviço dele.
        </p>
      )}
      {kind === 'image' && (entry?.engine === 'checkpoint' || entry?.engine === 'custom') && (
        <p className="text-[11px] text-muted">
          Este modelo pinta pelo texto. Quando o pedido leva a foto de um personagem, quem pinta é o Flux.2 Klein, para manter o rosto. Um fluxo próprio com {'{{reference_1}}'} recebe a foto direto.
        </p>
      )}

      {kind === 'image' && entry?.engine === 'checkpoint' && entry.checkpoint && (
        <CheckpointSampling
          key={`${value}-${entry.checkpoint.steps}-${entry.checkpoint.cfg}-${entry.checkpoint.sampler}-${entry.checkpoint.scheduler}`}
          block={entry.checkpoint}
          onApply={(checkpoint) => act(async () => (await api.updateLibraryModel('image', value, { checkpoint })).id, 'Amostragem aplicada.')}
          inputCls={inputCls}
          labelCls={labelCls}
        />
      )}

      {kind === 'image' && entry && entry.engine !== 'custom' && (
        <div className="space-y-1.5">
          <div className="flex items-center justify-between gap-2">
            <span className={labelCls}>LoRAs deste modelo</span>
            <button type="button" onClick={() => setPanel(panel === 'lora' ? '' : 'lora')} className="rounded-lg border border-subtle px-3 py-1.5 text-xs font-semibold text-secondary hover:text-primary">
              {panel === 'lora' ? 'Fechar' : 'Adicionar LoRA'}
            </button>
          </div>
          {loras.length === 0 && <p className="text-[11px] text-muted">Nenhum.</p>}
          {loras.map((lora) => (
            <div key={lora.name} className="flex items-center gap-2 rounded-lg border border-subtle bg-elevated/40 px-2.5 py-1.5 text-xs">
              <span className="min-w-0 flex-1 truncate font-mono text-[11px] text-primary">{lora.name}</span>
              <input
                type="number"
                step="0.05"
                aria-label={`Força de ${lora.name}`}
                defaultValue={lora.strength}
                onBlur={(e) => {
                  const strength = Number(e.target.value)
                  if (Number.isFinite(strength) && strength !== lora.strength) {
                    setLoras(loras.map((item) => (item.name === lora.name ? { ...item, strength } : item)))
                  }
                }}
                className={`${inputCls} w-20`}
              />
              <button type="button" aria-label={`Remover ${lora.name}`} disabled={busy} onClick={() => setLoras(loras.filter((item) => item.name !== lora.name))} className="text-danger-400 disabled:opacity-50">
                <Trash2 className="h-3.5 w-3.5" />
              </button>
            </div>
          ))}
        </div>
      )}

      {panel === 'search' && (
        <ModelSearch kind={kind} families={families.filter((id) => id !== 'custom')} onAdd={addFromSearch} busy={busy} inputCls={inputCls} />
      )}
      {panel === 'lora' && (
        <ModelSearch kind="lora" families={['lora']} onAdd={addLora} busy={busy} inputCls={inputCls} />
      )}
      {panel === 'custom' && (
        <CustomWorkflow kind={kind}
          onCreated={async (id) => { setPanel(''); await onLibraryChange(kind, id); toast('Fluxo salvo. Salve para aplicar.', 'ok') }}
          inputCls={inputCls}
          labelCls={labelCls}
        />
      )}

      <div className="flex flex-wrap items-center gap-2">
        {kind !== 'llm' && place === 'local' && entry && (
          library?.comfyui_root ? (
            <button type="button" disabled={installing} onClick={() => onInstall(kind, value)} className="flex min-h-10 items-center gap-1.5 rounded-xl border border-subtle bg-elevated px-3 py-1.5 text-xs font-semibold text-secondary hover:text-primary disabled:opacity-50">
              <Download className="h-3.5 w-3.5" />
              Baixar nesta máquina o que falta
            </button>
          ) : (
            <span className="text-[11px] text-muted">Pasta do ComfyUI não encontrada nesta máquina: defina COMFYUI_ROOT para baixar por aqui.</span>
          )
        )}
        {entry?.user && (
          <button type="button" disabled={busy} onClick={remove} className="rounded-xl border border-subtle px-3 py-1.5 text-xs font-semibold text-danger-300 disabled:opacity-50">
            Remover da biblioteca
          </button>
        )}
        {install?.model === value && install.state === 'running' && (
          <span className="text-[11px] text-secondary">
            Baixando {install.file} ({(install.done || 0) + 1} de {install.total})
            {install.file_bytes ? ` · ${Math.floor(((install.file_bytes_done || 0) / install.file_bytes) * 100)}%` : ''}
          </span>
        )}
        {install?.model === value && install.state === 'done' && <span className="text-[11px] text-secondary">Arquivos nesta máquina.</span>}
        {install?.model === value && install.state === 'error' && <span role="alert" className="text-[11px] text-danger-300">Download falhou: {install.error}</span>}
      </div>
    </section>
  )
}
