import { useCallback, useEffect, useRef, useState } from 'react'
import { Link, useNavigate } from 'react-router-dom'
import { api } from '../api.js'
import { clearToken } from '../auth.js'
import { useToast } from '../components/Toast.jsx'
import Modal from '../components/Modal.jsx'
import GenerateAdventureModal from '../components/GenerateAdventureModal.jsx'
import ReaderPreferencesModal from '../components/ReaderPreferencesModal.jsx'
import { fmtDate } from '../utils.js'
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
} from 'lucide-react'

// Tela 1 — Home (PLANO.md §4.5): grid "Your Adventures" + "New Adventure"
// (escolher scenario existente ou em branco) + "Import AI Dungeon backup".

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
          <p className="mt-1 text-xs text-muted">Self-hosted interactive storytelling — 100% private, local LLM.</p>
        </div>
        <div className="-mx-4 flex gap-2 overflow-x-auto px-4 pb-1 sm:mx-0 sm:flex-wrap sm:overflow-visible sm:px-0 items-center">
          <button
            onClick={() => setShowReaderModal(true)}
            title="Theme & Reader options"
            className="min-h-11 shrink-0 flex items-center gap-1.5 rounded-xl border border-subtle bg-surface px-3 py-2 text-xs font-medium text-secondary hover:text-primary hover:bg-elevated transition"
          >
            <BookOpen className="w-4 h-4 text-accent-pri" />
            <span className="hidden md:inline">Theme</span>
          </button>
          <Link
            to="/users"
            title="Accounts and password"
            className="min-h-11 shrink-0 flex items-center gap-1.5 rounded-xl border border-subtle bg-surface px-3.5 py-2 text-xs font-medium text-secondary hover:text-primary hover:bg-elevated transition"
          >
            <UsersIcon className="w-4 h-4 text-secondary" />
            <span>Users</span>
          </Link>
          <Link
            to="/settings"
            title="Global Settings (LLM, Image, Voice, Memory)"
            className="min-h-11 shrink-0 flex items-center gap-1.5 rounded-xl border border-subtle bg-surface px-3.5 py-2 text-xs font-medium text-secondary hover:text-primary hover:bg-elevated transition"
          >
            <SettingsIcon className="w-4 h-4 text-secondary" />
            <span>Settings</span>
          </Link>
          <button
            onClick={() => setShowImport(true)}
            className="min-h-11 shrink-0 flex items-center gap-1.5 rounded-xl border border-subtle bg-surface px-3.5 py-2 text-xs font-medium text-secondary hover:text-primary hover:bg-elevated transition"
          >
            <Upload className="w-4 h-4 text-muted" />
            <span>Import</span>
          </button>
          <button
            onClick={backupAll}
            disabled={backingUp}
            title="Download a .zip backup with ALL adventures"
            className="min-h-11 shrink-0 flex items-center gap-1.5 rounded-xl border border-subtle bg-surface px-3.5 py-2 text-xs font-medium text-secondary hover:text-primary hover:bg-elevated transition disabled:opacity-50"
          >
            <Download className="w-4 h-4 text-muted" />
            <span>{backingUp ? 'Exporting…' : 'Backup all'}</span>
          </button>
          <button
            onClick={() => setShowGenerate(true)}
            title="Describe an idea and let the AI draft a full scenario for you"
            className="min-h-11 shrink-0 flex items-center gap-1.5 rounded-xl border border-accent-pri/50 bg-elevated px-3.5 py-2 text-xs font-semibold text-accent-pri hover:bg-accent-pri/20 transition"
          >
            <Sparkles className="w-4 h-4" />
            <span>Generate with AI</span>
          </button>
          <button
            onClick={() => setShowNew(true)}
            className="min-h-11 shrink-0 flex items-center gap-1.5 rounded-xl bg-accent-pri px-4 py-2 text-xs font-semibold text-white hover:bg-accent-hov shadow transition"
          >
            <Plus className="w-4 h-4" />
            <span>New Adventure</span>
          </button>
          <button
            onClick={logout}
            title="Sign out and return to login"
            className="min-h-11 shrink-0 flex items-center gap-1.5 rounded-xl border border-subtle bg-surface px-3 py-2 text-xs font-medium text-muted hover:text-red-400 hover:bg-elevated transition"
          >
            <LogOut className="w-4 h-4" />
            <span>Log out</span>
          </button>
        </div>
      </header>

      <ReaderPreferencesModal
        isOpen={showReaderModal}
        onClose={() => setShowReaderModal(false)}
      />


      {error && (
        <div className="mb-6 flex items-center justify-between rounded-lg border border-red-900 bg-red-950/60 px-4 py-3 text-sm text-red-200">
          <span>
            {error.status === 0
              ? 'Cannot reach the backend server (http://127.0.0.1:8000). Is it running?'
              : error.message}
          </span>
          <button
            onClick={load}
            className="rounded-md border border-red-800 px-3 py-1 text-xs font-semibold hover:bg-red-900"
          >
            ↻ Retry
          </button>
        </div>
      )}

      {adventures === null ? (
        <p className="text-zinc-500">Loading…</p>
      ) : adventures.length === 0 && !error ? (
        <div className="flex flex-1 flex-col items-center justify-center gap-3 rounded-xl border border-dashed border-zinc-800 p-16 text-center">
          <p className="text-lg text-zinc-400">No adventures yet.</p>
          <p className="max-w-md text-sm text-zinc-600">
            Start a new adventure from a scenario, or import your AI Dungeon backup (.zip) to
            continue an existing story from the exact point where you stopped.
          </p>
        </div>
      ) : (
        <section>
          <h2 className="mb-3 text-sm font-semibold uppercase tracking-wide text-zinc-500">Your Adventures</h2>
          <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-3">
            {adventures.map((a) => (
              <Link
                key={a.id}
                to={`/play/${a.id}`}
                className="flex h-full flex-col rounded-xl border border-zinc-800 bg-zinc-900 p-4 transition-colors hover:border-emerald-700 hover:bg-zinc-900/80"
              >
                <div className="mb-1 flex items-start justify-between gap-2">
                  <h3 className="font-semibold text-zinc-100">
                    {a.title}
                    {session?.admin && a.owner ? (
                      <span className="ml-2 text-[10px] font-normal uppercase text-zinc-500">{a.owner}</span>
                    ) : null}
                  </h3>
                  <div className="flex shrink-0 items-center gap-1">
                    {a.source === 'import' && (
                      <span className="rounded bg-zinc-700 px-1.5 py-0.5 text-[10px] uppercase text-zinc-300">
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
                      <span className="rounded bg-zinc-800 px-1.5 py-0.5 text-[10px] text-zinc-300">
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
                <div className="flex items-center justify-between text-xs text-zinc-600">
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
// Modal "New Adventure": escolher um scenario existente (GET /api/scenarios)
// ou começar em branco → POST /api/adventures → navega p/ /play/:id.
function NewAdventureModal({ onClose, onDeleted }) {
  const toast = useToast()
  const navigate = useNavigate()
  const [scenarios, setScenarios] = useState(null)
  const [stories, setStories] = useState([])
  const [pendingDelete, setPendingDelete] = useState(null)
  const [deleting, setDeleting] = useState(false)
  const [selected, setSelected] = useState('blank') // 'blank' | scenario.id
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

  const start = async () => {
    setCreating(true)
    try {
      const payload = selected === 'blank' ? {} : { scenario_id: selected }
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
    if (!window.confirm(`Delete scenario "${s.title}"? Adventures that started from it keep running.`)) return
    try {
      await api.deleteScenario(s.id)
      setScenarios((list) => list.filter((x) => x.id !== s.id))
      setSelected((cur) => (cur === s.id ? 'blank' : cur))
      toast('Scenario deleted.', 'success')
    } catch (err) {
      toast(err.message)
    }
  }

  return (
    <Modal title="New Adventure" onClose={onClose} wide>
      {scenarios === null ? (
        <p className="text-sm text-zinc-500">Loading scenarios…</p>
      ) : (
        <div className="flex flex-col gap-2">
          {stories.length > 0 && (
            <div className="mb-2 rounded-lg border border-zinc-800 p-3">
              <div className="mb-2 text-xs font-semibold uppercase tracking-wide text-zinc-500">
                Delete an existing adventure
              </div>
              <ul className="max-h-40 space-y-1 overflow-y-auto">
                {stories.map((story) => (
                  <li key={story.id} className="flex items-center justify-between gap-2 rounded px-1 py-1">
                    <span className="truncate text-sm text-zinc-200">{story.title}</span>
                    <button
                      type="button"
                      onClick={() => setPendingDelete(story)}
                      className="shrink-0 rounded px-2 py-1 text-xs text-red-300 hover:bg-red-950"
                    >
                      Delete
                    </button>
                  </li>
                ))}
              </ul>
              {pendingDelete && (
                <div className="mt-2 rounded border border-red-900 bg-red-950/40 p-2 text-sm text-red-100">
                  Delete “{pendingDelete.title}” permanently?
                  <div className="mt-2 flex justify-end gap-2">
                    <button
                      type="button"
                      onClick={() => setPendingDelete(null)}
                      className="rounded border border-zinc-700 px-2 py-1 text-xs text-zinc-300"
                    >
                      Cancel
                    </button>
                    <button
                      type="button"
                      onClick={confirmDelete}
                      disabled={deleting}
                      className="rounded bg-red-700 px-2 py-1 text-xs font-semibold text-white disabled:opacity-50"
                    >
                      {deleting ? 'Deleting…' : 'Delete'}
                    </button>
                  </div>
                </div>
              )}
            </div>
          )}
          <label
            className={`flex cursor-pointer items-start gap-3 rounded-lg border p-3 ${
              selected === 'blank' ? 'border-emerald-600 bg-emerald-950/30' : 'border-zinc-800 hover:border-zinc-700'
            }`}
          >
            <input type="radio" name="scenario" checked={selected === 'blank'} onChange={() => setSelected('blank')} className="mt-1 accent-emerald-600" />
            <div>
              <div className="font-semibold text-zinc-100">Blank adventure</div>
              <div className="text-sm text-zinc-500">Start with an empty story — you set the scene.</div>
            </div>
          </label>

          {scenarios.map((s) => (
            <label
              key={s.id}
              className={`flex cursor-pointer items-start gap-3 rounded-lg border p-3 ${
                selected === s.id ? 'border-emerald-600 bg-emerald-950/30' : 'border-zinc-800 hover:border-zinc-700'
              }`}
            >
              <input type="radio" name="scenario" checked={selected === s.id} onChange={() => setSelected(s.id)} className="mt-1 accent-emerald-600" />
              <div className="min-w-0 flex-1">
                <div className="font-semibold text-zinc-100">{s.title}</div>
                {s.description && <div className="line-clamp-2 text-sm text-zinc-500">{s.description}</div>}
                {s.tags?.length > 0 && (
                  <div className="mt-1 flex flex-wrap gap-1">
                    {s.tags.map((t) => (
                      <span key={t} className="rounded-full bg-zinc-700 px-2 py-0.5 text-[10px] text-zinc-300">
                        {t}
                      </span>
                    ))}
                  </div>
                )}
              </div>
              <div className="flex shrink-0 gap-1">
                <button
                  type="button"
                  title="Edit scenario"
                  aria-label={`Edit “${s.title}”`}
                  onClick={(e) => editScenario(e, s)}
                  className="rounded-lg border border-zinc-700 px-2 py-1 text-sm text-zinc-400 hover:bg-zinc-800"
                >
                  ✎
                </button>
                <button
                  type="button"
                  title="Delete scenario"
                  aria-label={`Delete “${s.title}”`}
                  onClick={(e) => deleteScenario(e, s)}
                  className="rounded-lg border border-zinc-700 px-2 py-1 text-sm text-zinc-400 hover:bg-zinc-800"
                >
                  🗑
                </button>
              </div>
            </label>
          ))}

          <div className="mt-3 flex items-center justify-between">
            <Link to="/scenarios/new" className="text-sm text-emerald-400 hover:underline" onClick={onClose}>
              + Create a new scenario
            </Link>
            <button
              onClick={start}
              disabled={creating}
              className="rounded-lg bg-emerald-700 px-5 py-2 text-sm font-semibold text-white hover:bg-emerald-600 disabled:opacity-50"
            >
              {creating ? 'Creating…' : 'Start adventure'}
            </button>
          </div>
        </div>
      )}
    </Modal>
  )
}

// ---------------------------------------------------------------------------
// Modal "Import AI Dungeon backup": upload do .zip (multipart) → ImportReport
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
    <Modal title="Import AI Dungeon backup" onClose={onClose}>
      {!report ? (
        <div className="flex flex-col gap-4">
          <p className="text-sm text-zinc-400">
            Select the <code className="text-zinc-300">.zip</code> exported from AI Dungeon
            (contains <code className="text-zinc-300">metadata.json</code> +{' '}
            <code className="text-zinc-300">actions-*.json</code>). The story continues from the
            exact point where it stopped.
          </p>
          <button
            onClick={() => fileRef.current?.click()}
            className="rounded-lg border border-dashed border-zinc-700 p-6 text-center text-sm text-zinc-400 hover:border-zinc-500"
          >
            {file ? (
              <span className="text-zinc-200">
                📦 {file.name} <span className="text-zinc-500">({(file.size / 1024).toFixed(0)} KB)</span>
              </span>
            ) : (
              'Click to choose the .zip file'
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
            <button onClick={onClose} className="rounded-lg border border-zinc-700 px-4 py-2 text-sm text-zinc-300 hover:bg-zinc-800">
              Cancel
            </button>
            <button
              onClick={doImport}
              disabled={!file || importing}
              className="rounded-lg bg-emerald-700 px-4 py-2 text-sm font-semibold text-white hover:bg-emerald-600 disabled:opacity-50"
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
              <div className="rounded-lg border border-emerald-900 bg-emerald-950/40 p-4">
                <h3 className="font-semibold text-emerald-300">
                  Import complete — {report.imported} adventure{report.imported === 1 ? '' : 's'} imported
                </h3>
                <ul className="mt-2 max-h-48 space-y-1 overflow-y-auto">
                  {(report.results || []).map((r, i) => (
                    <li key={r.adventure_id ?? i} className="flex items-baseline justify-between gap-2 rounded bg-zinc-800/60 px-2 py-1 text-sm">
                      <span className="truncate text-zinc-200">{r.title}</span>
                      <span className="shrink-0 text-xs text-zinc-500">{r.actions_imported ?? 0} actions</span>
                    </li>
                  ))}
                </ul>
              </div>

              {(() => {
                const warnings = (report.results || []).flatMap((r) =>
                  (r.warnings || []).map((w) => `${r.title}: ${w}`)
                )
                return warnings.length > 0 ? (
                  <div className="rounded-lg border border-amber-900 bg-amber-950/40 p-3">
                    <h4 className="mb-1 text-sm font-semibold text-amber-300">
                      Warnings ({warnings.length})
                    </h4>
                    <ul className="max-h-40 list-disc overflow-y-auto pl-5 text-xs text-amber-200/80">
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
                  className="rounded-lg bg-emerald-700 px-4 py-2 text-sm font-semibold text-white hover:bg-emerald-600"
                >
                  Done
                </button>
              </div>
            </>
          ) : (
            <>
              <div className="rounded-lg border border-emerald-900 bg-emerald-950/40 p-4">
                <h3 className="font-semibold text-emerald-300">Import complete — “{report.title}”</h3>
            <ul className="mt-2 grid grid-cols-3 gap-2 text-center text-sm">
              <li className="rounded bg-zinc-800/60 p-2">
                <div className="text-xl font-bold text-zinc-100">{report.actions_imported ?? 0}</div>
                <div className="text-xs text-zinc-500">actions</div>
              </li>
              <li className="rounded bg-zinc-800/60 p-2">
                <div className="text-xl font-bold text-zinc-100">{report.cards_imported ?? 0}</div>
                <div className="text-xs text-zinc-500">story cards</div>
              </li>
              <li className="rounded bg-zinc-800/60 p-2">
                <div className="text-xl font-bold text-zinc-100">{report.memories_imported ?? 0}</div>
                <div className="text-xs text-zinc-500">memories</div>
              </li>
            </ul>
          </div>

          {report.warnings?.length > 0 && (
            <div className="rounded-lg border border-amber-900 bg-amber-950/40 p-3">
              <h4 className="mb-1 text-sm font-semibold text-amber-300">
                Warnings ({report.warnings.length})
              </h4>
              <ul className="max-h-40 list-disc overflow-y-auto pl-5 text-xs text-amber-200/80">
                {report.warnings.map((w, i) => (
                  <li key={i}>{w}</li>
                ))}
              </ul>
            </div>
          )}

          <div className="flex justify-end gap-2">
            <button onClick={onClose} className="rounded-lg border border-zinc-700 px-4 py-2 text-sm text-zinc-300 hover:bg-zinc-800">
              Close
            </button>
            <button
              onClick={() => navigate(`/play/${report.adventure_id}`)}
              className="rounded-lg bg-emerald-700 px-4 py-2 text-sm font-semibold text-white hover:bg-emerald-600"
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
    'block w-full px-3 py-2 text-left text-sm text-zinc-300 hover:bg-zinc-800 hover:text-zinc-100'

  return (
    <div ref={rootRef} className="relative" onClick={guard}>
      <button
        onClick={onToggle}
        aria-label={`Manage “${adventure.title}”`}
        title="Manage adventure"
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
            <span>Rename</span>
          </button>
          <button
            className="flex items-center gap-2 w-full px-3 py-2 text-left text-xs text-secondary hover:bg-elevated hover:text-primary transition"
            onClick={exportZip}
          >
            <Download className="w-3.5 h-3.5" />
            <span>Export .zip</span>
          </button>
          <button
            className="flex items-center gap-2 w-full px-3 py-2 text-left text-xs text-red-400 hover:bg-red-950/40 hover:text-red-300 transition"
            onClick={() => {
              onClose()
              onDelete()
            }}
          >
            <Trash2 className="w-3.5 h-3.5" />
            <span>Delete</span>
          </button>
        </div>
      )}

    </div>
  )
}

// ---------------------------------------------------------------------------
// Modal "Rename adventure": edita título (+ descrição opcional) → PATCH
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
    <Modal title="Rename adventure" onClose={onClose}>
      <div className="flex flex-col gap-4">
        <label className="flex flex-col gap-1">
          <span className="text-sm font-medium text-zinc-300">Title</span>
          <input
            autoFocus
            value={title}
            onChange={(e) => setTitle(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === 'Enter') save()
            }}
            className="rounded-lg border border-zinc-700 bg-zinc-800 px-3 py-2 text-sm text-zinc-100 outline-none focus:border-emerald-600"
          />
        </label>
        <label className="flex flex-col gap-1">
          <span className="text-sm font-medium text-zinc-300">
            Description <span className="text-zinc-500">(optional)</span>
          </span>
          <textarea
            value={description}
            onChange={(e) => setDescription(e.target.value)}
            rows={3}
            className="resize-y rounded-lg border border-zinc-700 bg-zinc-800 px-3 py-2 text-sm text-zinc-100 outline-none focus:border-emerald-600"
          />
        </label>
        <div className="flex justify-end gap-2">
          <button onClick={onClose} className="rounded-lg border border-zinc-700 px-4 py-2 text-sm text-zinc-300 hover:bg-zinc-800">
            Cancel
          </button>
          <button
            onClick={save}
            disabled={saving || !title.trim()}
            className="rounded-lg bg-emerald-700 px-4 py-2 text-sm font-semibold text-white hover:bg-emerald-600 disabled:opacity-50"
          >
            {saving ? 'Saving…' : 'Save'}
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
    <Modal title="Delete adventure" onClose={onClose}>
      <div className="flex flex-col gap-4">
        <p className="text-sm text-zinc-300">
          Delete <span className="font-semibold text-zinc-100">“{adventure.title}”</span>?
        </p>
        <p className="rounded-lg border border-red-900 bg-red-950/40 p-3 text-sm text-red-200">
          This permanently removes the story, its actions, story cards and memories. This action
          cannot be undone.
        </p>
        <div className="flex justify-end gap-2">
          <button onClick={onClose} className="rounded-lg border border-zinc-700 px-4 py-2 text-sm text-zinc-300 hover:bg-zinc-800">
            Cancel
          </button>
          <button
            onClick={confirm}
            disabled={busy}
            className="rounded-lg bg-red-700 px-4 py-2 text-sm font-semibold text-white hover:bg-red-600 disabled:opacity-50"
          >
            {busy ? 'Deleting…' : 'Delete permanently'}
          </button>
        </div>
      </div>
    </Modal>
  )
}
