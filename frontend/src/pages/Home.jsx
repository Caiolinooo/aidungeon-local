import { useCallback, useEffect, useRef, useState } from 'react'
import { Link, useNavigate } from 'react-router-dom'
import { api } from '../api.js'
import { storyStartsFromScenario } from '../api/storyStarts.js'
import { clearToken } from '../auth.js'
import { useToast } from '../components/Toast.jsx'
import Modal from '../components/Modal.jsx'
import GenerateAdventureModal from '../components/GenerateAdventureModal.jsx'
import ReaderPreferencesModal from '../components/ReaderPreferencesModal.jsx'
import { fmtDate } from '../utils.js'
import { useConfirm } from '../confirm.jsx'
import {
  Upload,
  Download,
  Sparkles,
  Plus,
  LogOut,
  Settings as SettingsIcon,
  MoreVertical,
  Edit2,
  Trash2,
  BookOpen,
  RefreshCw,
  Users as UsersIcon,
  ShieldCheck,
} from 'lucide-react'

// Tela 1 — Home (PLANO.md §4.5): grid "Suas aventuras" + "Nova aventura"
// (escolher scenario existente ou em branco) + "Importar backup do AI Dungeon".

// Slug seguro p/ nome de arquivo: "My Story!" → "my-story".
function slugify(title) {
  return (title || 'adventure').toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-+|-+$/g, '') || 'adventure'
}

// Download client-side de um Blob (Âncoras <a href> não levam Bearer — o fetch
// autenticado já foi feito em api.js; aqui só materializamos o arquivo).
function downloadBlob(blob, filename) {
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = filename
  document.body.appendChild(a)
  a.click()
  a.remove()
  URL.revokeObjectURL(url)
}

