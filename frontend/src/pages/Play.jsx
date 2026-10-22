import { useCallback, useEffect, useRef, useState, Suspense, lazy } from 'react'
import { Link, useParams } from 'react-router-dom'
import { api, API_BASE } from '../api.js'
import { getToken } from '../auth.js'
import { streamGeneration } from '../sse.js'
import { useToast } from '../components/Toast.jsx'
import Modal from '../components/Modal.jsx'
import FeedItem from '../components/FeedItem.jsx'
import InputBar from '../components/InputBar.jsx'
import ShareModal from '../components/ShareModal.jsx'
import CharacterModal from '../components/CharacterModal.jsx'
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

function unwrapCharacter(res) {
  if (!res || typeof res !== 'object') return null
  if ('card' in res) return res.card || null
  if (res.id || res.title || res.sheet) return res
  return null
}

function isCharacterRequired(err, meta) {
  const code = meta?.code || err?.code || ''
  const status = meta?.status ?? err?.status
  if (code === 'character_required' || status === 428) return true
  const msg = typeof err === 'string' ? err : (err?.message || '')
  return /character_required/i.test(msg) || /\(428\)/.test(msg)
}

function isPlayerAction(action) {
  return action?.type === 'do' || action?.type === 'say' || action?.type === 'story'
}

function lastPlayerAction(list) {
  if (!Array.isArray(list)) return null
  for (let i = list.length - 1; i >= 0; i -= 1) {
    if (isPlayerAction(list[i])) return list[i]
  }
  return null
}

function newestSince(list, baselineId, localOnly, me) {
  if (!Array.isArray(list)) return null
  const mine = (me || '').trim()
  for (let i = list.length - 1; i >= 0; i -= 1) {
    const action = list[i]
    if (!isPlayerAction(action) || action.id == null) continue
    if (baselineId != null && String(action.id) === String(baselineId)) return null
    if (localOnly) {
      const actor = (action.actor || '').trim()
      if (actor && mine && actor !== mine) continue
    }
    return action
  }
  return null
}

function blockerText(item) {
  if (typeof item === 'string') return item.trim()
  if (typeof item === 'number' && Number.isFinite(item)) return String(item)
  if (item && typeof item === 'object') {
    const text = item.text ?? item.reason ?? item.message ?? ''
    return String(text).trim()
  }
  return ''
}

function normalizeActionCheck(data) {
  if (!data || typeof data !== 'object') return null
  const raw = data.action_check && typeof data.action_check === 'object'
    ? data.action_check
    : (typeof data.possible === 'boolean' || Array.isArray(data.blockers) ? data : null)
  if (!raw) return null
  const blockers = Array.isArray(raw.blockers) ? raw.blockers.map(blockerText).filter(Boolean) : []
  return {
    possible: raw.possible === true,
    blockers,
    risks: Array.isArray(raw.risks) ? raw.risks : [],
    notes: typeof raw.notes === 'string' ? raw.notes : '',
  }
}

function changeLabel(change) {
  const who = change?.who || ''
  const what = change?.what || ''
  return `${who} ${what} ${change?.from}→${change?.to}`.trim()
}

const INTENT_LABELS = {
  attack: 'Ataque',
  skill: 'Perícia',
  save: 'Salvaguarda',
  use_item: 'Uso de item',
  buy: 'Compra',
  take: 'Pegar',
  rest: 'Descanso',
  none: 'Nenhuma',
}

const FLAG_LABELS = {
  impersonation: 'escreveu pelo jogador',
  mechanics_leak: 'vazou a rolagem',
  language: 'respondeu em inglês',
}

function RulesNote({ rules, flags }) {
  const steps = Array.isArray(rules?.steps) ? rules.steps : []
  const changes = Array.isArray(rules?.changes) ? rules.changes : []
  const reactions = Array.isArray(rules?.time?.reactions)
    ? rules.time.reactions.filter((line) => typeof line === 'string' && line.trim())
    : []
  const kind = rules?.intent?.kind
  const quality = (Array.isArray(flags) ? flags : (rules?.quality_flags || []))
    .map((flag) => FLAG_LABELS[flag] || flag)
    .filter(Boolean)
  const showKind = Boolean(kind && kind !== 'none')
  if (
    steps.length === 0 && changes.length === 0 && reactions.length === 0
    && !showKind && quality.length === 0
  ) return null
  return (
    <div className="mt-1 flex max-w-full flex-col gap-1 px-4 text-[11px] leading-snug">
      <p className="font-semibold uppercase tracking-wide text-muted">Resolução do sistema</p>
      {showKind && (
        <p className="text-[11px] text-muted">{INTENT_LABELS[kind] || kind}</p>
      )}
      {steps.map((step, i) => (
        <p key={i} className="break-words text-info">
          {step?.label ? <span className="font-semibold">{step.label}. </span> : null}
          {step?.text || ''}
        </p>
      ))}
      {changes.length > 0 && (
        <p className="break-words text-secondary">{changes.map(changeLabel).join('; ')}</p>
      )}
      {reactions.length > 0 && (
        <p className="break-words text-warn-200">
          <span className="font-semibold">Consequências: </span>
          {reactions.join(' ')}
        </p>
      )}
      {quality.length > 0 && (
        <p className="break-words text-warn-200">O narrador desviou da regra: {quality.join(', ')}</p>
      )}
    </div>
  )
}

