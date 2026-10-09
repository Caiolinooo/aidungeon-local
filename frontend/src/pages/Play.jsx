import { useCallback, useEffect, useRef, useState, Suspense, lazy } from 'react'
import { Link, useParams } from 'react-router-dom'
import { api } from '../api.js'
import { streamGeneration } from '../sse.js'
import { useToast } from '../components/Toast.jsx'
import Modal from '../components/Modal.jsx'
import FeedItem from '../components/FeedItem.jsx'
import InputBar from '../components/InputBar.jsx'
import Drawer from '../components/Drawer.jsx'
import SceneBackdrop from '../components/SceneBackdrop.jsx'
import { bySeq, playerDisplayText } from '../utils.js'
import { loosenProse } from '../prose.js'

// Dock de avatares (v0.6.0): lazy p/ manter o three.js fora do bundle
// inicial (mesmo padrão do AvatarsTab no Drawer).
const ChatAvatarDock = lazy(() => import('../components/avatar/ChatAvatarDock.jsx'))

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
  const [seeing, setSeeing] = useState(false) // geração de imagem ("See") em andamento
  const [seeOpen, setSeeOpen] = useState(false) // popover do extra_prompt
  const [seePrompt, setSeePrompt] = useState('')
  const [choices, setChoices] = useState([]) // sugestões de próxima ação (v2)
  const [inputDirty, setInputDirty] = useState(false) // usuário digitando → esconde chips
  const [dockRefresh, setDockRefresh] = useState(0) // v0.5.0 — refetch dos cards do dock VRM

  const abortRef = useRef(null)
  const feedRef = useRef(null)
  // v0.5.0 — scroll inteligente: true enquanto o usuário está perto do fim;
  // quem sobe p/ reler não é puxado quando chega texto novo.
  const nearBottomRef = useRef(true)
  // id do bloco que acabou de chegar (done do stream) — a âncora do scroll.
  const newBlockIdRef = useRef(null)
  const initialScrollDone = useRef(false)
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

  // ------------------------------------------------------- scroll inteligente
  // Regras (v0.5.0):
  //  1. carga inicial → vai ao fim;
  //  2. streaming token a token → acompanha o fim SÓ se o usuário está perto
  //     dele (< 120px); quem subiu p/ reler fica onde está;
  //  3. done de nova action → rola para o INÍCIO do bloco novo (retomar a
  //     leitura do começo), também só se o usuário estava acompanhando o fim.
  const onFeedScroll = useCallback(() => {
    const el = feedRef.current
    if (!el) return
    nearBottomRef.current = el.scrollHeight - el.scrollTop - el.clientHeight < 120
  }, [])

  useEffect(() => {
    const el = feedRef.current
    if (!el || actions === null) return
    if (!initialScrollDone.current) {
      initialScrollDone.current = true
      el.scrollTop = el.scrollHeight
      return
    }
    if (newBlockIdRef.current) {
      const anchor = document.getElementById(`action-${newBlockIdRef.current}`)
      newBlockIdRef.current = null
      if (anchor && nearBottomRef.current) {
        anchor.scrollIntoView({ block: 'start', behavior: 'smooth' })
      }
      return
    }
    if (nearBottomRef.current) el.scrollTop = el.scrollHeight
  }, [actions, gen?.text, seeing])

  // ------------------------------------------------------------ geração SSE
  // Choices (v2): busca sugestões após cada turno concluído. Chips são
  // opcionais — erros (409/503) só limpam as sugestões, sem toast.
  const fetchChoices = useCallback(async () => {
    try {
      const r = await api.getChoices(id)
      setChoices(Array.isArray(r?.choices) ? r.choices : [])
    } catch {
      setChoices([])
    }
  }, [id])

  const runStream = useCallback(
    (kind, path, body, applyDone) => {
      if (generating) return
      const ctrl = new AbortController()
      abortRef.current = ctrl
      setGen({ kind, text: '' })
      setChoices([])
      streamGeneration(path, {
        body,
        signal: ctrl.signal,
        onToken: (tok) => setGen((g) => (g ? { ...g, text: g.text + tok } : g)),
        onDone: (data) => {
          // Âncora do scroll: começo do turno novo (ação do jogador, se houver,
          // senão a resposta da IA) — ver efeito "scroll inteligente".
          newBlockIdRef.current = data.player_action?.id ?? data.action?.id ?? null
          setActions((prev) => applyDone(prev ?? [], data).sort(bySeq))
          setGen(null)
          fetchChoices() // input está idle após o envio → novas sugestões
        },
        onError: (msg) => {
          toast(msg)
          setGen(null)
        },
      })
    },
    [generating, toast, fetchChoices],
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
      setChoices([])
      toast('Last turn undone.', 'ok')
    } catch (e) {
      toast(e.message)
    }
  }

  // --------------------------------------------------------------- see (v2)
  // Gera imagem da cena da ÚLTIMA action (JSON, não-SSE — pode levar minutos).
  // Em sucesso a action type=see retornada entra no feed; 409/503 → toast.
  const doSee = async () => {
    const last = actions?.[actions.length - 1]
    if (!last || seeing || generating) return
    setSeeing(true)
    setSeeOpen(false)
    try {
      const extra = seePrompt.trim()
      const r = await api.see(id, { action_seq: last.seq, extra_prompt: extra || undefined })
      if (r.action) setActions((prev) => [...(prev ?? []), r.action].sort(bySeq))
      setSeePrompt('')
      toast('Image generated.', 'ok')
    } catch (e) {
      toast(e.message)
    } finally {
      setSeeing(false)
    }
  }

  // ----------------------------------------------------------- choices (v2)
  // Clique num chip envia imediatamente como aquela action type (mesmo caminho
  // do InputBar). Os chips somem ao iniciar a geração (setChoices([]) no runStream).
  const pickChoice = (choice) => {
    if (generating || seeing) return
    if (!['do', 'say', 'story'].includes(choice.type) || !choice.text?.trim()) return
    sendAction(choice.type, choice.text.trim())
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
  // "See" exige ao menos uma resposta da IA e nenhuma geração em andamento
  const canSee = !generating && !seeing && actions.some((a) => a.type === 'continue')
  // v0.5.0 — destaque "New": última resposta de texto gerada pela IA.
  const lastAiTextId = [...actions].reverse().find((a) => a.type === 'continue')?.id
  // Último texto narrativo (alimenta dock VRM + auto-emoção dos avatares).
  const latestStoryText = [...actions].reverse().find(
    (a) => a.type !== 'do' && a.type !== 'say' && a.type !== 'see',
  )?.text
  const showChoices = !generating && !seeing && !inputDirty && choices.length > 0
  const lastPlayer = [...actions].reverse().find(
    (a) => a.type === 'do' || a.type === 'say' || a.type === 'story',
  )

  return (
    <div className="flex h-full min-h-0 flex-col overflow-hidden">
      {/* header + toolbar */}
      <header className="flex flex-col gap-2 border-b border-zinc-800 bg-zinc-950/95 px-3 py-2 pt-[max(0.5rem,env(safe-area-inset-top))] sm:flex-row sm:items-center">
        <div className="flex min-w-0 items-center gap-2">
        <button
          onClick={() => setDrawerOpen(true)}
          className="shrink-0 rounded-md p-2 text-zinc-400 hover:bg-zinc-800 hover:text-zinc-100"
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
        </div>
        <div className="flex items-center gap-1 sm:shrink-0">
          {generating ? (
            <button
              onClick={stop}
              className="rounded-md bg-red-800 px-3 py-2 text-sm font-semibold text-white hover:bg-red-700"
              title="Stop generating (cancels the local stream)"
            >
              ■ Stop
            </button>
          ) : (
            <>
              <div className="-mx-1 flex items-center gap-1 overflow-x-auto px-1 sm:mx-0 sm:overflow-visible">
              <button
                onClick={doRetry}
                disabled={!canRetry}
                title={canRetry ? 'Regenerate the last AI response' : 'Retry is available when the last action is an AI response'}
                className="min-h-11 shrink-0 rounded-md border border-zinc-700 px-2.5 py-2 text-xs text-zinc-300 hover:bg-zinc-800 disabled:cursor-not-allowed disabled:opacity-40 sm:px-3 sm:text-sm"
              >
                ↻ Retry
              </button>
              <button
                onClick={doUndo}
                disabled={!canUndo}
                title="Undo the last turn"
                className="min-h-11 shrink-0 rounded-md border border-zinc-700 px-2.5 py-2 text-xs text-zinc-300 hover:bg-zinc-800 disabled:cursor-not-allowed disabled:opacity-40 sm:px-3 sm:text-sm"
              >
                ↩ Undo
              </button>
              <button
                onClick={doContinue}
                disabled={generating}
                title="Let the AI continue on its own"
                className="min-h-11 shrink-0 rounded-md bg-emerald-700 px-2.5 py-2 text-xs font-semibold text-white hover:bg-emerald-600 disabled:opacity-40 sm:px-3 sm:text-sm"
              >
                ▶ Continue
              </button>
              </div>
              <div className="relative shrink-0">
                <button
                  onClick={() => canSee && setSeeOpen((o) => !o)}
                  disabled={!canSee}
                  title={
                    canSee
                      ? 'Generate an image of the current scene'
                      : seeing
                        ? 'Image generation in progress…'
                        : 'See is available after the AI writes at least one response'
                  }
                  className="flex min-h-11 shrink-0 items-center gap-1.5 rounded-md border border-zinc-700 px-2.5 py-2 text-xs text-zinc-300 hover:bg-zinc-800 disabled:cursor-not-allowed disabled:opacity-40 sm:px-3 sm:text-sm"
                >
                  <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
                    <rect x="3" y="3" width="18" height="18" rx="2" />
                    <circle cx="8.5" cy="8.5" r="1.5" />
                    <path d="m21 15-5-5L5 21" />
                  </svg>
                  {seeing ? 'Seeing…' : 'See'}
                </button>
                {seeOpen && canSee && (
                  <div className="absolute right-0 top-full z-30 mt-1 w-[min(18rem,calc(100vw-1.5rem))] rounded-lg border border-zinc-700 bg-zinc-900 p-3 shadow-xl">
                    <label className="mb-1 block text-xs font-semibold uppercase tracking-wide text-zinc-500">
                      Extra detail (optional)
                    </label>
                    <textarea
                      value={seePrompt}
                      onChange={(e) => setSeePrompt(e.target.value)}
                      rows={2}
                      autoFocus
                      placeholder='e.g. "at sunset, storm approaching"'
                      className="w-full resize-y rounded-lg border border-zinc-700 bg-zinc-800 px-3 py-2 text-sm text-zinc-100 outline-none placeholder:text-zinc-500 focus:border-emerald-600"
                    />
                    <div className="mt-2 flex justify-end gap-2">
                      <button
                        onClick={() => setSeeOpen(false)}
                        className="rounded-md border border-zinc-700 px-3 py-1 text-xs text-zinc-300 hover:bg-zinc-800"
                      >
                        Cancel
                      </button>
                      <button
                        onClick={doSee}
                        className="rounded-md bg-emerald-700 px-3 py-1 text-xs font-semibold text-white hover:bg-emerald-600"
                      >
                        Generate image
                      </button>
                    </div>
                  </div>
                )}
              </div>
            </>
          )}
        </div>
      </header>

      {/* faixa da cena em cima; o texto fica no fundo sólido, sem a pintura atrás */}
      <div className="flex min-h-0 flex-1 flex-col overflow-hidden">
      <SceneBackdrop
        adventureId={id}
        storyText={latestStoryText}
        generating={generating}
        playerAction={lastPlayer}
      />
      <div className="relative min-h-0 flex-1">
      <main ref={feedRef} onScroll={onFeedScroll} className="h-full overflow-y-auto overscroll-y-contain bg-zinc-950 px-3 py-4">
        <div className="mx-auto flex max-w-prose flex-col gap-3 pb-14 sm:pb-0">
          {actions.length === 0 && !generating && (
            <p className="rounded-lg border border-dashed border-zinc-800 p-6 text-center text-sm text-zinc-500">
              A blank page. Send a <b>Story</b> action to set the scene, or press{' '}
              <b>▶ Continue</b> and let the AI begin.
            </p>
          )}

          {actions.map((a) => (
            <FeedItem key={a.id} action={a} onEdit={setEditing} disabled={generating} isLatest={a.id === lastAiTextId} />
          ))}

          {/* bloco "AI is writing…" com cursor animado (streaming token a token) */}
          {generating && (
            <div className="rounded-lg bg-zinc-900/50 px-3 py-2">
              <div className="mb-1 flex items-center gap-2 text-xs uppercase tracking-wide text-emerald-500">
                <span className="inline-block h-2 w-2 animate-pulse rounded-full bg-emerald-500" />
                AI is writing…
              </div>
              <p className="streaming-cursor whitespace-pre-wrap font-prose text-[1.05rem] leading-8 text-zinc-100">
                {loosenProse(gen.text)}
              </p>
            </div>
          )}

          {/* placeholder da geração de imagem ("See") — pode levar minutos */}
          {seeing && (
            <div className="rounded-lg bg-zinc-900/50 px-3 py-2">
              <div className="mb-1 flex items-center gap-2 text-xs uppercase tracking-wide text-emerald-500">
                <span className="inline-block h-2 w-2 animate-pulse rounded-full bg-emerald-500" />
                Generating image… (this can take a few minutes)
              </div>
              <div className="flex h-48 items-center justify-center rounded-lg border border-dashed border-zinc-800 text-sm text-zinc-600">
                Painting the scene…
              </div>
            </div>
          )}
        </div>
      </main>
      <Suspense fallback={null}>
        <ChatAvatarDock
          adventureId={id}
          storyText={latestStoryText}
          refreshKey={dockRefresh}
        />
      </Suspense>
      </div>
      </div>

      {/* choices (v2): sugestões clicáveis acima da InputBar — clicar envia já */}
      {showChoices && (
        <div className="border-t border-zinc-800/60 bg-zinc-950/95 px-3 pt-2 backdrop-blur">
          <div className="mx-auto flex max-w-3xl items-center gap-2 overflow-x-auto pb-1">
            <span className="shrink-0 text-[10px] font-semibold uppercase tracking-wide text-zinc-600">
              Suggested
            </span>
            {choices.map((c, i) => (
              <button
                key={i}
                onClick={() => pickChoice(c)}
                title={`Send as "${c.type}"`}
                className="max-w-[16rem] shrink-0 truncate rounded-full border border-zinc-700 bg-zinc-900 px-3 py-2 text-xs text-zinc-300 hover:border-emerald-700 hover:text-emerald-300"
              >
                <span className="mr-1 font-semibold uppercase text-zinc-500">{c.type}</span>
                {c.text}
              </button>
            ))}
          </div>
        </div>
      )}

      {/* barra de entrada fixa embaixo */}
      <InputBar
        disabled={generating}
        onSend={sendAction}
        onTextChange={(t) => setInputDirty(t.trim().length > 0)}
      />

      {/* drawer lateral — storyText (última narrativa da IA) alimenta o
          auto-detect de emoção dos avatares VRM (v0.4.0) */}
      <Drawer
        open={drawerOpen}
        onClose={() => {
          setDrawerOpen(false)
          // cards podem ter mudado no drawer (NPCs/Avatars) → dock refaz o fetch
          setDockRefresh((k) => k + 1)
        }}
        adventure={adventure}
        onAdventureChange={setAdventure}
        storyText={latestStoryText}
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