export default function Home() {
  const toast = useToast()
  const navigate = useNavigate()
  const [adventures, setAdventures] = useState(null)
  const [error, setError] = useState(null) // backend fora do ar / erro de API
  const [showNew, setShowNew] = useState(false)
  const [showImport, setShowImport] = useState(false)
  const [showGenerate, setShowGenerate] = useState(false)
  const [showReaderModal, setShowReaderModal] = useState(false)
  const [menuOpenId, setMenuOpenId] = useState(null) // id da aventura com menu ⋯ aberto
  const [renaming, setRenaming] = useState(null) // aventura alvo do Rename modal
  const [deleting, setDeleting] = useState(null) // aventura alvo do Delete modal
  const [backingUp, setBackingUp] = useState(false)
  const [session, setSession] = useState(null)

  // Atualiza/remove uma aventura na lista local após PATCH/DELETE.
  const patchLocal = (updated) =>
    setAdventures((list) => list?.map((a) => (a.id === updated.id ? { ...a, ...updated } : a)))
  const removeLocal = (id) => setAdventures((list) => list?.filter((a) => a.id !== id))

  const backupAll = async () => {
    setBackingUp(true)
    try {
      const blob = await api.downloadBackupAll()
      downloadBlob(blob, 'aidungeon-backup.zip')
    } catch (e) {
      toast(e.message)
    } finally {
      setBackingUp(false)
    }
  }

  const load = useCallback(async () => {
    setError(null)
    try {
      setAdventures(await api.listAdventures())
    } catch (e) {
      setError(e)
      setAdventures([])
    }
  }, [])

  useEffect(() => {
    load()
    api.checkAuth().then(setSession).catch(() => setSession(null))
  }, [load])

  const logout = () => {
    clearToken()
    navigate('/login', { replace: true })
  }

  return (
    <div className="mx-auto flex h-full max-w-6xl flex-col overflow-y-auto px-4 py-8 bg-canvas text-primary">
      <header className="mb-6 flex flex-col gap-4 sm:mb-8 sm:flex-row sm:items-center sm:justify-between">
        <div>
          <h1 className="text-3xl font-extrabold tracking-tight text-primary flex items-center gap-2">
            AI Dungeon <span className="text-accent-pri">Local</span>
          </h1>
          <p className="mt-1 text-xs text-muted">Contos interativos no seu servidor — 100% privado, modelo local.</p>
        </div>
        <div className="-mx-4 flex gap-2 overflow-x-auto px-4 pb-1 sm:mx-0 sm:flex-wrap sm:overflow-visible sm:px-0 items-center">
          <Link to="/catalog" className="flex h-10 shrink-0 items-center rounded-full border border-subtle bg-surface px-4 text-sm font-medium text-secondary hover:bg-elevated hover:text-primary">
            Catálogo
          </Link>
          <button
            onClick={() => setShowReaderModal(true)}
            title="Tema e tipografia da leitura"
            aria-label="Tema e tipografia da leitura"
            className="flex h-10 w-10 shrink-0 items-center justify-center rounded-full border border-subtle bg-surface text-secondary hover:text-primary hover:bg-elevated transition"
          >
            <BookOpen className="w-4 h-4 text-accent-pri" />
          </button>
          <Link
            to="/users"
            title="Contas e senha"
            className="flex h-10 w-10 shrink-0 items-center justify-center rounded-full border border-subtle bg-surface text-secondary hover:text-primary hover:bg-elevated transition"
          >
            <UsersIcon className="w-4 h-4 text-secondary" />
          </Link>
          {session?.admin && (
            <Link
              to="/rulesets"
              title="Rulesets de personagem"
              className="flex h-10 w-10 shrink-0 items-center justify-center rounded-full border border-subtle bg-surface text-secondary hover:text-primary hover:bg-elevated transition"
            >
              <ShieldCheck className="w-4 h-4 text-secondary" />
            </Link>
          )}
          <Link
            to="/settings"
            title="Configurações (modelo, imagem, voz, memória)"
            className="flex h-10 w-10 shrink-0 items-center justify-center rounded-full border border-subtle bg-surface text-secondary hover:text-primary hover:bg-elevated transition"
          >
            <SettingsIcon className="w-4 h-4 text-secondary" />
          </Link>
          <button
            onClick={() => setShowImport(true)}
            title="Importar backup do AI Dungeon (.zip)"
            aria-label="Importar backup do AI Dungeon"
            className="flex h-10 w-10 shrink-0 items-center justify-center rounded-full border border-subtle bg-surface text-secondary hover:text-primary hover:bg-elevated transition"
          >
            <Upload className="w-4 h-4 text-muted" />
          </button>
          <button
            onClick={backupAll}
            disabled={backingUp}
            title={backingUp ? 'Exportando…' : 'Baixar um .zip com todas as aventuras'}
            aria-label="Baixar um .zip com todas as aventuras"
            className="flex h-10 w-10 shrink-0 items-center justify-center rounded-full border border-subtle bg-surface text-secondary hover:text-primary hover:bg-elevated transition disabled:opacity-50"
          >
            <Download className="w-4 h-4 text-muted" />
          </button>
          <button
            onClick={() => setShowGenerate(true)}
            title="Descreva uma ideia e deixe a IA rascunhar o cenário"
            className="flex h-10 shrink-0 items-center gap-1.5 rounded-full border border-accent-pri/50 bg-elevated px-4 text-xs font-semibold text-accent-pri hover:bg-accent-pri/20 transition"
          >
            <Sparkles className="w-4 h-4" />
            <span>Gerar com IA</span>
          </button>
          <button
            onClick={() => setShowNew(true)}
            className="flex h-10 shrink-0 items-center gap-1.5 rounded-full bg-accent-pri px-4 text-xs font-semibold text-canvas hover:bg-accent-hov shadow transition"
          >
            <Plus className="w-4 h-4" />
            <span>Nova aventura</span>
          </button>
          <button
            onClick={logout}
            title="Sair e voltar para o login"
            aria-label="Sair"
            className="flex h-10 w-10 shrink-0 items-center justify-center rounded-full border border-subtle bg-surface text-muted hover:text-danger-400 hover:bg-elevated transition"
          >
            <LogOut className="w-4 h-4" />
          </button>
        </div>
      </header>

      <ReaderPreferencesModal
        isOpen={showReaderModal}
        onClose={() => setShowReaderModal(false)}
      />


      {error && (
        <div className="mb-6 flex items-center justify-between rounded-lg border border-danger-900 bg-danger-950/60 px-4 py-3 text-sm text-danger-200">
          <span>
            {error.status === 0
              ? 'Não foi possível falar com o servidor (http://127.0.0.1:8000). Ele está no ar?'
              : error.message}
          </span>
          <button
            onClick={load}
            className="rounded-md border border-danger-800 px-3 py-1 text-xs font-semibold hover:bg-danger-900"
          >
            ↻ Tentar de novo
          </button>
        </div>
      )}

      {adventures === null ? (
        <p className="text-muted">Carregando…</p>
      ) : adventures.length === 0 && !error ? (
        <div className="flex flex-1 flex-col items-center justify-center gap-3 rounded-xl border border-dashed border-subtle p-16 text-center">
          <p className="text-lg text-secondary">Nenhuma aventura ainda.</p>
          <p className="max-w-md text-sm text-muted">
            Comece uma aventura nova a partir de um cenário, ou importe o backup do
            AI Dungeon (.zip) para continuar uma história exatamente de onde parou.
          </p>
        </div>
      ) : (
        <section>
          <h2 className="mb-3 text-sm font-semibold uppercase tracking-wide text-muted">Suas aventuras</h2>
          <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-3">
            {adventures.map((a) => (
              <Link
                key={a.id}
                to={`/play/${a.id}`}
                className="group flex h-full flex-col rounded-2xl border border-subtle bg-elevated p-5 transition-all hover:-translate-y-0.5 hover:border-accent-pri/60 hover:shadow-lg hover:shadow-canvas/40"
              >
                <div className="mb-2 flex items-start justify-between gap-2">
                  <h3 className="font-prose text-lg font-semibold leading-snug text-primary group-hover:text-accent-pri transition-colors">
                    {a.title}
                    {session?.admin && a.owner ? (
                      <span className="ml-2 text-[10px] font-normal uppercase text-muted">{a.owner}</span>
                    ) : null}
                  </h3>
                  <div className="flex shrink-0 items-center gap-1">
                    {a.source === 'import' && (
                      <span className="rounded bg-elevated px-1.5 py-0.5 text-[10px] uppercase text-secondary">
                        imported
                      </span>
                    )}
                    <AdventureMenu
                      adventure={a}
                      open={menuOpenId === a.id}
                      onToggle={() => setMenuOpenId(menuOpenId === a.id ? null : a.id)}
                      onClose={() => setMenuOpenId(null)}
                      onRename={() => setRenaming(a)}
                      onDelete={() => setDeleting(a)}
                      toast={toast}
                    />
                  </div>
                </div>
                {(a.shared_by || (a.turn_mode && session?.user && a.turn_user === session.user)) && (
                  <div className="mb-2 flex flex-wrap items-center gap-1.5">
                    {a.shared_by ? (
                      <span className="rounded bg-surface px-1.5 py-0.5 text-[10px] text-secondary">
                        👥 de {a.shared_by}
                      </span>
                    ) : null}
                    {a.turn_mode && session?.user && a.turn_user === session.user ? (
                      <span className="inline-flex items-center gap-1 rounded bg-accent-pri/15 px-1.5 py-0.5 text-[10px] font-semibold text-accent-pri">
                        <span className="h-1.5 w-1.5 rounded-full bg-accent-pri" aria-hidden="true" />
                        🎲 sua vez
                      </span>
                    ) : null}
                  </div>
                )}
                <p className="mb-3 line-clamp-4 flex-1 whitespace-pre-wrap font-prose text-sm text-secondary">
                  {a.last_excerpt || a.excerpt || a.description || '…'}
                </p>
                <div className="flex items-center justify-between text-xs text-muted">
                  <span>{a.action_count ?? 0} actions</span>
                  <span>{fmtDate(a.updated_at)}</span>
                </div>
              </Link>
            ))}
          </div>
        </section>
      )}

      {showNew && (
        <NewAdventureModal
          onClose={() => setShowNew(false)}
          onDeleted={(id) => removeLocal(id)}
        />
      )}
      {showImport && <ImportModal onClose={() => setShowImport(false)} onImported={load} />}
      {showGenerate && <GenerateAdventureModal onClose={() => setShowGenerate(false)} />}
      {renaming && (
        <RenameModal
          adventure={renaming}
          onClose={() => setRenaming(null)}
          onRenamed={patchLocal}
        />
      )}
      {deleting && (
        <DeleteModal
          adventure={deleting}
          onClose={() => setDeleting(null)}
          onDeleted={() => removeLocal(deleting.id)}
        />
      )}
    </div>
  )
}

