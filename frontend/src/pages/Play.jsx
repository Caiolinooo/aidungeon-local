import { useCallback, useEffect, useRef, useState } from 'react'
import { Link, useParams } from 'react-router-dom'
import { api } from '../api.js'
import { streamGeneration } from '../sse.js'
import { useToast } from '../components/Toast.jsx'
import Modal from '../components/Modal.jsx'
import FeedItem from '../components/FeedItem.jsx'
import InputBar from '../components/InputBar.jsx'
import Drawer from '../components/Drawer.jsx'
import { bySeq, playerDisplayText } from '../utils.js'

// Tela 2 — Jogo (a principal, PLANO.md §4.5):
//  - feed central rolável com scroll automático (prosa da IA; ações do jogador
//    em destaque estilo citação, detectando pelo Action.type);
//  - barra de entrada fixa (tabs Do/Say/Story) — desabilitada durante a geração;
//  - toolbar: Retry / Undo / Continue (+ Stop durante o stream);
//  - hover num bloco → Edit (modal → PATCH /api/actions/:id);
//  - drawer lateral: Story Cards / Memory / Config / Details;
//  - SSE: token (append no bloco "AI is writing…"), done (substitui pela(s)
//    Action persistida(s)), error (toast + estado limpo); Stop = AbortController.
export default function Play() {
  const { id } = useParams()
  const toast = useToast()

  const [adventure, setAdventure] = useState(null)
  const [actions, setActions] = useState(null)
  const [error, setError] = useState(null)
  const [gen, setGen] = useState(null) // { kind: 'action'|'continue'|'retry', text }
  const [drawerOpen, setDrawerOpen] = useState(false)
  const [editing, setEditing] = useState(null) // action em edição

  const abortRef = useRef(null)
  const feedRef = useRef(null)
  const generating = gen !== null

  // ----------------------------------------------------------- carregar estado
  const load = useCallback(async () => {
    setError(null)
    try {
      const st = await api.getAdventure(id)
      setAdventure(st)
      setActions([...(st.actions ?? [])].sort(bySeq))
    } catch (e) {
      setError(e)
      setAdventure(null)
      setActions([])
    }
  }, [id])

  useEffect(() => {
    load()
  }, [load])

  // cancela stream pendente ao sair da tela
  useEffect(() => () => abortRef.current?.abort(), [])

  // ------------------------------------------------------- scroll automático
  useEffect(() => {
    const el = feedRef.current
    if (el) el.scrollTop = el.scrollHeight
  }, [actions?.length, gen?.text])

  // ------------------------------------------------------------ geração SSE
  const runStream = useCallback(
    (kind, path, body, applyDone) => {
      if (generating) return
      const ctrl = new AbortController()
      abortRef.current = ctrl
      setGen({ kind, text: '' })
      streamGeneration(path, {
        body,
        signal: ctrl.signal,
        onToken: (tok) => setGen((g) => (g ? { ...g, text: g.text + tok } : g)),
        onDone: (data) => {
          setActions((prev) => applyDone(prev ?? [], data).sort(bySeq))
          setGen(null)
        },
        onError: (msg) => {
          toast(msg)
          setGen(null)
        },
      })
    },
    [generating, toast],
  )

  const sendAction = (type, text) =>
    runStream('action', `/adventures/${id}/action`, { type, text }, (prev, data) => {
      // done: {"player_action": Action, "action": Action} — ambas persistidas
      const out = [...prev]
      if (data.player_action) out.push(data.player_action)
      if (data.action) out.push(data.action)
      return out
    })

  const doContinue = () =>
    runStream('continue', `/adventures/${id}/continue`, null, (prev, data) =>
      data.action ? [...prev, data.action] : prev,
    )

  const doRetry = () =>
    runStream('retry', `/adventures/${id}/retry`, null, (prev, data) => {
      // retry: done traz a action `continue` substituta (mesmo seq, novo id)
      if (!data.action) return prev
      return prev.map((a) => (a.seq === data.action.seq ? data.action : a))
    })

  // Stop: cancela o stream localmente. O backend pode concluir e persistir a
  // action mesmo assim — nesse caso ela aparece ao recarregar a aventura.
  const stop = () => {
    abortRef.current?.abort()
    setGen(null)
  }

  // ------------------------------------------------------------------- undo
  const doUndo = async () => {
    try {
      const res = await api.undo(id)
      const removedIds = new Set((res.removed ?? []).map((a) => a.id))
      setActions((prev) => (prev ?? []).filter((a) => !removedIds.has(a.id)))
      toast('Last turn undone.', 'ok')
    } catch (e) {
      toast(e.message)
    }
  }

  // ------------------------------------------------------------------- edit
  const saveEdit = async (action, text) => {
    try {
      const updated = await api.patchAction(action.id, text)
      setActions((prev) => prev.map((a) => (a.id === updated.id ? updated : a)))
      setEditing(null)
      toast('Action edited.', 'ok')
    } catch (e) {
      toast(e.message)
    }
  }

  // ---------------------------------------------------------------- render
  if (error) {
    return (
      <div className="flex h-full flex-col items-center justify-center gap-4 p-8 text-center">
        <p className="text-red-300">
          {error.status === 0
            ? 'Cannot reach the backend server (http://127.0.0.1:8000). Is it running?'
            : error.message}
        </p>
        <div className="flex gap-2">
          <button
            onClick={load}
            className="rounded-lg bg-emerald-700 px-4 py-2 text-sm font-semibold text-white hover:bg-emerald-600"
          >
            ↻ Retry
          </button>
          <Link to="/" className="rounded-lg border border-zinc-700 px-4 py-2 text-sm text-zinc-300 hover:bg-zinc-800">
            ← Back to home
          </Link>
        </div>
      </div>
    )
  }

  if (!adventure || actions === null) {
    return <div className="flex h-full items-center justify-center text-zinc-500">Loading…</div>
  }

  const lastAction = actions[actions.length - 1]
  const canRetry = !generating && lastAction?.type === 'continue'
  const canUndo = !generating && actions.some((a) => a.type !== 'start')

  return (
    <div className="flex h-full flex-col">
      {/* header + toolbar */}
      <header className="flex items-center gap-2 border-b border-zinc-800 bg-zinc-950/95 px-3 py-2">
        <button
          onClick={() => setDrawerOpen(true)}
          className="rounded-md p-2 text-zinc-400 hover:bg-zinc-800 hover:text-zinc-100"
          aria-label="Open panel"
          title="Story Cards · Memory · Config · Details"
        >
          ☰
        </button>
        <div className="min-w-0 flex-1">
          <Link to="/" className="text-xs text-zinc-500 hover:text-zinc-300">
            ← Home
          </Link>
          <h1 className="truncate text-sm font-semibold text-zinc-100">{adventure.title}</h1>
        </div>
        <div className="flex items-center gap-1">
          {generating ? (
            <button
              onClick={stop}
              className="rounded-md bg-red-800 px-3 py-1.5 text-sm font-semibold text-white hover:bg-red-700"
              title="Stop generating (cancels the local stream)"
            >
              ■ Stop
            </button>
          ) : (
            <>
              <button
                onClick={doRetry}
                disabled={!canRetry}
                title={canRetry ? 'Regenerate the last AI response' : 'Retry is available when the last action is an AI response'}
                className="rounded-md border border-zinc-700 px-3 py-1.5 text-sm text-zinc-300 hover:bg-zinc-800 disabled:cursor-not-allowed disabled:opacity-40"
              >
                ↻ Retry
              </button>
              <button
                onClick={doUndo}
                disabled={!canUndo}
                title="Undo the last turn"
                className="rounded-md border border-zinc-700 px-3 py-1.5 text-sm text-zinc-300 hover:bg-zinc-800 disabled:cursor-not-allowed disabled:opacity-40"
              >
                ↩ Undo
              </button>
              <button
                onClick={doContinue}
                disabled={generating}
                title="Let the AI continue on its own"
                className="rounded-md bg-emerald-700 px-3 py-1.5 text-sm font-semibold text-white hover:bg-emerald-600 disabled:opacity-40"
              >
                ▶ Continue
              </button>
            </>
          )}
        </div>
      </header>

      {/* feed central rolável */}
      <main ref={feedRef} className="flex-1 overflow-y-auto px-3 py-4">
        <div className="mx-auto flex max-w-3xl flex-col gap-1">
          {actions.length === 0 && !generating && (
            <p className="rounded-lg border border-dashed border-zinc-800 p-6 text-center text-sm text-zinc-500">
              A blank page. Send a <b>Story</b> action to set the scene, or press{' '}
              <b>▶ Continue</b> and let the AI begin.
            </p>
          )}

          {actions.map((a) => (
            <FeedItem key={a.id} action={a} onEdit={setEditing} disabled={generating} />
          ))}

          {/* bloco "AI is writing…" com cursor animado (streaming token a token) */}
          {generating && (
            <div className="rounded-lg bg-zinc-900/50 px-3 py-2">
              <div className="mb-1 flex items-center gap-2 text-xs uppercase tracking-wide text-emerald-500">
                <span className="inline-block h-2 w-2 animate-pulse rounded-full bg-emerald-500" />
                AI is writing…
              </div>
              <p className="streaming-cursor whitespace-pre-wrap font-prose leading-relaxed text-zinc-200">
                {gen.text}
              </p>
            </div>
          )}
        </div>
      </main>

      {/* barra de entrada fixa embaixo */}
      <InputBar disabled={generating} onSend={sendAction} />

      {/* drawer lateral */}
      <Drawer
        open={drawerOpen}
        onClose={() => setDrawerOpen(false)}
        adventure={adventure}
        onAdventureChange={setAdventure}
      />

      {/* modal de edição de ação */}
      {editing && (
        <EditActionModal
          action={editing}
          onClose={() => setEditing(null)}
          onSave={saveEdit}
        />
      )}
    </div>
  )
}

