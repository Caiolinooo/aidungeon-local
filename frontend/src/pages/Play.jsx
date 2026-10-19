import { useCallback, useEffect, useRef, useState, Suspense, lazy } from 'react'
import { Link, useParams } from 'react-router-dom'
import { api } from '../api.js'
import { streamGeneration } from '../sse.js'
import { useToast } from '../components/Toast.jsx'
import Modal from '../components/Modal.jsx'
import FeedItem from '../components/FeedItem.jsx'
import InputBar from '../components/InputBar.jsx'
import GamePanel from '../components/GamePanel.jsx'
import Drawer, { InspectorPane } from '../components/Drawer.jsx'
import SceneBackdrop from '../components/SceneBackdrop.jsx'
import ReaderPreferencesModal from '../components/ReaderPreferencesModal.jsx'
import { useTheme } from '../themeContext.jsx'
import { bySeq, playerDisplayText } from '../utils.js'
import { loosenProse } from '../prose.js'
import {
  ArrowLeft,
  RotateCcw,
  Undo2,
  Play as PlayIcon,
  Square,
  Image as ImageIcon,
  BookOpen,
  PanelRight,
  PanelRightClose,
  Menu,
  Settings as SettingsIcon,
  Sliders,
  ChevronRight,
  X,
} from 'lucide-react'

// Avatar dock (lazy to keep three.js out of main bundle)
const ChatAvatarDock = lazy(() => import('../components/avatar/ChatAvatarDock.jsx'))