// ---------------------------------------------------------------------------
// Modal "Nova aventura": escolher um scenario existente (GET /api/scenarios)
// ou começar em branco → POST /api/adventures → navega p/ /play/:id.
function NewAdventureModal({ onClose, onDeleted }) {
  const toast = useToast()
  const navigate = useNavigate()
  const { ask, bar } = useConfirm()
  const [scenarios, setScenarios] = useState(null)
  const [stories, setStories] = useState([])
  const [pendingDelete, setPendingDelete] = useState(null)
  const [deleting, setDeleting] = useState(false)
  const [selected, setSelected] = useState('blank') // 'blank' | scenario.id
  const [startId, setStartId] = useState('')
  const [creating, setCreating] = useState(false)

  useEffect(() => {
    api.listScenarios().then(setScenarios).catch((e) => {
      toast(e.message)
      setScenarios([])
    })
    api.listAdventures().then(setStories).catch((e) => toast(e.message))
  }, [toast])

  const confirmDelete = async () => {
    if (!pendingDelete) return
    setDeleting(true)
    try {
      await api.deleteAdventure(pendingDelete.id)
      setStories((list) => list.filter((item) => item.id !== pendingDelete.id))
      onDeleted?.(pendingDelete.id)
      setPendingDelete(null)
    } catch (e) {
      toast(e.message)
    } finally {
      setDeleting(false)
    }
  }

  const selectScenario = (id) => {
    setSelected(id)
    if (id === 'blank') {
      setStartId('')
      return
    }
    const scenario = (scenarios || []).find((item) => item.id === id)
    const starts = storyStartsFromScenario(scenario)
    setStartId(starts[0]?.id || '')
  }

  const start = async () => {
    setCreating(true)
    try {
      const payload = selected === 'blank'
        ? {}
        : { scenario_id: selected, ...(startId ? { start_id: startId } : {}) }
      const adv = await api.createAdventure(payload)
      navigate(`/play/${adv.id}`)
    } catch (e) {
      toast(e.message)
      setCreating(false)
    }
  }

  // A linha inteira é um <label> selecionável — os ícones precisam impedir
  // o clique de marcar o radio.
  const editScenario = (e, s) => {
    e.preventDefault()
    e.stopPropagation()
    navigate(`/scenarios/${s.id}/edit`)
    onClose()
  }

  const deleteScenario = async (e, s) => {
    e.preventDefault()
    e.stopPropagation()
    if (!ask(`scenario-${s.id}`, `Apagar o cenário "${s.title}"? As aventuras que nasceram dele continuam.`)) return
    try {
      await api.deleteScenario(s.id)
      setScenarios((list) => list.filter((x) => x.id !== s.id))
      if (selected === s.id) {
        setSelected('blank')
        setStartId('')
      }
      toast('Scenario deleted.', 'success')
    } catch (err) {
      toast(err.message)
    }
  }

  return (
    <Modal title="Nova aventura" onClose={onClose} wide>
      {bar}
      {scenarios === null ? (
        <p className="text-sm text-muted">Carregando cenários…</p>
      ) : (
        <div className="flex flex-col gap-2">
          {stories.length > 0 && (
            <div className="mb-2 rounded-lg border border-subtle p-3">
              <div className="mb-2 text-xs font-semibold uppercase tracking-wide text-muted">
                Delete an existing adventure
              </div>
              <ul className="max-h-40 space-y-1 overflow-y-auto">
                {stories.map((story) => (
                  <li key={story.id} className="flex items-center justify-between gap-2 rounded px-1 py-1">
                    <span className="truncate text-sm text-primary">{story.title}</span>
                    <button
                      type="button"
                      onClick={() => setPendingDelete(story)}
                      className="shrink-0 rounded px-2 py-1 text-xs text-danger-300 hover:bg-danger-950"
                    >
                      Apagar
                    </button>
                  </li>
                ))}
              </ul>
              {pendingDelete && (
                <div className="mt-2 rounded border border-danger-900 bg-danger-950/40 p-2 text-sm text-danger-100">
                  Delete “{pendingDelete.title}” permanently?
                  <div className="mt-2 flex justify-end gap-2">
                    <button
                      type="button"
                      onClick={() => setPendingDelete(null)}
                      className="rounded border border-subtle px-2 py-1 text-xs text-secondary"
                    >
                      Cancelar
                    </button>
                    <button
                      type="button"
                      onClick={confirmDelete}
                      disabled={deleting}
                      className="rounded bg-danger-700 px-2 py-1 text-xs font-semibold text-white disabled:opacity-50"
                    >
                      {deleting ? 'Apagando…' : 'Delete'}
                    </button>
                  </div>
                </div>
              )}
            </div>
          )}
          <label
            className={`flex cursor-pointer items-start gap-3 rounded-lg border p-3 ${
              selected === 'blank' ? 'border-accent-pri bg-accent-950/30' : 'border-subtle hover:border-subtle'
            }`}
          >
            <input type="radio" name="scenario" checked={selected === 'blank'} onChange={() => selectScenario('blank')} className="mt-1 accent-accent-600" />
            <div>
              <div className="font-semibold text-primary">Aventura em branco</div>
              <div className="text-sm text-muted">Começa com a história vazia — você define a cena.</div>
            </div>
          </label>

          {scenarios.map((s) => {
            const starts = storyStartsFromScenario(s)
            return (
            <div key={s.id} className="flex flex-col gap-1">
            <label
              className={`flex cursor-pointer items-start gap-3 rounded-lg border p-3 ${
                selected === s.id ? 'border-accent-pri bg-accent-950/30' : 'border-subtle hover:border-subtle'
              }`}
            >
              <input type="radio" name="scenario" checked={selected === s.id} onChange={() => selectScenario(s.id)} className="mt-1 accent-accent-600" />
              <div className="min-w-0 flex-1">
                <div className="font-semibold text-primary">{s.title}</div>
                {s.description && <div className="line-clamp-2 text-sm text-muted">{s.description}</div>}
                {s.tags?.length > 0 && (
                  <div className="mt-1 flex flex-wrap gap-1">
                    {s.tags.map((t) => (
                      <span key={t} className="rounded-full bg-elevated px-2 py-0.5 text-[10px] text-secondary">
                        {t}
                      </span>
                    ))}
                  </div>
                )}
              </div>
              <div className="flex shrink-0 gap-1">
                <button
                  type="button"
                  title="Editar cenário"
                  aria-label={`Editar “${s.title}”`}
                  onClick={(e) => editScenario(e, s)}
                  className="rounded-lg border border-subtle px-2 py-1 text-sm text-secondary hover:bg-surface"
                >
                  ✎
                </button>
                <button
                  type="button"
                  title="Apagar cenário"
                  aria-label={`Apagar “${s.title}”`}
                  onClick={(e) => deleteScenario(e, s)}
                  className="rounded-lg border border-subtle px-2 py-1 text-sm text-secondary hover:bg-surface"
                >
                  🗑
                </button>
              </div>
            </label>
            {selected === s.id && starts.length > 0 && (
              <div className="ml-8 flex flex-col gap-1">
                {starts.map((startItem) => (
                  <label
                    key={startItem.id}
                    className={`flex cursor-pointer items-start gap-2 rounded-lg border px-3 py-2 ${
                      startId === startItem.id ? 'border-accent-pri bg-accent-950/20' : 'border-subtle'
                    }`}
                  >
                    <input
                      type="radio"
                      name={`story-start-${s.id}`}
                      checked={startId === startItem.id}
                      onChange={() => setStartId(startItem.id)}
                      className="mt-1 accent-accent-600"
                    />
                    <span className="min-w-0">
                      <span className="block text-sm font-medium text-primary">
                        {startItem.title}
                        {starts[0]?.id === startItem.id ? ' · padrão' : ''}
                      </span>
                      <span className="line-clamp-2 text-xs text-muted">{startItem.text}</span>
                    </span>
                  </label>
                ))}
              </div>
            )}
            </div>
            )
          })}

          <div className="mt-3 flex items-center justify-between">
            <Link to="/scenarios/new" className="text-sm text-accent-pri hover:underline" onClick={onClose}>
              + Create a new scenario
            </Link>
            <button
              onClick={start}
              disabled={creating}
              className="rounded-lg bg-accent-pri px-5 py-2 text-sm font-semibold text-canvas hover:bg-accent-hov disabled:opacity-50"
            >
              {creating ? 'Criando…' : 'Começar aventura'}
            </button>
          </div>
        </div>
      )}
    </Modal>
  )
}

