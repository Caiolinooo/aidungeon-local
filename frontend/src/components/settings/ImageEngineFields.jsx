import { useEffect, useState } from 'react'
import { api } from '../../api.js'

export const IMAGE_ENGINE_DEFAULTS = {
  comfyui_engine: 'flux2_klein',
  flux2_steps: 4,
  flux2_cfg: 1,
  flux2_sampler: 'euler',
  flux2_megapixels: 0.25,
  zimage_steps: 8,
  zimage_cfg: 1,
  zimage_shift: 3,
  video_engine: 'ltxv',
  video_steps: 8,
  video_cfg: 1,
  video_width: 768,
  video_height: 448,
  video_frames: 121,
  video_fps: 24,
}

const VIDEO_PRESETS = {
  wan: {
    video_steps: 20,
    video_cfg: 5,
    video_width: 1280,
    video_height: 704,
  },
  h3: {
    video_steps: 8,
    video_cfg: 1,
    video_width: 768,
    video_height: 448,
    video_frames: 56,
    video_fps: 24,
  },
  ltxv: {
    video_steps: 8,
    video_cfg: 1,
    video_width: 768,
    video_height: 448,
    video_frames: 121,
    video_fps: 24,
  },
}

export function imageEngineFrom(source) {
  const base = { ...IMAGE_ENGINE_DEFAULTS }
  if (!source || typeof source !== 'object') return base
  for (const key of Object.keys(base)) {
    if (source[key] != null && source[key] !== '') base[key] = source[key]
  }
  return base
}

export function imageEnginePayload(form) {
  const engine = imageEngineFrom(form)
  return {
    comfyui_engine: engine.comfyui_engine,
    flux2_steps: Number(engine.flux2_steps),
    flux2_cfg: Number(engine.flux2_cfg),
    flux2_sampler: engine.flux2_sampler,
    flux2_megapixels: Number(engine.flux2_megapixels),
    zimage_steps: Number(engine.zimage_steps),
    zimage_cfg: Number(engine.zimage_cfg),
    zimage_shift: Number(engine.zimage_shift),
    video_engine: engine.video_engine,
    video_steps: Number(engine.video_steps),
    video_cfg: Number(engine.video_cfg),
    video_width: Number(engine.video_width),
    video_height: Number(engine.video_height),
    video_frames: Number(engine.video_frames),
    video_fps: Number(engine.video_fps),
  }
}

function liveNumber(value) {
  return value == null || Number.isNaN(Number(value)) ? '—' : Number(value)
}

function BudgetLines({ budget }) {
  if (!budget) return null
  const vramLive = liveNumber(budget.vram_free_mib)
  const ramLive = liveNumber(budget.ram_available_mib)
  const vramText = `VRAM livre ${vramLive} MiB · este preset pede ${budget.vram_mib} MiB`
  const ramText = `RAM livre ${ramLive} MiB · este preset pede ${budget.ram_mib} MiB`
  return (
    <div className="space-y-1">
      <p className={budget.fits_vram === false ? 'text-sm text-warn-300' : 'text-sm text-muted'}>
        {vramText}
      </p>
      <p className={budget.fits_ram === false ? 'text-sm text-warn-300' : 'text-sm text-muted'}>
        {ramText}
      </p>
      {Number(budget.spill_mib) > 0 && (
        <p className="text-sm text-warn-300">
          {`Não cabe inteiro ao lado do narrador. O Comfy troca ${budget.spill_mib} MiB com a RAM; o narrador continua. Fica mais lento.`}
        </p>
      )}
    </div>
  )
}