export default function Play() {
  const { id } = useParams()
  const toast = useToast()
  const { readerPrefs, theme } = useTheme()

  const [adventure, setAdventure] = useState(null)
  const [actions, setActions] = useState(null)
  const [error, setError] = useState(null)
  const [gen, setGen] = useState(null) // { kind: 'action'|'continue'|'retry', text }
  const [drawerOpen, setDrawerOpen] = useState(false)
  const [editing, setEditing] = useState(null)
  const [seeing, setSeeing] = useState(false)
  const [seeOpen, setSeeOpen] = useState(false)
  const [seePrompt, setSeePrompt] = useState('')
  const [choices, setChoices] = useState([])
  const [webSources, setWebSources] = useState([])
  const [inputDirty, setInputDirty] = useState(false)
  const [dockRefresh, setDockRefresh] = useState(0)

  // 2-Pane Desktop Layout & Inspector State
  const [isInspectorDocked, setIsInspectorDocked] = useState(() => {
    return typeof window !== 'undefined' && window.innerWidth >= 1024
  })
  const [inspectorTab, setInspectorTab] = useState('cards')
  const [showReaderModal, setShowReaderModal] = useState(false)

  // Dynamic game controls
  const [uiProfile, setUiProfile] = useState(undefined)
  const [uiState, setUiState] = useState({})
  const [panelOpen, setPanelOpen] = useState(false)
  const [regeneratingProfile, setRegeneratingProfile] = useState(false)
  const [inject, setInject] = useState(null)

  const abortRef = useRef(null)
  const seeTokenRef = useRef(0)
  const feedRef = useRef(null)
  const nearBottomRef = useRef(true)
  const newBlockIdRef = useRef(null)
  const initialScrollDone = useRef(false)
  const generating = gen !== null

  // Handle screen resize for responsive desktop docking
  useEffect(() => {
    const handleResize = () => {
      if (window.innerWidth < 1024) {
        setIsInspectorDocked(false)
      } else {
        setIsInspectorDocked(true)
      }
    }
    window.addEventListener('resize', handleResize)
    return () => window.removeEventListener('resize', handleResize)
  }, [])

  // Load adventure state
  const load = useCallback(async () => {
    setError(null)
    try {
      const st = await api.getAdventure(id)
      setAdventure(st)
      setActions([...(st.actions ?? [])].sort(bySeq))
      setUiProfile(st.ui_profile ?? null)
      setUiState(st.ui_state ?? {})
    } catch (e) {
      setError(e)
      setAdventure(null)
      setActions([])
    }
  }, [id])

  useEffect(() => {
    load()
  }, [load])

  // AI-generated UI controls
  const profileRequestedRef = useRef(false)
  useEffect(() => {
    if (uiProfile !== null || profileRequestedRef.current) return
    profileRequestedRef.current = true
    api.generateUiProfile(id)
      .then((r) => {
        setUiProfile(r.profile ?? null)
        setUiState(r.state ?? {})
        if ((r.profile?.widgets ?? []).length > 0) setPanelOpen(true)
      })
      .catch(() => setUiProfile(null))
  }, [uiProfile, id])

  const regenerateProfile = useCallback(async () => {
    if (regeneratingProfile) return
    setRegeneratingProfile(true)
    try {
      const r = await api.generateUiProfile(id)
      setUiProfile(r.profile ?? null)
      setUiState(r.state ?? {})
      if ((r.profile?.widgets ?? []).length > 0) {
        setPanelOpen(true)
        toast('Game controls redesigned.', 'ok')
      } else {
        setPanelOpen(false)
        toast('AI kept it text-only — no extra controls needed.', 'ok')
      }
    } catch (e) {
      toast(e.message)
    } finally {
      setRegeneratingProfile(false)
    }
  }, [id, regeneratingProfile, toast])

  // Debounced UI state sync
  const uiStateTimerRef = useRef(null)
  const handleUiStateChange = useCallback((next) => {
    setUiState(next)
    clearTimeout(uiStateTimerRef.current)
    uiStateTimerRef.current = setTimeout(() => {
      api.putUiState(id, next).catch(() => {})
    }, 600)
  }, [id])
  useEffect(() => () => clearTimeout(uiStateTimerRef.current), [])

  const insertToInput = useCallback((text) => {
    setInject({ text, nonce: Date.now() })
  }, [])

  useEffect(() => () => abortRef.current?.abort(), [])

  // Smart feed scroll
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

  // Choices suggestions
  const fetchChoices = useCallback(async () => {
    try {
      const r = await api.getChoices(id)
      setChoices(Array.isArray(r?.choices) ? r.choices : [])
    } catch {
      setChoices([])
    }
  }, [id])

  // SSE Stream runner
  const runStream = useCallback(
    (kind, path, body, applyDone) => {
      if (generating) return
      const ctrl = new AbortController()
      abortRef.current = ctrl
      setGen({ kind, text: '' })
      setChoices([])
      setWebSources([])
      streamGeneration(path, {
        body,
        signal: ctrl.signal,
        onToken: (tok) => setGen((g) => (g ? { ...g, text: g.text + tok } : g)),
        onDone: (data) => {
          newBlockIdRef.current = data.player_action?.id ?? data.action?.id ?? null
          setActions((prev) => applyDone(prev ?? [], data).sort(bySeq))
          setGen(null)
          if (Array.isArray(data.web_sources) && data.web_sources.length) {
            setWebSources(data.web_sources)
          }
          fetchChoices()
        },
        onError: (msg) => {
          toast(msg)
          setGen(null)
        },
      })
    },
    [generating, toast, fetchChoices],
  )

  const sendAction = (type, text, opts) =>
    runStream('action', `/adventures/${id}/action`, {
      type,
      text,
      ...(opts?.webSearch ? { web_search: true } : {}),
    }, (prev, data) => {
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
      if (!data.action) return prev
      return prev.map((a) => (a.seq === data.action.seq ? data.action : a))
    })

  const stop = () => {
    abortRef.current?.abort()
    setGen(null)
  }

  const doUndo = async () => {
    try {
      const res = await api.undo(id)
      const removedIds = new Set((res.removed ?? []).map((a) => a.id))
      setActions((prev) => (prev ?? []).filter((a) => !removedIds.has(a.id)))
      setChoices([])
      setWebSources([])
      toast('Last turn undone.', 'ok')
    } catch (e) {
      toast(e.message)
    }
  }

  const watchSee = (jobId, token) => {
    const wait = (ms) => new Promise((resolve) => setTimeout(resolve, ms))
    ;(async () => {
      const deadline = Date.now() + 180000
      try {
        while (Date.now() < deadline) {
          if (seeTokenRef.current !== token) return
          const job = await api.getAdventureJob(id, jobId)
          if (seeTokenRef.current !== token) return
          if (job.status === 'done') {
            const action = job.result?.image?.action
            if (action) setActions((prev) => [...(prev ?? []), action].sort(bySeq))
            toast('Image generated.', 'ok')
            return
          }
          if (job.status === 'error' || job.status === 'canceled') {
            toast(job.error || 'Could not paint the scene.')
            return
          }
          await wait(800)
        }
        toast('The painting is still running.')
      } catch (e) {
        if (seeTokenRef.current === token) toast(e.message)
      } finally {
        if (seeTokenRef.current === token) setSeeing(false)
      }
    })()
  }

  const doSee = async () => {
    const last = actions?.[actions.length - 1]
    if (!last || seeing || generating) return
    setSeeing(true)
    setSeeOpen(false)
    try {
      const extra = seePrompt.trim()
      const r = await api.see(id, { action_seq: last.seq, extra_prompt: extra || undefined })
      if (r?.action) {
        setActions((prev) => [...(prev ?? []), r.action].sort(bySeq))
        setSeePrompt('')
        toast('Image generated.', 'ok')
        setSeeing(false)
        return
      }
      if (!r?.job_id) {
        setSeeing(false)
        toast('Could not start the painting.')
        return
      }
      setSeePrompt('')
      const token = seeTokenRef.current + 1
      seeTokenRef.current = token
      watchSee(r.job_id, token)
    } catch (e) {
      setSeeing(false)
      toast(e.message)
    }
  }

  const pickChoice = (choice) => {
    if (generating) return
    if (!['do', 'say', 'story'].includes(choice.type) || !choice.text?.trim()) return
    sendAction(choice.type, choice.text.trim())
  }

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

  if (error) {
    return (
      <div className="flex h-full flex-col items-center justify-center gap-4 p-8 text-center bg-canvas text-primary">
        <p className="text-red-300">
          {error.status === 0
            ? 'Cannot reach the backend server (http://127.0.0.1:8000). Is it running?'
            : error.message}
        </p>
        <div className="flex gap-2">
          <button
            onClick={load}
            className="flex items-center gap-1.5 rounded-xl bg-accent-pri px-4 py-2 text-xs font-semibold text-white hover:bg-accent-hov transition"
          >
            <RotateCcw className="w-3.5 h-3.5" />
            <span>Retry</span>
          </button>
          <Link
            to="/"
            className="flex items-center gap-1.5 rounded-xl border border-subtle px-4 py-2 text-xs text-secondary hover:bg-elevated transition"
          >
            <ArrowLeft className="w-3.5 h-3.5" />
            <span>Back to Home</span>
          </Link>
        </div>
      </div>
    )
  }

  if (!adventure || actions === null) {
    return <div className="flex h-full items-center justify-center text-xs text-muted bg-canvas">Loading adventure…</div>
  }

  const lastAction = actions[actions.length - 1]
  const canRetry = !generating && lastAction?.type === 'continue'
  const canUndo = !generating && actions.some((a) => a.type !== 'start')
  const canSee = !generating && !seeing && actions.some((a) => a.type === 'continue')
  const lastAiTextId = [...actions].reverse().find((a) => a.type === 'continue')?.id
  const latestStoryText = [...actions].reverse().find(
    (a) => a.type !== 'do' && a.type !== 'say' && a.type !== 'see',
  )?.text
  const showChoices = !generating && !inputDirty && choices.length > 0
  const lastPlayer = [...actions].reverse().find(
    (a) => a.type === 'do' || a.type === 'say' || a.type === 'story',
  )

  // Typography Preferences mapping
  const colWidthCls = {
    compact: 'max-w-[560px]',
    standard: 'max-w-[720px]',
    wide: 'max-w-[880px]',
    full: 'max-w-full px-2',
  }[readerPrefs?.columnWidth || 'standard']

  const fontFamilyCls = {
    serif: 'font-serif',
    sans: 'font-sans',
    mono: 'font-mono',
  }[readerPrefs?.fontFamily || 'serif']

  const lineHeightValue = {
    normal: 1.5,
    relaxed: 1.75,
    loose: 2.0,
  }[readerPrefs?.lineHeight || 'relaxed']

  const fontSizeValue = readerPrefs?.fontSize || 17

  const controlsContent = uiProfile ? (
    <GamePanel
      profile={uiProfile}
      state={uiState}
      disabled={generating}
      regenerating={regeneratingProfile}
      onStateChange={handleUiStateChange}
      onSend={sendAction}
      onInsert={insertToInput}
      onRegenerate={regenerateProfile}
      inline={true}
    />
  ) : null

  return (
    <div className="flex h-full w-full overflow-hidden bg-canvas text-primary">
      {/* LEFT COLUMN: Main Story Pane */}
      <div className="flex flex-1 min-w-0 flex-col h-full overflow-hidden relative">
        {/* Header + Action Bar */}
        <header className="flex flex-col gap-2 border-b border-subtle bg-surface px-3 py-2 pt-[max(0.5rem,env(safe-area-inset-top))] sm:flex-row sm:items-center sm:justify-between shrink-0">
          <div className="flex min-w-0 items-center gap-2">
            {/* Mobile Drawer Button */}
            <button
              onClick={() => setDrawerOpen(true)}
              className="lg:hidden min-h-11 min-w-11 flex items-center justify-center rounded-xl p-2 text-secondary hover:bg-elevated hover:text-primary transition"
              aria-label="Open inspector drawer"
              title="Cards, NPCs, Memory, Controls"
            >
              <Menu className="w-5 h-5" />
            </button>

            <Link
              to="/"
              title="Return to home"
              className="min-h-11 flex items-center gap-1.5 rounded-xl border border-subtle bg-surface px-2.5 py-1.5 text-xs text-secondary hover:text-primary hover:bg-elevated transition"
            >
              <ArrowLeft className="w-3.5 h-3.5" />
              <span className="hidden sm:inline">Home</span>
            </Link>

            <div className="min-w-0 flex-1 pl-1">
              <h1 className="truncate text-xs sm:text-sm font-bold text-primary">
                {adventure.title}
              </h1>
            </div>
          </div>

          {/* Action Toolbar */}
          <div className="flex items-center gap-1.5 flex-wrap sm:flex-nowrap justify-end">
            {generating ? (
              <button
                onClick={stop}
                className="min-h-11 flex items-center gap-1.5 rounded-xl bg-red-800 px-4 py-2 text-xs font-semibold text-white hover:bg-red-700 shadow transition"
                title="Stop generation"
              >
                <Square className="w-3.5 h-3.5 fill-current" />
                <span>Stop</span>
              </button>
            ) : (
              <>
                <div className="flex items-center gap-1">
                  <button
                    onClick={doRetry}
                    disabled={!canRetry}
                    aria-label="Retry last response"
                    title={canRetry ? 'Regenerate the last AI response' : 'Retry is available after an AI response'}
                    className="min-h-11 flex items-center gap-1 rounded-xl border border-subtle bg-surface px-3 py-2 text-xs text-secondary hover:text-primary hover:bg-elevated disabled:cursor-not-allowed disabled:opacity-40 transition"
                  >
                    <RotateCcw className="w-3.5 h-3.5" />
                    <span className="hidden sm:inline">Retry</span>
                  </button>

                  <button
                    onClick={doUndo}
                    disabled={!canUndo}
                    aria-label="Undo last turn"
                    title="Undo the last player and AI action"
                    className="min-h-11 flex items-center gap-1 rounded-xl border border-subtle bg-surface px-3 py-2 text-xs text-secondary hover:text-primary hover:bg-elevated disabled:cursor-not-allowed disabled:opacity-40 transition"
                  >
                    <Undo2 className="w-3.5 h-3.5" />
                    <span className="hidden sm:inline">Undo</span>
                  </button>

                  <button
                    onClick={doContinue}
                    disabled={generating}
                    aria-label="Continue writing"
                    title="Let the AI write the next paragraph"
                    className="min-h-11 flex items-center gap-1 rounded-xl bg-accent-pri px-3.5 py-2 text-xs font-semibold text-white hover:bg-accent-hov shadow transition disabled:opacity-40"
                  >
                    <PlayIcon className="w-3.5 h-3.5 fill-current" />
                    <span className="hidden sm:inline">Continue</span>
                  </button>
                </div>

                {/* See Image Button & Popover */}
                <div className="relative">
                  <button
                    onClick={() => canSee && setSeeOpen((o) => !o)}
                    disabled={!canSee}
                    aria-label={seeing ? 'Generating scene image' : 'Generate scene image'}
                    title={canSee ? 'Generate an image of the current scene' : 'See is available after the AI writes a response'}
                    className="min-h-11 flex items-center gap-1.5 rounded-xl border border-subtle bg-surface px-3 py-2 text-xs text-secondary hover:text-primary hover:bg-elevated disabled:cursor-not-allowed disabled:opacity-40 transition"
                  >
                    <ImageIcon className="w-3.5 h-3.5 text-accent-pri" />
                    <span className="hidden sm:inline">{seeing ? 'Painting…' : 'See'}</span>
                  </button>

                  {seeOpen && canSee && (
                    <div className="absolute right-0 top-full z-40 mt-1 w-[min(18rem,calc(100vw-2rem))] rounded-2xl border border-subtle bg-surface p-3 shadow-2xl animate-in slide-in-from-top-2">
                      <label className="mb-1 block text-xs font-semibold uppercase tracking-wider text-muted">
                        Visual Detail (optional)
                      </label>
                      <textarea
                        value={seePrompt}
                        onChange={(e) => setSeePrompt(e.target.value)}
                        rows={2}
                        autoFocus
                        placeholder='e.g. "at sunset, stormy rain slicked granite"'
                        className="w-full resize-y rounded-xl border border-subtle bg-surface-input px-3 py-2 text-xs text-primary outline-none focus:border-accent-pri transition placeholder:text-muted"
                      />
                      <div className="mt-2 flex justify-end gap-2">
                        <button
                          onClick={() => setSeeOpen(false)}
                          className="rounded-lg border border-subtle px-3 py-1.5 text-xs text-secondary hover:bg-elevated transition"
                        >
                          Cancel
                        </button>
                        <button
                          onClick={doSee}
                          className="rounded-lg bg-accent-pri px-3 py-1.5 text-xs font-semibold text-white hover:bg-accent-hov shadow transition"
                        >
                          Paint Scene
                        </button>
                      </div>
                    </div>
                  )}
                </div>

                {/* Reader Typography Controls Popover */}
                <button
                  onClick={() => setShowReaderModal(true)}
                  title="Reader typography & theme preferences"
                  className="min-h-11 flex items-center justify-center rounded-xl border border-subtle bg-surface p-2 text-secondary hover:text-primary hover:bg-elevated transition"
                >
                  <BookOpen className="w-4 h-4 text-accent-pri" />
                </button>

                {/* Global Settings Link */}
                <Link
                  to="/settings"
                  title="Global Settings (LLM, Image, Voice, Memory)"
                  className="min-h-11 flex items-center justify-center rounded-xl border border-subtle bg-surface p-2 text-secondary hover:text-primary hover:bg-elevated transition"
                >
                  <SettingsIcon className="w-4 h-4 text-secondary" />
                </Link>

                {/* Desktop Inspector Sidebar Toggle */}
                <button
                  onClick={() => setIsInspectorDocked(!isInspectorDocked)}
                  title={isInspectorDocked ? 'Hide side inspector dock' : 'Open docked side inspector'}
                  className="hidden lg:flex min-h-11 items-center justify-center rounded-xl border border-subtle bg-surface p-2 text-secondary hover:text-primary hover:bg-elevated transition"
                >
                  {isInspectorDocked ? (
                    <PanelRightClose className="w-4 h-4 text-accent-pri" />
                  ) : (
                    <PanelRight className="w-4 h-4 text-secondary" />
                  )}
                </button>
              </>
            )}
          </div>
        </header>

        {/* Scene Backdrop */}
        <SceneBackdrop
          adventureId={id}
          storyText={latestStoryText}
          generating={generating}
          playerAction={lastPlayer}
        />

        {/* Story Prose Feed */}
        <div className="relative min-h-0 flex-1 bg-canvas">
          <main
            ref={feedRef}
            onScroll={onFeedScroll}
            className="h-full overflow-y-auto overscroll-y-contain px-4 py-6"
          >
            <div
              className={`mx-auto flex flex-col gap-4 pb-14 sm:pb-4 transition-all duration-150 ${colWidthCls} ${fontFamilyCls}`}
              style={{
                fontSize: `${fontSizeValue}px`,
                lineHeight: lineHeightValue,
              }}
            >
              {actions.length === 0 && !generating && (
                <div className="rounded-2xl border border-dashed border-subtle p-8 text-center text-xs text-muted">
                  <p className="font-semibold text-primary mb-1">A fresh adventure begins.</p>
                  <p>Send a <b>Story</b> action below to set the initial scene, or press <b>▶ Continue</b> to let the AI narrate the opening.</p>
                </div>
              )}

              {actions.map((a) => (
                <FeedItem
                  key={a.id}
                  action={a}
                  onEdit={setEditing}
                  disabled={generating}
                  isLatest={a.id === lastAiTextId}
                  adventureId={id}
                />
              ))}

              {/* Streaming Cursor Block */}
              {generating && (
                <div className="rounded-xl border border-accent-pri/40 bg-elevated/80 p-4 shadow-sm animate-in fade-in">
                  <div className="mb-2 flex items-center gap-2 text-xs font-semibold uppercase tracking-wider text-accent-pri">
                    <span className="inline-block h-2 w-2 animate-ping rounded-full bg-accent-pri" />
                    AI is storytelling…
                  </div>
                  <p
                    className={`streaming-cursor whitespace-pre-wrap text-primary ${fontFamilyCls}`}
                    style={{
                      fontSize: `${fontSizeValue}px`,
                      lineHeight: lineHeightValue,
                    }}
                  >
                    {loosenProse(gen.text)}
                  </p>
                </div>
              )}

              {/* See image generation placeholder */}
              {seeing && (
                <div className="rounded-xl border border-subtle bg-elevated/60 p-4 animate-in fade-in">
                  <div className="mb-2 flex items-center gap-2 text-xs font-semibold uppercase tracking-wider text-accent-pri">
                    <span className="inline-block h-2 w-2 animate-pulse rounded-full bg-accent-pri" />
                    Generating scene painting… (can take 20–60 seconds)
                  </div>
                  <div className="flex h-48 items-center justify-center rounded-xl border border-dashed border-subtle text-xs text-muted">
                    Painting the scene...
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

        {/* Suggestion Choices Chips */}
        {showChoices && (
          <div className="border-t border-subtle bg-surface/95 px-4 pt-2.5 backdrop-blur z-10">
            <div className={`mx-auto flex items-center gap-2 overflow-x-auto pb-2 ${colWidthCls}`}>
              <span className="shrink-0 text-[10px] font-semibold uppercase tracking-wider text-muted">
                Suggested:
              </span>
              {choices.map((c, i) => (
                <button
                  key={i}
                  onClick={() => pickChoice(c)}
                  title={`Send action as "${c.type}"`}
                  className="min-h-8 shrink-0 flex items-center gap-1.5 rounded-full border border-subtle bg-elevated px-3 py-1 text-xs text-secondary hover:border-accent-pri hover:text-primary transition"
                >
                  <span className="text-[10px] font-bold uppercase text-accent-pri">{c.type}</span>
                  <span className="truncate max-w-[14rem]">{c.text}</span>
                </button>
              ))}
            </div>
          </div>
        )}

        {webSources.length > 0 && (
          <div className="border-t border-subtle bg-surface/95 px-4 pt-2.5 backdrop-blur z-10">
            <div className={`mx-auto flex items-center gap-2 overflow-x-auto pb-2 ${colWidthCls}`}>
              <span className="shrink-0 text-[10px] font-semibold uppercase tracking-wider text-muted">
                🌐 Sources:
              </span>
              {webSources.map((s, i) => {
                const safe = typeof s.url === 'string' && /^https?:\/\//i.test(s.url)
                const chip = 'min-h-8 shrink-0 truncate max-w-[14rem] rounded-full border border-subtle bg-elevated px-3 py-1 text-xs'
                return safe ? (
                  <a
                    key={s.url || i}
                    href={s.url}
                    target="_blank"
                    rel="noreferrer"
                    className={`${chip} text-secondary hover:border-accent-pri hover:text-primary transition`}
                  >
                    {s.title}
                  </a>
                ) : (
                  <span key={s.url || i} className={`${chip} text-muted`}>
                    {s.title}
                  </span>
                )
              })}
              <button
                type="button"
                onClick={() => setWebSources([])}
                title="Dismiss sources"
                aria-label="Dismiss sources"
                className="min-h-8 shrink-0 rounded-full border border-subtle bg-elevated px-2.5 py-1 text-xs text-muted hover:text-primary transition"
              >
                ✕
              </button>
            </div>
          </div>
        )}

        {/* Mobile / Floating GamePanel */}
        {panelOpen && !isInspectorDocked && (
          <GamePanel
            profile={uiProfile}
            state={uiState}
            disabled={generating}
            regenerating={regeneratingProfile}
            onStateChange={handleUiStateChange}
            onSend={sendAction}
            onInsert={insertToInput}
            onRegenerate={regenerateProfile}
            onClose={() => setPanelOpen(false)}
          />
        )}

        {/* Fixed Bottom Input Bar */}
        <InputBar
          disabled={generating}
          onSend={sendAction}
          onTextChange={(t) => setInputDirty(t.trim().length > 0)}
          tabs={uiProfile?.tabs}
          inject={inject}
          panelAvailable={(uiProfile?.widgets ?? []).length > 0}
          panelOpen={panelOpen}
          onTogglePanel={() => {
            if (isInspectorDocked) {
              setInspectorTab('controls')
            } else {
              setPanelOpen((o) => !o)
            }
          }}
        />
      </div>

      {/* RIGHT COLUMN: Docked Inspector (Desktop >= 1024px) */}
      {isInspectorDocked && (
        <div className="hidden lg:flex w-[380px] xl:w-[420px] shrink-0 h-full overflow-hidden border-l border-subtle bg-surface">
          <InspectorPane
            adventure={adventure}
            onAdventureChange={setAdventure}
            storyText={latestStoryText}
            tab={inspectorTab}
            setTab={setInspectorTab}
            onClose={() => setIsInspectorDocked(false)}
            isDocked={true}
            controlsComponent={controlsContent}
          />
        </div>
      )}

      {/* Mobile Slide-Over Drawer (< 1024px or requested) */}
      <Drawer
        open={drawerOpen}
        onClose={() => {
          setDrawerOpen(false)
          setDockRefresh((k) => k + 1)
        }}
        adventure={adventure}
        onAdventureChange={setAdventure}
        storyText={latestStoryText}
        tab={inspectorTab}
        setTab={setInspectorTab}
        controlsComponent={controlsContent}
      />

      {/* Action Edit Modal */}
      {editing && (
        <EditActionModal
          action={editing}
          onClose={() => setEditing(null)}
          onSave={saveEdit}
        />
      )}

      {/* Reader Typography Modal */}
      <ReaderPreferencesModal
        isOpen={showReaderModal}
        onClose={() => setShowReaderModal(false)}
      />
    </div>
  )
}

function EditActionModal({ action, onClose, onSave }) {
  const [text, setText] = useState(
    action.type === 'do' || action.type === 'say'
      ? playerDisplayText(action.type, action.text)
      : action.text,
  )
  const [saving, setSaving] = useState(false)

  const label =
    { do: 'You do', say: 'You say', story: 'Story', start: 'Opening', continue: 'AI response' }[
      action.type
    ] ?? action.type

  return (
    <Modal title={`Edit — ${label}`} onClose={onClose} wide>
      <div className="space-y-4">
        <textarea
          value={text}
          onChange={(e) => setText(e.target.value)}
          rows={10}
          autoFocus
          className="w-full resize-y rounded-xl border border-subtle bg-surface px-3 py-2 text-xs text-primary font-serif outline-none focus:border-accent-pri transition leading-relaxed"
        />
        <div className="flex justify-end gap-2 border-t border-subtle pt-3">
          <button
            onClick={onClose}
            className="rounded-xl border border-subtle px-4 py-2 text-xs text-secondary hover:bg-elevated transition"
          >
            Cancel
          </button>
          <button
            onClick={async () => {
              setSaving(true)
              await onSave(action, text)
              setSaving(false)
            }}
            disabled={saving}
            className="rounded-xl bg-accent-pri px-4 py-2 text-xs font-semibold text-white hover:bg-accent-hov shadow transition disabled:opacity-50"
          >
            {saving ? 'Saving…' : 'Save Changes'}
          </button>
        </div>
      </div>
    </Modal>
  )
}