// ---------------------------------------------------------------------------
// Modal "Importar backup do AI Dungeon": upload do .zip (multipart) → ImportReport
// (actions/cards/memories importados + warnings) → botão p/ abrir a aventura.
function ImportModal({ onClose, onImported }) {
  const toast = useToast()
  const navigate = useNavigate()
  const fileRef = useRef(null)
  const [file, setFile] = useState(null)
  const [importing, setImporting] = useState(false)
  const [report, setReport] = useState(null)

  const doImport = async () => {
    if (!file) return
    setImporting(true)
    try {
      const r = await api.importBackup(file)
      setReport(r)
      onImported?.()
    } catch (e) {
      toast(e.message)
    } finally {
      setImporting(false)
    }
  }

  return (
    <Modal title="Importar backup do AI Dungeon" onClose={onClose}>
      {!report ? (
        <div className="flex flex-col gap-4">
          <p className="text-sm text-secondary">
            Escolha o <code className="text-secondary">.zip</code> exportado do AI Dungeon
            (contém <code className="text-secondary">metadata.json</code> +{' '}
            <code className="text-secondary">actions-*.json</code>). A história continua do
            ponto exato em que parou.
          </p>
          <button
            onClick={() => fileRef.current?.click()}
            className="rounded-lg border border-dashed border-subtle p-6 text-center text-sm text-secondary hover:border-subtle"
          >
            {file ? (
              <span className="text-primary">
                📦 {file.name} <span className="text-muted">({(file.size / 1024).toFixed(0)} KB)</span>
              </span>
            ) : (
              'Clique para escolher o arquivo .zip'
            )}
          </button>
          <input
            ref={fileRef}
            type="file"
            accept=".zip,application/zip"
            className="hidden"
            onChange={(e) => setFile(e.target.files?.[0] ?? null)}
          />
          <div className="flex justify-end gap-2">
            <button onClick={onClose} className="rounded-lg border border-subtle px-4 py-2 text-sm text-secondary hover:bg-surface">
              Cancelar
            </button>
            <button
              onClick={doImport}
              disabled={!file || importing}
              className="rounded-lg bg-accent-pri px-4 py-2 text-sm font-semibold text-canvas hover:bg-accent-hov disabled:opacity-50"
            >
              {importing ? 'Importing…' : 'Import'}
            </button>
          </div>
        </div>
      ) : (
        <div className="flex flex-col gap-4">
          {report.multi ? (
            // Backup multi-aventura: resumo agregado + lista de títulos importados.
            <>
              <div className="rounded-lg border border-accent-900 bg-accent-950/40 p-4">
                <h3 className="font-semibold text-accent-pri">
                  Import complete — {report.imported} adventure{report.imported === 1 ? '' : 's'} imported
                </h3>
                <ul className="mt-2 max-h-48 space-y-1 overflow-y-auto">
                  {(report.results || []).map((r, i) => (
                    <li key={r.adventure_id ?? i} className="flex items-baseline justify-between gap-2 rounded bg-surface/60 px-2 py-1 text-sm">
                      <span className="truncate text-primary">{r.title}</span>
                      <span className="shrink-0 text-xs text-muted">{r.actions_imported ?? 0} actions</span>
                    </li>
                  ))}
                </ul>
              </div>

              {(() => {
                const warnings = (report.results || []).flatMap((r) =>
                  (r.warnings || []).map((w) => `${r.title}: ${w}`)
                )
                return warnings.length > 0 ? (
                  <div className="rounded-lg border border-warn-900 bg-warn-950/40 p-3">
                    <h4 className="mb-1 text-sm font-semibold text-warn-300">
                      Warnings ({warnings.length})
                    </h4>
                    <ul className="max-h-40 list-disc overflow-y-auto pl-5 text-xs text-warn-200/80">
                      {warnings.map((w, i) => (
                        <li key={i}>{w}</li>
                      ))}
                    </ul>
                  </div>
                ) : null
              })()}

              <div className="flex justify-end">
                <button
                  onClick={onClose}
                  className="rounded-lg bg-accent-pri px-4 py-2 text-sm font-semibold text-canvas hover:bg-accent-hov"
                >
                  Done
                </button>
              </div>
            </>
          ) : (
            <>
              <div className="rounded-lg border border-accent-900 bg-accent-950/40 p-4">
                <h3 className="font-semibold text-accent-pri">Import complete — “{report.title}”</h3>
            <ul className="mt-2 grid grid-cols-3 gap-2 text-center text-sm">
              <li className="rounded bg-surface/60 p-2">
                <div className="text-xl font-bold text-primary">{report.actions_imported ?? 0}</div>
                <div className="text-xs text-muted">ações</div>
              </li>
              <li className="rounded bg-surface/60 p-2">
                <div className="text-xl font-bold text-primary">{report.cards_imported ?? 0}</div>
                <div className="text-xs text-muted">fichas</div>
              </li>
              <li className="rounded bg-surface/60 p-2">
                <div className="text-xl font-bold text-primary">{report.memories_imported ?? 0}</div>
                <div className="text-xs text-muted">memórias</div>
              </li>
            </ul>
          </div>

          {report.warnings?.length > 0 && (
            <div className="rounded-lg border border-warn-900 bg-warn-950/40 p-3">
              <h4 className="mb-1 text-sm font-semibold text-warn-300">
                Warnings ({report.warnings.length})
              </h4>
              <ul className="max-h-40 list-disc overflow-y-auto pl-5 text-xs text-warn-200/80">
                {report.warnings.map((w, i) => (
                  <li key={i}>{w}</li>
                ))}
              </ul>
            </div>
          )}

          <div className="flex justify-end gap-2">
            <button onClick={onClose} className="rounded-lg border border-subtle px-4 py-2 text-sm text-secondary hover:bg-surface">
              Fechar
            </button>
            <button
              onClick={() => navigate(`/play/${report.adventure_id}`)}
              className="rounded-lg bg-accent-pri px-4 py-2 text-sm font-semibold text-canvas hover:bg-accent-hov"
            >
              ▶ Open adventure
            </button>
          </div>
            </>
          )}
        </div>
      )}
    </Modal>
  )
}

