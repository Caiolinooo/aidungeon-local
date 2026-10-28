import { memo, useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState, Suspense, lazy } from 'react'
import { Link, useParams } from 'react-router-dom'
import { api, API_BASE } from '../api.js'
import { getToken } from '../auth.js'
import { streamGeneration } from '../sse.js'
import { useToast } from '../components/Toast.jsx'
import Modal from '../components/Modal.jsx'
import FeedItem from '../components/FeedItem.jsx'
import TurnBeat from '../components/TurnBeat.jsx'
import {
  adjudicationMode,
  beatsFromActions,
  mergeBeat,
  presentAdjudication,
  spotlightIds,
  storeBeat,
  visualHints,
} from '../components/adjudicationView.js'
import InputBar from '../components/InputBar.jsx'
import ShareModal from '../components/ShareModal.jsx'
import PublicationModal from '../components/PublicationModal.jsx'
import ModsModal from '../components/ModsModal.jsx'
import DmTray from '../components/DmTray.jsx'
import CharacterModal from '../components/CharacterModal.jsx'
import GamePanel from '../components/GamePanel.jsx'
import Drawer, { InspectorPane } from '../components/Drawer.jsx'
import SceneBackdrop from '../components/SceneBackdrop.jsx'
import './Play.css'
import AdventureJobs from '../components/AdventureJobs.jsx'
import GenerationPreferences from '../components/GenerationPreferences.jsx'
import LiveFeed from '../components/LiveFeed.jsx'
import MiniHud from '../components/MiniHud.jsx'
import WorldMap from '../components/WorldMap.jsx'
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

// Dock de retratos 2D, carregado sob demanda.
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


function lastMatchingAction(list, predicate) {
  if (!Array.isArray(list)) return null
  for (let i = list.length - 1; i >= 0; i -= 1) {
    if (predicate(list[i])) return list[i]
  }
  return null
}
function reconcileActions(current, incoming, baseline, removeMissing = false) {
  const indices = new Map()
  const incomingIndices = new Map()
  const baselineIndices = new Map()
  const idKey = (action) => action.id == null ? null : `id:${typeof action.id}:${action.id}`
  const seqKey = (action) => action.seq == null ? null : `seq:${typeof action.seq}:${action.seq}`
  const register = (map, action, index) => {
    const byId = idKey(action)
    const bySequence = seqKey(action)
    if (byId) map.set(byId, index)
    if (bySequence) map.set(bySequence, index)
  }
  const find = (map, action) => {
    const byId = idKey(action)
    if (byId && map.has(byId)) return map.get(byId)
    const bySequence = seqKey(action)
    return bySequence && map.has(bySequence) ? map.get(bySequence) : -1
  }
  incoming.forEach((action, index) => register(incomingIndices, action, index))
  baseline.forEach((action, index) => register(baselineIndices, action, index))
  const out = current.filter((action) => {
    if (!removeMissing) return true
    const baselineIndex = find(baselineIndices, action)
    if (baselineIndex < 0 || action !== baseline[baselineIndex]) return true
    return find(incomingIndices, action) >= 0
  })
  out.forEach((action, index) => register(indices, action, index))
  for (const action of incoming) {
    const index = find(indices, action)
    if (index < 0) {
      register(indices, action, out.length)
      out.push(action)
      continue
    }
    const baselineIndex = find(baselineIndices, out[index])
    // Só o snapshot substitui uma ação que não mudou desde o início do GET.
    if (baselineIndex >= 0 && out[index] === baseline[baselineIndex]) {
      out[index] = action
      register(indices, action, index)
    }
  }
  return out.sort(bySeq)
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
  compliance: 'obedeceu no lugar de recusar',
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
  const line = check.possible
    ? (check.consequence || '').trim()
    : ((Array.isArray(check.blockers) ? check.blockers[0] : '') || '').trim()
  if (!line) return null
  return (
    <p className={`mt-1 px-4 text-[11px] leading-snug ${check.possible ? 'text-secondary' : 'text-warn-200/95'}`}>
      {line}
    </p>
  )
}

const FEED_PAGE_SIZE = 100

const StoryFeedItem = memo(function StoryFeedItem({
  action, check, rules, beat, onEdit, disabled, isLatest, adventureId,
}) {
  return (
    <div className="min-w-0">
      {beat ? <TurnBeat beat={beat} /> : null}
      <FeedItem
        action={action}
        onEdit={onEdit}
        disabled={disabled}
        isLatest={isLatest}
        adventureId={adventureId}
      />
      {isPlayerAction(action) && check && <ActionCheckNote check={check} />}
      {isPlayerAction(action) && (rules || (action.quality_flags || []).length > 0) && (
        <RulesNote rules={rules} flags={action.quality_flags} />
      )}
    </div>
  )
})

