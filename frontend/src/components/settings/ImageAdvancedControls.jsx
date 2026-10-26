import { SettingField } from './Controls.jsx'

export default function ImageAdvancedControls({ form, onChange, inputCls, labelCls }) {
  const update = (patch) => onChange({ ...form, ...patch })
  return (
    <fieldset className="grid grid-cols-1 gap-4 rounded-xl border border-subtle bg-elevated/30 p-3 sm:grid-cols-3">
      <legend className="px-1 text-xs font-semibold uppercase tracking-wider text-muted">Avançado</legend>
      <SettingField label="Seed (vazio = aleatória)" labelClassName={labelCls}>
        <input type="number" step="1" value={form.seed ?? ''} onChange={(e) => update({ seed: e.target.value })} placeholder="Aleatória" className={inputCls} />
      </SettingField>
      <SettingField label="Variação (inteiro ≥ 0)" labelClassName={labelCls}>
        <input type="number" min="0" step="1" value={form.variation} onChange={(e) => update({ variation: e.target.value })} className={inputCls} />
      </SettingField>
      <SettingField label="Tempo limite (segundos)" labelClassName={labelCls}>
        <input type="number" min="1" max="600" step="1" value={form.timeout_seconds} onChange={(e) => update({ timeout_seconds: e.target.value })} className={inputCls} />
      </SettingField>
      {form.provider === 'sdwebui' && (
        <>
          <SettingField label="CFG Scale" labelClassName={labelCls}>
            <input type="number" min="1" max="30" step="0.5" value={form.cfg_scale} onChange={(e) => update({ cfg_scale: e.target.value })} className={inputCls} />
          </SettingField>
          <SettingField label="Denoise img2img" labelClassName={labelCls}>
            <input type="number" min="0" max="1" step="0.05" value={form.denoising_strength} onChange={(e) => update({ denoising_strength: e.target.value })} className={inputCls} />
          </SettingField>
          <SettingField label="Sampler" labelClassName={labelCls}>
            <input type="text" value={form.sampler_name} onChange={(e) => update({ sampler_name: e.target.value })} className={inputCls} />
          </SettingField>
          <SettingField label="Scheduler" labelClassName={labelCls}>
            <input type="text" value={form.scheduler} onChange={(e) => update({ scheduler: e.target.value })} className={inputCls} />
          </SettingField>
          <div className="sm:col-span-2">
            <SettingField label="Checkpoint (vazio = atual do WebUI)" labelClassName={labelCls}>
              <input type="text" value={form.sd_model_checkpoint} onChange={(e) => update({ sd_model_checkpoint: e.target.value })} className={inputCls} />
            </SettingField>
          </div>
        </>
      )}
      {form.provider === 'comfyui' && (
        <>
          <SettingField label="CFG Scale (1 no Klein destilado)" labelClassName={labelCls}>
            <input type="number" min="1" max="30" step="0.1" value={form.comfyui_cfg_scale} onChange={(e) => update({ comfyui_cfg_scale: e.target.value })} className={inputCls} />
          </SettingField>
          <SettingField label="Sampler (t2i)" labelClassName={labelCls}>
            <input type="text" value={form.comfyui_sampler_name} onChange={(e) => update({ comfyui_sampler_name: e.target.value })} className={inputCls} />
          </SettingField>
          <SettingField label="Scheduler (t2i)" labelClassName={labelCls}>
            <input type="text" value={form.comfyui_scheduler} onChange={(e) => update({ comfyui_scheduler: e.target.value })} className={inputCls} />
          </SettingField>
        </>
      )}
    </fieldset>
  )
}