// ---------------------------------------------------------------------------
// Menu ⋯ do card de aventura (Rename / Export .zip / Delete). O card inteiro é
// um <Link> para /play/:id, então TODO clique aqui dentro precisa de
// preventDefault + stopPropagation para não navegar. Fecha ao clicar fora
// (mousedown no document) ou no Esc.
function AdventureMenu({ adventure, open, onToggle, onClose, onRename, onDelete, toast }) {
  const rootRef = useRef(null)

  useEffect(() => {
    if (!open) return
    const onDocMouseDown = (e) => {
      if (!rootRef.current?.contains(e.target)) onClose()
    }
    const onKey = (e) => {
      if (e.key === 'Escape') onClose()
    }
    document.addEventListener('mousedown', onDocMouseDown)
    document.addEventListener('keydown', onKey)
    return () => {
      document.removeEventListener('mousedown', onDocMouseDown)
      document.removeEventListener('keydown', onKey)
    }
  }, [open, onClose])

  // Impede a navegação do <Link> pai em qualquer clique do menu.
  const guard = (e) => {
    e.preventDefault()
    e.stopPropagation()
  }

  const exportZip = async () => {
    onClose()
    try {
      const blob = await api.downloadExport(adventure.id)
      downloadBlob(blob, `${slugify(adventure.title)}.zip`)
    } catch (e) {
      toast(e.message)
    }
  }

  const itemCls =
    'block w-full px-3 py-2 text-left text-sm text-secondary hover:bg-surface hover:text-primary'

  return (
    <div ref={rootRef} className="relative" onClick={guard}>
      <button
        onClick={onToggle}
        aria-label={`Gerir “${adventure.title}”`}
        title="Gerir aventura"
        className="rounded-lg p-1.5 text-muted hover:bg-elevated hover:text-primary transition"
      >
        <MoreVertical className="w-4 h-4" />
      </button>
      {open && (
        <div className="absolute right-0 top-full z-30 mt-1 w-44 overflow-hidden rounded-xl border border-subtle bg-surface py-1 shadow-xl">
          <button
            className="flex items-center gap-2 w-full px-3 py-2 text-left text-xs text-secondary hover:bg-elevated hover:text-primary transition"
            onClick={() => {
              onClose()
              onRename()
            }}
          >
            <Edit2 className="w-3.5 h-3.5" />
            <span>Renomear</span>
          </button>
          <button
            className="flex items-center gap-2 w-full px-3 py-2 text-left text-xs text-secondary hover:bg-elevated hover:text-primary transition"
            onClick={exportZip}
          >
            <Download className="w-3.5 h-3.5" />
            <span>Exportar .zip</span>
          </button>
          <button
            className="flex items-center gap-2 w-full px-3 py-2 text-left text-xs text-danger-400 hover:bg-danger-950/40 hover:text-danger-300 transition"
            onClick={() => {
              onClose()
              onDelete()
            }}
          >
            <Trash2 className="w-3.5 h-3.5" />
            <span>Apagar</span>
          </button>
        </div>
      )}

    </div>
  )
}

