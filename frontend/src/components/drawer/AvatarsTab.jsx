import { useCallback, useEffect, useRef, useState } from 'react'
import { api } from '../../api.js'
import { useToast } from '../Toast.jsx'
import TalkingAvatar from '../avatar/TalkingAvatar.jsx'
import {
  AVATARS,
  DEFAULT_AVATAR_CONFIG,
  MOODS,
  parseAvatarConfig,
  sameAvatarConfig,
  serializeAvatarConfig,
} from '../avatar/avatarConfig.js'
import { detectEmotion } from '../avatar/emotionDetect.js'

// Quanto tempo um mood escolhido manualmente vence o auto-detect.
const MANUAL_OVERRIDE_MS = 60_000

const MOOD_LABELS = {
  neutral: 'Neutral',
  happy: 'Happy',
  angry: 'Angry',
  sad: 'Sad',
  fear: 'Fear',
  disgust: 'Disgust',
  love: 'Love',
  sleep: 'Sleep',
}

// Aba Avatars: viewer 3D (TalkingHead/GLB) por NPC, com seleção de modelo,
// corpo e mood persistidos em card.vrm_id (JSON — ver avatarConfig.js; nome
// da coluna mantido por compatibilidade de schema).
//
// Prop opcional `storyText`: texto narrativo mais recente da aventura
// (quem renderiza o Drawer pode passar o último texto do feed). Quando
// presente e "Follow the story" está ligado, emotionDetect procura menções
// do NPC perto de palavras de emoção e aplica o mood automaticamente.
// Override manual vence por 60s, depois volta ao auto. Sem a prop, o
// auto-detect simplesmente não dispara e os moods ficam 100% manuais.
export default function AvatarsTab({ adventure, storyText }) {
  const toast = useToast()
  const [cards, setCards] = useState(null)
  const [selectedId, setSelectedId] = useState(null)
  const [cfg, setCfg] = useState({ ...DEFAULT_AVATAR_CONFIG })
  const [autoMood, setAutoMood] = useState(null)
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

  // Carrega a config persistida do NPC selecionado (parse tolerante —
  // legado VRM de cores cai nos defaults automaticamente).
  useEffect(() => {
    if (!selected) return
    setCfg(parseAvatarConfig(selected.vrm_id))
    setAutoMood(null)
    setManualUntil(0)
    clearTimeout(overrideTimer.current)
  }, [selected?.id, selected?.vrm_id]) // eslint-disable-line react-hooks/exhaustive-deps

  // Auto-detect "seguindo a história": roda quando o feed chega pela prop.
  useEffect(() => {
    if (!followStory || !storyText || !selected) return
    if (Date.now() < manualUntil) return // override manual ainda valendo
    const hit = detectEmotion(storyText, selected.title)
    setAutoMood(hit ? hit.emotion : null)
  }, [storyText, followStory, selected, manualUntil])

  if (cards === null) return <p className="text-sm text-muted">Carregando…</p>

  if (npcs.length === 0) {
    return (
      <p className="text-sm text-muted">
        No NPCs yet. Create character cards in the NPCs tab to customize their avatars.
      </p>
    )
  }

  const pickNpc = (id) => setSelectedId(id)

  // Troca de modelo: preset zera a URL custom e herda o body do preset;
  // "custom" mantém a URL já digitada e o body selecionado.
  const pickModel = (model) =>
    setCfg((c) => ({
      ...c,
      model,
      url: model === 'custom' ? c.url : '',
      body: model === 'custom' ? c.body : AVATARS[model].body,
    }))

  // Clique manual num mood: persiste na config e segura o auto por 60s.
  const pickMood = (mood) => {
    setCfg((c) => ({ ...c, mood }))
    setAutoMood(null)
    setManualUntil(Date.now() + MANUAL_OVERRIDE_MS)
    clearTimeout(overrideTimer.current)
    overrideTimer.current = setTimeout(() => setManualUntil(0), MANUAL_OVERRIDE_MS)
  }

  // PATCH do card com o JSON da config (mesmo fluxo de salvamento do NpcsTab).
  const save = async () => {
    if (!selected) return
    setSaving(true)
    try {
      await api.updateCard(adventure.id, selected.id, { vrm_id: serializeAvatarConfig(cfg) })
      toast('Avatar saved.', 'ok')
      reload()
    } catch (e) {
      toast(e.message)
    } finally {
      setSaving(false)
    }
  }

  const dirty = selected ? !sameAvatarConfig(cfg, parseAvatarConfig(selected.vrm_id)) : false
  const manualActive = Date.now() < manualUntil
  const effectiveMood = !manualActive && followStory && autoMood ? autoMood : cfg.mood

  const inputCls =
    'rounded-lg border border-subtle bg-surface px-2 py-1 text-sm outline-none focus:border-accent-pri'

  return (
    <div>
      <h3 className="mb-2 text-sm font-semibold uppercase tracking-wide text-secondary">
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
                ? 'bg-accent-pri text-white'
                : 'border border-subtle text-secondary hover:bg-elevated'
            }`}
          >
            {c.title || 'Sem título'}
          </button>
        ))}
      </div>

      {selected && (
        <>
          {/* preview reflete o mood efetivo (auto ou manual) sem sujar a config */}
          <TalkingAvatar cfg={{ ...cfg, mood: effectiveMood }} className="h-80 w-full" view="mid" />

          {/* modelo: presets locais (public/avatars) ou GLB custom por URL */}
          <div className="mt-3 flex flex-wrap gap-1">
            {Object.entries(AVATARS).map(([key, a]) => (
              <button
                key={key}
                onClick={() => pickModel(key)}
                className={`rounded-md px-2 py-1 text-xs font-medium ${
                  cfg.model === key
                    ? 'bg-accent-pri text-white'
                    : 'border border-subtle text-secondary hover:bg-elevated'
                }`}
              >
                {a.label}
              </button>
            ))}
            <button
              onClick={() => pickModel('custom')}
              className={`rounded-md px-2 py-1 text-xs font-medium ${
                cfg.model === 'custom'
                  ? 'bg-accent-pri text-white'
                  : 'border border-subtle text-secondary hover:bg-elevated'
              }`}
            >
              Custom URL
            </button>
          </div>

          {cfg.model === 'custom' && (
            <div className="mt-2">
              <input
                type="text"
                value={cfg.url}
                onChange={(e) => setCfg((c) => ({ ...c, url: e.target.value.trim() }))}
                placeholder="https://…/avatar.glb"
                className={`${inputCls} w-full`}
              />
              <p className="mt-1 text-xs text-muted">
                Ready Player Me GLB com morphTargets=ARKit,Oculus Visemes
              </p>
            </div>
          )}

          {/* corpo (forma usada pelo TalkingHead p/ poses) */}
          <div className="mt-3 flex items-center gap-2">
            <span className="text-xs text-secondary">Corpo:</span>
            {['F', 'M'].map((b) => (
              <button
                key={b}
                onClick={() => setCfg((c) => ({ ...c, body: b }))}
                className={`rounded-md px-2 py-1 text-xs font-medium ${
                  cfg.body === b
                    ? 'bg-accent-pri text-white'
                    : 'border border-subtle text-secondary hover:bg-elevated'
                }`}
              >
                {b}
              </button>
            ))}
          </div>

          {/* moods */}
          <div className="mt-4 flex flex-wrap gap-1">
            {MOODS.map((m) => (
              <button
                key={m}
                onClick={() => pickMood(m)}
                className={`rounded-md px-2 py-1 text-xs font-medium ${
                  effectiveMood === m
                    ? 'bg-accent-pri text-white'
                    : 'border border-subtle text-secondary hover:bg-elevated'
                }`}
              >
                {MOOD_LABELS[m]}
              </button>
            ))}
          </div>

          <label className="mt-3 flex items-center gap-2 text-sm text-secondary">
            <input
              type="checkbox"
              checked={followStory}
              onChange={(e) => setFollowStory(e.target.checked)}
              className="accent-accent-600"
            />
            Follow the story
            {!storyText && (
              <span className="text-xs text-muted">(aguardando o feed da história)</span>
            )}
          </label>
          {followStory && autoMood && !manualActive && (
            <p className="mt-1 text-xs text-accent-pri">
              Auto-detected: {MOOD_LABELS[autoMood]}
            </p>
          )}

          <div className="mt-4 flex justify-end">
            <button
              onClick={save}
              disabled={!dirty || saving}
              className={`${inputCls} bg-accent-pri font-medium text-white hover:bg-accent-hov disabled:opacity-50`}
            >
              {saving ? 'Salvando…' : 'Salvar avatar'}
            </button>
          </div>
        </>
      )}
    </div>
  )
}