export default function Play() {
  const { id } = useParams()
  const toast = useToast()
  const { readerPrefs, theme } = useTheme()

  const [adventure, setAdventure] = useState(null)
  const [actions, setActions] = useState(null)
  const [visibleCount, setVisibleCount] = useState(FEED_PAGE_SIZE)
  const [playerCard, setPlayerCard] = useState(null)
  const [sceneInfo, setSceneInfo] = useState(null)
  const [error, setError] = useState(null)
  const [gen, setGen] = useState(null) // { kind: 'action'|'continue'|'retry', text }
  const [drawerOpen, setDrawerOpen] = useState(false)
  const [editing, setEditing] = useState(null)
  const [seeing, setSeeing] = useState(false)
  const [seeOpen, setSeeOpen] = useState(false)
  const [seePrompt, setSeePrompt] = useState('')
  const [seeVariation, setSeeVariation] = useState(0)
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
  const [publishOpen, setPublishOpen] = useState(false)
  const [modsOpen, setModsOpen] = useState(false)
  const [live, setLive] = useState('')
  const [characterOpen, setCharacterOpen] = useState(false)
  const [characterCard, setCharacterCard] = useState(null)
  const [characterLoading, setCharacterLoading] = useState(false)
  const [needsCharacter, setNeedsCharacter] = useState(false)
  const [intents, setIntents] = useState([])
  const [actionChecks, setActionChecks] = useState({})
  const [rulesChips, setRulesChips] = useState({})
  const [liveBeat, setLiveBeat] = useState(null)
  const [beatsBySeq, setBeatsBySeq] = useState({})
  const [simVersion, setSimVersion] = useState(0)
  const [pendingCheck, setPendingCheck] = useState(null)
  const [mapOpen, setMapOpen] = useState(false)

  const abortRef = useRef(null)
  const seenIdsRef = useRef(new Set())
  const readyRef = useRef(false)
  const genRef = useRef(null)
  const liveBeatRef = useRef(null)
  const streamMetaRef = useRef({ type: '' })
  liveBeatRef.current = liveBeat
  const openCharacterRef = useRef(() => {})
  const currentUserRef = useRef('')
  const isDmRef = useRef(false)
  const seeJobRef = useRef(null)
  const feedRef = useRef(null)
  const nearBottomRef = useRef(true)
  const newBlockIdRef = useRef(null)
  const initialScrollDone = useRef(false)
  const pendingPrependRef = useRef(null)
  const adventureRequestRef = useRef(0)
  const actionsRef = useRef(null)
  const generationPrefsRef = useRef({})
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
  generationPrefsRef.current = adventure?.ui_state?._generation || {}
  actionChecksRef.current = actionChecks
  const feedMounted = !!adventure && actions !== null
  const visibleStart = Math.max(0, (actions?.length ?? 0) - visibleCount)
  const visibleActions = useMemo(
    () => (actions ?? []).slice(visibleStart),
    [actions, visibleStart],
  )
  const isDm = !!(
    session?.admin
    || !adventure?.owner
    || currentUser === adventure.owner
  )
  const dmMode = !!adventure?.dm_mode
  const queuesIntent = !!(dmMode && currentUser && !isDm)
  isDmRef.current = isDm
  const turnBlocked = !!(
    adventure?.turn_mode
    && !(dmMode && isDm)
    && currentUser !== (adventure?.turn_user || '')
  )
  const actionLocked = queuesIntent ? false : turnBlocked

  useEffect(() => {
    if (!id || !dmMode) {
      setIntents([])
      return undefined
    }
    let cancelled = false
    api.listIntents(id, 'pending')
      .then((res) => {
        if (!cancelled) setIntents(Array.isArray(res?.intents) ? res.intents : [])
      })
      .catch(() => {
        if (!cancelled) setIntents([])
      })
    return () => { cancelled = true }
  }, [id, dmMode, currentUser])

  // Width only. Hiding the dock is the user's choice and must survive a resize.
  useEffect(() => {
    const handleResize = () => {
      setIsWide(window.innerWidth >= 1024)
    }
    window.addEventListener('resize', handleResize)
    return () => window.removeEventListener('resize', handleResize)
  }, [])

  const refreshPlayer = useCallback(() => {
    api.getCharacter(id)
      .then((res) => setPlayerCard(unwrapCharacter(res)))
      .catch(() => {})
  }, [id])

  // Load adventure state
  const load = useCallback(async () => {
    const requestId = ++adventureRequestRef.current
    setError(null)
    readyRef.current = false
    try {
      const st = await api.getAdventure(id)
      if (requestId !== adventureRequestRef.current) return
      const list = [...(st.actions ?? [])].sort(bySeq)
      seenIdsRef.current = new Set(list.map((a) => a.id).filter((x) => x != null))
      livePlayerIdsRef.current = new Set()
      awaitingCheckRef.current = false
      awaitingBaselineRef.current = null
      setActionChecks({})
      setPendingCheck(null)
      setAdventure(st)
      setActions(list)
      setBeatsBySeq(beatsFromActions(list))
      liveBeatRef.current = null
      setLiveBeat(null)
      setUiProfile(st.ui_profile ?? null)
      setUiState(st.ui_state ?? {})
      setLive('')
      readyRef.current = true
    } catch (e) {
      if (requestId !== adventureRequestRef.current) return
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
    const requestId = ++adventureRequestRef.current
    const baseline = actionsRef.current ?? []
    return api.getAdventure(id).then((st) => {
      if (requestId !== adventureRequestRef.current) return null
      setAdventure((prev) => (prev ? { ...prev, ...st } : st))
      if (!replaceActions) return st
      if (genRef.current && !forceActions) return st
      const list = [...(st.actions ?? [])].sort(bySeq)
      const restored = beatsFromActions(list)
      setBeatsBySeq((prev) => Object.entries(restored).reduce(
        (next, [seq, beat]) => storeBeat(next, seq, beat), prev,
      ))
      setActions((prev) => {
        const next = reconcileActions(prev ?? [], list, baseline, forceActions)
        seenIdsRef.current = new Set(next.map((a) => a.id).filter((x) => x != null))
        return next
      })
      return st
    }).catch(() => null)
  }, [id])

  useEffect(() => {
    load()
  }, [load])

  useEffect(() => {
    refreshPlayer()
  }, [refreshPlayer])
  useEffect(() => {
    seeJobRef.current = null
    setSeeing(false)
    setSeeVariation(0)
  }, [id])

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
      fan('player_action', data)
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
      fan('action', data)
      setLive('')
      if (genRef.current) return
      if (!data || typeof data !== 'object' || data.id == null) return
      if (seenIdsRef.current.has(data.id)) return
      seenIdsRef.current.add(data.id)
      if (isPlayerAction(data)) livePlayerIdsRef.current.add(data.id)
      if (data.type === 'continue' && data.seq != null && liveBeatRef.current) {
        const pending = liveBeatRef.current
        setBeatsBySeq((prev) => storeBeat(prev, data.seq, pending))
        liveBeatRef.current = null
        setLiveBeat(null)
      }
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
    const onAdjudication = (ev) => {
      if (genRef.current) return
      const view = presentAdjudication(parse(ev), { includeDeltas: false })
      if (!view) return
      const next = mergeBeat(liveBeatRef.current, view)
      liveBeatRef.current = next
      setLiveBeat(next)
      fan('adjudication', view)
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
    const onJobChanged = (ev) => {
      fan('job_changed', parse(ev))
      fan('job_progress', parse(ev))
    }
    const onBackdrop = (ev) => {
      fan('backdrop_ready', parse(ev))
    }
    const onClip = (ev) => {
      fan('clip_ready', parse(ev))
    }
    const onSee = (ev) => {
      const data = parse(ev)
      if (!data?.job_id || !seeJobRef.current || String(data.job_id) === seeJobRef.current) {
        seeJobRef.current = null
        setSeeing(false)
      }
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
    const onUiChanged = (ev) => {
      const data = parse(ev)
      if (data?.ui_state && typeof data.ui_state === 'object') {
        setUiState(data.ui_state)
      }
      refreshPlayer()
    }
    const onUndo = () => {
      setChoices([])
      setWebSources([])
      setLive('')
      setPendingCheck(null)
      awaitingCheckRef.current = false
      refreshAdventure({ forceActions: true }).then((st) => {
        if (!st) return
        setUiProfile(st.ui_profile ?? null)
        setUiState(st.ui_state ?? {})
      })
      refreshPlayer()
    }
    const rememberIntent = (data) => {
      if (!data?.id) return
      const mine = isDmRef.current || data.username === currentUserRef.current
      if (!mine) return
      setIntents((prev) => (prev.some((item) => item.id === data.id) ? prev : [...prev, data]))
    }
    const onIntentPending = (ev) => rememberIntent(parse(ev))
    const onIntentClosed = (ev) => {
      const data = parse(ev)
      if (!data?.id) return
      setIntents((prev) => prev.filter((item) => item.id !== data.id))
    }
    es.addEventListener('intent_pending', onIntentPending)
    es.addEventListener('intent_approved', onIntentClosed)
    es.addEventListener('intent_rejected', onIntentClosed)
    es.addEventListener('turn_changed', onTurn)
    es.addEventListener('player_action', onPlayer)
    es.addEventListener('token', onToken)
    es.addEventListener('action', onAction)
    es.addEventListener('action_check', onCheck)
    es.addEventListener('adjudication', onAdjudication)
    es.addEventListener('turn_done', onTurnDone)
    es.addEventListener('choices_ready', onChoices)
    es.addEventListener('choices_failed', onChoicesFailed)
    es.addEventListener('portrait_ready', onPortrait)
    es.addEventListener('backdrop_ready', onBackdrop)
    es.addEventListener('clip_ready', onClip)
    es.addEventListener('see_ready', onSee)
    es.addEventListener('card_assets_ready', onCardAssets)
    es.addEventListener('job_progress', onJobChanged)
    es.addEventListener('job_queued', onJobChanged)
    es.addEventListener('card_changed', onCard)
    es.addEventListener('character_changed', onCharacter)
    es.addEventListener('adventure_updated', onAdventureUpdated)
    es.addEventListener('ui_changed', onUiChanged)
    const onLifeTick = (ev) => {
      fan('life_tick_completed', parse(ev))
      setSimVersion((v) => v + 1)
    }
    const onResetRequired = (ev) => {
      fan('reset_required', parse(ev))
      refreshAdventure({ forceActions: true })
    }
    es.addEventListener('life_tick_completed', onLifeTick)
    es.addEventListener('reset_required', onResetRequired)
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
      es.removeEventListener('adjudication', onAdjudication)
      es.removeEventListener('turn_done', onTurnDone)
      es.removeEventListener('choices_ready', onChoices)
      es.removeEventListener('choices_failed', onChoicesFailed)
      es.removeEventListener('portrait_ready', onPortrait)
      es.removeEventListener('backdrop_ready', onBackdrop)
      es.removeEventListener('clip_ready', onClip)
      es.removeEventListener('see_ready', onSee)
      es.removeEventListener('job_progress', onJobChanged)
      es.removeEventListener('job_queued', onJobChanged)
      es.removeEventListener('card_assets_ready', onCardAssets)
      es.removeEventListener('card_changed', onCard)
      es.removeEventListener('character_changed', onCharacter)
      es.removeEventListener('adventure_updated', onAdventureUpdated)
      es.removeEventListener('ui_changed', onUiChanged)
      es.removeEventListener('undo', onUndo)
      es.removeEventListener('life_tick_completed', onLifeTick)
      es.removeEventListener('reset_required', onResetRequired)
      es.removeEventListener('sim_changed', onSimChanged)
      es.removeEventListener('rules_roll', onRulesRoll)
      es.removeEventListener('intent_pending', onIntentPending)
      es.removeEventListener('intent_approved', onIntentClosed)
      es.removeEventListener('intent_rejected', onIntentClosed)
      es.close()
    }
  }, [id, handleCheckData, refreshAdventure, refreshPlayer])

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
        ...('dm_mode' in dict ? { dm_mode: !!dict.dm_mode } : {}),
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
  useEffect(() => {
    initialScrollDone.current = false
    newBlockIdRef.current = null
    pendingPrependRef.current = null
    nearBottomRef.current = true
    setVisibleCount(FEED_PAGE_SIZE)
  }, [id])

  const onFeedScroll = useCallback(() => {
    const el = feedRef.current
    if (!el) return
    nearBottomRef.current = el.scrollHeight - el.scrollTop - el.clientHeight < 120
  }, [])

  const loadEarlier = useCallback(() => {
    const el = feedRef.current
    if (!el) return
    pendingPrependRef.current = { height: el.scrollHeight, top: el.scrollTop }
    nearBottomRef.current = false
    const total = actionsRef.current?.length ?? 0
    setVisibleCount((count) => Math.min(total, count + FEED_PAGE_SIZE))
  }, [])

  useLayoutEffect(() => {
    const el = feedRef.current
    if (!el || actions === null) return
    const prepend = pendingPrependRef.current
    if (prepend) {
      pendingPrependRef.current = null
      el.scrollTop = Math.max(0, el.scrollHeight - prepend.height + prepend.top)
      return
    }
    if (!initialScrollDone.current) {
      initialScrollDone.current = true
      nearBottomRef.current = true
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
  }, [actions, gen?.text, seeing, live, visibleCount])

  useEffect(() => {
    const el = feedRef.current
    const content = el?.firstElementChild
    if (!el || !content || typeof ResizeObserver === 'undefined') return undefined
    const observer = new ResizeObserver(() => {
      if (initialScrollDone.current && nearBottomRef.current && !pendingPrependRef.current) {
        el.scrollTop = el.scrollHeight
      }
    })
    observer.observe(content)
    return () => observer.disconnect()
  }, [id, feedMounted])

  // SSE Stream runner
  const runStream = useCallback(
    (kind, path, body, applyDone, openStream) => {
      if (generating || needsCharacter) return
      const ctrl = new AbortController()
      abortRef.current = ctrl
      const nextGen = { kind, text: '' }
      genRef.current = nextGen
      setGen(nextGen)
      setLive('')
      setChoices([])
      setWebSources([])
      if (adjudicationMode(streamMetaRef.current.type) === 'ignore') {
        liveBeatRef.current = null
        setLiveBeat(null)
      }
      const fail = (msg, meta) => {
        if (abortRef.current !== ctrl) return
        genRef.current = null
        setGen(null)
        setLive('')
        if (meta?.code === 'llm_cancelled') return
        if (meta?.code === 'not_your_turn' || meta?.code === 'intent_not_pending') {
          awaitingCheckRef.current = false
          setPendingCheck(null)
        }
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
      const streamOptions = {
        signal: ctrl.signal,
        onToken: (tok) => {
          if (abortRef.current !== ctrl) return
          setGen((g) => (g ? { ...g, text: g.text + tok } : g))
        },
        onEvent: (name, data) => {
          if (abortRef.current !== ctrl) return
          if (name === 'action_check') handleCheckData(data)
          if (name === 'rules_roll' && data?.action_id != null) {
            setRulesChips((prev) => ({ ...prev, [String(data.action_id)]: data }))
            setSimVersion((v) => v + 1)
          }
          if (name === 'adjudication') {
            if (adjudicationMode(streamMetaRef.current.type) === 'ignore') return
            const view = presentAdjudication(data, { includeDeltas: false })
            if (!view) return
            setLiveBeat((prev) => {
              const next = mergeBeat(prev, view)
              liveBeatRef.current = next
              return next
            })
            window.dispatchEvent(new CustomEvent('aidungeon-live', {
              detail: { type: 'adjudication', adventureId: id, data: view },
            }))
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
          if (abortRef.current !== ctrl) return
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
          const metaType = streamMetaRef.current.type
          if (adjudicationMode(metaType) === 'apply' && data.action?.seq != null) {
            const committed = presentAdjudication(data.adjudication, { includeDeltas: true })
            const view = mergeBeat(liveBeatRef.current, committed)
            if (view) setBeatsBySeq((prev) => storeBeat(prev, data.action.seq, view))
          }
          liveBeatRef.current = null
          setLiveBeat(null)
          genRef.current = null
          setGen(null)
          setLive('')
          window.dispatchEvent(new CustomEvent('aidungeon-cards-sync', {
            detail: { adventureId: id },
          }))
          if (Array.isArray(data.web_sources) && data.web_sources.length) {
            setWebSources(data.web_sources)
          }
          if (kind !== 'retry' && data.action?.type === 'continue') {
            const preferences = generationPrefsRef.current
            if (preferences.auto_narrate) {
              window.setTimeout(() => window.dispatchEvent(new CustomEvent('aidl:auto-narrate', {
                detail: { actionId: data.action.id },
              })), 0)
            }
            if (preferences.auto_see) {
              window.setTimeout(() => requestSee({
                action_seq: data.action.seq, variation: 0,
              }).catch((e) => toast(e.message)), 0)
            }
          }
          refreshAdventure({ replaceActions: false })
        },
        onError: (msg, meta) => fail(msg, meta),
      }
      const pending = openStream
        ? openStream(streamOptions)
        : streamGeneration(path, { body, ...streamOptions })
      Promise.resolve(pending).catch((e) => {
        if (e?.name === 'AbortError') return
        fail(e)
      })
    },
    [generating, needsCharacter, toast, refreshAdventure, id, promptCharacter, handleCheckData],
  )

  const applyStreamedAction = (prev, data) => {
    const out = [...prev]
    if (data.player_action) out.push(data.player_action)
    if (data.action) out.push(data.action)
    return out
  }

  const sendAction = async (type, text, opts) => {
    if (needsCharacter) {
      openCharacter()
      return
    }
    if (queuesIntent) {
      try {
        const res = await api.postAction(id, {
          type,
          text,
          ...(opts?.webSearch ? { web_search: true } : {}),
        })
        const intent = res?.intent
        if (intent?.id) {
          setIntents((prev) => (
            prev.some((item) => item.id === intent.id) ? prev : [...prev, intent]
          ))
        }
        toast('Intenção enviada ao mestre')
      } catch (e) {
        if (isCharacterRequired(e, e)) {
          promptCharacter()
          return
        }
        toast(e.message || 'Não foi possível enviar a intenção')
      }
      return
    }
    if (turnBlocked) return
    streamMetaRef.current = { type }
    if (type === 'do' || type === 'say' || type === 'story') {
      awaitingCheckRef.current = true
      awaitingBaselineRef.current = lastPlayerAction(actionsRef.current)?.id ?? null
      sentTextRef.current = text
    }
    return runStream('action', `/adventures/${id}/action`, {
      type,
      text,
      ...(opts?.webSearch ? { web_search: true } : {}),
    }, applyStreamedAction)
  }

  const approveIntent = (intent) => {
    if (!intent?.id || generating) return
    streamMetaRef.current = { type: 'do' }
    return runStream(
      'action',
      `/adventures/${id}/intents/${encodeURIComponent(intent.id)}/approve`,
      null,
      applyStreamedAction,
    )
  }

  const rejectIntent = async (intent) => {
    if (!intent?.id) return
    try {
      await api.rejectIntent(id, intent.id)
      setIntents((prev) => prev.filter((item) => item.id !== intent.id))
    } catch (e) {
      toast(e.message || 'Não foi possível rejeitar')
    }
  }

  const doContinue = () => {
    if (needsCharacter) {
      openCharacter()
      return
    }
    if (turnBlocked) return
    streamMetaRef.current = { type: 'continue' }
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
    streamMetaRef.current = { type: 'retry' }
    setLiveBeat(null)
    liveBeatRef.current = null
    return runStream('retry', `/adventures/${id}/retry`, null, (prev, data) => {
      if (!data.action) return prev
      return prev.map((a) => (a.seq === data.action.seq ? data.action : a))
    })
  }

  const stop = async () => {
    try {
      await api.cancelGeneration(id)
    } catch (e) {
      if (e?.code !== 'no_generation_in_progress') toast(e.message)
    } finally {
      abortRef.current?.abort()
      genRef.current = null
      setGen(null)
      setLive('')
      refreshAdventure({ forceActions: true })
    }
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
      setLiveBeat(null)
      liveBeatRef.current = null
      setBeatsBySeq((prev) => dropBeats(prev, (res.removed ?? []).map((action) => action.seq)))
      window.dispatchEvent(new CustomEvent('aidungeon-cards-sync', {
        detail: { adventureId: id },
      }))
      toast('Último turno desfeito.', 'ok')
    } catch (e) {
      toast(e.message)
    }
  }

  const requestSee = useCallback(async ({ action_seq, extra_prompt, variation = 0 }) => {
    setSeeing(true)
    try {
      const r = await api.see(id, { action_seq, extra_prompt, variation })
      if (r?.action) {
        if (r.action.id != null) seenIdsRef.current.add(r.action.id)
        setActions((prev) => {
          const list = prev ?? []
          if (r.action.id != null && list.some((a) => a.id === r.action.id)) return list
          return [...list, r.action].sort(bySeq)
        })
        setSeeing(false)
        toast('Imagem gerada.', 'ok')
        return r
      }
      if (!r?.job_id) {
        setSeeing(false)
        toast('Não foi possível iniciar a pintura.')
        return r
      }
      seeJobRef.current = String(r.job_id)
      return r
    } catch (e) {
      seeJobRef.current = null
      setSeeing(false)
      throw e
    }
  }, [id, toast])

  const handleJobsChange = useCallback((jobs) => {
    const jobId = seeJobRef.current
    if (!jobId) return
    const job = jobs.find((item) => String(item.id) === jobId)
    if (!job || ['preparing', 'pending', 'running'].includes(job.status)) return
    seeJobRef.current = null
    setSeeing(false)
    if (job.status === 'done') {
      const action = job.result?.image?.action
      if (action) {
        if (action.id != null) seenIdsRef.current.add(action.id)
        setActions((prev) => {
          const list = prev ?? []
          if (action.id != null && list.some((a) => a.id === action.id)) return list
          return [...list, action].sort(bySeq)
        })
      }
      toast('Imagem gerada.', 'ok')
    } else if (job.status === 'canceled') {
      toast('Pintura cancelada.')
    } else {
      toast(job.error || 'Não foi possível pintar a cena.')
    }
  }, [toast])

  const retrySee = useCallback((job) => {
    const payload = job.payload || {}
    if (payload.action_seq == null) {
      throw new Error('Este job não tem os dados necessários para uma nova tentativa.')
    }
    return requestSee({
      action_seq: payload.action_seq,
      extra_prompt: payload.extra_prompt || undefined,
      variation: payload.variation ?? 0,
    })
  }, [requestSee])

  const doSee = async (variation = 0) => {
    const target = [...(actions || [])].reverse().find(
      (a) => a.type === 'continue' || a.type === 'start' || a.type === 'story',
    )
    if (!target || seeing || generating) return
    setSeeOpen(false)
    try {
      const extra = seePrompt.trim()
      const r = await requestSee({
        action_seq: target.seq,
        extra_prompt: extra || undefined,
        variation,
      })
      if (r?.action || r?.job_id) setSeePrompt('')
    } catch (e) {
      toast(e.message)
    }
  }

  const pickChoice = (choice) => {
    if (generating || actionLocked) return
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
            className="flex items-center gap-1.5 rounded-xl bg-accent-pri px-4 py-2 text-xs font-semibold text-canvas hover:bg-accent-hov transition"
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
  const lastAiTextId = lastMatchingAction(actions, (a) => a.type === 'continue')?.id
  const latestStoryText = lastMatchingAction(
    actions,
    (a) => a.type !== 'do' && a.type !== 'say' && a.type !== 'see',
  )?.text
  const showChoices = !generating && !inputDirty && choices.length > 0
  const lastPlayer = lastMatchingAction(actions, isPlayerAction)

  // Typography Preferences mapping
  const colWidthCls = {
    compact: 'max-w-[560px] 2xl:max-w-[640px]',
    standard: 'max-w-[720px] xl:max-w-[780px] 2xl:max-w-[880px]',
    wide: 'max-w-[880px] 2xl:max-w-[1040px]',
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

  const fontSizeValue = readerPrefs?.fontSize || 19
  const storyFontSize = `calc(${fontSizeValue}px * var(--ui-scale, 1))`

  const shareButton = canShare ? (
    <button
      type="button"
      onClick={() => setShareOpen(true)}
      aria-label="Compartilhar aventura"
      title="Compartilhar aventura"
      className="flex h-10 w-10 items-center justify-center rounded-full border border-subtle bg-surface text-sm text-secondary hover:text-primary hover:bg-elevated transition"
    >
      👥
    </button>
  ) : null

  const packOn = !!(adventure?.sim_state?.pack)
  const controlsContent = uiProfile ? (
    <GamePanel
      profile={uiProfile}
      state={uiState}
      disabled={generating || actionLocked || needsCharacter}
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
    <div className="play-shell flex h-full w-full min-w-0 max-w-full overflow-hidden bg-canvas text-primary">
      {/* LEFT COLUMN: Main Story Pane */}
      <div className="relative flex h-full min-h-0 min-w-0 max-w-full flex-1 flex-col overflow-hidden">
        {/* Header + Action Bar */}
        <header className="relative z-30 flex min-w-0 shrink-0 flex-col gap-2 border-b border-subtle/70 bg-canvas/95 px-3 py-2 pt-[max(0.5rem,env(safe-area-inset-top))] backdrop-blur sm:flex-row sm:flex-wrap sm:items-center sm:justify-between">
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
              <h1 className="display-serif truncate text-sm font-semibold tracking-tight text-primary sm:text-base">
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
          <div className="flex min-w-0 max-w-full flex-wrap items-center justify-end gap-1.5">
            <GenerationPreferences
              adventure={adventure}
              onSaved={(uiState) => setAdventure((current) => ({ ...current, ui_state: uiState }))}
            />
            <LiveFeed adventureId={id} />
            <AdventureJobs
              adventureId={id}
              onJobsChange={handleJobsChange}
              onRetrySee={retrySee}
              watching={seeing}
            />

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
                    className="flex h-10 items-center gap-1.5 rounded-full bg-accent-pri px-4 text-xs font-semibold text-canvas hover:bg-accent-hov shadow transition disabled:opacity-40"
                  >
                    <PlayIcon className="w-3.5 h-3.5 fill-current" />
                    <span className="hidden sm:inline">Continuar</span>
                  </button>
                </div>

                {shareButton}

                {canShare && (
                  <>
                    <button onClick={() => setPublishOpen(true)} className="h-10 rounded-full border border-subtle bg-surface px-3 text-xs font-medium text-secondary hover:bg-elevated">Publicar</button>
                    <button onClick={() => setModsOpen(true)} className="h-10 rounded-full border border-subtle bg-surface px-3 text-xs font-medium text-secondary hover:bg-elevated">Mods</button>
                  </>
                )}

                <button
                  onClick={() => {
                    if (!canSee) {
                      toast(seeing
                        ? 'A imagem ainda está sendo pintada.'
                        : 'Disponível depois que a IA escreve uma resposta')
                      return
                    }
                    setSeeOpen(true)
                  }}
                  disabled={seeing || generating}
                  aria-label={seeing ? 'Gerando a imagem da cena' : 'Gerar imagem da cena'}
                  title={canSee ? 'Gerar uma imagem da cena atual' : 'Disponível depois que a IA escreve uma resposta'}
                  className="flex h-10 w-10 shrink-0 items-center justify-center rounded-full border border-subtle bg-surface text-secondary hover:text-primary hover:bg-elevated disabled:cursor-not-allowed disabled:opacity-40 transition"
                >
                  <ImageIcon className="w-4 h-4 text-accent-pri" />
                </button>

                {/* Reader Typography Controls Popover */}
                <button
                  onClick={() => setShowReaderModal(true)}
                  title="Tipografia e tema da leitura"
                  className="flex h-10 w-10 items-center justify-center rounded-full border border-subtle bg-surface text-secondary hover:text-primary hover:bg-elevated transition"
                >
                  <BookOpen className="w-4 h-4 text-accent-pri" />
                </button>
                <button
                  onClick={() => setMapOpen(true)}
                  title="Mapa, locais e journal do mundo"
                  className="flex h-10 w-10 items-center justify-center rounded-full border border-subtle bg-surface text-secondary hover:text-primary hover:bg-elevated transition"
                >
                  <span className="text-xs font-semibold">Map</span>
                </button>
                <Link to={`/adventures/${id}/novel`} title="Abrir Visual Novel e replay"
                  className="flex h-10 items-center rounded-full border border-subtle bg-surface px-3 text-xs font-medium text-secondary hover:bg-elevated">Novel</Link>


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

        <MiniHud
          profile={uiProfile}
          state={uiState}
          playerCard={playerCard}
          location={sceneInfo?.location}
          onOpenCharacter={openCharacter}
        />

        {/* Scene Backdrop */}
        <SceneBackdrop
          adventureId={id}
          storyText={latestStoryText}
          generating={generating}
          playerAction={lastPlayer}
          onSceneChange={setSceneInfo}
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

        {/* Prosa no centro; retratos ocupam um trilho, sem cobrir o texto. */}
        <div className="play-story-pane flex min-h-0 min-w-0 max-w-full flex-1 flex-col bg-canvas sm:flex-row">
          <main
            ref={feedRef}
            onScroll={onFeedScroll}
            className="min-h-0 min-w-0 flex-1 overflow-x-hidden overflow-y-auto overscroll-y-contain px-4 py-6"
          >
            <div
              className={`mx-auto flex w-full min-w-0 flex-col gap-6 pb-14 sm:pb-4 transition-all duration-150 ${colWidthCls} ${fontFamilyCls}`}
              style={{
                fontSize: storyFontSize,
                lineHeight: lineHeightValue,
              }}
            >
              {actions.length === 0 && !generating && (
                <div className="rounded-2xl border border-dashed border-subtle p-8 text-center text-xs text-muted">
                  <p className="font-semibold text-primary mb-1">Uma aventura nova começa.</p>
                  <p>Envie uma ação de <b>História</b> abaixo para abrir a cena, ou pressione <b>▶ Continuar</b> para a IA narrar o começo.</p>
                </div>
              )}

              {visibleStart > 0 && (
                <button
                  type="button"
                  onClick={loadEarlier}
                  className="self-center rounded-full border border-subtle bg-surface px-4 py-2 text-xs font-semibold text-secondary hover:bg-elevated hover:text-primary transition"
                >
                  Carregar {Math.min(FEED_PAGE_SIZE, visibleStart)} ações anteriores
                </button>
              )}

              {visibleActions.map((a, index) => {
                const check = a?.id != null ? actionChecks[String(a.id)] : null
                const rules = a?.id != null
                  ? (rulesChips[String(a.id)] || a.rules || null)
                  : null
                return (
                  <StoryFeedItem
                    key={a.id ?? a.seq ?? `${visibleStart + index}`}
                    action={a}
                    check={check}
                    rules={rules}
                    beat={a?.type === 'continue' && a.seq != null ? beatsBySeq[String(a.seq)] : null}
                    onEdit={setEditing}
                    disabled={generating}
                    isLatest={a.id === lastAiTextId}
                    adventureId={id}
                  />
                )
              })}

              {generating && liveBeat ? <TurnBeat beat={liveBeat} /> : null}

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
              spotlightIds={[
                ...spotlightIds(liveBeat),
                ...spotlightIds(beatsBySeq[String(lastMatchingAction(actions, (action) => action?.type === 'continue')?.seq)]),
              ]}
              beatSeqs={[
                lastPlayer?.seq,
                lastMatchingAction(actions, (action) => action?.type === 'continue')?.seq,
              ]}
              visualHints={{
                ...visualHints(beatsBySeq[String(lastMatchingAction(actions, (action) => action?.type === 'continue')?.seq)]),
                ...visualHints(liveBeat),
              }}
            />
          </Suspense>
        </div>
        {panelOpen && !isInspectorDocked && (
          <div
            className="fixed inset-0 z-40 flex items-end justify-center bg-black/60 backdrop-blur-sm sm:items-stretch sm:justify-end"
            onPointerDown={(event) => {
              if (event.target === event.currentTarget) setPanelOpen(false)
            }}
          >
            <aside
              aria-label="Controles do jogo"
              className="flex min-h-0 max-h-[min(70dvh,100%)] w-full min-w-0 max-w-full flex-col overflow-hidden border-t border-subtle bg-surface shadow-2xl sm:h-full sm:max-h-full sm:w-[min(24rem,calc(100%-1rem))] sm:border-l sm:border-t-0"
              onPointerDown={(event) => event.stopPropagation()}
            >
              <GamePanel
                profile={uiProfile}
                state={uiState}
                disabled={generating || actionLocked || needsCharacter}
                regenerating={regeneratingProfile}
                onStateChange={handleUiStateChange}
                onSend={sendAction}
                onInsert={insertToInput}
                onRegenerate={regenerateProfile}
                onClose={() => setPanelOpen(false)}
                adventureId={id}
                packOn={packOn}
                embedded
              />
            </aside>
          </div>
        )}

        {/* Suggestion Choices Chips */}
        {showChoices && (
          <div className="z-10 min-w-0 max-w-full border-t border-subtle bg-surface/95 px-4 pt-2.5 backdrop-blur">
            <div className={`mx-auto flex min-w-0 max-w-full items-center gap-2 overflow-x-auto pb-2 ${colWidthCls}`}>
              <span className="shrink-0 text-[10px] font-semibold uppercase tracking-wider text-muted">
                Suggested:
              </span>
              {choices.map((c, i) => (
                <button
                  key={i}
                  onClick={() => pickChoice(c)}
                  disabled={actionLocked}
                  title={actionLocked ? 'Aguarde a sua vez' : `Send action as "${c.type}"`}
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
          <div className="z-10 min-w-0 max-w-full border-t border-subtle bg-surface/95 px-4 pt-2.5 backdrop-blur">
            <div className={`mx-auto flex min-w-0 max-w-full items-center gap-2 overflow-x-auto pb-2 ${colWidthCls}`}>
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


        <DmTray
          enabled={dmMode}
          isDm={isDm}
          intents={intents}
          busy={generating}
          onApprove={approveIntent}
          onReject={rejectIntent}
        />

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
          disabledTurn={actionLocked || needsCharacter}
          onSend={sendAction}
          onTextChange={(t) => setInputDirty(t.trim().length > 0)}
          tabs={uiProfile?.tabs}
          inject={inject}
          storyActions={Array.isArray(uiState?._actions) ? uiState._actions : []}
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
        <div className="hidden h-full min-h-0 w-[min(22rem,32%)] max-w-[22rem] shrink-0 overflow-hidden border-l border-subtle bg-surface lg:flex">
          <InspectorPane
            adventure={adventure}
            onAdventureChange={setAdventure}
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

      {seeOpen && (
        <Modal title="Pintar a cena" onClose={() => setSeeOpen(false)}>
          <label className="mb-1 block text-xs font-semibold uppercase tracking-wider text-muted">
            Detalhe visual (opcional)
          </label>
          <textarea
            value={seePrompt}
            onChange={(e) => setSeePrompt(e.target.value)}
            rows={3}
            autoFocus
            placeholder='ex.: "ao pôr do sol, chuva na pedra"'
            className="w-full resize-y rounded-xl border border-subtle bg-surface-input px-3 py-2 text-sm text-primary outline-none focus:border-accent-pri transition placeholder:text-muted"
          />
          <div className="mt-3 flex justify-end gap-2">
            <button
              type="button"
              onClick={() => setSeeOpen(false)}
              className="rounded-lg border border-subtle px-3 py-1.5 text-xs text-secondary hover:bg-elevated transition"
            >
              Cancelar
            </button>
            <button
              type="button"
              onClick={() => doSee(0)}
              className="rounded-lg bg-accent-pri px-3 py-1.5 text-xs font-semibold text-canvas hover:bg-accent-hov shadow transition"
            >
              Pintar a cena
            </button>
            <button
              type="button"
              onClick={() => {
                const nextVariation = seeVariation + 1
                setSeeVariation(nextVariation)
                doSee(nextVariation)
              }}
              className="rounded-lg border border-accent-pri/50 px-3 py-1.5 text-xs font-semibold text-accent-pri hover:bg-accent-pri/10 transition"
              title={`Gerar a variação ${seeVariation + 1} da cena`}
            >
              Gerar variação
            </button>
          </div>
        </Modal>
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

      {publishOpen && <PublicationModal adventure={adventure} onClose={() => setPublishOpen(false)} />}
      {modsOpen && <ModsModal adventureId={id} onClose={() => setModsOpen(false)} />}

      {characterOpen && (
        <CharacterModal
          adventureId={id}
          rules={adventure.character_rules || ''}
          card={characterCard}
          loading={characterLoading}
          onClose={() => setCharacterOpen(false)}
          onSaved={(card) => {
            setCharacterCard(card)
            setPlayerCard(card)
            setNeedsCharacter(false)
            setCharacterOpen(false)
            toast('Personagem salvo.', 'ok')
          }}
        />
      )}
      {mapOpen && (
        <div
          className="fixed inset-0 z-50 flex justify-end bg-black/60 backdrop-blur-sm"
          onPointerDown={(event) => {
            if (event.target === event.currentTarget) setMapOpen(false)
          }}
        >
          <div className="h-full min-h-0 w-[min(40rem,calc(100%-0.5rem))] min-w-0 max-w-full overflow-x-hidden border-l border-subtle bg-canvas shadow-2xl">
            <WorldMap adventure={adventure} open={mapOpen} onClose={() => setMapOpen(false)} />
          </div>
        </div>
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
            className="rounded-xl bg-accent-pri px-4 py-2 text-xs font-semibold text-canvas hover:bg-accent-hov shadow transition disabled:opacity-50"
          >
            {saving ? 'Salvando…' : 'Salvar'}
          </button>
        </div>
      </div>
    </Modal>
  )
}