// ---------------------------------------------------------------------------
// Modal "Renomear aventura": edita título (+ descrição opcional) → PATCH
// /api/adventures/:id → atualiza a lista local com a resposta.
function RenameModal({ adventure, onClose, onRenamed }) {
  const toast = useToast()
  const [title, setTitle] = useState(adventure.title || '')
  const [description, setDescription] = useState(adventure.description || '')
  const [saving, setSaving] = useState(false)

  const save = async () => {
    if (!title.trim()) return
    setSaving(true)
    try {
      const updated = await api.updateAdventure(adventure.id, {
        title: title.trim(),
        description: description.trim(),
      })
      onRenamed(updated)
      onClose()
    } catch (e) {
      toast(e.message)
      setSaving(false)
    }
  }

  return (
    <Modal title="Renomear aventura" onClose={onClose}>
      <div className="flex flex-col gap-4">
        <label className="flex flex-col gap-1">
          <span className="text-sm font-medium text-secondary">Título</span>
          <input
            autoFocus
            value={title}
            onChange={(e) => setTitle(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === 'Enter') save()
            }}
            className="rounded-lg border border-subtle bg-surface px-3 py-2 text-sm text-primary outline-none focus:border-accent-pri"
          />
        </label>
        <label className="flex flex-col gap-1">
          <span className="text-sm font-medium text-secondary">
            Descrição <span className="text-muted">(opcional)</span>
          </span>
          <textarea
            value={description}
            onChange={(e) => setDescription(e.target.value)}
            rows={3}
            className="resize-y rounded-lg border border-subtle bg-surface px-3 py-2 text-sm text-primary outline-none focus:border-accent-pri"
          />
        </label>
        <div className="flex justify-end gap-2">
          <button onClick={onClose} className="rounded-lg border border-subtle px-4 py-2 text-sm text-secondary hover:bg-surface">
            Cancelar
          </button>
          <button
            onClick={save}
            disabled={saving || !title.trim()}
            className="rounded-lg bg-accent-pri px-4 py-2 text-sm font-semibold text-canvas hover:bg-accent-hov disabled:opacity-50"
          >
            {saving ? 'Salvando…' : 'Salvar'}
          </button>
        </div>
      </div>
    </Modal>
  )
}