// Modal Edit: textarea → PATCH /api/actions/:id. Para do/say mostra o texto sem
// o wrapper "> You ..." (o backend reformata de forma idempotente no contexto).
function EditActionModal({ action, onClose, onSave }) {
  const [text, setText] = useState(
    action.type === 'do' || action.type === 'say'
      ? playerDisplayText(action.type, action.text)
      : action.text,
  )
  const [saving, setSaving] = useState(false)

  const label = { do: 'You do', say: 'You say', story: 'Story', start: 'Opening', continue: 'AI response' }[action.type] ?? action.type

  return (
    <Modal title={`Edit — ${label}`} onClose={onClose} wide>
      <textarea
        value={text}
        onChange={(e) => setText(e.target.value)}
        rows={10}
        autoFocus
        className="w-full resize-y rounded-lg border border-zinc-700 bg-zinc-800 px-3 py-2 font-prose text-sm text-zinc-100 outline-none focus:border-emerald-600"
      />
      <div className="mt-3 flex justify-end gap-2">
        <button onClick={onClose} className="rounded-lg border border-zinc-700 px-4 py-2 text-sm text-zinc-300 hover:bg-zinc-800">
          Cancel
        </button>
        <button
          onClick={async () => {
            setSaving(true)
            await onSave(action, text)
            setSaving(false)
          }}
          disabled={saving}
          className="rounded-lg bg-emerald-700 px-4 py-2 text-sm font-semibold text-white hover:bg-emerald-600 disabled:opacity-50"
        >
          {saving ? 'Saving…' : 'Save'}
        </button>
      </div>
    </Modal>
  )
}