function ActionCheckNote({ check }) {
  if (!check) return null
  if (check.possible) {
    return (
      <p className="mt-1 px-4 text-[11px] leading-snug text-accent-pri/90">
        Ação verificada: possível
      </p>
    )
  }
  const blockers = Array.isArray(check.blockers) ? check.blockers : []
  const shown = blockers.slice(0, 3)
  const rest = blockers.length - shown.length
  return (
    <div className="mt-1 max-w-full px-4 text-[11px] leading-snug text-warn-200/95">
      {shown.length === 0 ? (
        <p>Ação verificada: não é possível</p>
      ) : (
        <ul className="list-disc pl-4">
          {shown.map((line, i) => (
            <li key={i} className="break-words">{line}</li>
          ))}
          {rest > 0 && <li>…</li>}
        </ul>
      )}
    </div>
  )
}

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
  const [isWide, setIsWide] = useState(() => {
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
  const [session, setSession] = useState(null)
  const [shareOpen, setShareOpen] = useState(false)
  const [live, setLive] = useState('')
  const [characterOpen, setCharacterOpen] = useState(false)
  const [characterCard, setCharacterCard] = useState(null)
  const [characterLoading, setCharacterLoading] = useState(false)
  const [needsCharacter, setNeedsCharacter] = useState(false)
  const [actionChecks, setActionChecks] = useState({})
  const [rulesChips, setRulesChips] = useState({})
  const [simVersion, setSimVersion] = useState(0)
  const [pendingCheck, setPendingCheck] = useState(null)

  const abortRef = useRef(null)
  const seenIdsRef = useRef(new Set())
  const readyRef = useRef(false)
  const genRef = useRef(null)
  const openCharacterRef = useRef(() => {})
  const currentUserRef = useRef('')
  const seeTokenRef = useRef(0)
  const feedRef = useRef(null)
  const nearBottomRef = useRef(true)
  const newBlockIdRef = useRef(null)
  const initialScrollDone = useRef(false)
  const actionsRef = useRef(null)
  const actionChecksRef = useRef({})
  const awaitingCheckRef = useRef(false)
  const awaitingBaselineRef = useRef(null)
  const livePlayerIdsRef = useRef(new Set())
  const sentTextRef = useRef('')
  const generating = gen !== null
  const currentUser = session?.user || ''
  genRef.current = gen
  currentUserRef.current = currentUser
  actionsRef.current = actions
  actionChecksRef.current = actionChecks
  const turnBlocked = !!(
    adventure?.turn_mode && currentUser !== (adventure?.turn_user || '')
  )

  // Width only. Hiding the dock is the user's choice and must survive a resize.
  useEffect(() => {
    const handleResize = () => {
      setIsWide(window.innerWidth >= 1024)
    }
    window.addEventListener('resize', handleResize)
    return () => window.removeEventListener('resize', handleResize)
  }, [])

  // Load adventure state
  const load = useCallback(async () => {
    setError(null)
    readyRef.current = false
    try {
      const st = await api.getAdventure(id)
      const list = [...(st.actions ?? [])].sort(bySeq)
      seenIdsRef.current = new Set(list.map((a) => a.id).filter((x) => x != null))
      livePlayerIdsRef.current = new Set()
      awaitingCheckRef.current = false
      awaitingBaselineRef.current = null
      setActionChecks({})
      setPendingCheck(null)
      setAdventure(st)
      setActions(list)
      setUiProfile(st.ui_profile ?? null)
      setUiState(st.ui_state ?? {})
      setLive('')
      readyRef.current = true
    } catch (e) {
      setError(e)
      setAdventure(null)
      setActions([])
      seenIdsRef.current = new Set()
      readyRef.current = false
    }
  }, [id])

  // Mesmo GET do load, sem derrubar o stream local nem os checks.
  const refreshAdventure = useCallback((opts) => {
    const replaceActions = !opts || opts.replaceActions !== false
    const forceActions = !!opts?.forceActions
    return api.getAdventure(id).then((st) => {
      setAdventure((prev) => (prev ? { ...prev, ...st } : st))
      if (!replaceActions) return st
      if (genRef.current && !forceActions) return st
      const list = [...(st.actions ?? [])].sort(bySeq)
      seenIdsRef.current = new Set(list.map((a) => a.id).filter((x) => x != null))
      setActions(list)
      return st
    }).catch(() => null)
  }, [id])

  useEffect(() => {
    load()
  }, [load])

  // Veredito da checagem de viabilidade. Vem pelo bus (outro jogador) ou pelo
  // stream da própria ação (evento "action_check", antes dos tokens).
  const handleCheckData = useCallback((data) => {
    const check = normalizeActionCheck(data)
    if (!check || !data || typeof data !== 'object') return
    const raw = data.action_check && typeof data.action_check === 'object' ? data.action_check : data
    const actionId = data.action_id ?? data.player_action_id ?? raw.action_id ?? raw.player_action_id ?? null
    const seq = data.seq ?? data.action_seq ?? raw.seq ?? raw.action_seq
    const list = actionsRef.current ?? []
    const bind = (id) => {
      if (id == null) return false
      awaitingCheckRef.current = false
      setPendingCheck(null)
      setActionChecks((prev) => ({ ...prev, [String(id)]: check }))
      return true
    }
    if (actionId != null) {
      bind(actionId)
      return
    }
    if (seq != null && seq !== '') {
      const found = list.find((a) => isPlayerAction(a) && String(a.seq) === String(seq))
      if (found?.id != null) {
        bind(found.id)
        return
      }
      setPendingCheck({ check, baselineId: null, seq, awaiting: awaitingCheckRef.current })
      return
    }
    if (awaitingCheckRef.current) {
      const own = newestSince(list, awaitingBaselineRef.current, true, currentUserRef.current)
      if (own && !actionChecksRef.current[String(own.id)]) {
        bind(own.id)
        return
      }
      setPendingCheck({
        check,
        baselineId: awaitingBaselineRef.current,
        seq: null,
        awaiting: true,
        local: true,
      })
      return
    }
    const last = lastPlayerAction(list)
    const already = last?.id != null && !!actionChecksRef.current[String(last.id)]
    if (last?.id != null && livePlayerIdsRef.current.has(last.id) && !already) {
      bind(last.id)
      return
    }
    setPendingCheck({
      check,
      baselineId: last?.id ?? null,
      seq: null,
      awaiting: true,
      local: false,
    })
  }, [])

  useEffect(() => {
    let cancelled = false
    api.checkAuth()
      .then((s) => { if (!cancelled) setSession(s) })
      .catch(() => { if (!cancelled) setSession(null) })
    return () => { cancelled = true }
  }, [])

  // Barramento SSE (turno, ação de outro jogador, tokens e narração final).
  // Sem close no erro: o EventSource reconecta sozinho e reenvia Last-Event-ID.
  useEffect(() => {
    const token = getToken()
    if (!id || !token) return undefined
    const es = new EventSource(
      `${API_BASE}/api/adventures/${id}/events?token=${encodeURIComponent(token)}`,
    )
    const parse = (ev) => {
      try {
        return JSON.parse(ev.data || 'null')
      } catch {
        return null
      }
    }
    const onTurn = (ev) => {
      const data = parse(ev)
      if (typeof data?.turn_user !== 'string') return
      setAdventure((prev) => (prev ? { ...prev, turn_user: data.turn_user } : prev))
    }
    const onPlayer = (ev) => {
      if (!readyRef.current) return
      const data = parse(ev)
      if (!data || typeof data !== 'object' || data.id == null) return
      const actor = typeof data.actor === 'string' ? data.actor : ''
      if (actor === currentUserRef.current) return
      if (seenIdsRef.current.has(data.id)) return
      seenIdsRef.current.add(data.id)
      if (isPlayerAction(data)) livePlayerIdsRef.current.add(data.id)
      setLive('')
      setActions((prev) => {
        const list = prev ?? []
        if (list.some((a) => a.id === data.id)) return list
        return [...list, data].sort(bySeq)
      })
    }
    const onToken = (ev) => {
      if (!readyRef.current || genRef.current) return
      const data = parse(ev)
      const tok = data && typeof data === 'object' && typeof data.token === 'string'
        ? data.token
        : ''
      if (!tok) return
      setLive((prev) => prev + tok)
    }
    const onAction = (ev) => {
      if (!readyRef.current) return
      const data = parse(ev)
      setLive('')
      if (genRef.current) return
      if (!data || typeof data !== 'object' || data.id == null) return
      if (seenIdsRef.current.has(data.id)) return
      seenIdsRef.current.add(data.id)
      if (isPlayerAction(data)) livePlayerIdsRef.current.add(data.id)
      setActions((prev) => {
        const list = prev ?? []
        if (list.some((a) => a.id === data.id)) return list
        if (data.seq != null) {
          const idx = list.findIndex((a) => a.seq === data.seq)
          if (idx >= 0) {
            const next = list.slice()
            next[idx] = data
            return next.sort(bySeq)
          }
        }
        return [...list, data].sort(bySeq)
      })
    }
    const onCheck = (ev) => {
      const data = parse(ev)
      handleCheckData(data)
    }
    const fan = (type, data) => {
      window.dispatchEvent(new CustomEvent('aidungeon-live', {
        detail: { type, adventureId: id, data },
      }))
    }
    const onTurnDone = () => {
      setLive('')
      if (!genRef.current) refreshAdventure()
    }
    const onChoices = (ev) => {
      const data = parse(ev)
      setChoices(Array.isArray(data?.choices) ? data.choices : [])
    }
    const onChoicesFailed = () => {
      setChoices([])
    }
    const onPortrait = (ev) => {
      fan('portrait_ready', parse(ev))
    }
    const onBackdrop = (ev) => {
      fan('backdrop_ready', parse(ev))
    }
    const onSee = (ev) => {
      const data = parse(ev)
      setSeeing(false)
      const action = data?.image?.action
      if (!action || typeof action !== 'object' || action.id == null) {
        if (!genRef.current) refreshAdventure()
        return
      }
      if (seenIdsRef.current.has(action.id)) return
      seenIdsRef.current.add(action.id)
      setActions((prev) => {
        const list = prev ?? []
        if (list.some((a) => a.id === action.id)) return list
        return [...list, action].sort(bySeq)
      })
    }
    const onCardAssets = (ev) => {
      fan('card_assets_ready', parse(ev))
    }
    const onCard = (ev) => {
      fan('card_changed', parse(ev))
      if (!genRef.current) refreshAdventure()
    }
    const onCharacter = (ev) => {
      const data = parse(ev)
      if (!genRef.current) refreshAdventure()
      const username = typeof data?.username === 'string' ? data.username : ''
      if (!username || username !== currentUserRef.current) return
      api.getCharacter(id).then((res) => {
        const card = unwrapCharacter(res)
        setCharacterCard(card)
        if (card) setNeedsCharacter(false)
      }).catch(() => {})
    }
    const onAdventureUpdated = () => {
      refreshAdventure({ replaceActions: false })
    }
    const onUndo = () => {
      setChoices([])
      setWebSources([])
      setLive('')
      setPendingCheck(null)
      awaitingCheckRef.current = false
      refreshAdventure({ forceActions: true })
    }
    es.addEventListener('turn_changed', onTurn)
    es.addEventListener('player_action', onPlayer)
    es.addEventListener('token', onToken)
    es.addEventListener('action', onAction)
    es.addEventListener('action_check', onCheck)
    es.addEventListener('turn_done', onTurnDone)
    es.addEventListener('choices_ready', onChoices)
    es.addEventListener('choices_failed', onChoicesFailed)
    es.addEventListener('portrait_ready', onPortrait)
    es.addEventListener('backdrop_ready', onBackdrop)
    es.addEventListener('see_ready', onSee)
    es.addEventListener('card_assets_ready', onCardAssets)
    es.addEventListener('card_changed', onCard)
    es.addEventListener('character_changed', onCharacter)
    es.addEventListener('adventure_updated', onAdventureUpdated)
    const onSimChanged = () => {
      setSimVersion((v) => v + 1)
      if (!genRef.current) refreshAdventure({ replaceActions: false })
    }
    const onRulesRoll = (ev) => {
      const data = parse(ev)
      if (!data || data.action_id == null) return
      setRulesChips((prev) => ({ ...prev, [String(data.action_id)]: data }))
      setSimVersion((v) => v + 1)
    }
    es.addEventListener('undo', onUndo)
    es.addEventListener('sim_changed', onSimChanged)
    es.addEventListener('rules_roll', onRulesRoll)
    return () => {
      es.removeEventListener('turn_changed', onTurn)
      es.removeEventListener('player_action', onPlayer)
      es.removeEventListener('token', onToken)
      es.removeEventListener('action', onAction)
      es.removeEventListener('action_check', onCheck)
      es.removeEventListener('turn_done', onTurnDone)
      es.removeEventListener('choices_ready', onChoices)
      es.removeEventListener('choices_failed', onChoicesFailed)
      es.removeEventListener('portrait_ready', onPortrait)
      es.removeEventListener('backdrop_ready', onBackdrop)
      es.removeEventListener('see_ready', onSee)
      es.removeEventListener('card_assets_ready', onCardAssets)
      es.removeEventListener('card_changed', onCard)
      es.removeEventListener('character_changed', onCharacter)
      es.removeEventListener('adventure_updated', onAdventureUpdated)
      es.removeEventListener('undo', onUndo)
      es.removeEventListener('sim_changed', onSimChanged)
      es.removeEventListener('rules_roll', onRulesRoll)
      es.close()
    }
  }, [id, handleCheckData, refreshAdventure])

  useEffect(() => {
    if (!pendingCheck || !Array.isArray(actions) || actions.length === 0) return
    let target = null
    if (pendingCheck.seq != null) {
      target = actions.find((a) => isPlayerAction(a) && String(a.seq) === String(pendingCheck.seq)) || null
    } else {
      target = newestSince(actions, pendingCheck.baselineId, !!pendingCheck.local, currentUserRef.current)
      if (!target?.id || !livePlayerIdsRef.current.has(target.id)) return
    }
    if (!target?.id) return
    const key = String(target.id)
    setActionChecks((prev) => (prev[key] ? prev : { ...prev, [key]: pendingCheck.check }))
    setPendingCheck(null)
    awaitingCheckRef.current = false
  }, [actions, pendingCheck])

  const openCharacter = useCallback(async () => {
    setCharacterOpen(true)
    setCharacterLoading(true)
    try {
      const res = await api.getCharacter(id)
      setCharacterCard(unwrapCharacter(res))
    } catch (e) {
      setCharacterCard(null)
      toast(e.message)
    } finally {
      setCharacterLoading(false)
    }
  }, [id, toast])
  openCharacterRef.current = openCharacter

  const promptCharacter = useCallback(() => {
    setNeedsCharacter(true)
    toast('Crie seu personagem para jogar')
    openCharacterRef.current()
  }, [toast])

  const applySharing = (dict) => {
    if (!dict || typeof dict !== 'object') return
    setAdventure((prev) => {
      if (!prev) return prev
      return {
        ...prev,
        ...(dict.owner != null ? { owner: dict.owner } : {}),
        ...('turn_mode' in dict ? { turn_mode: !!dict.turn_mode } : {}),
        ...(dict.turn_user != null ? { turn_user: dict.turn_user } : {}),
        ...(Array.isArray(dict.shares) ? { shares: dict.shares } : {}),
      }
    })
  }

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
  }, [actions, gen?.text, seeing, live])

  // SSE Stream runner
  const runStream = useCallback(
    (kind, path, body, applyDone) => {
      if (generating || needsCharacter) return
      const ctrl = new AbortController()
      abortRef.current = ctrl
      const nextGen = { kind, text: '' }
      genRef.current = nextGen
      setGen(nextGen)
      setLive('')
      setChoices([])
      setWebSources([])
      const fail = (msg, meta) => {
        genRef.current = null
        setGen(null)
        if (isCharacterRequired(msg, meta)) {
          promptCharacter()
          return
        }
        const text = typeof msg === 'string' && msg ? msg : (msg?.message || 'A geração falhou')
        if (meta?.code === 'rules_refused') {
          toast(text)
          return
        }
        toast(text)
      }
      streamGeneration(path, {
        body,
        signal: ctrl.signal,
        onToken: (tok) => setGen((g) => (g ? { ...g, text: g.text + tok } : g)),
        onEvent: (name, data) => {
          if (name === 'action_check') handleCheckData(data)
          if (name === 'rules_roll' && data?.action_id != null) {
            setRulesChips((prev) => ({ ...prev, [String(data.action_id)]: data }))
            setSimVersion((v) => v + 1)
          }
          if (name === 'refused') {
            const actionId = data?.action_id
            if (actionId != null) {
              setActions((prev) => (prev ?? []).filter((a) => a.id !== actionId))
              seenIdsRef.current.delete(actionId)
              livePlayerIdsRef.current.delete(actionId)
            }
            const sent = sentTextRef.current
            if (sent) setInject({ text: sent, nonce: Date.now() })
          }
        },
        onDone: (data) => {
          newBlockIdRef.current = data.player_action?.id ?? data.action?.id ?? null
          setActions((prev) => {
            const merged = applyDone(prev ?? [], data)
            const seen = new Set()
            const out = []
            for (const action of merged) {
              if (action?.id != null) {
                if (seen.has(action.id)) continue
                seen.add(action.id)
              }
              out.push(action)
            }
            for (const actionId of seen) seenIdsRef.current.add(actionId)
            const prevIds = new Set((prev ?? []).map((a) => a.id))
            for (const action of out) {
              if (action?.id != null && !prevIds.has(action.id) && isPlayerAction(action)) {
                livePlayerIdsRef.current.add(action.id)
              }
            }
            return out.sort(bySeq)
          })
          const baked = normalizeActionCheck(data)
          const bakedId = data?.player_action?.id
          if (baked && bakedId != null) {
            awaitingCheckRef.current = false
            setPendingCheck(null)
            setActionChecks((prev) => ({ ...prev, [String(bakedId)]: baked }))
          }
          genRef.current = null
          setGen(null)
          setLive('')
          if (Array.isArray(data.web_sources) && data.web_sources.length) {
            setWebSources(data.web_sources)
          }
          refreshAdventure({ replaceActions: false })
        },
        onError: (msg, meta) => fail(msg, meta),
      }).catch((e) => {
        if (e?.name === 'AbortError') return
        fail(e)
      })
    },
    [generating, needsCharacter, toast, refreshAdventure, id, promptCharacter, handleCheckData],
  )

  const sendAction = (type, text, opts) => {
    if (needsCharacter) {
      openCharacter()
      return
    }
    if (turnBlocked) return
    if (type === 'do' || type === 'say' || type === 'story') {
      awaitingCheckRef.current = true
      awaitingBaselineRef.current = lastPlayerAction(actionsRef.current)?.id ?? null
      sentTextRef.current = text
    }
    return runStream('action', `/adventures/${id}/action`, {
      type,
      text,
      ...(opts?.webSearch ? { web_search: true } : {}),
    }, (prev, data) => {
      const out = [...prev]
      if (data.player_action) out.push(data.player_action)
      if (data.action) out.push(data.action)
      return out
    })
  }

  const doContinue = () => {
    if (needsCharacter) {
      openCharacter()
      return
    }
    if (turnBlocked) return
    return runStream('continue', `/adventures/${id}/continue`, null, (prev, data) =>
      data.action ? [...prev, data.action] : prev,
    )
  }

  const doRetry = () => {
    if (needsCharacter) {
      openCharacter()
      return
    }
    if (turnBlocked) return
    return runStream('retry', `/adventures/${id}/retry`, null, (prev, data) => {
      if (!data.action) return prev
      return prev.map((a) => (a.seq === data.action.seq ? data.action : a))
    })
  }

  const stop = () => {
    abortRef.current?.abort()
    genRef.current = null
    setGen(null)
  }

  const doUndo = async () => {
    try {
      const res = await api.undo(id)
      const removedIds = new Set((res.removed ?? []).map((a) => a.id))
      const removedKeys = new Set([...removedIds].map((item) => String(item)))
      setActions((prev) => (prev ?? []).filter((a) => !removedIds.has(a.id)))
      setActionChecks((prev) => {
        const next = {}
        for (const [key, value] of Object.entries(prev)) {
          if (!removedKeys.has(key)) next[key] = value
        }
        return next
      })
      setPendingCheck(null)
      awaitingCheckRef.current = false
      for (const removedId of removedIds) livePlayerIdsRef.current.delete(removedId)
      setChoices([])
      setWebSources([])
      toast('Último turno desfeito.', 'ok')
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
            if (action) {
              setActions((prev) => {
                const list = prev ?? []
                if (action.id != null && list.some((a) => a.id === action.id)) return list
                return [...list, action].sort(bySeq)
              })
            }
            toast('Imagem gerada.', 'ok')
            return
          }
          if (job.status === 'error' || job.status === 'canceled') {
            toast(job.error || 'Não foi possível pintar a cena.')
            return
          }
          await wait(800)
        }
        toast('A pintura ainda está em andamento.')
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
        toast('Imagem gerada.', 'ok')
        setSeeing(false)
        return
      }
      if (!r?.job_id) {
        setSeeing(false)
        toast('Não foi possível iniciar a pintura.')
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
    if (generating || turnBlocked) return
    if (!['do', 'say', 'story'].includes(choice.type) || !choice.text?.trim()) return
    sendAction(choice.type, choice.text.trim())
  }

  const saveEdit = async (action, text) => {
    try {
      const updated = await api.patchAction(action.id, text)
      setActions((prev) => prev.map((a) => (a.id === updated.id ? updated : a)))
      setEditing(null)
      toast('Ação editada.', 'ok')
    } catch (e) {
      toast(e.message)
    }
  }

  if (error) {
    return (
      <div className="flex h-full flex-col items-center justify-center gap-4 p-8 text-center bg-canvas text-primary">
        <p className="text-danger-300">
          {error.status === 0
            ? 'Não foi possível falar com o servidor (http://127.0.0.1:8000). Ele está no ar?'
            : error.message}
        </p>
        <div className="flex gap-2">
          <button
            onClick={load}
            className="flex items-center gap-1.5 rounded-xl bg-accent-pri px-4 py-2 text-xs font-semibold text-white hover:bg-accent-hov transition"
          >
            <RotateCcw className="w-3.5 h-3.5" />
            <span>Tentar de novo</span>
          </button>
          <Link
            to="/"
            className="flex items-center gap-1.5 rounded-xl border border-subtle px-4 py-2 text-xs text-secondary hover:bg-elevated transition"
          >
            <ArrowLeft className="w-3.5 h-3.5" />
            <span>Voltar ao início</span>
          </Link>
        </div>
      </div>
    )
  }

  if (!adventure || actions === null) {
    return <div className="flex h-full items-center justify-center text-xs text-muted bg-canvas">Carregando a aventura…</div>
  }

  const lastAction = actions[actions.length - 1]
  const canShare = !!(session?.admin || (adventure.owner && currentUser === adventure.owner))
  const myTurn = !!(adventure.turn_mode && currentUser && currentUser === (adventure.turn_user || ''))
  const canRetry = !generating && !turnBlocked && !needsCharacter && lastAction?.type === 'continue'
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
    serif: 'font-prose',
    sans: 'font-sans',
    mono: 'font-mono',
  }[readerPrefs?.fontFamily || 'serif']

  const lineHeightValue = {
    normal: 1.5,
    relaxed: 1.75,
    loose: 2.0,
  }[readerPrefs?.lineHeight || 'relaxed']

  const fontSizeValue = readerPrefs?.fontSize || 17

  const shareButton = canShare ? (
    <button
      type="button"
      onClick={() => setShareOpen(true)}
      aria-label="Compartilhar aventura"
      title="Compartilhar aventura"
      className="min-h-11 flex items-center justify-center rounded-xl border border-subtle bg-surface px-3 py-2 text-sm text-secondary hover:text-primary hover:bg-elevated transition"
    >
      👥
    </button>
  ) : null

  const packOn = !!(adventure?.sim_state?.pack)
  const controlsContent = uiProfile ? (
    <GamePanel
      profile={uiProfile}
      state={uiState}
      disabled={generating || turnBlocked || needsCharacter}
      regenerating={regeneratingProfile}
      onStateChange={handleUiStateChange}
      onSend={sendAction}
      onInsert={insertToInput}
      onRegenerate={regenerateProfile}
      inline={true}
      adventureId={id}
      packOn={packOn}
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
              aria-label="Abrir o inspetor"
              title="Fichas, NPCs, memória, controles"
            >
              <Menu className="w-5 h-5" />
            </button>

            <Link
              to="/"
              title="Voltar ao início"
              className="flex h-10 w-10 items-center justify-center rounded-full border border-subtle bg-surface text-secondary hover:text-primary hover:bg-elevated transition"
            >
              <ArrowLeft className="w-4 h-4" />
            </Link>

            <div className="min-w-0 flex-1 pl-1">
              <h1 className="truncate text-xs sm:text-sm font-bold text-primary">
                {adventure.title}
              </h1>
            </div>

            <button
              type="button"
              onClick={openCharacter}
              aria-label="Personagem"
              title="Seu personagem"
              className="flex h-10 w-10 shrink-0 items-center justify-center rounded-full border border-subtle bg-surface text-base text-secondary hover:text-primary hover:bg-elevated transition"
            >
              🧙
            </button>
          </div>

          {/* Action Toolbar */}
          <div className="flex items-center gap-1.5 flex-wrap sm:flex-nowrap justify-end">
            {generating ? (
              <>
                {shareButton}
                <button
                  onClick={stop}
                  className="flex h-10 items-center gap-1.5 rounded-full bg-danger-800 px-4 text-xs font-semibold text-white hover:bg-danger-700 shadow transition"
                  title="Parar a geração"
                >
                  <Square className="w-3.5 h-3.5 fill-current" />
                  <span>Parar</span>
                </button>
              </>
            ) : (
              <>
                <div className="flex items-center gap-1">
                  <button
                    onClick={doRetry}
                    disabled={!canRetry}
                    aria-label="Tentar a última resposta de novo"
                    title={needsCharacter ? 'Crie seu personagem para jogar' : (turnBlocked ? 'Aguarde a sua vez' : (canRetry ? 'Gerar de novo a última resposta' : 'Disponível depois de uma resposta da IA'))}
                    className="flex h-10 w-10 shrink-0 items-center justify-center rounded-full border border-subtle bg-surface text-secondary hover:text-primary hover:bg-elevated disabled:cursor-not-allowed disabled:opacity-40 transition"
                  >
                    <RotateCcw className="w-4 h-4" />
                  </button>

                  <button
                    onClick={doUndo}
                    disabled={!canUndo}
                    aria-label="Desfazer o último turno"
                    title="Desfaz a última ação sua e a resposta da IA"
                    className="flex h-10 w-10 shrink-0 items-center justify-center rounded-full border border-subtle bg-surface text-secondary hover:text-primary hover:bg-elevated disabled:cursor-not-allowed disabled:opacity-40 transition"
                  >
                    <Undo2 className="w-4 h-4" />
                  </button>

                  <button
                    onClick={doContinue}
                    disabled={generating || turnBlocked || needsCharacter}
                    aria-label="Continuar a narração"
                    title={needsCharacter ? 'Crie seu personagem para jogar' : (turnBlocked ? 'Aguarde a sua vez' : 'A IA escreve o próximo parágrafo')}
                    className="flex h-10 items-center gap-1.5 rounded-full bg-accent-pri px-4 text-xs font-semibold text-white hover:bg-accent-hov shadow transition disabled:opacity-40"
                  >
                    <PlayIcon className="w-3.5 h-3.5 fill-current" />
                    <span className="hidden sm:inline">Continuar</span>
                  </button>
                </div>

                {shareButton}

                {/* See Image Button & Popover */}
                <div className="relative">
                  <button
                    onClick={() => canSee && setSeeOpen((o) => !o)}
                    disabled={!canSee}
                    aria-label={seeing ? 'Gerando a imagem da cena' : 'Gerar imagem da cena'}
                    title={canSee ? 'Gerar uma imagem da cena atual' : 'Disponível depois que a IA escreve uma resposta'}
                    className="flex h-10 w-10 shrink-0 items-center justify-center rounded-full border border-subtle bg-surface text-secondary hover:text-primary hover:bg-elevated disabled:cursor-not-allowed disabled:opacity-40 transition"
                  >
                    <ImageIcon className="w-4 h-4 text-accent-pri" />
                  </button>

                  {seeOpen && canSee && (
                    <div className="absolute right-0 top-full z-40 mt-1 w-[min(18rem,calc(100vw-2rem))] rounded-2xl border border-subtle bg-surface p-3 shadow-2xl animate-slide-in-top">
                      <label className="mb-1 block text-xs font-semibold uppercase tracking-wider text-muted">
                        Detalhe visual (opcional)
                      </label>
                      <textarea
                        value={seePrompt}
                        onChange={(e) => setSeePrompt(e.target.value)}
                        rows={2}
                        autoFocus
                        placeholder='ex.: "ao pôr do sol, chuva na pedra"'
                        className="w-full resize-y rounded-xl border border-subtle bg-surface-input px-3 py-2 text-xs text-primary outline-none focus:border-accent-pri transition placeholder:text-muted"
                      />
                      <div className="mt-2 flex justify-end gap-2">
                        <button
                          onClick={() => setSeeOpen(false)}
                          className="rounded-lg border border-subtle px-3 py-1.5 text-xs text-secondary hover:bg-elevated transition"
                        >
                          Cancelar
                        </button>
                        <button
                          onClick={doSee}
                          className="rounded-lg bg-accent-pri px-3 py-1.5 text-xs font-semibold text-white hover:bg-accent-hov shadow transition"
                        >
                          Pintar a cena
                        </button>
                      </div>
                    </div>
                  )}
                </div>

                {/* Reader Typography Controls Popover */}
                <button
                  onClick={() => setShowReaderModal(true)}
                  title="Tipografia e tema da leitura"
                  className="flex h-10 w-10 items-center justify-center rounded-full border border-subtle bg-surface text-secondary hover:text-primary hover:bg-elevated transition"
                >
                  <BookOpen className="w-4 h-4 text-accent-pri" />
                </button>

                {/* Global Settings Link */}
                <Link
                  to="/settings"
                  title="Configurações (modelo, imagem, voz, memória)"
                  className="flex h-10 w-10 items-center justify-center rounded-full border border-subtle bg-surface text-secondary hover:text-primary hover:bg-elevated transition"
                >
                  <SettingsIcon className="w-4 h-4 text-secondary" />
                </Link>

                {/* Desktop Inspector Sidebar Toggle */}
                <button
                  onClick={() => setIsInspectorDocked(!isInspectorDocked)}
                  title={isInspectorDocked ? 'Esconder o inspetor' : 'Mostrar o inspetor ao lado'}
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

        {adventure.turn_mode && (
          <div
            className={`shrink-0 border-b px-4 py-2 text-center text-sm font-semibold ${
              myTurn
                ? 'border-accent-pri/40 bg-accent-hov/15 text-accent-pri'
                : 'border-accent-pri/40 bg-accent-pri/15 text-accent-pri'
            }`}
            role="status"
          >
            {myTurn ? '🎲 Sua vez' : `🎲 Vez de ${adventure.turn_user || '…'}`}
          </div>
        )}

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
                  <p className="font-semibold text-primary mb-1">Uma aventura nova começa.</p>
                  <p>Envie uma ação de <b>História</b> abaixo para abrir a cena, ou pressione <b>▶ Continuar</b> para a IA narrar o começo.</p>
                </div>
              )}

              {actions.map((a) => {
                const check = a?.id != null ? actionChecks[String(a.id)] : null
                const rules = a?.id != null
                  ? (rulesChips[String(a.id)] || a.rules || null)
                  : null
                return (
                  <div key={a.id} className="min-w-0">
                    <FeedItem
                      action={a}
                      onEdit={setEditing}
                      disabled={generating}
                      isLatest={a.id === lastAiTextId}
                      adventureId={id}
                    />
                    {isPlayerAction(a) && check && <ActionCheckNote check={check} />}
                    {isPlayerAction(a) && (rules || (a.quality_flags || []).length > 0) && (
                      <RulesNote rules={rules} flags={a.quality_flags} />
                    )}
                  </div>
                )
              })}

              {/* Streaming Cursor Block */}
              {generating && (
                <div className="animate-fade-in border-l-2 border-accent-pri/70 pl-4">
                  <div className="mb-1.5 flex items-center gap-2 text-[10px] font-semibold uppercase tracking-[0.18em] text-accent-pri">
                    <span className="inline-block h-1.5 w-1.5 animate-pulse rounded-full bg-accent-pri" />
                    Narrando
                  </div>
                  <p className="streaming-cursor whitespace-pre-wrap font-prose text-primary">
                    {loosenProse(gen.text)}
                  </p>
                </div>
              )}

              {!generating && live && (
                <div className="animate-fade-in border-l-2 border-accent-pri/70 pl-4">
                  <div className="mb-1.5 flex items-center gap-2 text-[10px] font-semibold uppercase tracking-[0.18em] text-accent-pri">
                    <span className="inline-block h-1.5 w-1.5 animate-pulse rounded-full bg-accent-pri" />
                    Narrando
                  </div>
                  <p className="streaming-cursor whitespace-pre-wrap font-prose text-primary">
                    {loosenProse(live)}
                  </p>
                </div>
              )}

              {/* See image generation placeholder */}
              {seeing && (
                <div className="rounded-xl border border-subtle bg-elevated/60 p-4 animate-fade-in">
                  <div className="mb-2 flex items-center gap-2 text-xs font-semibold uppercase tracking-wider text-accent-pri">
                    <span className="inline-block h-2 w-2 animate-pulse rounded-full bg-accent-pri" />
                    Gerando a pintura da cena… (pode levar de 20 a 60 segundos)
                  </div>
                  <div className="flex h-48 items-center justify-center rounded-xl border border-dashed border-subtle text-xs text-muted">
                    Pintando a cena...
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
                  disabled={turnBlocked}
                  title={turnBlocked ? 'Aguarde a sua vez' : `Send action as "${c.type}"`}
                  className="min-h-8 shrink-0 flex items-center gap-1.5 rounded-full border border-subtle bg-elevated px-3 py-1 text-xs text-secondary hover:border-accent-pri hover:text-primary transition disabled:cursor-not-allowed disabled:opacity-40"
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
                title="Dispensar fontes"
                aria-label="Dispensar fontes"
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
            disabled={generating || turnBlocked || needsCharacter}
            regenerating={regeneratingProfile}
            onStateChange={handleUiStateChange}
            onSend={sendAction}
            onInsert={insertToInput}
            onRegenerate={regenerateProfile}
            onClose={() => setPanelOpen(false)}
            adventureId={id}
            packOn={packOn}
          />
        )}

        {needsCharacter && (
          <div className="border-t border-warn-600/40 bg-warn-950/50 px-4 py-2 text-center text-sm text-warn-200">
            Crie seu personagem para jogar.
            <button
              type="button"
              onClick={openCharacter}
              className="ml-2 underline decoration-warn-400/70 hover:text-warn-50"
            >
              Abrir ficha
            </button>
          </div>
        )}

        {/* Fixed Bottom Input Bar */}
        <InputBar
          disabled={generating}
          disabledTurn={turnBlocked || needsCharacter}
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
      {isInspectorDocked && isWide && (
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
            simVersion={simVersion}
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
        simVersion={simVersion}
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

      {shareOpen && (
        <ShareModal
          adventureId={id}
          owner={adventure.owner || ''}
          onClose={() => setShareOpen(false)}
          onChanged={applySharing}
        />
      )}

      {characterOpen && (
        <CharacterModal
          adventureId={id}
          rules={adventure.character_rules || ''}
          card={characterCard}
          loading={characterLoading}
          onClose={() => setCharacterOpen(false)}
          onSaved={(card) => {
            setCharacterCard(card)
            setNeedsCharacter(false)
            setCharacterOpen(false)
            toast('Personagem salvo.', 'ok')
          }}
        />
      )}
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

  const actorName = (action.actor || '').trim()
  const label = actorName
    ? (action.type === 'say' ? `${actorName} diz` : actorName)
    : ({ do: 'Você age', say: 'Você diz', story: 'Narração', start: 'Abertura', continue: 'Resposta da IA' }[
        action.type
      ] ?? action.type)

  return (
    <Modal title={`Editar — ${label}`} onClose={onClose} wide>
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
            Cancelar
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
            {saving ? 'Salvando…' : 'Salvar'}
          </button>
        </div>
      </div>
    </Modal>
  )
}