// ---------------------------------------------------------------------------
// Modal de confirmação de Delete: mostra o título, avisa que é irreversível →
// DELETE /api/adventures/:id → remove da lista local.
function DeleteModal({ adventure, onClose, onDeleted }) {
  const toast = useToast()
  const [busy, setBusy] = useState(false)

  const confirm = async () => {
    setBusy(true)
    try {
      await api.deleteAdventure(adventure.id)
      onDeleted()
      onClose()
    } catch (e) {
      toast(e.message)
      setBusy(false)
    }
  }

  return (
    <Modal title="Apagar aventura" onClose={onClose}>
      <div className="flex flex-col gap-4">
        <p className="text-sm text-secondary">
          Delete <span className="font-semibold text-primary">“{adventure.title}”</span>?
        </p>
        <p className="rounded-lg border border-danger-900 bg-danger-950/40 p-3 text-sm text-danger-200">
          This permanently removes the story, its actions, fichas and memories. This action
          cannot be undone.
        </p>
        <div className="flex justify-end gap-2">
          <button onClick={onClose} className="rounded-lg border border-subtle px-4 py-2 text-sm text-secondary hover:bg-surface">
            Cancelar
          </button>
          <button
            onClick={confirm}
            disabled={busy}
            className="rounded-lg bg-danger-700 px-4 py-2 text-sm font-semibold text-white hover:bg-danger-600 disabled:opacity-50"
          >
            {busy ? 'Apagando…' : 'Apagar de vez'}
          </button>
        </div>
      </div>
    </Modal>
  )
}
