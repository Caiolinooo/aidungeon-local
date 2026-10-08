import { useCallback, useEffect, useRef, useState } from 'react'
import { api } from '../../api.js'
import { useToast } from '../Toast.jsx'
import VrmAvatar from '../vrm/VrmAvatar.jsx'
import {
  DEFAULT_VRM_CONFIG,
  EMOTIONS,
  parseVrmConfig,
  sameVrmConfig,
  serializeVrmConfig,
  VRM_MODELS,
} from '../vrm/vrmConfig.js'
import { detectEmotion } from '../vrm/emotionDetect.js'

// Quanto tempo uma emoção escolhida manualmente vence o auto-detect.
const MANUAL_OVERRIDE_MS = 60_000

const COLOR_FIELDS = [
  { key: 'hair', label: 'Hair' },
  { key: 'eyes', label: 'Eyes' },
  { key: 'skin', label: 'Skin' },
  { key: 'top', label: 'Top' },
  { key: 'bottom', label: 'Bottom' },
]

const EMOTION_LABELS = {
  neutral: 'Neutral',
  happy: 'Happy',
  angry: 'Angry',
  sad: 'Sad',
  surprised: 'Surprised',
  relaxed: 'Relaxed',
}

// Aba Avatars (v0.4.0): viewer 3D VRM por NPC, com customização de cores e
// emoções persistida em card.vrm_id (JSON — ver vrmConfig.js).
//
// Prop opcional `storyText`: texto narrativo mais recente da aventura
// (quem renderiza o Drawer pode passar o último texto do feed). Quando
// presente e "Follow the story" está ligado, emotionDetect procura menções
// do NPC perto de palavras de emoção e aplica a expressão automaticamente.
// Override manual vence por 60s, depois volta ao auto. Sem a prop, o
// auto-detect simplesmente não dispara e as emoções ficam 100% manuais.
export default function AvatarsTab({ adventure, storyText }) {
  const toast = useToast()
  const [cards, setCards] = useState(null)
  const [selectedId, setSelectedId] = useState(null)
  const [cfg, setCfg] = useState({ ...DEFAULT_VRM_CONFIG })
  const [autoEmotion, setAutoEmotion] = useState(null)
  const [followStory, setFollowStory] = useState(true)
  const [manualUntil, setManualUntil] = useState(0)
  const [saving, setSaving] = useState(false)
  const overrideTimer = useRef(0)

  const reload = useCallback(async () => {
    try {
      setCards(await api.listCards(adventure.id))
    } catch (e) {
      toast(e.message)
    }
  }, [adventure.id, toast])

  useEffect(() => {
    reload()
  }, [reload])

  const npcs = (cards || []).filter((c) => c.type === 'character')
  const selected = npcs.find((c) => c.id === selectedId) || npcs[0] || null

  // Carrega a config persistida do NPC selecionado (parse tolerante).
  useEffect(() => {
    if (!selected) return
    setCfg(parseVrmConfig(selected.vrm_id))
    setAutoEmotion(null)
    setManualUntil(0)
    clearTimeout(overrideTimer.current)
  }, [selected?.id, selected?.vrm_id]) // eslint-disable-line react-hooks/exhaustive-deps

  // Auto-detect "seguindo a história": roda quando o feed chega pela prop.
  useEffect(() => {
    if (!followStory || !storyText || !selected) return
    if (Date.now() < manualUntil) return // override manual ainda valendo
    const hit = detectEmotion(storyText, selected.title)
    setAutoEmotion(hit ? hit.emotion : null)
  }, [storyText, followStory, selected, manualUntil])

  if (cards === null) return <p className="text-sm text-zinc-500">Loading…</p>

  if (npcs.length === 0) {
    return (
      <p className="text-sm text-zinc-500">
        No NPCs yet. Create character cards in the NPCs tab to customize their avatars.
      </p>
    )
  }

  const pickNpc = (id) => setSelectedId(id)

  const setColor = (key, value) => setCfg((c) => ({ ...c, [key]: value }))

  // Clique manual numa emoção: persiste na config e segura o auto por 60s.
  const pickEmotion = (emotion) => {
    setCfg((c) => ({ ...c, emotion }))
    setAutoEmotion(null)
    setManualUntil(Date.now() + MANUAL_OVERRIDE_MS)
    clearTimeout(overrideTimer.current)
    overrideTimer.current = setTimeout(() => setManualUntil(0), MANUAL_OVERRIDE_MS)
  }

  const resetColors = () =>
    setCfg((c) => ({
      ...c,
      hair: DEFAULT_VRM_CONFIG.hair,
      eyes: DEFAULT_VRM_CONFIG.eyes,
      skin: DEFAULT_VRM_CONFIG.skin,
      top: DEFAULT_VRM_CONFIG.top,
      bottom: DEFAULT_VRM_CONFIG.bottom,
    }))

  // PATCH do card com o JSON da config (mesmo fluxo de salvamento do NpcsTab).
  const save = async () => {
    if (!selected) return
    setSaving(true)
    try {
      await api.updateCard(adventure.id, selected.id, { vrm_id: serializeVrmConfig(cfg) })
      toast('Avatar saved.', 'ok')
      reload()
    } catch (e) {
      toast(e.message)
    } finally {
      setSaving(false)
    }
  }

  const dirty = selected ? !sameVrmConfig(cfg, parseVrmConfig(selected.vrm_id)) : false
  const manualActive = Date.now() < manualUntil
  const effectiveEmotion =
    !manualActive && followStory && autoEmotion ? autoEmotion : cfg.emotion

  const inputCls =
    'rounded-lg border border-zinc-700 bg-zinc-800 px-2 py-1 text-sm outline-none focus:border-emerald-600'

  return (
    <div>
      <h3 className="mb-2 text-sm font-semibold uppercase tracking-wide text-zinc-400">
        Avatars ({npcs.length})
      </h3>

      {/* seletor de NPC */}
      <div className="mb-3 flex flex-wrap gap-1">
        {npcs.map((c) => (
          <button
            key={c.id}
            onClick={() => pickNpc(c.id)}
            className={`rounded-md px-2 py-1 text-xs font-medium ${
              selected?.id === c.id
                ? 'bg-emerald-700 text-white'
                : 'border border-zinc-700 text-zinc-300 hover:bg-zinc-700'
            }`}
          >
            {c.title || 'Untitled'}
          </button>
        ))}
      </div>

      {selected && (
        <>
          <VrmAvatar
            url={VRM_MODELS[cfg.model] || VRM_MODELS.base}
            colors={{ hair: cfg.hair, eyes: cfg.eyes, skin: cfg.skin, top: cfg.top, bottom: cfg.bottom }}
            emotion={effectiveEmotion}
          />
          <p className="mt-1 text-xs text-zinc-500">Drag to rotate.</p>

          {/* cores (tints multiplicativos; branco = cor original) */}
          <div className="mt-3 grid grid-cols-5 gap-2">
            {COLOR_FIELDS.map(({ key, label }) => (
              <label key={key} className="flex flex-col items-center gap-1 text-xs text-zinc-400">
                <input
                  type="color"
                  value={cfg[key]}
                  onChange={(e) => setColor(key, e.target.value)}
                  className="h-8 w-full cursor-pointer rounded border border-zinc-700 bg-zinc-800"
                />
                {label}
              </label>
            ))}
          </div>
          <button
            onClick={resetColors}
            className="mt-2 rounded-md border border-zinc-700 px-2 py-1 text-xs text-zinc-300 hover:bg-zinc-700"
          >
            Reset colors
          </button>

          {/* emoções */}
          <div className="mt-4 flex flex-wrap gap-1">
            {EMOTIONS.map((e) => (
              <button
                key={e}
                onClick={() => pickEmotion(e)}
                className={`rounded-md px-2 py-1 text-xs font-medium ${
                  effectiveEmotion === e
                    ? 'bg-emerald-700 text-white'
                    : 'border border-zinc-700 text-zinc-300 hover:bg-zinc-700'
                }`}
              >
                {EMOTION_LABELS[e]}
              </button>
            ))}
          </div>

          <label className="mt-3 flex items-center gap-2 text-sm text-zinc-300">
            <input
              type="checkbox"
              checked={followStory}
              onChange={(e) => setFollowStory(e.target.checked)}
              className="accent-emerald-600"
            />
            Follow the story
            {!storyText && (
              <span className="text-xs text-zinc-500">(waiting for story feed)</span>
            )}
          </label>
          {followStory && autoEmotion && !manualActive && (
            <p className="mt-1 text-xs text-emerald-400">
              Auto-detected: {EMOTION_LABELS[autoEmotion]}
            </p>
          )}

          <div className="mt-4 flex justify-end">
            <button
              onClick={save}
              disabled={!dirty || saving}
              className={`${inputCls} bg-emerald-700 font-medium text-white hover:bg-emerald-600 disabled:opacity-50`}
            >
              {saving ? 'Saving…' : 'Save avatar'}
            </button>
          </div>
        </>
      )}
    </div>
  )
}