export default function ImageEngineFields({ form, onChange, inputCls, labelCls }) {
  const [healthTick, setHealthTick] = useState(0)
  const [imageBudget, setImageBudget] = useState(null)
  const [videoBudget, setVideoBudget] = useState(null)
  const engine = form.comfyui_engine || 'flux2_klein'
  const videoEngine = form.video_engine || 'ltxv'

  useEffect(() => {
    let stop = false
    const pull = async () => {
      try {
        await api.healthLlm()
      } catch {
        // A sonda pode falhar com o narrador ocupado. O orçamento segue.
      }
      if (!stop) setHealthTick((value) => value + 1)
    }
    pull()
    const timer = setInterval(pull, 5000)
    return () => {
      stop = true
      clearInterval(timer)
    }
  }, [])

  useEffect(() => {
    let stop = false
    const pull = async () => {
      try {
        const body = await api.getImageBudget({
          kind: 'image',
          engine,
          width: Number(form.comfyui_width) || 1024,
          height: Number(form.comfyui_height) || 1024,
          frames: 1,
        })
        if (!stop) setImageBudget(body)
      } catch {
        if (!stop) setImageBudget(null)
      }
    }
    pull()
    return () => {
      stop = true
    }
  }, [engine, form.comfyui_width, form.comfyui_height, healthTick])

  useEffect(() => {
    let stop = false
    const pull = async () => {
      try {
        const body = await api.getImageBudget({
          kind: 'video',
          engine: videoEngine,
          width: Number(form.video_width) || 768,
          height: Number(form.video_height) || 448,
          frames: Number(form.video_frames) || 1,
        })
        if (!stop) setVideoBudget(body)
      } catch {
        if (!stop) setVideoBudget(null)
      }
    }
    pull()
    return () => {
      stop = true
    }
  }, [videoEngine, form.video_width, form.video_height, form.video_frames, healthTick])

  const set = (patch) => onChange({ ...form, ...patch })

  return (
    <div className="space-y-4">
      <label className="block">
        <span className={labelCls}>Motor de imagem</span>
        <select
          value={engine}
          onChange={(event) => set({ comfyui_engine: event.target.value })}
          className={inputCls}
        >
          <option value="qwen">FineView (qwen)</option>
          <option value="flux2_klein">FLUX.2 Klein</option>
          <option value="z_image_turbo">Z-Image-Turbo</option>
        </select>
      </label>

      {engine === 'qwen' && (
        <label className="block">
          <span className={labelCls}>Passos do FineView (fixos no schedule)</span>
          <input
            type="number"
            min="8"
            max="8"
            readOnly
            value={form.comfyui_steps ?? 8}
            className={inputCls}
          />
        </label>
      )}

      {engine === 'flux2_klein' && (
        <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
          <label className="block">
            <span className={labelCls}>Passos do Klein</span>
            <input
              type="number"
              min="1"
              max="8"
              value={form.flux2_steps ?? 4}
              onChange={(event) => set({ flux2_steps: event.target.value })}
              className={inputCls}
            />
          </label>
          <label className="block">
            <span className={labelCls}>CFG do Klein</span>
            <input
              type="number"
              min="1"
              max="2"
              step="0.1"
              value={form.flux2_cfg ?? 1}
              onChange={(event) => set({ flux2_cfg: event.target.value })}
              className={inputCls}
            />
          </label>
          <label className="block">
            <span className={labelCls}>Sampler do Klein</span>
            <select
              value={form.flux2_sampler || 'euler'}
              onChange={(event) => set({ flux2_sampler: event.target.value })}
              className={inputCls}
            >
              <option value="euler">euler</option>
              <option value="res_multistep">res_multistep</option>
            </select>
          </label>
          <label className="block">
            <span className={labelCls}>Megapixels do Klein</span>
            <select
              value={String(form.flux2_megapixels ?? 0.25)}
              onChange={(event) => set({ flux2_megapixels: Number(event.target.value) })}
              className={inputCls}
            >
              <option value="0.25">0.25</option>
              <option value="1">1</option>
            </select>
          </label>
        </div>
      )}

      {engine === 'z_image_turbo' && (
        <div className="space-y-3">
          <div className="grid grid-cols-1 gap-3 sm:grid-cols-3">
            <label className="block">
              <span className={labelCls}>Passos do Z-Image</span>
              <input
                type="number"
                min="1"
                max="20"
                value={form.zimage_steps ?? 8}
                onChange={(event) => set({ zimage_steps: event.target.value })}
                className={inputCls}
              />
            </label>
            <label className="block">
              <span className={labelCls}>CFG do Z-Image</span>
              <input
                type="number"
                min="0"
                max="20"
                step="0.1"
                value={form.zimage_cfg ?? 1}
                onChange={(event) => set({ zimage_cfg: event.target.value })}
                className={inputCls}
              />
            </label>
            <label className="block">
              <span className={labelCls}>Shift do Z-Image</span>
              <input
                type="number"
                min="0.01"
                max="100"
                step="0.1"
                value={form.zimage_shift ?? 3}
                onChange={(event) => set({ zimage_shift: event.target.value })}
                className={inputCls}
              />
            </label>
          </div>
          <p className="text-sm text-muted">
            Z-Image-Turbo não recebe as fotos de referência; a identidade fica no prompt.
          </p>
        </div>
      )}

      <BudgetLines budget={imageBudget} />

      <label className="block">
        <span className={labelCls}>Motor de vídeo</span>
        <select
          value={videoEngine}
          onChange={(event) => set({
            video_engine: event.target.value,
            ...(VIDEO_PRESETS[event.target.value] || {}),
          })}
          className={inputCls}
        >
          <option value="ltxv">LTXV</option>
          <option value="wan">Wan</option>
          <option value="h3">MiniMax H3</option>
        </select>
      </label>

      <div className="grid grid-cols-1 gap-3 sm:grid-cols-3">
        <label className="block">
          <span className={labelCls}>Passos do vídeo</span>
          <input
            type="number"
            min="1"
            max="40"
            value={form.video_steps ?? 8}
            onChange={(event) => set({ video_steps: event.target.value })}
            className={inputCls}
          />
        </label>
        <label className="block">
          <span className={labelCls}>CFG do vídeo</span>
          <input
            type="number"
            min="0"
            max="20"
            step="0.1"
            value={form.video_cfg ?? 1}
            onChange={(event) => set({ video_cfg: event.target.value })}
            className={inputCls}
          />
        </label>
        <label className="block">
          <span className={labelCls}>FPS</span>
          <input
            type="number"
            min="8"
            max="30"
            step="1"
            value={form.video_fps ?? 24}
            onChange={(event) => set({ video_fps: event.target.value })}
            className={inputCls}
          />
        </label>
        <label className="block">
          <span className={labelCls}>Largura do vídeo</span>
          <input
            type="number"
            min="64"
            max="1920"
            value={form.video_width ?? 768}
            onChange={(event) => set({ video_width: event.target.value })}
            className={inputCls}
          />
        </label>
        <label className="block">
          <span className={labelCls}>Altura do vídeo</span>
          <input
            type="number"
            min="64"
            max="1920"
            value={form.video_height ?? 448}
            onChange={(event) => set({ video_height: event.target.value })}
            className={inputCls}
          />
        </label>
        <label className="block">
          <span className={labelCls}>Frames</span>
          <input
            type="number"
            min="5"
            max="241"
            value={form.video_frames ?? 121}
            onChange={(event) => set({ video_frames: event.target.value })}
            className={inputCls}
          />
        </label>
      </div>

      <BudgetLines budget={videoBudget} />
    </div>
  )
}
